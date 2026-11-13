> **Status：已作废（历史稿，已归档）** — 本文描述的是历史规划（`POST /topics`、`POST /datasets/upload`、`credit-agent run`、`code_sandbox` 均未实现），现行交付以 `README.md` 与 `docs/workbuddy-skill-mcp-integration-guide.md` 为准；本文仅作历史决策追溯，不作为实现依据。

# 数据输入说明

课题要分析就得先有数据。平台提供三种数据输入方式，最终都落到同一个东西上：**本地快照**。

---

## 1. 三种输入方式

| 方式 | 适用场景 | 入口 |
| --- | --- | --- |
| **用户直供取数 SQL** | 分析师已经知道要查哪张表 | `POST /topics` 的 `data_input.sql`；CLI `--sql` |
| **上传 CSV/Excel/Parquet 文件** | 外部导出、第三方数据、本地样本 | `POST /datasets/upload` + `POST /topics` 的 `dataset_ref`；CLI `--csv` |
| **数仓表/视图** | 直接查平台已接入的数仓 | `code_sandbox` 插件的 `sql` 参数 |

---

## 2. 用户直供 SQL

### HTTP

```bash
curl -X POST http://127.0.0.1:8890/topics \
  -H 'Content-Type: application/json' \
  -d '{
    "query": "分析进件客群外部负债的变化趋势，识别共债风险集中的客群切片",
    "data_input": {
      "sql": "select loan_no, debt_trend, org_num from opmgt_dev.t_zy_early_risk_mob where mob = 6;",
      "name": "early_risk_mob6",
      "limit": 200000
    }
  }'
```

响应里会带上数据血缘：

```json
{
  "session_id": "71c5a73f79ff",
  "waiting": true,
  "dataset": {
    "name": "early_risk_mob6",
    "row_count": 2556,
    "format": "csv",
    "sha1": "078b0c0f3d793caa",
    "path": "~/.credit-agent/data/datasets/early_risk_mob6.csv"
  }
}
```

### CLI

```bash
credit-agent run "分析进件客群外部负债的变化趋势" \
  --sql "select loan_no, debt_trend, org_num from opmgt_dev.t_zy_early_risk_mob where mob = 6;" \
  --dataset-name early_risk_mob6 \
  --auto-approve
```

### Python

```python
from credit_agent.data.ingest import ingest_sql
from credit_agent.graph.engine import TopicRunner

dataset = ingest_sql(
    "select loan_no, debt_trend, org_num from opmgt_dev.t_zy_early_risk_mob where mob = 6;",
    "early_risk_mob6",
)
runner = TopicRunner()
out = runner.start("分析进件客群外部负债的变化趋势", dataset_ref=dict(dataset))
```

---

## 3. 上传数据文件（CSV / Excel）

上传是取数 SQL 之外的第二条输入路径，两者**最终产物完全一致**：一份落盘的快照 + 一份 `DatasetRef`
（行数、列画像、格式、哈希、来源），下游分析与沙箱看到的东西没有任何区别。

### HTTP

```bash
# 1) 上传并快照（multipart）
curl -X POST http://127.0.0.1:8890/datasets/upload \
  -F "file=@外部负债名单.csv" \
  -F "name=early_risk_mob6"        # 可选，缺省由文件名推导
  -F "delimiter="                  # 可选，留空自动探测 , ; \t |
  -F "encoding="                   # 可选，留空自动探测 UTF-8/GBK…

# 2) 用这个数据集建课题
curl -X POST http://127.0.0.1:8890/topics \
  -H 'Content-Type: application/json' \
  -d '{"query": "分析进件客群外部负债的变化趋势", "dataset_ref": <上一步返回的 dataset>}'
```

上传响应会带上列名与预览行，便于在 UI 里先确认"我传对了文件"：

```json
{
  "dataset": {
    "name": "early_risk_mob6",
    "row_count": 2556,
    "format": "csv",
    "origin": "upload",
    "source_file": "外部负债名单.csv",
    "delimiter": ",",
    "sha1": "078b0c0f3d793caa",
    "path": "…/data/datasets/early_risk_mob6.csv"
  },
  "preview": { "columns": [{"column": "loan_no", "type": "string"}], "rows": [ …前 20 行… ] }
}
```

其它端点：`GET /datasets`（列出历史快照，可跨课题复用）、`GET /datasets/{name}?preview=20`、
`GET /datasets/limits/current`（当前体积/行数上限）。

### CLI

```bash
credit-agent run "分析进件客群外部负债的变化趋势" \
  --csv ./外部负债名单.csv \
  --dataset-name early_risk_mob6 \   # 可选
  --delimiter , --encoding gbk \     # 可选
  --auto-approve
```

### Python

```python
from credit_agent.data.ingest import ingest_file, ingest_bytes

ref = ingest_file("./外部负债名单.csv")          # 本地文件
ref = ingest_bytes(raw_bytes, "名单.csv")        # 来自 HTTP 上传
```

### 上传侧的清洗规则（都是为了防止"看起来成功、实际错误"）

