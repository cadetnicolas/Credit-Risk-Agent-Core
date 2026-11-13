# 评分阈值（Cutoff）决策矩阵、边际利润与置换集（Swap Set）方法论

本文档参考《自动化信用管理的理论与实践》中的决策策略优化章节，并对齐 `credit_agent.analytics`（`optimize.py`, `rules.py`, `profit.py`, `funnel.py`）的实现规范。

---

## 1. 评分 Cutoff 决策表与边际坏账率公式

将成熟样本按信用分 $S$ 从高到低划分为 $K$ 个有序区间 $I_1, I_2, \dots, I_K$（$I_1$ 为最高分段，$I_K$ 为最低分段）。记第 $k$ 档的总样本数为 $n_k$，坏样本数为 $b_k$，全量样本数 $N = \sum_{k=1}^K n_k$，全量坏样本数 $B = \sum_{k=1}^K b_k$。

当设定第 $c$ 档下界为审批通过线（即保留 $1 \dots c$ 档，拒绝 $c+1 \dots K$ 档）时：

1. **当档边际坏账率（Marginal Bad Rate）**：
   $$r_{\text{marg}}(c) = \frac{b_c}{n_c}$$
2. **累计通过率（Cumulative Approval Rate）**：
   $$A(c) = \frac{\sum_{k=1}^{c} n_k}{N}$$
3. **累计通过客群坏账率（Cumulative Approved Bad Rate）**：
   $$R_{\text{app}}(c) = \frac{\sum_{k=1}^{c} b_k}{\sum_{k=1}^{c} n_k}$$
4. **累计坏账捕获率 / 拒绝坏样本召回率（Bad Capture Rate）**：
   $$\text{Recall}_{\text{bad}}(c) = \frac{\sum_{k=c+1}^{K} b_k}{B}$$

---

## 2. 盈亏平衡坏账率（Break-even Bad Rate）与最优 Cutoff 推导

在给定单笔贷款经济模型参数时（参见 `credit_agent.analytics.profit`）：
- $G_{\text{good}}$：单笔正常履约贷款的净收益（利息与手续费净现值扣除资金成本与运营成本）；
- $L_{\text{bad}}$：单笔违约贷款的净损失（$\text{EAD} \times \text{LGD}$，即违约敞口乘以违约损失率）。

当某评分档 $c$ 的期望边际利润为零时：
$$(1 - r_{\text{marg}}(c)) \cdot G_{\text{good}} - r_{\text{marg}}(c) \cdot L_{\text{bad}} = 0$$

解得**理论盈亏平衡边际坏账率**：
$$r_{\text{break-even}} = \frac{G_{\text{good}}}{G_{\text{good}} + L_{\text{bad}}}$$

**决策准则**：
- 凡是 $r_{\text{marg}}(k) < r_{\text{break-even}}$ 的评分分档，其边际期望净贡献为正，理论上应予通过；
- 凡是 $r_{\text{marg}}(k) > r_{\text{break-even}}$ 的评分分档，其边际期望净贡献为负，应予拦截。

### 2.1 模式 C：逐档盈亏平衡与建议 Cutoff 区间

模式 C 传入 `economics` 参数（按 `analytics/profit.py` 口径），对每一评分档逐档核算：
- `break_even_bad_rate`：按该档 `G_good` 与 `L_bad` 由上式解出的盈亏平衡边际坏账率；
- `marginal_net_gain`：该档在现行通过线附近的边际净增益，仅由已放款成熟样本推算；
- `suggested_cutoff_range`：由 `marginal_net_gain` 符号变化给出的建议 Cutoff 区间。

Cutoff 摘要据此给出**激进 / 平衡 / 温和**三档候选。**严禁把放宽审批后新增通过客群的收益算成收益**：该客群不在已放款样本内、真实坏账率未知，不得估算其净收益或据此上调收益结论。

---

## 3. 候选规则挖掘（Rule Mining）评估指标与防过拟合约束

