---
name: credit-data-admission
description: 信贷数据准入体检、表现期成熟度与字段时点核查；标准条件不足时向用户解释可用范围并请求选择替代分析方式。未成熟样本不得当作成熟好样本。
license: MIT
allowed-tools:
  - download_sample
  - import_sample_file
  - inspect_and_bind_snapshot
  - check_label_maturity
  - resolve_metric_and_dimensions
  - verify_and_commit_report
metadata:
  version: "1.6.0"
  contract_version: "0.5"
---

# 信贷数据准入与样本设计规程 (`credit-data-admission`)

本规程基于《自动化信用管理的理论与实践》中的样本设计方法论制定。**任何存量资产监控、异常归因、特征工程或评分卡建模任务启动前，必须先通过本规程完成数据准入体检。** 未通过准入或缺少关键要素时，必须按本规程降级或终止后续分析，严禁带着未成熟标签或穿越字段强行计算。

详细数学定义、字段角色契约与 YAML 配置规范见 [references/data-contract-and-windows.md](references/data-contract-and-windows.md)。

---

## 取数（本规程的前置动作）

准入体检要跑在本地快照上，所以先取数。两条路，产出同一种 `sample_manifest`，下游不区分来源：

- **数仓下载**：`download_sample(sql=..., user_sql=..., as_of_date="YYYY-MM-DD", dataset_name=..., grain_key=...)`。SQL 由分析师本人提供（清洗逻辑、多表打平、窗口去重都在他的 SQL 里），工具保证：一条语句、只读、行数有上限、留痕，**并且只执行需求方给过的那条语句**。先跑 `preview_only=true` 确认列名与类型，再正式拉取。
  - **原样执行是硬门槛，不是事后留痕**：`user_sql` 必填，`sql` 必须与之逐字一致（列清单、WHERE、表名、时间窗一律不动）。不一致时工具**直接抛错拒绝执行**，不读数据、不落盘，返回码 `SQL_SCOPE_NOT_AUTHORIZED`。预览同样受约束。**不存在"先按自己的口径取一份、再在结果里标注说明"这条路径。**
  - **要加字段先问，同意了才填 `authorization`**：先讲清"现有字段不够在哪、要加哪几列、会改变什么口径"，等需求方回话；同意后把他的**原话**填进 `authorization` 才放行（写入 `checks.deviation_authorized_by`），不得自行编造。扩列后用新的 `dataset_name` 单独取一份，不覆盖原快照。
  - **`purpose` 必填**（预览除外）：写清用途与授权来源，例如"需求方给定 SQL，原样执行"。写不出授权来源，说明还没拿到授权，此时也不该取。
  - **元数据不受限**：只看列名/类型用 `desc <table>`（走 `schema()`，不读数据行）。
- **本地文件**：`import_sample_file(file_path=..., dataset_name=..., column_map={...})`。适配 Excel / CSV / Parquet，自动探测编码与分隔符，返回体检报告。

`as_of_date` **必填**（预览除外）：成熟度 = 批次月末 + 表现期，而表现期终点只有取数那一刻知道，数据本身推不出来。缺它就无法判定成熟度，因此工具直接报错而不是猜。

`grain_key` 建议一并传：`一行一申请` 与 `一行一笔贷款` 混用会让 vintage、迁徙率、坏账率全部静默错位。传了就会返回重复行数。

**`download_sample` 返回 `BACKEND_UNAVAILABLE` 或缺变量清单时，不要重试、不要让用户改代码**：这说明数仓凭证还没配。让用户在他自己的终端运行 `credit-risk-config`（或 `python scripts/config_setup.py`），浏览器里会打开一个配置页，选数据库类型、填地址与账号、点保存即可；保存后需重启客户端。如果用户反馈"页面能填但点保存没反应"（常见于 IDE 内置的 HTML 预览面板），让他改用 `credit-risk-config --serve`：会起一个只监听 `127.0.0.1` 的服务，浏览器访问终端打印的地址、填完点保存就直接写回文件，不依赖页面脚本。凭证只有 `download_sample` 需要，`import_sample_file` 与本规程其余工具完全离线可用——如果用户暂时拿不到库权限，走本地文件那条路。

---

## 何时使用 / 何时不使用

