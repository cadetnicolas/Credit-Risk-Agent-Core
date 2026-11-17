---
name: policy-cutoff-simulation
description: 信贷审批策略规则挖掘、评分阈值（Cutoff）决策表模拟、置换集（Swap Set）分析、边际坏账率与利润权衡及样本选择偏差控制标准作业程序（SOP）。只搜索单条件规则；调入集坏账率保持未知，不估算放宽审批后的新增收益。
license: MIT
allowed-tools:
  - inspect_and_bind_snapshot
  - check_label_maturity
  - resolve_metric_and_dimensions
  - simulate_policy_rules
  - verify_and_commit_report
metadata:
  version: "1.6.0"
  contract_version: "0.5"
---

# 信贷策略规则挖掘与阈值调优规程 (`policy-cutoff-simulation`)

本规程基于《自动化信用管理的理论与实践》中的信贷决策管理、评分阈值（Cutoff）设定、置换集（Swap Set）分析及拒绝推断（Reject Inference）边界理论制定。用于**基于存量风险发现或新评分卡输出，搜索高风险拦截规则、模拟评分阈值调整对规模与风险的权衡，并生成待人工审批的策略试验方案**。

详细 Cutoff 决策矩阵、Swap Set 四宫格核算、边际坏账率公式及过拟合防范准则见 [references/cutoff-and-swapset-methodology.md](references/cutoff-and-swapset-methodology.md)。

---

## 何时使用 / 何时不使用

**五规程优先级**：任何监控、归因、策略或建模任务必须先经 `credit-data-admission` 完成数据准入体检；本规程只在已锁定成熟样本快照 `dataset_snapshot_id` 与目标坏账指标后启动。

**何时使用（触发条件）**：需要基于存量风险发现或新评分卡输出，搜索单条件高风险拦截规则、模拟评分阈值（Cutoff）调整对规模与风险的权衡、构造置换集（Swap Set），或生成待人工审批的离线策略试验方案时使用。

**何时不使用（不得引用本规程结论的情形）**：
- 只有 latest_observed 最新可观察样本时，不得据本规程得出目标表现期坏账率或上线收益结论；**latest_observed 最新可观察样本不得用于建模或目标坏账率结论**，该模式只做描述性测算，所有坏账率、Lift 与正式推荐为空。
- 现行拒绝客群不在已放款样本内，严禁估算放宽审批后新增通过客群的真实坏账率与净收益；`swap_in_bad_rate` 固定为空，正文写「未知」。
- 试验单 `status="pending_approval"`、`policy_trial` 未签收时，不得写成已批准或已上线；不得用历史回测结果承诺未来上线后的真实财务实现。

---

## 一、核心原则（不可违背的红线）

1. **已放款样本的选择偏差红线（Reject Inference Boundary）**：
   - 存量放款数据表仅包含当时“审批通过并放款（Through-the-Door Approved）”的客群，**不包含被拒客群（Rejects）的真实还款表现**。
   - **收紧策略（拦截存量高危子群 / 提高评分 Cutoff）**：可以在历史已放款样本上准确计算拦截多少笔数、减少多少已发生坏账（历史描述性缩减效果）。
   - **放宽策略（降低 Cutoff / 取消原有拦截规则）**：由于历史上被拒客户没有还款表现，**严禁在缺少可信拒绝推断或随机试验（Champion-Challenger）样本的情况下，估算“放宽后新增通过客群的真实坏账率与净收益”**。
2. **规则挖掘防过拟合原则**：
   - 候选规则只在已登记的**决策时点字段**上搜索，并且每条规则只有一个条件（一次比较，或一个取值相等）。
   - 标准路径必须报告**总搜索候选数（Trials）**，满足最小命中样本量门槛，并在**独立时间外窗口（OOT）**验证规则的 Lift 与坏账率稳定性。搜索只用开发期样本。若不能形成 OOT，先询问用户选择较弱的研究方式；该方式输出 `candidates`，不得写成已验证的 `recommended`。
3. **边际坏账率（Marginal Bad Rate）优先于累计平均坏账率**：
   - 设定评分 Cutoff 时，不能只看整体通过客群的平均坏账率（Cumulative Approved Bad Rate），必须核算 Cutoff 切点附近的**边际坏账率（Marginal Bad Rate）**是否低于盈亏平衡坏账率（Break-even Bad Rate）。
4. **人工审批与小流量试验前置**：
   - 本规程的一切产出均为**离线策略研究候选**，必须经过风险策略负责人审批并通过线上冠军挑战者（Champion-Challenger）试验验证。产出停在待确认的试验单，不修改生产决策引擎。

---

## 二、工具返回与停机

策略数字只来自 `simulate_policy_rules` 的摘要。`swap_in_bad_rate` 固定为空，正文把调入集坏账率写成“未知”。allowed-tools 只是工具声明，实际门禁由工具层返回的 envelope 与审批账本保证。

