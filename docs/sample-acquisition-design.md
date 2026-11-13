# 样本获取通道设计（Sample Acquisition）

> 目标版本 v0.4 ｜ 状态：v2 已按「用户提供 SQL」重写 ｜ 关联：`data/source.py`、`data/ingest.py`、`tools/codes.py`

## 1. 定位

**取数在数据平台，落地在分析工具，交付靠证据包。**

本工具不建表、不做 ETL、不做调度、不梳理数据集市、不落生产明细。

## 2. 责任分工：泄漏防治在源头，工具只管作业安全

这一版最重要的判断：**数据泄漏防治不是本工具的职责**。理由不在"难做"，而在**责任主体错位**——源头有字段脱敏策略、字段权限管控、审计留痕，下游工具再做一层管控，实际效果是把数据岗的责任平移给了分析师，而分析师既无权限也无能力处置。

| 关注点 | 责任方 | 本工具 |
|---|---|---|
| 字段脱敏、行列权限、导出审批 | 数据平台 / 数仓权限体系 | 不做，不重复建设 |
| SQL 语义、清洗逻辑、多表打平 | 分析师（业务口径只有他知道） | 不代写、不代生成 |
| 样本集市梳理、表登记、列资产目录 | 数据治理 | 不做 |
| **只读、单语句、防手滑** | 本工具 | ✅ 做 |
| **超量保护（别拖爆库 / 别拉爆本地）** | 本工具 | ✅ 做 |
| **"我到底拉了什么"可复算留痕** | 本工具 | ✅ 做 |
| **结论可审计、可交付** | 本工具 | ✅ 做（证据包） |

一句话：**上游管"能不能看"，本工具管"拉得对不对、算得清不清、交得出不出"**。

## 3. 决策记录（已否决方案）

留档，避免后续反复。

| 方案 | 否决理由 |
|---|---|
| `datasets.yaml` 数据集登记表（列级白名单 + 粒度 + 上限） | 维护它等于把数据集市梳理一遍，工作量与工具定位不符；且数据治理已有目录，重复投入 |
| 按口径生成 SQL（传 `columns + filters`，工具拼 SQL） | 真实样本表带清洗逻辑（脏值映射、多表打平、窗口去重），生成器覆盖不了，必然退回手写 |
| 值级 PII 探针（身份证校验位 / Luhn / 手机号）作为**阻断** | 源头已脱敏；下游阻断只在源头的脱敏有漏时才生效，属于兜底重复 |
| 准标识三元组合审批（精确年龄+区县+日级申请日） | 同上。且会显著削弱评分卡区分度，代价大于收益 |
| 本地工作区的访问管控归属讨论 | **不做**：这是个人分析工具，没有多租户、没有归属问题 |
| 对聚合 SQL 做扫描量计量 | **不做**：样本量不设业务约束，`row_cap` 只作为防手滑的安全阀 |

**保留的降级用法**：敏感列名的**识别与告警**（不阻断），服务于证据包自动剔除，见 4.3。

**遗留脚本**：`tools/pg_conn.py`、`tools/holo_conn.py`、`tools/odps_conn*.py` 已被 `src/credit_agent/data/source.py` 统一取代，Agent 链路不再引用。其中前两个文件**含明文生产库密码/AccessKey**，建议删除或清空后仅作参考。


## 4. 保留的校验（最小集，逐条算成本）

### 4.1 SQL 静态校验 —— 成本 ≈ 0，已有实现

`data/source.py:assert_read_only` 已具备：语句前缀白名单（`select/with/describe/show/explain/pragma`）+ 写操作关键词黑名单 + 注释剥离。

这一层的定位是**防手滑与防误粘贴**，不是防泄漏。分析师从 DataWorks 粘过来一段带 `SET`、`${param}`、`|` 分隔的 SQL 是高频事故，`data/sql_prepare.py` 已经能拦并给出可操作提示。这条保留，不扩展。

补两条（成本同样接近 0）：

- **单语句**：出现未闭合外的第二个 `;` 即拒绝。多语句是典型误粘贴。
- **`?` 未绑定参数**、`${...}` 未展开：已有。

