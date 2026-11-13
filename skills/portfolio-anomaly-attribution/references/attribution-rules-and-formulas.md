# 存量资产归因分解公式、显著性检验与定位判据

本文档为 `portfolio-anomaly-attribution` 的数学与工程参考规范，对应 `credit_agent.analytics` 下的 `decompose.py`、`drilldown.py`、`vintage.py` 与 `migration.py`。

---

## 1. Shift-Share 结构效应与风险效应分解公式

设基期（0）与对比期（1）在某决策时点维度 $S$ 上共有 $K$ 个互斥分群 $s \in \{1, \dots, K\}$。
记分群 $s$ 在第 $t \in \{0, 1\}$ 期的分母权重（借据数或放款金额）为 $n_{s,t}$，总分母为 $N_t = \sum_s n_{s,t}$，份额占比为 $w_{s,t} = n_{s,t} / N_t$，分群内逾期率为 $r_{s,t}$。

组合总逾期率变化 $\Delta R = R_1 - R_0$ 精确分解为三项之和（参见 `credit_agent.analytics.decompose.shift_share`）：

$$\Delta R = \underbrace{\sum_{s=1}^{K} (w_{s,1} - w_{s,0}) \cdot r_{s,0}}_{\text{结构效应 (Mix Effect)}} + \underbrace{\sum_{s=1}^{K} w_{s,0} \cdot (r_{s,1} - r_{s,0})}_{\text{组内风险效应 (Rate Effect)}} + \underbrace{\sum_{s=1}^{K} (w_{s,1} - w_{s,0}) \cdot (r_{s,1} - r_{s,0})}_{\text{交互效应 (Interaction)}}$$

- **闭合校验（Closure Check）**：`closure_error = |total_change - (mix_effect + rate_effect + interaction)|` 必须严格小于 $10^{-9}$（即 `1e-9`）。当 `closure_error ≥ 1e-9` 时，工具返回 `degraded`，在摘要中写明残差数值并收窄结论，不得给出完整归因结论。
- **视图状态区分**：视图未配置时返回 `ok` 并附 `notes` 说明"未配置、未参与本项"；视图已声明却缺列或两期不可比时返回 `degraded`，不得以 `ok` 静默跳过。
- **缺失分群借用规则**：若某分群仅在一期出现（例如本期新增渠道，$w_{s,0}=0$），则借用另一期的比率以保持分解闭合，并在报告中显式标记为“新增/退出分群”。

---

## 2. 辛普森悖论（Simpson's Paradox）自动判据

当出现以下任一情形时，判定该维度发生**结构反转（Simpson's Reversal）**：
1. 组合总变化 $\Delta R > 0$（整体恶化），但所有有效分群的组内变化 $r_{s,1} - r_{s,0} \le 0$（各分群内部均改善或持平），且结构效应 $\sum (w_{s,1} - w_{s,0}) r_{s,0} > 0$；
2. 组合总变化 $\Delta R < 0$（整体改善），但所有有效分群的组内变化 $r_{s,1} - r_{s,0} \ge 0$。

**强制动作**：一旦触发辛普森反转，系统禁止将该维度下的任何子客群标记为“风险恶化客群”，报告必须定性为**高风险分群权重上升导致的组合结构效应**。

---

## 3. 单维/二维下钻与定位恶化客群的四项充要条件

对维度 $S$ 的每个切片 $s$，计算：
- 基期样本 $n_{s,0}$、本期样本 $n_{s,1}$；
- 基期比率 $r_{s,0}$、本期比率 $r_{s,1}$、比率差 $\Delta r_s = r_{s,1} - r_{s,0}$；
- 本期权重贡献度 $C_s = w_{s,1} \cdot \Delta r_s / \Delta R$；
- 两独立样本比例检验 $Z$ 统计量与双侧 $p$ 值（`credit_agent.analytics.stats`）。

