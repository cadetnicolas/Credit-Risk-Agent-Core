> **Status：已作废（历史稿，已归档）** — 本文描述的是历史规划（`credit-agent init` 与所引测试从未实现），现行交付以 `README.md` 与 `docs/workbuddy-skill-mcp-integration-guide.md` 为准；本文仅作历史决策追溯，不作为实现依据。

# Mock 信贷数仓说明

`credit-agent init` 会生成一份结构化、可离线运行的合成信贷资产池（`~/.credit-agent/data/credit.duckdb`），覆盖设计文档中全部课题类型所需的取数场景。

---

## 1. 规模与生成

| 项 | 值 |
| --- | --- |
| 客户数 | 40,000 |
| 申请笔数 | 150,000 |
| 月份跨度 | 18 个月（截至 `2024-12`） |
| 随机种子 | `20240501`（固定 → 结果可复现） |
| 后端 | DuckDB（`CREATE OR REPLACE`，重复 init 不会破坏已有文件） |

生成器全向量化（`numpy`），构建耗时秒级。

---

## 2. 表结构

### `dim_customer` — 客户主表（40k）
`cust_id`, `product`, `channel`, `region`, `segment`, `age`, `gender`, `education`, `occupation`, `marriage`

### `fct_application` — 申请事实表（150k）
`apply_id`, `cust_id`, `apply_month`, `product`, `channel`, `region`, `segment`, `age`,
`apply_amount`, `approved`, `credit_score`, `dti`, `query_cnt_3m`, `overdue_cnt_hist`,
`income`, `credit_utilisation`, `tenure_months`

### `fct_loan` — 放款事实表
`loan_id`, `apply_id`, `cust_id`, `product`, `channel`, `region`, `loan_month`,
`principal`, `term_months`, `annual_rate`

### `fct_repayment` — 还款表现表
`loan_id`, `cust_id`, `product`, `channel`, `region`, `segment`, `loan_month`, `mob`,
`principal`, `max_dpd`, `bad_flag`, `pd_score`

### `dim_feature_snapshot` — 月度特征快照（PSI 漂移分析用）
`snapshot_month`, `product`, `sample_cnt`, `credit_score_mean`, `credit_score_std`,
`dti_mean`, `income_mean`, `query_cnt_3m_mean`, ...

### 视图
- `vw_overdue_by_month`：`loan_month × product × channel × region × segment` 的 `loan_cnt / principal_amt / bad_cnt / bad_principal`
- `vw_approval_by_month`：`apply_month × product × channel × region` 的 `apply_cnt / approved_cnt / avg_credit_score / avg_dti`

---

## 3. 维度取值

| 维度 | 取值 |
| --- | --- |
| `product` | 惠农贷（34%）、消费贷（42%）、经营贷（24%） |
| `channel` | 自营App / 渠道A / 渠道B / 渠道C / 线下网点 |
| `region` | 华东 / 华南 / 华中 / 华北 / 西南 / 西北 |
| `segment` | 新客 / 存量复借 / 存量增额 / 流失召回 |

---

## 4. 注入的异动信号（课题的"标准答案"）

最后 3 个月对**惠农贷 × 华中**注入风险状态迁移：

```
惠农贷 × 华中           × 2.8
惠农贷 × 华中 × 渠道C   × 1.7（叠加）
```

同时 `dim_feature_snapshot` 中惠农贷的 `credit_score_mean` 在窗口内下移 22 分、`dti_mean` 上移 0.05，供 PSI 稳定性类课题检出漂移。

**关键设计**：审批环节使用的是**异动前的 odds**（`odds_pre_anomaly`）。真实评分卡不会在风险状态迁移开始的当月就反应过来——正因为如此，这批劣化客群才会被放款、进而在后续月份表现为逾期。如果审批直接用异动后的 odds，坏客群会被当场拒绝，永远进不了表现表，课题就查不出任何东西。

端到端跑通后应能检出（实测值）：

| 切片 | 逾期率 |
| --- | --- |
| 惠农贷整体 | 2.9% → 4.4% |
| 惠农贷 × 华中 | 8.81%（其余 3.09%，z ≈ 6.16） |
| 惠农贷 × 华中 × 渠道C | 12.2% |

`tests/test_e2e.py::test_report_identifies_the_injected_anomaly` 会把这条信号作为回归门禁。

---

## 5. 口径定义

知识库（`credit-agent init` 播种的三篇文档）定义了统一口径，分析链路必须与之一致：

- **逾期率（放款口径）**：放款后 6 个月内出现 DPD30+ 的笔数 / 同期放款笔数
- **通过率**：`approved=1` 笔数 / 同期申请笔数
- **坏样本**：放款后 6 个账单期内出现 DPD30+ 记 `bad=1`
- **PSI**：<0.10 稳定；0.10–0.25 需关注；≥0.25 显著漂移
- **模型门槛**：KS ≥ 0.25、AUC ≥ 0.65 方可进入上线评审

---

## 6. 换成真实数仓

`data/source.py` 定义了只读数据源抽象：

```python
class DataSource(ABC):
    def query(self, sql: str) -> pd.DataFrame: ...
    def assert_read_only(self, sql: str) -> None: ...   # 白名单：SELECT/WITH/DESCRIBE/SHOW/EXPLAIN/PRAGMA
```

- `DuckDBSource`：本地实现
- `ODPSAdapter`：MaxCompute 适配器占位（填 `CREDIT_AGENT_ODPS_*` 即可对接）

写语句（`INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/ATTACH/COPY/EXPORT`）在 `assert_read_only` 处直接被拒，`CREDIT_AGENT_MAX_SCAN_ROWS` 控制单次扫描上限。切换到真实数仓不需要改动任何插件代码。
