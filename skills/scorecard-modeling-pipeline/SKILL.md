---
name: scorecard-modeling-pipeline
description: 标准信用评分卡与风控机器学习模型开发，优先进行成熟标签与时间外验证；无法形成 OOT 时由用户选择较弱验证方式并明确标注局限。
license: MIT
allowed-tools:
  - inspect_and_bind_snapshot
  - check_label_maturity
  - prepare_modeling_sample
  - screen_features
  - profile_binning
  - train_and_evaluate_candidate
  - verify_and_commit_report
metadata:
  version: "1.6.0"
  contract_version: "0.5"
---

# 标准评分卡与风控建模验证规程 (`scorecard-modeling-pipeline`)

本规程依据《自动化信用管理的理论与实践》中的信用评分卡工程方法论制定。适用于申请评分卡（A-Card）、行为评分卡（B-Card）、催收评分卡（C-Card）及对比机器学习候选模型的开发、验证与审阅交付。

详细评分卡数学公式（WOE、IV、PSI、PDO 刻度转换）及各阶段评审阈值表见 [references/scorecard-math-and-thresholds.md](references/scorecard-math-and-thresholds.md)。

---

## 何时使用 / 何时不使用

**五规程优先级**：任何监控、归因、策略或建模任务必须先经 `credit-data-admission` 完成数据准入体检；本规程只在已取得成熟标签样本后启动，无成熟标签时不走任何回退方式。

**何时使用（触发条件）**：需要开发申请评分卡（A 卡）、行为评分卡（B 卡）、催收评分卡（C 卡）或对比机器学习候选模型，执行成熟标签的时间外验证、特征防泄漏筛选、人工特征确认与模型卡交付时使用。

**何时不使用（不得引用本规程结论的情形）**：
- 只有 latest_observed 最新可观察样本、或目标表现期没有成熟批次时，不得据本规程训练模型或给出目标表现期坏账率结论；**latest_observed 最新可观察样本不得用于建模或目标坏账率结论**。
- 未形成 OOT 且用户未选择较弱验证方式、或特征确认单未落账时，不得开始训练；同一 `screen_run_id` 的确认单不得重放训练。
- 产物 `signoff_status="pending"`、模型卡仍为 `candidate`、且 `model_signoff` 未签收时，不得写成「已批准/建议上线」。

---

## 一、核心原则（不可违背的红线）

1. **成熟标签必需，OOT 优先**：
   - 训练与验证样本必须先通过 `credit-data-admission` 的成熟度检查。
   - 标准路径用前段时点组成开发集，内部交叉验证；后段时点是**时间外样本（OOT）**。切分按不同观察时点，不按行数随机抽取。若成熟样本无法形成 OOT，先说明原因并等待用户选择较弱验证方式，绝不自动退回随机切分。无成熟标签时不能走这些回退方式。
2. **先基线（WOE + LR）、后复杂模型（LightGBM）**：
   - 必须先建立经过分箱 WOE 变换的可解释逻辑回归基线模型（Scorecard Baseline），再训练树模型等候选模型进行对照。不接受没有可解释基线的黑盒单一输出。
3. **特征入模必须经过“防泄漏 + 稳定性 + 人工确认”三道闸门**：
   - $\text{IV} > 0.50$ 的超高区分度特征默认标记为**疑似未来信息泄漏（Suspect Leakage）**，必须逐一核查其在评分时点 $T_0$ 的可得性。
   - 跨期 $\text{PSI} > 0.25$ 的显著漂移特征默认剔除。
   - **人工审核硬节点**：`screen_features` 返回 `needs_human` 与 `review_id` 后必须停下。审批卡把确认写入账本之后，才能训练。对话中的同意和实验规格里的预锁清单都不能代替这笔确认。
4. **综合审视而非唯 KS/AUC 论**：
   - 模型评价必须同时报告：**区分度（KS / AUC）**、**跨期稳定性（Score PSI 与开发集到 OOT 的 KS 衰减率）**、**排序单调性（十分位分箱坏账率是否反转）**以及**概率校准度**。若 OOT 分箱坏账率出现显著非单调反转，不得标记为“建议准出”。

---

## 二、工具返回与停机

建模数字只来自 `prepare_modeling_sample`、`screen_features`、`train_and_evaluate_candidate` 的摘要。分箱明细和评分卡点值在产物文件中。allowed-tools 只是工具声明，实际门禁由工具层返回的 envelope 与审批账本保证。

