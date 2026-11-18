# 信贷数据契约、观察窗核算与字段登记规范

本文档为 `credit-data-admission` 的技术参考规范，定义了观察期与表现期的数学边界、字段角色绑定契约（对齐 `credit_agent` 内核）以及口径与维度登记表标准格式。

---

## 1. 观察点、观察期与表现期数学定义

在《自动化信用管理的理论与实践》框架下，任何信贷样本的时间轴由三个基准点界定：

```
  [ 历史观察期 Observation Window ]        [ 未来表现期 Performance Window ]
-----------------------------------|----------------------------------------|----------> 时间轴
                              T0 (放款批次月末)                T_cohort + W <= T_cutoff
                         (申请日/放款日/重评日)              (W 个完整账龄月期满)  (数据截止日)
```

1. **特征提取约束（防未来信息穿越）**：
   对于任意候选特征或归因维度 $X_j$，设其数据产生或更新时点为 $t(X_j)$，必须满足：
   $$t(X_j) \le T_0$$
   凡是 $t(X_j) > T_0$ 的变量（如贷后还款次数、最近一次催收结果、当前逾期天数 `dpd`、历史最高逾期阶段 `max_delq`），一律归类为**表现期结果变量**，严禁进入特征白名单 `feature_allowlist` 或归因维度表。

2. **样本成熟度判定准则（月度批次口径）**：
   建模与监控默认 `observation_mode="monthly_cohort"`，按 Vintage/MOB 行业惯例，**以放款批次（Cohort）为单位整体裁定**：设放款批次月末为 $T_{\text{cohort}}$，目标风险指标要求观察满 $W$ 个完整账龄月（即目标账龄 $\text{MOB}^* = W$），数据截止日为 $T_{\text{cutoff}}$。
   - **成熟批次（Mature Cohort）**：$T_{\text{cohort}} + W \text{ 个完整账龄月} \le T_{\text{cutoff}}$。
   - **未成熟批次（Immature Cohort）**：$T_{\text{cohort}} + W \text{ 个完整账龄月} > T_{\text{cutoff}}$。
   - `decision_time_column` 只用于逐笔校验 T0 与登记批次月份是否一致：不一致计数写入摘要 `t0_cohort_mismatch` 并降级，不据此改写批次成熟结论。
   - FPD 类以自然日计的窗口按 $T_0 + days \le T_{\text{cutoff}}$ 逐笔判定，不走月度批次口径。
   - 摘要固定记录 `observation_mode`、`maturity_rule`、`window`、`cutoff`、`decision_time_column`、`t0_cohort_mismatch`。
   - **红线不变**：未成熟批次不混入坏账率分母，也不当作 Good。
   - **特殊情形（早偿与提前坏）**：尽管部分未成熟样本在 $\text{MOB} < W$ 时已发生严重逾期（Early Bad），若仅将未成熟批次中的“已逾期客户”纳入坏样本，而未成熟批次中的“暂未逾期客户”尚未走完表现期，会导致分母缺失或分子高估。因此，**必须以放款批次（Cohort）为单位整体裁定该批次在目标账龄 $W$ 下是否成熟**。

### 1.1 观察模式（observation_mode）与最新可观察样本

| `observation_mode` | 适用场景 | 成熟度口径 | 产出 |
| --- | --- | --- | --- |
| `monthly_cohort`（默认） | 建模、监控、目标表现期坏账率 | 放款批次月末 + $W$ 个完整账龄月 ≤ `cutoff` | 成熟批次进入分母，未成熟批次隔离 |
| `latest_observed` | 策略分析尽可能拿最新样本 | 生成含未成熟批次的最新可观察掩码 | 仅描述性测算，返回 `degraded` |

`latest_observed` 路径的掩码固定包含 `latest_review_id`、`latest_row_count`、`latest_cohorts`、`label_status="immature"`；只做描述性测算，**不构成目标表现期坏账率，不得用于建模或上线收益测算**。三项选择（改选更短且已签收的表现期 / 无标签描述 / 等待成熟）见第 4 节。

---

## 2. 字段角色映射契约（对齐 `credit_agent`）

数据准入服务将表字段映射至以下标准角色（对应 `credit_agent.analytics.columns.ROLE_LABELS` 与 `credit_agent.modeling.config.ColumnBinding`）：