| status | 动作 |
| --- | --- |
| `ok` | 按摘要撰写候选方案，并保留工具返回的 `caveats` |
| `degraded` | 只报告已放款样本的研究候选与已观察缩减效果；无 OOT 时不形成正式规则推荐；latest_observed 模式下所有坏账率、Lift 与正式推荐为空 |
| `needs_human` | 若是验证方式选择，展示选项并等待确认；若是试验签收，输出建议书后停止 |
| `blocked` | 按下表停机 |

任何工具异常统一返回 `blocked` + `block_code`（`RESOURCE_UNAVAILABLE` / `UNSUPPORTED_SCOPE` 等）的结构化 envelope，不再抛裸异常。

| block_code | 停机时怎么写 |
| --- | --- |
| `ADMISSION_REQUIRED` | 先执行 `credit-data-admission` |
| `LABEL_IMMATURE` | 说明目标表现期没有成熟样本 |
| `TIME_POINT_VIOLATION` | 说明该列是观察时点字段或时点不明，不能用于规则或阈值 |
| `DIMENSION_UNREGISTERED` | 说明 `features` 不在已签收登记表 `point_in_time=decision` 的维度子集内，不临时加列 |
| `UNSUPPORTED_SCOPE` | 非法 mode、缺 score/edges、多条件规则串或要求估算放宽审批后的新增收益时，写明当前做不到，不改写成另一道已验证规则 |
| `VALIDATION_UNAVAILABLE` | 坏样本为 0、缺 score 分档或样本不足以评估时，停止并说明补数要求，不输出候选规则 |
| `REPORT_UNTRACED` | 删掉或改正文中对不上的数字，再重新核验 |
| `RESOURCE_UNAVAILABLE` | 说明数据、维表或运行环境暂不可用，写出缺口后停止，不臆造数值重试 |
| `VALIDATION_CHOICE_REQUIRED` | 说明无法形成 OOT，展示较弱的验证方式选项并等待用户选择 |
| `USER_CHOSE_STOP` | 说明用户选择停止，输出已观察描述性结果后结束，不继续规则推荐 |
| `SIGNOFF_REQUIRED` | 说明 `policy_trial` 尚未签收，报告不得写「已批准/已上线」，停在待审批并等待审批卡 |

规则模式优先传入真实 `time_column`。`simulate_policy_rules` 新增 `economics`（模式 C）、`observation_mode`（`mature_only` / `latest_observed`）与 `latest_review_id`。无法形成 OOT 时，工具返回 `needs_human` 与 `VALIDATION_CHOICE_REQUIRED`：用户可选择 `random_holdout`（随机留出，只能做非跨期复核）、`descriptive_only`（当前成熟样本探索，无独立验证）或 `stop`（对应 `USER_CHOSE_STOP`，输出描述性结果后结束）。受信任审批卡记录 `gate=validation_fallback` 与所选 `method` 后，以 `fallback_review_id` 重试。前两种结果均为 `degraded`，仅在 `candidates` 中展示，`recommended` 为空，不提供上线试验推荐。latest_observed 模式只做描述性测算、所有坏账率/Lift/正式推荐为空、返回 `degraded`，不构成目标表现期坏账率、不可用于建模或上线收益测算。开发期 Lift 与时间外 Lift 方向相反的候选不进入标准推荐表。

`record_human_decision`（`gate=policy_trial`）只由审批卡调用，Agent 不自行写确认。报告提交输出 `policy_trial` 签收状态；试验单 `trial_design` 的 `status="pending_approval"`，在确认落账前保持待审批，未签收却写「已批准/已上线」返回 `SIGNOFF_REQUIRED`。

定稿调用 `verify_and_commit_report`。命中率、Lift、通过率、坏账率、样本量和 bp 都提交 `citations`；同时输出 `signoff_status`（`policy_trial`）。公式见 [references/cutoff-and-swapset-methodology.md](references/cutoff-and-swapset-methodology.md)。

---

## 三、标准作业步骤（5 步策略调优法）

### 步骤 1：明确调优目标、约束条件与样本范围
1. 确认已通过 `credit-data-admission` 锁定成熟样本快照 `dataset_snapshot_id` 与目标坏账指标。工具返回 `ADMISSION_REQUIRED` 时停止。
2. 与用户确认本次策略模拟的目标与硬约束：
   - **模式 A（风险压降型）**：在存量通过规模缩减不超过 $\Delta A_{\max}$（如 $\le 10\%$）的前提下，最大化降低组合坏账率；
   - **模式 B（规模维持置换型 Swap Set）**：引入新模型或新规则，在保持总体通过率基本不变的前提下，核算调出客户（Swap-out）的已观察坏账；调入客户（Swap-in）只计笔数，坏账率记为未知；
   - **模式 C（盈亏平衡型）**：给定单笔正常收益（APR / 息费净收入）与单笔违约损失率（LGD），寻找边际利润为零的最优 Cutoff 点。