切片 $s$ 被认定为**“定位到的恶化客群”**当且仅当同时满足：
1. **样本充足性**：基期与本期都满足 $n \ge N_{\min}$（取该维度登记的 `min_cell_n` 或工具参数 `min_n`，未配置时默认 50），且每一期同时满足期望坏样本 $n \cdot r \ge \text{min_events}$、期望好样本 $n \cdot (1-r) \ge \text{min_events}$（默认 5）。
2. **统计显著性与方向一致**：$\text{sign}(\Delta r_s) == \text{sign}(\Delta R)$ 且 $p < \alpha$（默认 0.05，可由工具参数覆盖）。
3. **实质贡献度**：$C_s \ge \text{contribution\_floor}$（默认 0.20），或者 $s = \arg\max_{j} C_j$ 且新增坏账笔数 $\Delta k_s = k_{s,1} - k_{s,0} \ge \text{min_bad_increase}$。组合总变化为 0 时，没有任何切片满足本条。
4. **无结构反转**：该维度未触发第 2 节中的辛普森悖论。

工具摘要里的 `located` 已按这四条筛完，`observe_only` 存放样本不足的切片，且**名单不截断**（不因展示条数上限丢弃切片）。报告只转述这两份名单和 `stop_rule`，不另算一份定位结果。`allowed_conclusions` 给出的枚举之外，不增加结论类型。目标账龄下无成熟批次时返回 `LABEL_IMMATURE`，不产出归因结果。

### 3.1 登记表参数逐维生效与工具覆盖

- 登记表登记的 `max_cardinality`、`min_cell_n`、`min_expected_bad`、`bins`、`cut_points` **逐维生效**，不再使用全局硬编码阈值；未登记该键的维度沿用工具默认值。
- 工具参数可覆盖 `min_n`、`min_events`、`contribution_floor`、`alpha`、`min_bad_increase`（分别对应样本充足性、期望事件数、贡献度门槛、显著性水平、新增坏账绝对笔数门槛），覆盖值写入本次摘要以便复核；未覆盖时取登记值或默认值。
- 连续变量使用**事先登记的固定分档刀口**（`bins` / `cut_points`），不在运行期按样本重新分箱，保证基期与对比期切片定义一致、可比。

---

## 4. 逾期迁徙矩阵（Roll Rate）与净坏账流量核算

在《自动化信用管理的理论与实践》中，迁徙率（Roll Rate）衡量账龄每推进一期，账户从上一逾期状态 $i \in \{C, M1, M2, M3, \dots\}$ 向下一状态 $j$ 滚动的比率：

$$\text{RollRate}(i \to j) = \frac{\text{期末处于状态 } j \text{ 且期初处于状态 } i \text{ 的余额（或笔数）}}{\text{期初处于状态 } i \text{ 的总余额（或笔数）}}$$

- **前端风险指示器**：$\text{RollRate}(C \to M1)$ 反映新入催压力，与进件质量及还款提醒直接相关。
- **中后端催收效率指示器**：$\text{RollRate}(M1 \to M2)$、$\text{RollRate}(M2 \to M3)$ 反映早中催回收率（Cure Rate $= 1 - \text{RollRate}$）。

---

## 5. 调用参数与阻断码

`run_portfolio_monitoring` 的 `base_periods` 与 `comp_periods` 是**必填且非空**的期次列表；两次运行要比较同一窗口时，传入同一对列表。期次缺失、为空或与登记窗口不匹配时返回 `PERIOD_MISMATCH`，**绝不静默回退默认窗口**。

| block_code | 含义 |
| --- | --- |
| `ADMISSION_REQUIRED` | 该 `snapshot_id` 没有准入记录 |
| `LABEL_IMMATURE` | 目标账龄没有成熟批次 |
| `PERIOD_MISMATCH` | `base_periods` / `comp_periods` 缺失、为空或与登记窗口不匹配 |
| `METRIC_UNSIGNED` | 口径版本未签收 |
| `TIME_POINT_VIOLATION` | 观察时点字段被当作原因维度 |
| `DIMENSION_UNREGISTERED` | 维度不在登记表内 |
| `SIGNOFF_REQUIRED` | 报告提交时处置结论未签收 |
| `REPORT_UNTRACED` | 正文数字没有落到本次摘要字段 |

处置研究在审批卡写入 `anomaly_disposition` 之前保持待确认。报告提交统一输出 `signoff_status`；未签收却在正文写"已立项""已批准"等既成结论时返回 `SIGNOFF_REQUIRED`，结论只能表述为待确认。