### 2.1 分析与监控角色（`analytics`）
| 角色键名 | 业务含义 | 常见列名示例 | 时点属性 |
| --- | --- | --- | --- |
| `id` | 借据或申请唯一主键 | `loan_id`, `apply_id`, `contract_no` | 标识列 |
| `cohort` | 放款批次月 / 申请月 | `vintage`, `loan_month`, `apply_month` | 决策时点 $T_0$ |
| `obs_time` | 观察日 / 快照月份 | `obs_date`, `stat_month`, `snapshot_dt` | 观察时点 $T_{\text{obs}}$ |
| `mob` | 账龄（Months on Book） | `mob`, `age_month`, `term_elapsed` | 观察时点派生 |
| `segment` | 预登记的归因切片维度 | `channel`, `product`, `region`, `customer_tier` | 决策时点冻结 |
| `policy_ver` | 审批当时命中的策略版本 | `strategy_version`, `rule_set_id` | 决策时点冻结 |
| `weight` | 分母权重（金额口径） | `principal`, `loan_amount`, `balance` | 放款时点或当期余额 |
| `bad_flag` | 二值风险结果标记（0/1） | `fpd30`, `dpd30_flag`, `is_bad`, `ever_90` | 表现期结果 |
| `delq_bucket` | 逾期阶段分档（用于迁徙矩阵） | `delq_stage`, `m_bucket` (`C`, `M1`, `M2`, `M3+`) | 表现期结果 |

字段角色按两类区分，**不得混为一谈**：

- `observation_outcomes`（正常表现字段）：如 `bad_flag`、`delq_bucket`，其时点属性本就是表现期结果，作为标签或监控口径正常登记，**不导致准入降级**。
- `suspect_leakage`（疑似穿越字段）：决策时点不明、却被登记为可用于特征/归因的字段，才触发准入降级与人工时点核查。

准入 Level A 在列绑定校验之外增加成熟样本门槛：成熟样本 ≥ 50，且正类（Bad）与负类（Good）各 ≥ 10；不满足时按 `LABEL_IMMATURE` 处理。已登记的 `weight_column`（分母权重口径）写入快照 `meta`，作为下游分析的默认权重来源。

### 2.2 建模列绑定契约（`ColumnBinding`）
```json
{
  "label": "is_bad_mob6",
  "positive_values": [1],
  "negative_values": [0],
  "gray_values": [2, -1],
  "gray_policy": "exclude",
  "time_column": "apply_month",
  "id_columns": ["loan_id", "customer_id"],
  "exclude_columns": ["current_dpd", "writeoff_amt", "repay_status"],
  "feature_allowlist": []
}
```

### 2.3 准入阶段的缺失画像（可读路径）

`inspect_and_bind_snapshot` 的 `summary` 里，缺失按**空值或字面缺失码**统计（`N/A`、`-`、`未知` 一类），与无标签描述工具用的是同一份清单——两份清单各自维护一次，就会在同一个列上报出 0% 和 87% 两个答案。下面这些路径用于**在对话里读摘要**（`column_missing` 为空数组时 `.0` 不存在）：

| 路径 | 含义 |
| --- | --- |
| `column_missing.0.column` | 缺失率最高的那一列 |
| `column_missing.0.missing_share` | 该列缺失率 |
| `column_missing.0.n_missing` | 缺失条数 |
| `column_missing_summary.columns_with_missing` | 有缺失的列数 |
| `column_missing_summary.returned` / `.truncated` | 实际返回几列 / 是否因超过 50 列被截断 |
| `high_missing_columns` | 缺失率 $> 95\%$ 的列名 |
| `constant_columns` | 单一值占**非缺失** $> 99\%$ 的列名 |
| `key_null_rate` | 主键缺失率（同一口径：字面缺失码也算缺失） |

**这些路径不能直接写进 `verify_and_commit_report` 的 `citation.path`**：准入工具只落快照、不落 run（它的 `run_id` 是 `null`），而引用核验读的是某个 run 的 `summary`。要在报告里引用某列缺失率，先用 `describe_population` 描述这一列（口径里声明好字段语义），再引它的 `column.missing_share`——那个数字与这里的 `missing_share` 是同一套口径算出来的，可以互相印证，但只有 run 里的数字能被核验。

**「不在名单上」只有在 `truncated` 为 `false` 时才等于「0 缺失」**：宽表截断后，第 51 名以后的缺失列是看不见的，`columns_with_missing` 才是真实总数。

这里**只报比率，不报「原本写的是 `N/A` 还是本来就空」**。CSV 一进 `pd.read_csv`，`N/A` / `NA` / `NULL` / `#NA` / `<NA>` 就已经被解析器转成空值，Parquet 才会原样保留；两条路到不了同一个拆分点，把它写进摘要只会出现「同一列，换个文件格式缺失原因不一样」。要这个区别去问取数侧，不要从摘要反推。

不过**缺失率本身必须两条路一致**，所以工具词表刻意对齐了 pandas 的默认 NA 集合（`#NA`、`<NA>`、`#N/A N/A`、`-nan` 这些 Excel 导出写法都算缺失）。词表之外的写法（`*`、`.`、`N.A`、`N.A.`）两种格式都当取值——`N.A` / `N.A.` 刻意不收，它们都可能是 North America 的缩写，一个尾点不足以消歧，两种写法必须同进同出。