**五规程优先级**：任何存量资产监控、异常归因、策略模拟、评分卡建模或无标签分布分析任务，都必须先经 `credit-data-admission` 完成数据准入体检，取得合法的 `dataset_snapshot_id` 与成熟样本结论；未过准入，其余四个分析规程不得启动。

**何时使用（触发条件）**：需要绑定数据快照、核查表现期成熟度、划分好坏灰样本、登记字段时点角色，或在标准成熟样本不足时向用户解释可用范围并请求选择替代分析方式时使用。信贷建模按月份观察（monthly_cohort），策略分析尽可能拿最新可观察样本（latest_observed），由调用方按场景选择 `observation_mode`。

**何时不使用（不得引用本规程结论的情形）**：
- 目标表现期没有成熟批次、或仅有 latest_observed 最新可观察样本时，不得引用本规程结论计算目标表现期坏账率、训练模型或把未成熟样本当作 Good；**latest_observed 最新可观察样本不得用于建模或目标坏账率结论**，只做描述性测算。
- 判定为 Level B / C 的快照，不得据此放行长期坏账率归因、模型训练或策略上线收益测算；未成熟标签不得视作成熟结果。
- 已完成准入的存量报告，不因本规程再次改写其指标口径；本规程只裁定可用范围，不在准入阶段另算风险指标。

---

## 一、核心原则（不可违背的红线）

1. **观察点（Observation Point）隔离原则**：
   - 每个样本必须有明确的观察时点 $T_0$（如申请日 `apply_date`、放款日 `loan_date` 或行为评分观察日 `obs_date`）。
   - **观察期（Observation Window, $\le T_0$）**：用于提取特征或归因维度的历史窗口。只允许使用在 $T_0$ 当时已冻结、已知的字段。
   - **表现期（Performance Window, $(T_0, T_0 + W]$）**：用于观察还款表现、定义好坏标签或计算风险指标的未来窗口。表现期内的动态字段（如当前逾期天数、催收记录、贷后还款额）**绝对禁止**作为预测特征或原因归因维度。
2. **样本成熟度（Sample Maturity）硬性门禁（按场景分化）**：
   - 设数据快照截止日为 $T_{\text{cutoff}}$，表现期长度为 $W$。**信贷建模按月份观察、策略分析尽可能拿最新样本**：`check_label_maturity` 的 `observation_mode` 取 `monthly_cohort`（默认）或 `latest_observed`。
   - monthly_cohort 按 Vintage/MOB 行业惯例以批次判成熟：**放款批次月末 $+ W$ 个完整账龄月 $\le T_{\text{cutoff}}$** 才算成熟，不是逐笔按 $T_0 + W$ 判；FPD 类自然日窗口仍按 $T_0 + \text{days} \le T_{\text{cutoff}}$ 逐笔判定。
   - `decision_time_column` 用于逐笔校验 $T_0$ 与登记批次月份是否一致，不一致计数入摘要 `t0_cohort_mismatch` 并降级；摘要同时给出 `observation_mode`、`maturity_rule`、`window`、`cutoff`、`decision_time_column`、`t0_cohort_mismatch`。
   - 未成熟样本尚未走完表现期，其当前“未逾期”不代表最终是好客户。**严禁将未成熟样本混入分母计算目标账龄坏账率，或作为好样本（Good）放入训练集。**
3. **好、坏、灰与排除样本（Good / Bad / Indeterminate / Exclusion）明确划分**：
   - 建模与归因前必须显式登记：坏样本定义（Bad）、好样本定义（Good）、灰样本策略（Gray/Indeterminate，默认剔除 `exclude`）以及总体排除项（Exclusion，如欺诈、政策性拒单、测试单、关键主键缺失）。

---

## 二、工具返回与停机

本规程只通过 `inspect_and_bind_snapshot`、`check_label_maturity`、`resolve_metric_and_dimensions`、`verify_and_commit_report` 取数和落结论。样本明细、成熟掩码和字段示例值留在工具进程内。评估单只引用本次返回摘要中的计数字段。allowed-tools 只是工具声明，实际门禁由工具层返回的 envelope 与审批账本保证。

| status | 动作 |
| --- | --- |
| `ok` | 继续下一步 |
| `degraded` | 把可用范围写入评估单；下游只能做摘要允许的任务 |
| `needs_human` | 展示目标表现期的成熟样本数和替代路径，请用户选择；选择前不继续目标风险计算 |
| `blocked` | 写出缺口后停止，不进入归因、建模或策略模拟 |