| status | 动作 |
| --- | --- |
| `ok` | 继续下一步 |
| `degraded` | 按摘要中的限制写入模型卡的局限一节。`oot_validation_complete=false` 时不得称为跨期有效；若 `candidate_comparison_complete=false`，也不得声称完成双路径对比或建议准出 |
| `needs_human` | 若是验证方式选择，展示选项并等待用户确认；若是特征清单审核，展示清单后停止，不调用训练 |
| `blocked` | 按下表停机 |

任何工具异常统一返回 `blocked` + `block_code`（`RESOURCE_UNAVAILABLE` / `UNSUPPORTED_SCOPE` 等）的结构化 envelope，不再抛裸异常。

| block_code | 停机时怎么写 |
| --- | --- |
| `ADMISSION_REQUIRED` | 先执行 `credit-data-admission` |
| `LABEL_IMMATURE` | 说明没有成熟样本，不开始训练 |
| `RANDOM_SPLIT_FORBIDDEN` | 先核对时间列与可用成熟批次；仍无法形成 OOT 时让用户从工具给出的方式中选择 |
| `VALIDATION_UNAVAILABLE` | 小样本、类别单一、无特征或选定方式仍缺足够样本时停止，说明补数要求 |
| `BASELINE_UNAVAILABLE` | 基线模型没有训练成功，停止候选对比并检查样本及特征 |
| `TIME_POINT_VIOLATION` | 显式把观察时点列放进 `features` 时停机，将其列入 `exclude_columns` 后只重新做筛选，不直接进入训练 |
| `FEATURE_REVIEW_REQUIRED` | 说明入模确认单缺失或已被消费，继续等待审批卡 |
| `REPORT_UNTRACED` | 删掉或改正文中对不上的数字，再重新核验 |
| `RESOURCE_UNAVAILABLE` | 说明数据、依赖或运行环境暂不可用，写出缺口后停止，不臆造数值重试 |
| `UNSUPPORTED_SCOPE` | 无可用特征、请求超出建模范围或参数非法时，写明当前做不到，不退回随机切分 |
| `VALIDATION_CHOICE_REQUIRED` | 说明无法形成 OOT，展示较弱的验证方式选项并等待用户选择 |
| `USER_CHOSE_STOP` | 说明用户选择停止，保留已观察样本描述后结束，不开始训练 |
| `SIGNOFF_REQUIRED` | 说明 `model_signoff` 尚未签收，模型卡不得写「已批准/建议上线」，停在 `candidate` 并等待审批卡 |

`prepare_modeling_sample` 摘要新增嵌套 `split: {method, cutoff, time_column}`（保留 `split_method` 旧字段），`split.method` 是合法 citation path、标准路径仍必须 `oot`；新增 `random_state` 保证可复现。摘要中的开发集行数、OOT 行数、两侧坏账率和时间切点原样引用。

`screen_features` 新增 `min_iv`、`max_feature_psi`、`max_features`，以及分箱参数 `woe_bins`、`binning_method`、`monotonic_direction`、`min_bin_fraction`、`max_prebins`（摘要落 `binning_policy`）；`profile_binning` 输出单特征分箱档与 `merge_log`；摘要顶层 `suspect_leakage: list[str]` 合并 IV > 0.50 与时点不明/疑似穿越列，`selected: list[str]` 与 `selected_details: list[dict]` 与落账、返回摘要结构一致。`IV > 0.50` 或时点不明的特征以摘要里的 `suspect_leakage` 为准，逐条交给人工，不在清单里自行删掉标记。训练只接受账本中与本次筛选 `run_id` 对应的 `feature_list` 确认单（一次性消费，同一 `screen_run_id` 不能重放训练），且确认的特征必须是该次筛选结果的子集。

产物含 `signoff_status="pending"`、`signoff_gate="model_signoff"`、`signoff_required=true`，模型卡初始状态仍 `candidate`。`record_human_decision`（`gate=model_signoff`）只由审批卡调用，审批卡 `review_id` 必须等于对应工具 `run_id`，由受信任宿主 `scripts/host_approval.py` 落账，Agent 不调用。报告提交输出 `model_signoff` 签收状态；没有这笔确认时模型卡不写“已批准”或“建议上线”，否则返回 `SIGNOFF_REQUIRED`。

