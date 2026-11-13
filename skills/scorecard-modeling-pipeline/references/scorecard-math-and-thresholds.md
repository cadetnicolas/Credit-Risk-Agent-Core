# 信用评分卡核心数学公式与行业评审阈值规范

本文档参考《自动化信用管理的理论与实践》并对齐 `credit_agent.modeling`（`binning.py`, `woe.py`, `iv.py`, `feature_psi.py`, `scoring.py`, `metrics.py`）的实现规范。

---

## 1. 证据权重（WOE）与信息值（IV）

设特征 $X$ 被划分为 $m$ 个分箱（含独立缺失箱），第 $i$ 箱的好样本数为 $G_i$，坏样本数为 $B_i$；总体好样本数为 $G = \sum_{i=1}^m G_i$，总体坏样本数为 $B = \sum_{i=1}^m B_i$。记 $g_i = G_i / G$，$b_i = B_i / B$（计算时采用 Laplace 平滑防止零除）。

$$\text{WOE}_i = \ln\left(\frac{b_i}{g_i}\right)$$

$$\text{IV} = \sum_{i=1}^{m} (b_i - g_i) \times \text{WOE}_i$$

### IV 预测能力分级与防泄漏判据
| IV 取值区间 | 预测能力评价 | 标准处理动作 |
| --- | --- | --- |
| $< 0.02$ | 无有效预测力 | **自动剔除** |
| $[0.02, 0.10)$ | 弱预测力 | 视业务可解释性备选保留 |
| $[0.10, 0.30)$ | 中等预测力 | **核心入模候选** |
| $[0.30, 0.50]$ | 强预测力 | **核心入模候选**，核对分箱单调性 |
| $> 0.50$ | 异常超高（极度可疑） | **触发数据泄漏核查（Suspect Leakage）**，未经人工确认时点合规性严禁入模 |

---

## 2. 群体稳定性指数（PSI）

设预期分布（Expected / 开发集）在第 $i$ 箱的样本占比为 $E_i$，实际分布（Actual / OOT 或监控期）在第 $i$ 箱的样本占比为 $A_i$：

$$\text{PSI} = \sum_{i=1}^{m} (A_i - E_i) \times \ln\left(\frac{A_i}{E_i}\right)$$

### PSI 稳定性分级标准（适用于单特征 Feature PSI 与模型总分 Score PSI）
| PSI 取值区间 | 稳定性状态 | 标准处理动作 |
| --- | --- | --- |
| $< 0.10$ | 分布稳定 | 正常通过 |
| $[0.10, 0.25]$ | 轻微偏移 | 发出关注提示，检查客群渠道变化 |
| $> 0.25$ | 显著漂移（不稳定） | 特征筛选阶段**默认剔除**；模型监控阶段**触发重校准或重训练预警** |

---

## 3. 逻辑回归评分卡与 PDO 标准刻度转换（Scaling）

设客户违约概率为 $p = P(\text{Bad}=1 | \mathbf{x})$，坏好比（Odds of Bad）为 $\text{Odds} = \frac{p}{1-p}$。
逻辑回归模型满足：
$$\ln(\text{Odds}) = \beta_0 + \sum_{j=1}^{p} \beta_j \cdot \text{WOE}_j(x_j)$$

标准评分卡将对数坏好比线性映射为信用分 $\text{Score}$（坏账率越低，分数越高）：
$$\text{Score} = A - B \cdot \ln(\text{Odds})$$

其中补偿常数 $A$（Offset）与刻度因子 $B$（Factor）由三个业务锚点唯一确定：
- $\text{BaseScore}$（基准分，默认 $600$ 分）；
- $\text{BaseOdds}$（基准坏好比 $\theta_0$，默认 $1/20 = 0.05$）；
- $\text{PDO}$（Points to Double the Odds，坏好比翻倍时减少的分数，默认 $50$ 分）。

求解得：
$$B = \frac{\text{PDO}}{\ln 2} \approx 72.1348, \quad A = \text{BaseScore} + B \cdot \ln(\text{BaseOdds})$$

**基础分与单特征分箱加分拆解**：
$$\text{BasePoints} = A - B \cdot \beta_0, \quad \text{Points}_{j, i} = -B \cdot \beta_j \cdot \text{WOE}_{j, i}$$

---

## 4. 模型验证核心指标与准出参考门槛

1. **区分度（Discrimination）**：
   - $\text{KS} = \max_{s} |F_{\text{Bad}}(s) - F_{\text{Good}}(s)|$。信贷评分卡常见有效区间为 $0.20 \sim 0.60$；若 $\text{OOT KS} < 0.15$ 视为区分度不足；若 $\text{OOT KS} > 0.70$ 须复查是否存在标签穿越。
   - **跨期衰减率**：$\frac{\text{Train KS} - \text{OOT KS}}{\text{Train KS}}$ 建议控制在 $\le 15\% \sim 20\%$ 以内。
2. **排序单调性（Rank Ordering）**：
   - 将 OOT 样本按评分等频分为 10 箱（Deciles），检查各箱实际坏账率是否随分数升高严格单调递减。当 `OOT KS >= 0.05` 但十分位坏账率出现反转时，系统标记 `monotonic_bad_rate = False`，要求人工复核分箱粒度或过拟合情况。