任何工具异常统一返回 `blocked` + `block_code`（`RESOURCE_UNAVAILABLE` / `UNSUPPORTED_SCOPE` 等）的结构化 envelope，不再抛裸异常。

| block_code | 停机时怎么写 |
| --- | --- |
| `SNAPSHOT_INVALID` | 写明主键、观察日或已绑定列的缺口。不改粒度后重试 |
| `METRIC_UNSIGNED` | 写明指标未签收或版本不一致。不自拟分子、分母 |
| `LABEL_IMMATURE` | 展示目标表现期的成熟样本数与三项选择（较短且已签收表现期 / 无标签描述 / 等待成熟），请用户明确选择；未落账不自动切换指标 |
| `VALIDATION_UNAVAILABLE` | 说明 `check_label_maturity` 的 `maturity_choice_review_id` 缺失或非法（所选方式未落账或不合法），停止并请用户重新选择，不自动切换指标 |
| `REPORT_UNTRACED` | 删掉或改正文中对不上的数字，再重新核验 |
| `RESOURCE_UNAVAILABLE` | 说明数据文件、维表或运行环境暂不可用，写出缺口后停止，不臆造数值重试 |

调用参数必须写明 `grain`、`key_columns`、`decision_time_column`、`observation_mode` 与 `weight_column`。monthly_cohort 按「放款批次月末 $+ W \le T_{\text{cutoff}}$」判成熟；FPD 类自然日窗口按 $T_0 + \text{days} \le T_{\text{cutoff}}$ 判。`weight_column` 写入快照 meta 作下游金额加权默认。摘要中的 `observation_mode`、`maturity_rule`、`window`、`cutoff`、`decision_time_column`、`t0_cohort_mismatch` 与 `selected_option` 原样引用；这与 Vintage 表里的账龄覆盖率是两个字段，评估单分别引用。

知识库和参考文档里的 YAML 只说明文件格式。示例中的版本号、刀口和签收人不得写入评估单。口径以 `resolve_metric_and_dimensions` 的摘要为准。

定稿时调用 `verify_and_commit_report`。每个比率、样本量和日期都提交一条 `citations`：`path` 指向本次摘要字段，`rendered` 是正文中的写法。citation 的 `rendered` 支持整数与日期文本（日期按去空格后精确比对），比率仍按展示精度对照产物中保留的 6 位小数。

错误码与字段角色的完整对照见 [references/data-contract-and-windows.md](references/data-contract-and-windows.md)。

---

## 三、标准作业步骤（6 步准入法）

### 步骤 1：绑定数据快照与基础完整性体检
调用 `inspect_and_bind_snapshot` 对目标数据集进行扫描，核查以下五项基础要素：
1. **主键唯一性与粒度**：明确一行代表“一笔借据（Loan）”、“一次申请（Application）”还是“借据 × 观察月（Loan-Month）”。检查主键重复率与缺失率——缺失率与 `column_missing` 同一口径（字面缺失码也算），重复率只在**主键齐全**的行上算（两行都写 `N/A` 不等于同一笔业务，它们只进缺失率；没有一行主键完整时直接 `SNAPSHOT_INVALID`，三种粒度一致，因为一行都标识不了）。主键按字符串读入，`001` 的前导零是身份的一部分，重复率不受 CSV/Parquet 格式影响。
2. **时间轴范围**：核实最早与最晚放款日/申请日、最新数据观察截止日 $T_{\text{cutoff}}$，检查是否存在未来日期异常（$T_0 > T_{\text{cutoff}}$）或时间断档。
3. **分母与权重字段**：确认笔数分母与金额权重字段（如放款本金 `principal`、在贷余额 `balance`），检查负值、零值或极端离群值。
4. **缺失与常量扫描**：`column_missing` 逐列给出缺失率（按缺失率降序，最多 50 列；`column_missing_summary.truncated` 为 `true` 时，**名单外还有缺失的列**，只是没进前 50）。只给列名的名单看不出「缺了 62%」和「缺了 96%」的区别，而前者还能用、后者不能用。`high_missing_columns`（缺失率 $> 95\%$）与 `constant_columns`（单一值占**非缺失** $> 99\%$，空白不算一个取值）只是同一批数字的两个名单。
   **缺失按「空值或字面缺失码」统一口径统计**，与无标签描述工具同一份清单：否则一列 87% 是 `"N/A"` 的数据在准入报 0% 缺失、到描述里变成 87%，而先被看到的总是前者。注意这里**只报比率、不报「原本写的是 N/A 还是本来就空」**：CSV 读入时 `N/A`/`NA`/`NULL` 已被解析器转成空值，Parquet 才会原样保留，两条路给不出同一个拆分——要这个区别得问取数侧。口径里用到的高缺失列，描述阶段会先被拦成 `needs_human` 要求声明缺失含义（`human_gate.kind` 是 `binary_column_role` 还是 `field_semantics` 取决于列形态），不会带着未定义的分母出数。