定稿调用 `verify_and_commit_report`，每个 KS、AUC、PSI、样本量和坏账率都提交 `citations`，并输出 `model_signoff` 签收状态。阈值与 PDO 公式见 [references/scorecard-math-and-thresholds.md](references/scorecard-math-and-thresholds.md)。

---

## 三、标准作业步骤（6 步建模法）

### 步骤 1：实验意图定义与规格锁定
与用户确认并生成结构化实验规格（对应 `ModelingJobSpec`）：
1. **业务场景与评分时点**：明确是贷前申请（A卡，时点为提交申请时）、贷中行为（B卡，时点为账单日/观察日）还是贷后催收（C卡，时点为入催日）。
2. **标签与表现期绑定**：明确好坏标签列 `label`、坏样本取值 `positive_values=(1,)`、灰样本策略 `gray_policy="exclude"`、时间列 `time_column` 及主键列 `id_columns`。
3. **明确排除列 `exclude_columns`**：将所有贷后表现字段、审批结果字段、衍生标签字段列入黑名单。

### 步骤 2：样本准备与跨时间切分（开发集 / OOT）
调用 `prepare_modeling_sample`。成熟掩码必需；标准路径尽量提供真实 `time_column`，并传入 `random_state` 保证可复现。摘要新增嵌套 `split: {method, cutoff, time_column}`（保留 `split_method` 旧字段）：`split.method` 是合法 citation path，标准路径仍必须 `oot`，`split.cutoff` 与 `split.time_column` 原样引用。如果成熟批次太少、仅有一个时点或无法形成 OOT，工具返回 `needs_human` 与 `VALIDATION_CHOICE_REQUIRED`；`stop` 对应 `USER_CHOSE_STOP`。向用户说明样本、目标标签、时间覆盖与以下选项，**等用户明确选择后**由受信任审批卡记录 `method`，并用原样本参数加 `fallback_review_id` 重试：

- `random_holdout`：按实体隔离随机留出（无实体列时按标签分层）；可以报告独立留出表现，但不能报告跨期稳定性或 OOT。
- `cv_only`：所有成熟样本只做开发集交叉验证；没有独立留出集，AUC/KS 仅为开发集 OOF，不给出 OOT、测试集 PSI 或上线准出建议。
- `stop`：等待更多已成熟批次或更完整的时间字段。

确认单只适用于本次快照、标签定义和样本参数；参数改变须重新申请。`record_human_decision`（`gate=validation_fallback`）由审批卡调用，Agent 不自行写确认。训练产物保留 `validation_mode`、`fallback_review_id` 和 `oot_validation_complete=false`。较弱验证仍须通过特征人工审查。
1. 核查剔除灰样本与未成熟样本后的有效样本总量、正负样本数及各月事件率（Bad Rate）曲线。
2. 切分按不同观察时点的个数：
   - 前段时点用于开发训练与交叉验证（Development / OOF CV）；
   - 后段时点固定为**时间外样本（OOT）**。默认后段约占 distinct 时点的 30%，对应 `test_size=0.3`。
3. 记录开发集与 OOT 的基准坏账率。返回的切分方式不是 `oot` 时停止，不采用该次样本。

### 步骤 3：粗分类分箱、WOE/IV 计算与跨期 PSI 筛选
调用 `screen_features` 对候选特征池执行分层过滤（对应 `FeatureSelectPolicy`）；显式把观察时点列放进 `features` 时返回 `TIME_POINT_VIOLATION`。筛选参数 `min_iv`、`max_feature_psi`、`max_features` 与摘要顶层 `suspect_leakage: list[str]`（合并 IV > 0.50 与时点不明/疑似穿越）、`selected: list[str]`、`selected_details: list[dict]` 落账与返回结构一致：
1. **基础质量过滤**：剔除缺失率 $> 95\%$ 或单一值占比 $> 99\%$ 的特征。
2. **分箱与单调性约束（Binning & WOE）**：
   - 对连续特征执行分箱（等频 `quantile`、ChiMerge `chi2` 或单调分箱 `monotonic`），确保每箱样本占比 $\ge 5\%$（缺失值独立成箱）。
   - ChiMerge 在工具内实现：等频预分箱 → 小样本箱并入卡方更近的邻箱 → 相邻两箱按 2×2 列联表卡方逐对合并到 `woe_bins`，最小箱门槛的分母是**该特征有值样本数**，合并过程写入 `merge_log`。
   - 计算每箱的证据权重（WOE）并核查坏账率随分箱变化的单调趋势（严格单调递增、递减或具有明确业务逻辑的 U 型）。
   - 需要单独复核某个特征的逐箱明细（样本、坏率、WOE、IV 贡献、KS、PSI、合并过程）时调用 `profile_binning`，不要另写脚本二次分箱——审查与训练必须是同一套箱边界。