### 步骤 2：候选拦截规则挖掘与跨期复核（Rule Mining）
当针对特定风险异动挖掘硬规则时，调用 `simulate_policy_rules`（模式：`rule_mining`，每条规则一个条件）：
1. **候选特征白名单**：`features` 必填，且必须是已签收登记表 `point_in_time=decision` 的维度子集；不在子集内返回 `DIMENSION_UNREGISTERED` 或 `TIME_POINT_VIOLATION`，**不再全表扫描**。只搜索单条件规则（一次比较或一个取值相等），多条件规则串返回 `UNSUPPORTED_SCOPE`。
2. **候选评估**：在授权特征上逐条计算：
   - **命中率（Hit Rate / Reject Rate）**：命中样本占总体样本的比例；
   - **命中坏账率（Hit Bad Rate）**与**提升度（Lift = 命中坏账率 / 总体基准坏账率）**；
   - **规则剔除后剩余客群坏账率（Post-rule Bad Rate）**及坏账压降幅度（bp）；
   - **增量贡献（Incremental Lift）与重叠度（Overlap）**：剔除与既有规则高度重叠的冗余规则；
   - `trials` 记录全部被评估候选数，命中样本须满足 `min_hits=50`，不足者不进入候选表。
3. **跨期稳定性复核**：将开发期筛选出的 Top 候选规则应用于独立时间窗（OOT），剔除在 OOT 上 Lift 大幅衰减或方向反转的过拟合规则。

### 步骤 3：评分阈值决策表（Cutoff Matrix）与边际分析
当基于信用评分或风险概率设定准入线时，调用 `simulate_policy_rules`（模式：`cutoff`）生成标准 Cutoff 决策表；缺少 score 分档或 edges 时返回结构化停机，不臆造切点：
1. 按评分分档（如每 10 分或等频 20 档）从高分到低分逐档输出：
   - **当档样本量、当档占比、当档实际坏账率（即边际坏账率 Marginal Bad Rate）**；
   - **若以该档下限为 Cutoff 时的累计通过率（Approval Rate）、累计通过坏账率（Approved Bad Rate）、累计拦截坏账占比（Bad Capture Rate / Recall）**。
2. 模式 C 传入 `economics`，核算 `break_even_bad_rate`、逐档 `marginal_net_gain` 与 `suggested_cutoff_range`，标注边际坏账率等于盈亏平衡坏账率（$r_{\text{break-even}}$）的建议切点区间。
3. cutoff 摘要给出激进 / 平衡 / 温和三档 `candidate_plans` 与 `trial_design`（挑战者流量 5%~10%、观察周期、熔断回滚、`status="pending_approval"`）；latest_observed 模式下这些正式推荐为空。

### 步骤 4：新旧策略置换集分析（Swap Set Analysis）
当对比“调整后新策略（或新模型 Cutoff）”与“现行基准策略”时，构造完整 $2 \times 2$ 置换矩阵（含 `keep_rejected` 双拒绝格）：

| 现行策略 \ 新策略 | 新策略：通过 (Approve) | 新策略：拒绝 (Reject) |
| --- | --- | --- |
| **现行：通过 (Approve)** | **双通过 (Keep Approved)**：核算样本量与实测坏账率 | **调出集 (Swap-out)**：核算拦截笔数与实测高坏账率（已避免的历史损失） |
| **现行：拒绝 (Reject)** | **调入集 (Swap-in)**：仅统计申请笔数；坏账率写“未知”，与摘要中的空值一致 | **双拒绝 (Keep Rejected)**：统计申请笔数 |

- 当底层数据仅为已放款借据表时，明确指出**现行拒绝客群不在已放款样本内**，矩阵下半行只统计申请笔数；`swap_in_bad_rate` 固定为空、正文写「未知」，本次分析仅覆盖上半行（即对既有通过客群的二次收紧/筛选）。**严禁估算放宽审批后新增通过客群的真实坏账率与净收益。**

### 步骤 5：数字溯源核验与生成《策略试验建议书》
调用 `verify_and_commit_report`，为所有比率、bp 与样本量提交 `citations` 并输出 `signoff_status`（`policy_trial`）后，输出结构化报告：
1. **推荐候选方案对比表**：提供激进（侧重压降风险）、平衡（默认推荐）、温和（侧重保规模）三档 `candidate_plans` 候选阈值或规则组合；latest_observed 模式下该表为空。
2. **强制风险与偏差声明**：显式声明历史回测仅代表存量样本的描述性重演，不等于未来上线后的真实财务实现；`swap_in_bad_rate` 为空、调入集坏账率写「未知」，严禁估算放宽审批后的新增收益。
3. **下一步人工动作**：生成 `status="pending_approval"` 的《线上小流量试验（Champion-Challenger）设计单》即 `trial_design`，列明建议挑战者流量比例（5%~10%）、观察周期与熔断回滚指标；`policy_trial` 未签收时不写「已批准/已上线」，否则返回 `SIGNOFF_REQUIRED`。