5. **生成不可变快照标识**：锁定 `dataset_snapshot_id` 与数据哈希，后续所有分析与实验必须引用该 ID。

### 步骤 2：核对已签收指标口径与维度登记表
调用 `resolve_metric_and_dimensions` 读取当前机构已签收的业务口径配置：
- 核实目标风险指标的**分子、分母、加权方式、观察窗 $W$、过滤条件及口径版本号（`metric_version`）**。
- 将数据表中的列划分为四类角色：
  - `id_and_time`：主键、申请/放款日、批次月（Cohort）、观察日、账龄（MOB）；
  - `decision_point_dimensions`：决策时点冻结字段（渠道、产品、客群、地区、审批评分档、授信额度档、当时命中的策略版本）；
  - `observation_outcomes`：观察时点结果字段（DPD、逾期阶段 M0~M4+、坏账标记、核销标记、在贷余额）；
  - `suspect_leakage`：未登记时点属性、命名含贷后特征（如 `repay_*`, `collect_*`, `current_dpd`, `writeoff_*`）的疑似穿越列。

`inspect_and_bind_snapshot` 的 `column_roles` 接受 `decision_time` / `observation_outcome` / `suspect_leakage` / `feature`。`feature` 用于**主动声明某列是决策时点已知的特征**，主要针对 0/1 标记列：无标签描述工具（`population-distribution-analysis`）会把所有未声明为 `feature` 的 0/1 列拦在 `needs_human`，因为它们的均值就是比率——不声明它是什么，那 0.1 既可能是特征命中率，也可能是坏账率。

`decision_time_column` 必须是**真日期**：取值为 0/1（数值、`object`、布尔都算）、小编码（`1/2`、`2/3`）、或纯数值编码（`20240901`、epoch 秒都会被 pandas 读成 1970 年纳秒时间戳）一律在绑定时以 `SNAPSHOT_INVALID` 拒绝。**数值与日期混在一列同样拒**：只要整个年份区间里出现 1970，这列就不是日期列——一个文本日期会把最大年份抬到 2024，按最大值判断会让剩下的编码行溜过去。否则一个 0/1 标签列可以冒充决策时点，之后按它分期，两期分母正好是坏客户数与好客户数。存量数据若用整数存日期，请先 `to_datetime(...).dt.strftime('%Y-%m-%d')` 再绑定。

### 步骤 3：样本成熟度与有效时间窗核算（按 observation_mode 分化）
调用 `check_label_maturity` 传入 `dataset_snapshot_id`、批次列、决策时点列、目标表现期 $W$ 与 `observation_mode`：
1. **monthly_cohort（默认，信贷建模）**：以放款批次为单位裁定成熟，判据为「批次月末 $+ W$ 个完整账龄月 $\le T_{\text{cutoff}}$」，不是逐笔 $T_0$；FPD 类自然日窗口按 $T_0 + \text{days} \le T_{\text{cutoff}}$ 逐笔判定。`decision_time_column` 逐笔校验 $T_0$ 与登记批次月份，不一致的计数写入 `t0_cohort_mismatch` 并降级。划分**已成熟批次区间**与**未成熟批次区间**，引用摘要 `mask_id`（掩码不进入对话）；建模任务只把成熟批次交给开发集与时间外样本，存量监控只在各批次已走完的账龄上比较。
2. **latest_observed（策略分析，尽可能拿最新样本）**：生成含未成熟批次的最新可观察掩码，返回 `degraded` 与 `latest_review_id`、`latest_row_count`、`latest_cohorts`、`label_status="immature"`。该模式**只做描述性测算**：不产出目标表现期坏账率、不把未成熟样本视为 Good、**不可用于建模或目标坏账率结论**。
3. 目标账龄没有成熟批次时，工具返回 `needs_human` 与 `LABEL_IMMATURE`，准入记录仍为阻断。展示三项选择：① 改用较短且已签收的表现期重新核查；② 只做不含坏账率的无标签描述分析；③ 等待目标标签成熟。三项选择由审批卡 `gate=maturity_choice` 落账，传入 `maturity_choice_review_id` 后摘要给出 `selected_option`；**未落账不自动切换指标**。选择②不计算目标坏账率、不建模、不声称策略效果。
4. 摘要的 `observation_mode`、`maturity_rule`、`window`、`cutoff`、`decision_time_column`、`t0_cohort_mismatch` 原样引用；若同时给出 Vintage 覆盖率，只作为作图覆盖说明，不当作本步骤的成熟结论。