3. **预测力（IV）与稳定性（PSI）联合过滤**：
   - 按 `min_iv` 剔除低于阈值的无预测力特征（默认 $\text{IV} < 0.02$）；对 $\text{IV} > 0.50$ 的特征触发**数据泄漏二级警告**并并入 `suspect_leakage`。
   - 计算开发集与 OOT 之间的特征分布稳定性指数（Feature PSI），按 `max_feature_psi` 剔除 $\text{PSI} > 0.25$ 的高漂移特征。
4. **相关性冗余聚类剔除**：
   - 对两两皮尔逊相关系数 $|r| > 0.85$ 的特征簇，仅保留 IV 更高且业务含义更直接的特征，按 `max_features` 控制入模特征总数不超过上限（默认 $\le 40$，标准评分卡建议精选 $10 \sim 20$ 个）。

### 步骤 4：人工特征审查确认
向用户输出《候选特征审查清单》，包含每个入选与被剔除特征的：**字段名、业务含义、缺失率、IV 值、跨期 PSI、WOE 单调方向、剔除/保留原因及疑似泄漏标记**。这些值来自本次筛选摘要。
- 存在 `suspect_leakage` 时，清单逐条列出，工作流停在 `needs_human`。
- 用户看完清单之后，等待审批卡写入 `feature_list` 确认单。确认单**一次性消费**：同一 `screen_run_id` 不能重放训练，审批卡 `review_id` 必须等于对应工具 `run_id`；确认单未落账时不进入步骤 5。

### 步骤 5：基线与候选模型训练、PDO 刻度转换与多维验证
调用 `train_and_evaluate_candidate` 执行训练与评估；小样本、类别单一、无特征或基线不可用等一律返回结构化 `VALIDATION_UNAVAILABLE` / `BASELINE_UNAVAILABLE` / `UNSUPPORTED_SCOPE`，不抛裸异常：
1. **双路径对照训练**：
   - **路径 A（标准评分卡基线 `logistic_regression`）**：基于 WOE 编码训练逻辑回归，不设类别权重（`class_weight=None`）以保持预测概率与真实坏账率一致；核查回归系数符号（WOE 正向编码下各特征回归系数须同号，防止多重共线性反转）。
   - **路径 B（树模型候选 `lightgbm`）**：在同一套样本切分与特征集上训练梯度提升树作为性能参照。
2. **标准评分卡刻度转换（PDO Scaling）**：
   - 按基准分 $\text{BaseScore}=600$、基准好坏比 $\text{BaseOdds}=1/20$、好坏比翻倍分差 $\text{PDO}=50$ 将预测概率映射为标准整数信用分，并导出各特征分箱加分表（`scorecard.json`）。
3. **开发集与 OOT 全维验证**：
   - 对比开发集交叉验证与 OOT 上的 **KS、AUC**，核算跨期衰减幅度；
   - 生成 OOT 十分位（10-bin）评分表，检查从低分箱到高分箱的实际坏账率是否严格单调递减（检查 `monotonic_bad_rate` 标记与头尾 Lift 倍数）；
   - 计算模型总分在开发集与 OOT 之间的 **Score PSI**。

### 步骤 6：生成可复现模型卡（Model Card）与数字核验
调用 `verify_and_commit_report` 校验所有报告数字并归档模型卡：
- 模型卡必须完整包含：**数据快照 ID 与时间切分边界、好坏灰定义、最终入模特征表（含 IV/PSI/系数/分箱得分）、基线与候选模型在开发集交叉验证与 OOT 上的对比表、十分位坏账率与 Lift 表、已知局限与适用边界**。
- 调用 `verify_and_commit_report` 时，上述数字逐项提交 `citations`。
- 产物含 `signoff_status="pending"`、`signoff_gate="model_signoff"`、`signoff_required=true`，模型初始状态固定登记为 `candidate`；报告提交输出 `model_signoff` 签收状态，未签收却写「已批准/建议上线」返回 `SIGNOFF_REQUIRED`，账本中没有 `model_signoff` 确认单时模型卡不标记为生产已批准。