---

## 5. 样本切分与确认单

### 5.1 样本切分（prepare_modeling_sample）

样本只有两段，切分单位是不同观察时点的个数：

- 前段时点是开发集。区分度里的开发集数字来自该段上的 OOF 交叉验证。
- 后段时点是时间外样本。默认约占 distinct 时点的 30%（`test_size=0.3`）。
- 没有 `time_column` 时工具返回 `RANDOM_SPLIT_FORBIDDEN`。

`prepare_modeling_sample` 摘要含嵌套 `split: {method, cutoff, time_column}`：
- `split.method` 是合法 citation path，**标准路径必须为 `oot`**；
- `split.cutoff` 为切分时点，`split.time_column` 为切分依据列；
- 顶层保留 `split_method` 兼容字段，取值与 `split.method` 一致。

### 5.2 特征筛选（screen_features）

`screen_features` 暴露 `min_iv`、`max_feature_psi`、`max_features` 三个阈值参数，以及 `woe_bins`、`binning_method`、`monotonic_direction`、`min_bin_fraction`、`max_prebins` 五个分箱参数；覆盖值写入摘要 `binning_policy`。返回摘要结构：
- 顶层 `suspect_leakage: list[str]`：**合并两类**——`IV > 0.50` 的特征，以及时点不明/疑似穿越的特征；
- `selected: list[str]` 与 `selected_details: list[dict]`：入选特征及其明细（含逐箱 `bin_profile`）；
- 落账（审批卡）与返回摘要的结构保持一致。

`screen_features` 返回 `needs_human` 与 `review_id`。训练只接受审批卡写入的 `feature_list` 确认单，且确认特征必须是该次 `selected` 的子集；观察时点字段列入模返回 `TIME_POINT_VIOLATION`。**特征确认单一次性消费、不可重放**，未经该确认单不得入模。

#### 5.2.1 ChiMerge 分箱内核（工具内实现）

`binning_method="chi2"` 走工具内置的 ChiMerge（Kerber 1992），不是等频分箱的近似：

1. **预分箱**：按 `max_prebins`（默认 20）等频切成细区间。
2. **最小箱合并**：样本量不足 `min_bin_fraction` 的箱，先并入卡方值更小的邻箱。门槛分母是**该特征有值样本数**（`min_bin_fraction_scope="valid_only"`），缺失独立成箱、不参与箱体大小约束。把分母写成全量会让高缺失特征的有值箱被系统性判为「过小」而过度合并。
3. **卡方合并**：用相邻两箱的标准 2×2 列联表卡方（好、坏两列都计入）逐对合并，直到箱数不超过 `woe_bins`。两箱合计只有单一类别时卡方记为 0（分布相同，优先合并），不记为无穷大。
4. **留痕**：每次合并写入 `merge_log`（阶段、左右区间、卡方值、合并后箱数），分箱过程可回放、可审阅。

WOE 一律用 $\text{WOE}_i=\ln\dfrac{\text{Bad}_i/\text{Bad}_{total}}{\text{Good}_i/\text{Good}_{total}}$，分母含缺失箱；计数加 0.5 做拉普拉斯平滑。**不要用 $10^{-6}$ 这类极端平滑**：某个箱坏样本为 0 时 WOE 会被推到 ±11 量级，IV 与分箱得分同时虚高，评分卡出现 ±100 分的极端点值。

`profile_binning` 把单特征分箱档独立成工具：给一份已准备的样本定义和一组特征，返回逐箱样本/坏数/坏率/WOE/IV 贡献/占比、分箱 KS、PSI、单调性、`bin_edges` 与 `merge_log`。它和 `screen_features`、`train_and_evaluate_candidate` 共用同一套分箱内核，**审查时看到的箱边界就是训练时用的箱边界**。

### 5.3 训练失败与结构化停机

训练常见失败映射为结构化停机，不返回半成品模型：

| 失败情形 | block_code |
| --- | --- |
| 小样本、类别单一、校验无法完成 | `VALIDATION_UNAVAILABLE` |
| 基线模型不可用 | `BASELINE_UNAVAILABLE` |
| 无可用特征 / 不支持的建模范围 | `UNSUPPORTED_SCOPE` |

### 5.4 模型卡与报告签收

模型卡初始状态为 `candidate`，并含 `signoff_status="pending"`、`signoff_gate="model_signoff"`、`signoff_required=true`；直到审批卡写入 `model_signoff` 才变更。`verify_and_commit_report` 提交时输出签收状态；未签收却在正文写“已批准”“建议上线”等既成结论时返回 `SIGNOFF_REQUIRED`。审批卡的 `review_id` 等于对应工具的 `run_id`。

### 5.5 报告引证核对

`verify_and_commit_report` 的 `citations` 支持纯整数与日期/文本型 citation（文本按**去空格后精确比对**），比率仍按产物 **6 位小数**核对。KS、AUC、PSI 的报告值只引用训练摘要。**正文出现未引用的纯整数会被 `REPORT_UNTRACED` 拦截**。