### 4.1.1 取数范围门禁 —— 执行前拒绝，不是执行后标注

`data/sql_prepare.py:describe_sql_deviation(user_sql, executed_sql)` 比对需求方原句与实际执行语句：新增/删除了哪些列、是否自行加了 `WHERE`、是否换了来源表。

`download_sample` 的规则是**硬拒绝**：

- `user_sql` 必填（预览也必填——预览一样会读到行）；
- `sql` 与 `user_sql` 不一致 → 抛 `IngestError`，**在建数据源之前**，因此不连接数据库、不读数据、不落盘，MCP 侧返回 `SQL_SCOPE_NOT_AUTHORIZED`；
- 唯一例外是 `authorization` 里带着需求方的授权原话，此时放行并把原话写进 manifest 的 `deviation_authorized_by`。

为什么不做「先取再标注」：数据一旦落盘，下游所有占比、分群、结论都建立在被改写过的口径上。manifest 里那行标注撤不回任何一个已经算错的结论，只会在正确结论旁边多留一个错误结论。把决定权放在执行前，是这一层唯一有效的时点。

### 4.2 超量保护 —— 成本低，痛感强

这条不是为了合规，是为了**别把 ODPS 拖死、别把本地磁盘打满、别让一次手滑跑两小时**。

- **强制 `LIMIT` 兜底**：工具在用户 SQL 外层包 `LIMIT row_cap+1`，返回 `row_cap+1` 行即判定溢出 → 拒绝落盘，返回建议（"按 `apply_month` 分批，或收窄分区"）。用户 SQL 自带更小 `LIMIT` 时尊重用户的。
- **分区条件提示**（不阻断）：扫描 `WHERE` 子句中是否出现 `dt` / `partition` / `pt` 类条件，缺失则告警。ODPS 全表扫描是按量计费的，这个提示值钱。
- **空结果提示**：0 行时提示检查分区与时间窗（已有行为，保留）。

`row_cap` 默认 200 万行，可由环境变量覆盖；不做"按 dataset 分别设"，因为没有登记表。

### 4.3 schema 记录与敏感列告警 —— 不比对，只记录

没有登记表，就没有"契约"可比。这一层降级为**记录 + 告警**：

| 动作 | 行为 |
|---|---|
| 列名 / 类型全量记录 | 进 manifest，作为证据链基础 |
| 敏感列名识别 | 命中词表（`id_card` `mobile` `phone` `bank_card` `name` `addr` `email` `device_id` `cust_name` 等）→ 写入 `sensitive_columns`，**只告警不阻断** |
| 常见类型坑记录 | `date` 以 `string` 返回、金额为 `Decimal`、布尔为 `0/1` → 记录，供后续工具按字符串解析 |
| 敏感列的用途收敛 | **证据包导出时自动剔除敏感列**，报告中禁止引用（`REPORT_UNTRACED` 既有机制扩展） |

`sensitive_columns` 是纯粹的"提示"，分析师可以无视而继续；但它让证据包有了自动脱敏的依据——**这一个用途就足以证明它值得保留**。

### 4.4 粒度校验（可选参数）—— 分析师自己要的

自由 SQL 下工具不知道主键，但**调用方可以传 `grain_key`**（如 `apply_id`）。传了就对结果做唯一性校验，`duplicate > 0` 即告警。

这不是管控，是**防止静默错位**：`一行一申请` 与 `一行一笔贷款` 混用，会让 vintage、迁徙率、坏账率全部算错，而表结构上看不出任何异常。分析人员通常也希望有人替他检查这一项。

### 4.5 多后端：一个函数，五个源

`download_sample` 不关心数据在哪种库上。后端由 `CREDIT_AGENT_DATA_BACKEND` 选择，SQL 原样交给驱动：

