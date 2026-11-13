> **Status：被取代（历史说明，已归档）** — 本文描述的是历史规划（当时建模能力尚未接入工具链），现行交付以 `README.md` 与 `docs/workbuddy-skill-mcp-integration-guide.md` 为准；本文仅作历史决策追溯，不作为实现依据。

# 建模分析工具（`credit_agent.modeling`）

本目录的正式建模能力在 `src/credit_agent/modeling/`。它还没有接成 harness 的 `ToolDefinition`。  
同级的 `auto_model_code/` 只作历史参考，不进入 Agent 执行链。

## 结构

| 模块 | 职责 |
|------|------|
| `metrics.py`, `iv.py`, `preprocess.py` | KS/AUC/PSI、IV、缺失与同值率过滤 |
| `config.py`, `schema.py` | 列绑定与策略；多标签候选时要求显式 `label` |
| `workflows/` | 特征筛选、训练（LR + 可选 LightGBM） |
| `artifacts.py` | 标准 JSON 产物 |
| `eda.py` | 样本列级画像 |
| `analyzer.py` | `ModelingAnalyzer` 统一入口 |

## DAG 用法

```json
{
  "plugin": "modeling_analysis",
  "params": {
    "step": "feature_select",
    "objective": "IV 筛选入模特征",
    "modeling": {
      "columns": {
        "label": "mob3_dpd30",
        "exclude_columns": ["loan_no", "apply_date"]
      },
      "feature_select": { "min_iv": 0.02, "max_features": 40 }
    }
  }
}
```

步骤与产物：

| `step` | 产物 |
|--------|------|
| `sample_profile` | `sample_profile.json` |
| `feature_select` | `feature_select.json` |
| `model_train` | `model_train.json` |
| `model_eval` | `model_eval.json` |
| `woe_profile` | `woe_profile.json`（分箱 WOE/IV 明细） |
| `scorecard` | `scorecard.json`（LR + WOE → PDO 点数表） |
| `model_package` | `model_package.json`（`model_bundle.joblib` + `score.py` + 可选 `scorecard.json`） |

策略示例：

```json
"feature_select": {
  "max_feature_psi": 0.25,
  "numeric_only": false,
  "woe_bins": 10,
  "numeric_binning": "chi2",
  "monotonic_direction": "increasing",
  "min_bin_fraction": 0.05
},
"train": { "n_folds": 5, "use_woe_transform": true },
"score": { "enabled": true, "base_score": 600, "pdo": 50, "base_odds": 0.05 },
"export": { "export_model": true, "fold_inference": "average" }
```

`score.py` 中 `predict()` 在 `score.enabled` 时额外返回 `credit_score`（Prob2Score / PDO 映射）。

建模课题应以 **`model_package` + `review: true`** 作为训练评审收口；通过后 validator/report 再自动跑。

前置条件：课题 `context.dataset_ref` 已有快照（`data_input` 或 API 上传）。

## 标签口径（多值标签必须人工确认）

真实建模样本的标签列经常是多值编码，例如 `ylabel ∈ {-99, 0, 1, 2}`。每个取值必须指定一个角色：

| 角色 | 字段 | 含义 | 处理 |
|------|------|------|------|
| 坏样本 | `positive_values` | 目标事件（如 `2`=黑） | y=1 |
| 好样本 | `negative_values` | 正常（如 `0`） | y=0 |
| 灰样本 | `gray_values` + `gray_policy` | 表现不确定（如 `1`） | `exclude` 剔除，或按策略并入好/坏 |
| 无效样本 | `exclude_label_values` | 无表现 / 数据问题（如 `-99`） | 剔除 |

- 只要有取值没分配角色，训练就会被拒绝（`unassigned_values`），不会把未知取值默认当成好样本。
- `label_setup`（`label_probe.json`）在口径未确认时返回 `needs_label_definition`，评审面板会为每个取值提供角色下拉框。人工点「通过」后，口径写入 `context.modeling_label_spec`，**该步骤会按确认后的口径重跑**，产出完整的 `sample_profile.json`（坏样本数、坏占比、建模样本 / 原始行数），validator 和报告都读这一份。
- 人工确认的口径优先级最高：规划器或 LLM 推断出的 `label_column` 不能覆盖它。
- 报告的「建模样本量」指剔除无效 / 灰样本后的行数，「原始快照」是上传行数，两者分开展示。

## 时间切分（OOT）

- 配置 `columns.time_column`（如 `apply_month`）后，最晚一段时间的样本作为 **OOT 测试集**，其余为开发集。报告写作「开发 / OOT 测试」。
- 未配置时退化为随机留出，报告写作「开发 / 留出测试」。这种情况下**跳过特征 PSI**：随机切分衡量不了时间漂移。
- 分箱、WOE、编码、缺失填充只在开发集上拟合，测试集只做 transform，避免泄漏。
- `model_package` 在开发样本上全量重训单个模型，用于上线；评估指标仍以 OOT 测试集为准。

## 评分方向（PDO）

约定与行业一致：**分数越高，风险越低**。

```text
factor = pdo / ln 2
offset = base_score + factor · ln(base_odds)
score  = offset − factor · ln(p / (1 − p))
```

- `base_odds` 是在 `base_score` 处的**坏:好**比（默认 `1/20`）。分数每增加 `pdo` 分，坏:好比减半。
- 评分卡点数：`points_i = −factor · coef_i · woe_i`，基础分 `offset − factor · intercept`。`score.py` 与 `scorecard.json` 用同一套公式，测试里有对账用例。
- `class_weight` 默认 `None`。如果设为 `balanced`，概率会被抬高，PDO 分数整体偏移，需要重新校准后再上线。

## 单调性判定

`model_eval.json` 的 `monotonic_deciles` 按风险十分位坏账率判断：只有相邻分位的倒挂**显著**（差值 > 1.96·SE）时才判为不单调，噪声级倒挂会列进 `decile_inversions`，不阻断。`strict_monotonic` 保留严格口径供参考。

## 库调用

```python
from credit_agent.modeling import ModelingAnalyzer, ModelingJobSpec
from credit_agent.data.ingest import load_dataset_frame

spec = ModelingJobSpec.from_mapping({"columns": {"label": "ylabel"}})
df = load_dataset_frame(dataset_ref)
out = ModelingAnalyzer(spec).run(df, "feature_select.json")
```

## 与 `code_sandbox` 的分工

- **modeling_analysis**：结构化建模流水线，可复现、可校验、可进报告。
- **code_sandbox**：探索性 SQL/Python、非标准分析脚本；不承担默认建模路径。