`constant_columns` 的分母是**非缺失**条数：一列 40% 空白、其余全是一个值，仍然算常量——空白不是一个取值，把它算进分母会让常量列漏判。`key_null_rate` 同理走这套归一。`key_duplicate_rate` **只在主键齐全的行上算**：没有身份的行谈不上「重复了另一笔业务」，三行都写 `N/A` 只会被 `key_null_rate` 报一次，不再额外算成重复主键——否则一份「主键没填」的文件会被诊断成「主键重复」，方向正好相反。分母仍是全表行数，读法是「这份文件里有多大比例在重复一条已有的主键」。主键列从 CSV 读入时**固定按字符串**读（`pd.read_csv` 默认推断会把 `001` 读成整数 `1`，于是 `001` 与 `1` 被误判成同一笔；Parquet 本就保留文本），所以重复率与文件格式无关，前导零是身份的一部分，不是可推断的数量。**没有一行主键完整时**（每一行都缺至少一个主键分量，含复合主键交叉缺失）按 `SNAPSHOT_INVALID` 拦下，三种粒度一致——没有身份的行既不能算「重复」，也不该按粒度统计。

口径里用到的高缺失列，到描述阶段会先被拦成 `needs_human`（`human_gate.kind` 视列形态是 `binary_column_role` 或 `field_semantics`），工具不会带着未定义的分母出数。

---

## 3. 已签收口径与维度登记表 YAML 规范

在 WorkBuddy 知识库与 MCP 服务中，口径与维度通过结构化 YAML 管理。MCP 的 `registry_name` 指向一个同时包含 `metrics` 与 `dimensions` 的登记文件；下面两段应合并到同一文件中。版本号、刀口和签收人只说明文件形状。评估单里的口径版本、阈值和样本量只引用工具摘要，不引用本节示例。

```yaml
# signed_metrics.yaml 示例
metrics:
  - metric_id: "mob3_dpd30_bal"
    version: "v2.1"
    name: "账龄3个月30+逾期率（余额口径）"
    numerator: "sum(principal) where mob == 3 and dpd >= 30"
    denominator: "sum(principal) where mob == 3"
    maturity_mob: 3
    weight_column: "principal"
    flag_column: "dpd30_flag"
    signed_by: "risk_committee"

# dimension_registry.yaml 示例
dimensions:
  - column: "channel"
    label: "获客渠道"
    point_in_time: "decision"
    kind: "categorical"
    max_cardinality: 30
    min_cell_n: 50
    min_expected_bad: 5
  - column: "approval_score_bin"
    label: "审批评分分档"
    point_in_time: "decision"
    kind: "pre_binned"
    cut_points: [0, 520, 560, 600, 640, 1000]
    min_cell_n: 50
    min_expected_bad: 5
```

---

## 4. 两套“成熟”分开引用

| 结果 | 判定 | 能决定的事 |
| --- | --- | --- |
| 表现期成熟 | 整个批次满足放款批次月末 + $W$ 个完整账龄月 ≤ 数据截止日 | 能否进入坏账率分母、开发集和时间外样本 |
| Vintage 覆盖率 | 某个 MOB 上已观察到的借据占比达到展示门槛（计算库默认 0.9） | Vintage 曲线画到哪一格 |

没有表现期成熟样本时，工具返回 `status=needs_human`、`block_code=LABEL_IMMATURE` 和可选处理方式；账本准入状态仍为阻断。可选处理方式固定为三项：改选更短且已签收的表现期、只做无标签描述分析、等待批次成熟，由审批卡 `gate=maturity_choice` 落账，未落账不自动切换指标。不能用覆盖率把未成熟批次算进分母。

---

## 5. 本规程要处理的阻断码

| block_code | 含义 |
| --- | --- |
| `SNAPSHOT_INVALID` | 主键重复、缺观察日或已声明的列不在快照中 |
| `METRIC_UNSIGNED` | `metric_id` 与 `metric_version` 不在已签收文件中，或版本字符串不一致 |
| `LABEL_IMMATURE` | 目标表现期下成熟行数为 0 |
| `RESOURCE_UNAVAILABLE` | 登记文件、快照或依赖资源不可读取 |
| `VALIDATION_UNAVAILABLE` | 样本/列校验无法完成（成熟样本不足、类别单一等） |
| `REPORT_UNTRACED` | 评估单中的数字没有落到本次摘要字段 |

`status=blocked` 或 `LABEL_IMMATURE` 待选择时，评估单只保留缺口，不给出可被下游引用的坏账率。工具异常（资源缺失、校验失败等）统一返回结构化 envelope，携带 `status`、`block_code`、摘要与缺口说明，不抛裸异常、不静默降级为成功。