| backend | 驱动 | 只读由谁保证 | 默认端口 |
|---|---|---|---|
| `duckdb` | duckdb | 连接级 `read_only=True` | — |
| `odps` | pyodps | **仅 SQL 文本审计** | — |
| `postgres` | psycopg2 | 服务端 `set_session(readonly=True)` | 5432 |
| `holo` | psycopg2 | 服务端 `set_session(readonly=True)` | 80 |
| `mysql` | pymysql | 服务端 `SET SESSION TRANSACTION READ ONLY` | 3306 |

这个区别值得写进文档，因为它决定了"只读"到底有多硬：

- PostgreSQL / Hologres / MySQL 的只读是**服务器执行**的。即使文本审计漏了一条 `INSERT`，服务端也会拒绝。
- MaxCompute 没有等价的会话级开关，`odps` 后端完全依赖文本审计——三层里最弱的一层。`download_sample` 会把这一点写进返回值（`checks.readonly_note`），不假装等价。

MySQL 上若服务端版本过老（< 5.7）导致该语句失败，不静默忽略，而是把 `readonly_enforced=false` 记进报告。

Hologres 与 PostgreSQL 共用一套连接参数（`CREDIT_AGENT_PG_*`），因为它是 PostgreSQL 协议，只差默认端口。

## 5. 统一契约：`sample_manifest.json`

两条入口（下载 / 上传）产出同一个结构，下游工具不区分来源。

```jsonc
{
  "sample_id": "smp_20261113_8f3a2c",
  "source_kind": "sql_download",            // sql_download | file_upload
  "source_ref": {
    "sql_hash": "sha256:...",               // 只存 hash，不存 SQL 全文（含内网表名）
    "sql_preview": "SELECT apply_month, age, ... (48 列) FROM dwd_apply_month WHERE dt BETWEEN ...",
    "file_name": null, "file_sha256": null
  },
  "row_count": 184230,
  "columns": [
    {"name": "apply_month", "dtype": "string", "null_rate": 0.0,  "is_sensitive": false},
    {"name": "age",         "dtype": "int",    "null_rate": 0.012, "is_sensitive": false},
    {"name": "cust_id",     "dtype": "string", "null_rate": 0.0,  "is_sensitive": true}
  ],
  "sensitive_columns": ["cust_id"],
  "time_span": {"min_apply_month": "2025-06", "max_apply_month": "2026-04"},
  "observation": {"as_of_date": "2026-10-31", "observation_end": "2026-10-31"},
  "checks": {
    "read_only": true, "single_statement": true,
    "partition_pruned": true, "row_cap": {"limit": 2000000, "actual": 184230, "exceeded": false},
    "grain": {"key": "apply_id", "duplicates": 0},   // 未传 grain_key 则为 null
    "type_caveats": [{"column": "apply_dt", "note": "date returned as string"}]
  },
  "fingerprint": {"schema_hash": "...", "rows_hash": "...", "generator": "credit-agent/0.4.0"},
  "purpose": "2026Q2 准入策略 cutoff 模拟",
  "operator": "zhongxing",
  "acquired_at": "2026-11-13T10:40:00+08:00"
}
```

两个必填项的来历：

- **`observation.as_of_date` 必填**：标签成熟度 = 批次月末 + 表现期，而"数据到哪天为止"只有取数那一刻知道。此前实测 `check_label_maturity` 出现 `degraded 5/6`，根因就是拿不到 `as_of`。固定进 manifest 后，成熟度从"猜"变"算"，且可复算。
- **`sql_hash` 必存、`sql` 全文不存**：分析师改一个字，结论就可能变，所以需要指纹锁定；但 SQL 里有内网库表名，不能进证据包外发。

## 6. 工具面：12 → 14

砍掉 `list_sample_sources`（依赖登记表）、砍掉生成式 `acquire_sample`（依赖登记表 + 生成器）。加两个：

| 新增工具 | 入参要点 | 返回 |
|---|---|---|
| `download_sample` | `sql`（必填）、`as_of_date`（必填，预览时除外）、`dataset_name`、`out_path`、`observation_end`、`row_cap`、`grain_key`、`purpose`、`preview_only`、`fmt`、`overwrite` | manifest + checks |
| `import_sample_file` | `file_path`（必填）、`dataset_name`、`out_path`、`encoding`、`delimiter`、`sheet`、`column_map`、`grain_key`、`as_of_date`、`purpose`、`overwrite`、`out_fmt` | manifest + 数据体检报告 |