| 风险 | 处理 |
| --- | --- |
| 中文导出常见 GBK/GB18030 | 编码探测链 `utf-8-sig → utf-8 → gb18030 → gbk → big5 → latin-1`；BOM 自动剥离 |
| 分隔符猜错 | 按表头行统计 `, ; \t |` 命中数自动探测；**只解析出 1 列时直接报错**，提示显式指定 `delimiter`，绝不静默产出垃圾 |
| 空表头 / 重复列名 | 空表头补 `col_<n>`，重复列加 `_1` 后缀；`.` 与空格折成 `_`（否则下游 DuckDB 里列互相覆盖） |
| CSV 公式注入 | 以 `= + - @ TAB CR` 开头的字符串单元格统一加前导 `'` 中和（上传文件不可信） |
| 超大文件 | 体积上限 `CREDIT_AGENT_UPLOAD_MAX_MB`（默认 50）、行数上限 `CREDIT_AGENT_UPLOAD_MAX_ROWS`（默认 200 万）；超限明确报错并提示怎么调 |
| 伪造 `dataset_ref` | `dataset_ref` 经由浏览器往返，服务端只接受 **datasets 目录内真实存在**的快照路径，否则 422 |

支持 `.csv / .tsv / .txt / .parquet / .pq / .xlsx / .xls`。Excel 首行作为字段名；单表自动读取，多表必须选择工作表，不自动合并。文本编号保留前导零，日期和数值按单元格实际值解析。公式仅读取 Excel 已保存的计算值，不执行公式；缺少计算缓存或存在错误单元格时提示修正，避免静默丢失数据。

`POST /datasets/workbook` 仅返回工作表清单，不创建快照；`POST /datasets/upload` 的 `sheet_name` 表单字段指定工作表。CLI 使用 `--file 样本.xlsx --sheet 样本`（保留 `--csv` 别名）。上传结果包含源文件、工作表、格式与源内容 SHA256，解析后进入与 CSV 相同的预检和研究流程。

产品入口仅提供上传文件和外部数仓 SQL；SQL 使用 `external_only: true`，外部连接未配置时不会回退本机演示库。默认 `credit-agent init` 不创建业务数仓，开发演示需明确使用 `init --demo-warehouse`。上传与取数结果保留分析快照用于复核，不写入本机业务数仓。

---

## 4. 为什么要快照，而不是每次实时查

1. **可复现**：报告里的每个数字都能从一份固定输入重新算出来。上游表后来变了，审计结论依然站得住。
2. **省钱**：MaxCompute 按扫描量计费。快照之后，下钻、假设验证、建模都不再重复扫描。
3. **安全边界**：沙箱只能读本地文件，**物理上不可能碰到生产凭证或生产表**。分析代码无论怎么写都越不过这条线。

快照优先写 Parquet（保留类型），缺 pyarrow 时回落 CSV。

---

## 5. 只读强制执行

所有 SQL 在执行前都要过 `DataSource.assert_read_only()`：

- 语句首词必须是白名单之一：`SELECT / WITH / DESCRIBE / SHOW / EXPLAIN / PRAGMA`
- 语句中不得出现 `INSERT / UPDATE / DELETE / DROP / ALTER / CREATE / TRUNCATE / MERGE / COPY / ATTACH`
- 会先剥离前导注释（`--`、`/* */`），避免绕过

拒绝发生在**任何驱动看到语句之前**。

---

## 6. 凭证规范（重要）

**凭证只允许来自环境变量，代码中严禁硬编码。**

```bash
cp .env.example .env
# 编辑 .env 填入真实凭证
```

仓库根目录的 `.env` 不入库。`credit_risk_agent/tools/odps_conn.py` 与 `credit_risk_agent/tools/odps_conn_opmgt.py` 通过 `get_credentials()` 从环境读取：

| 变量 | 说明 |
| --- | --- |
| `CREDIT_AGENT_ODPS_ACCESS_ID` | AccessKey ID |
| `CREDIT_AGENT_ODPS_ACCESS_KEY` | AccessKey Secret |
| `CREDIT_AGENT_ODPS_ENDPOINT` | 可选，有默认值 |
| `CREDIT_AGENT_ODPS_PROJECT` | 项目空间，如 `opmgt_dev` |

缺少凭证时，会抛出明确指出缺哪个变量的 `CredentialsError`，而不是裸崩溃或泄漏。

`ODPSAdapter` 会优先复用 `credit_risk_agent/tools/` 下的连接工具（优先 `odps_conn_opmgt`），保证凭证与项目接线只有一处。

---

## 7. 后端切换

```bash
# 离线 / 本地等价数据
CREDIT_AGENT_DATA_BACKEND=duckdb

# 生产 MaxCompute（只读）
CREDIT_AGENT_DATA_BACKEND=odps
```

**同一条 SQL 在两个后端上原样可跑**：本地模式会在 DuckDB 中建立同名 `opmgt_dev` schema，因此 `from opmgt_dev.t_zy_early_risk_mob` 无需改写。切换后端不应要求修改一条本来能跑的查询。

---

## 8. 本次验证的外部负债课题

| 项 | 内容 |
| --- | --- |
| 取数 SQL | `select loan_no, debt_trend, org_num from opmgt_dev.t_zy_early_risk_mob where mob = 6;` |
| 意图类型 | `external_debt_trend` |
| DAG | 7 个任务：知识检索 → 质量校验 → 数据集画像 → 趋势分布 → 交叉分析 → 风险假设 → 报告 |
| 人工评审点 | 3 个（交叉分析、风险假设、报告） |

分析产出：
- 全列画像（缺失率 / 基数 / 数值分布）
- `debt_trend` 分布（**自动识别**是分档标签还是数值，不臆造取值域）
- `org_num` 分档分布 + 多头借贷（≥3 家）占比
- `debt_trend × org_num` 交叉表 + 各趋势档位的机构数均值/P90

演示脚本：`python scripts/demo_odps_topic.py --show-report`