### 步骤 4：好坏灰样本与排除规则确认（建模与策略场景必做）
核查目标标签列（Label）的分布与完备性：
- **坏样本（Bad = 1）**：在表现期 $W$ 内曾经或期末达到约定逾期深度（如 `DPD30+`）或发生核销/理赔。
- **灰样本（Indeterminate / Gray）**：曾发生轻度逾期（如 `1 <= DPD < 30`）但未达坏标准，或提前结清且存续期不足。默认按 `gray_policy="exclude"` 从训练集中剔除（但在全量策略回测与通过率核算时必须重新计入）。
- **好样本（Good = 0）**：走完完整表现期 $W$ 且始终正常履约的客户。
- **样本量底线检查**：成熟样本中总行数须 $\ge 50$（生产级建议 $\ge 2000$），正负样本各自不少于最小门槛（测试底线 $\ge 10$，生产级建议坏样本 $\ge 100$）。准入 Level A 现在只按「成熟样本 $\ge 50$、正负各 $\ge 10$」判定，`observation_outcomes` 里的正常表现字段不再触发降级。

### 步骤 5：判定可交付能力与必须暂停的承诺
根据体检结果，严格对照下表判定后续工作边界：

| 体检结果等级 | 满足的条件 | 允许放行的下游任务 | 必须阻断或暂停的承诺 |
| --- | --- | --- | --- |
| **Level A（完备）** | 主键唯一、口径已签收、有成熟批次且坏样本充足、决策时点字段清晰 | 存量监控与归因（`portfolio-anomaly-attribution`）、评分卡建模（`scorecard-modeling-pipeline`）、策略模拟（`policy-cutoff-simulation`） | 无阻断；但若无拒绝样本表现，仍禁止承诺放宽策略的真实上线收益 |
| **Level B（部分受限）** | 有存量明细与逾期状态，但缺策略版本日志、或部分新批次未成熟 | 同账龄存量监控、结构效应与组内效应分解、已成熟批次的探索性分析 | **禁止**归因到“策略失效”；未成熟批次**禁止**用于训练模型或评估长期坏账率 |
| **Level C（阻断）** | 缺主键/缺观察日/全量样本均未走完观察窗/无权限或口径冲突 | 输出《数据缺口与最小补数方案》；用户选择后可做不含目标坏账率的描述统计 | 阻断目标风险归因、模型训练与策略调优，不能把未成熟标签视作成熟结果 |

### 步骤 6：输出《数据准入与可用范围评估单》
在进入任何下游 Skill 前，必须向用户输出结构化的准入摘要，并调用 `verify_and_commit_report` 核验其中的数字：
1. **快照概览**：`dataset_snapshot_id`、样本粒度、总记录数、覆盖时间跨度、最新观察截止日 $T_{\text{cutoff}}$。
2. **口径与成熟度结论**：绑定的 `metric_version`、目标表现期 $W$、已成熟批次范围、有效成熟样本量及基准事件率（Bad Rate）。
3. **字段角色与风险隔离清单**：允许用于归因/入模的决策时点字段数、被拦截的观察时点/疑似穿越字段名单。
4. **准入等级与后续动作**：明确标注 `Level A / B / C` 及下一步将调用的业务规程。等级只决定能否进入下游，不在这一步另算风险指标。三次工具调用均为 `ok` 且成熟样本 ≥ 50、正负样本各 ≥ 10、且 `observation_outcomes` 正常表现字段不再触发降级时为 Level A；快照可用、但存在未成熟批次或缺少策略版本时为 Level B；任一调用为 `blocked` 时为 Level C。