`preview_only=true`：跑 `SELECT * FROM (<你的SQL>) LIMIT 100`，只看列与类型，**不落盘、不进证据链**。作用是**在拉 200 万行之前确认列名、类型、分区对不对**——写错分区跑三分钟才发现 0 行，是最常见的时间浪费。预览不要求 `as_of_date`。

### 6.1 输出路径：默认生成，允许覆盖

`out_path` 决定快照落在哪里，规则按优先级：

1. **不传** → `<data-root>/<dataset_name>.parquet`。注意是 **MCP 的 `--data-root`**，不是库自己的运行目录——否则 `list_credit_datasets` 与 `inspect_and_bind_snapshot` 会看不到刚下载的文件。
2. **传目录** → 目录 + 默认文件名。
3. **传文件名** → 用你的文件名，**后缀决定格式**（`.csv` 写 CSV，`.parquet` / `.pq` 写 Parquet，`.json` 写 JSON）。
4. 后缀不认识 → 报错并列出可用后缀，不猜。

两条安全规则：

- **默认不覆盖**。同名文件已存在时报错，除非显式 `overwrite=true`。理由：旧快照可能是某份已签字报告的输入。
- **不能写进 ledger / registry**。那两个根是只追加的证据目录，写进去等于污染审计链。

Parquet 写不出来（缺 pyarrow / 混合类型无法 round-trip）时降级为同目录 `.csv`，并把真实 `path` 与 `format` 回写到 manifest，不假装成功。

新增 `block_code`（`codes.py`）4 个，全部是作业性质：

```
ROW_CAP_EXCEEDED        超出 row_cap，拒绝落盘并建议拆分
MULTI_STATEMENT_SQL     多语句（误粘贴）
SAMPLE_FILE_UNREADABLE  文件无法解析（编码/分隔符/损坏/不存在）
BACKEND_UNAVAILABLE     驱动缺失或连接信息不全
```

新增 `gate`：无。这一版不引入任何新的人工审批环节（源头已有权限体系，下游再加一道只会阻塞分析）。

## 7. 本地导入：`import_sample_file`

针对非数据库存储的样本（Excel 交付、合作方 CSV、离线评分结果）。`data/ingest.py` 已有能力，补产品化外壳：

| 项 | 行为 |
|---|---|
| 编码 | UTF-8 → GB18030/GBK → UTF-8-SIG 依次探测，结果回写 manifest（信贷 Excel 导出 GBK 极常见） |
| 分隔符 | `,` `;` `\t` `\|` 嗅探，可显式覆盖 |
| xlsx | 列 sheet，默认第一个；需指定时提示可选值 |
| 列名 | 去空格/BOM/重复后缀（重名 → `name_2`） |
| 公式注入 | `= + - @` 开头字符串加前导 `'`（已有 `_guard_formula`） |
| 上限 | 50MB / 200 万行，超限给"请按月拆分"的具体建议 |
| 敏感列 | 同 4.3，只告警 |

**导入即体检**（报告可直接贴进证据包）：

1. 主键唯一性（传了 `grain_key` 才查）；
2. 标签列识别（`dpd*` / `bad*` / `y_*` / `flag*`）→ 无标签则 `degraded`，只能做描述性分析；
3. 时间列可解析 → 否则无法 vintage / OOT；
4. 缺失率 top10 与高缺失列（> 60%）；
5. 敏感列清单（告警）。

**列角色映射（`column_map`）**：Excel 列名千奇百怪（"放款金额(元)"、"MOB1逾期天数"），允许映射到标准角色 `key/label/cohort/time/feature`，写进 manifest，后续工具按角色取列，不靠名字猜。

**清洗怎么处理**：工具**不实现任何清洗算子**。分析师在本地清洗后再次 `import_sample_file`，形成一条新的 manifest——链条上留下两次快照，谁都看得出"原始长这样、清洗后长这样"。这比在工具里塞清洗功能更灵活，也让清洗本身留痕。