对单条候选拦截规则 $R$（例如 `channel == 'X'`，**每条规则只含一个条件**）：
- **命中率（Hit Rate）**：$H(R) = n_R / N$，绝对命中样本量门槛 `min_hits=50`（$n_R \ge 50$）；
- **命中客群坏账率**：$r(R) = b_R / n_R$；
- **风险提升度（Lift）**：$\text{Lift}(R) = r(R) / R_{\text{base}}$（通常要求 $\text{Lift} \ge 1.5 \sim 2.0$）；
- **跨期一致性**：在开发期与时间外样本（OOT）上必须同时满足 $\text{Lift}_{\text{dev}} \ge \theta$ 且 $\text{Lift}_{\text{oot}} \ge \theta_{\text{oot}}$，且两边方向一致。搜索只在开发期进行。
- **条件个数**：一个条件是一次比较，或一个取值相等。并且、或者、取反、多条件规则，以及同时使用两个阈值，工具返回 `UNSUPPORTED_SCOPE`；工具**只搜索单条件规则**，该候选不写入试验单，也不拆成多条单条件后再合并成一条已验证规则。
- **候选维度约束**：`features` 必填，且必须是已签收登记表中 `point_in_time=decision` 的维度子集；缺失或含未登记维度返回 `DIMENSION_UNREGISTERED`，含观察/表现时点维度返回 `TIME_POINT_VIOLATION`。
- **搜索量记账**：`trials` 记录本次**全部被评估的候选规则数**（含被 `min_hits`、Lift、跨期一致性淘汰的候选），用于防过拟合，不记为通过数。

---

## 4. 拒绝推断边界与冠军挑战者（Champion-Challenger）验证

由于历史存量放款数据存在固有的**样本选择偏差（Sample Selection Bias）**：
1. **置换集（Swap Set）按完整 2×2 核算**：`keep_approved`（现行通过、候选仍通过）、`swap_out`（现行通过、候选改拒绝）、`swap_in`（现行拒绝、候选改通过）、`keep_rejected`（现行拒绝、候选仍拒绝）。**`swap_in_bad_rate` 固定为空、写「未知」**——现行拒绝客群不在已放款样本内，没有表现期结果。严禁估算放宽审批后新增通过客群的真实坏账率或净收益；该收益属于 `UNSUPPORTED_SCOPE`，试验单写明无法估算，不给期望收益数字。
2. 新策略或新 Cutoff 上线前，标准交付物为**冠军挑战者（Champion-Challenger）分流方案**，即《冠军挑战者试验设计单》：
   - Cutoff 摘要含**激进 / 平衡 / 温和**三档候选（见第 2.1 节）；
   - **冠军组（Champion, 90%~95% 流量）**：执行现行生产策略；
   - **挑战者组（Challenger, 5%~10% 随机同质流量）**：执行新候选策略；
   - **观察周期**：写明覆盖目标表现期的观察时长；
   - **熔断回滚（Circuit Breaker）**：写明短周期先行指标（如 `FPD7`、`FPD15` 或首月催收流入率）、阈值与切回冠军策略的回滚条件；流量切换由决策引擎执行，本规程只把条件写进待审批试验单；
   - 试验单 `status="pending_approval"`，未签收前不得上线、不得写成已批准。

### 4.1 Swap Set 2×2 象限与可观测性

| 象限 | 含义 | 是否在已放款样本内 | 坏账率 |
| --- | --- | --- | --- |
| `keep_approved` | 现行通过、候选仍通过 | 是 | 可由成熟样本核算 |
| `swap_out` | 现行通过、候选改拒绝 | 是 | 可由成熟样本核算 |
| `swap_in` | 现行拒绝、候选改通过 | 否 | 固定为空（未知） |
| `keep_rejected` | 现行拒绝、候选仍拒绝 | 否 | 固定为空（未知） |

### 4.2 最新可观察样本（observation_mode="latest_observed"）路径

`observation_mode="latest_observed"` 生成含未成熟批次的最新可观察掩码，策略分析**只做描述性测算**：坏账率、`Lift`、正式推荐一律为空，返回 `degraded`；不构成目标表现期坏账率，不得用于建模或上线收益测算；三档候选 Cutoff 与试验设计单在此模式下不产出。