## 8. 证据包导出：`export_evidence_pack`

把"分析结果"变成"可审批材料"。消金/银行的模型与策略上线要过模型风险管理（MRM）与内审，目前这块要么手工拼 PPT，要么留不全。

```
evidence_pack_<report_id>/
├── README.md                  # 结论摘要、版本、有效期、复核人栏
├── report.html                # 已通过 verify_and_commit_report 验真的报告
├── citations.csv              # 每个数字 → 工具调用 → manifest 字段
├── params.json                # 全部入参与阈值（可原样重跑）
├── metrics.json               # KS/AUC/PSI/坏账率/Lift 的口径定义与值
├── manifests/                 # 本次引用的全部 sample_manifest.json
├── decisions.jsonl            # 人工决策：gate、选择、签核人、时间
├── data_profile/              # 分箱、缺失率、Vintage 曲线（聚合，非明细）
└── reproduce.md               # 重跑指令：工具名 + 参数
```

三条铁律：

1. **不含明细行**，只有聚合与指纹 —— 这是它可以自由流转（内审、外部评审）的前提。
2. **敏感列自动剔除**：manifest 里 `sensitive_columns` 命中的列不出现在任何交付物中，对应引用被判 `REPORT_UNTRACED`。
3. **写清 `as_of_date` 与适用批次区间**：样本窗口一变结论即失效，证据包有保鲜期。

## 9. 优先级与验收

已实现（v0.4）：`download_sample`（多后端）、`import_sample_file`、`sample_manifest` 契约、`out_path` 覆盖规则。

### 验收（已由 `tests/test_sample_acquisition.py` 覆盖，51 项）

- 未传 `user_sql`、扩列、加 `WHERE`、换表 → 一律拒绝且不落盘；带需求方授权原话才放行并留痕；

- `select ';' as s` 算一条语句；`select 1; drop table t` 被拒；
- DataWorks 的 `set odps.sql.*` 前置行不会误判成第二条语句；
- PG / Holo / MySQL 端口留空时分别回落到 5432 / 80 / 3306；
- 缺凭证时报错**点名缺失的变量**，且 `safe_target()` 不含密码；
- `out_path` 三种形态（缺省 / 目录 / 文件名）与后缀决定格式；
- 同名文件默认拒绝覆盖，`overwrite=true` 才替换；
- 缺 `as_of_date` 报错；`preview_only` 不落盘；
- `row_cap=2` 时拒绝写入，且磁盘上不留残文件；
- manifest 含全部契约字段，`sql_preview` 把字面量掩码为 `'?'`；
- 同一 SQL 两次下载 `sql_hash` 与 `rows_hash` 一致；
- GBK + 中文表头的 CSV 能解析，`身份证号` 被标为敏感、`逾期天数` 被识别为标签、`申请月份` 被识别为时间列；
- 两个入口产出的 manifest 键集合一致，下游无法区分来源。

### 未做（明确留白）

- `export_evidence_pack`：证据包导出。结构已在第 8 节定稿，尚未实现。
- 清洗算子：**故意不做**。清洗在本地完成，然后再次 `import_sample_file`，链条上留下"原始"与"清洗后"两个快照。

## 10. 这一版留下的风险

1. **口径漂移**：用户 SQL 可随时改，同名样本两次内容可能不同。缓解：`sql_hash` + `rows_hash` + `as_of_date` 全部写进 manifest，同名不同 hash 时可被发现。**这是设计上接受的**——SQL 归分析师所有，工具只负责让变化可见。
2. **`LIMIT` 对聚合 SQL 无效**：`GROUP BY` 汇总行数天然少，`row_cap` 不触发，但底层扫描量可能巨大。缓解：分区条件缺失时告警（`checks.partition_warning`）。**已接受**：个人分析场景下，扫描成本由分析师自己承担。
3. **odps 后端的只读最弱**：没有服务端只读会话，只有文本审计。缓解：返回值里显式标注 `server_side_read_only=false` 与 `readonly_note`，不给出虚假的安全感。
