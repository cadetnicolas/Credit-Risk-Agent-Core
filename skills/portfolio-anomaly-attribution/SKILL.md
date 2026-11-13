---
name: portfolio-anomaly-attribution
description: 存量信贷资产监控、同账龄 Vintage 分析、逾期迁徙率（Roll Rate）追踪、Shift-Share 结构与组内风险效应分解及恶化客群定位标准作业程序（SOP）。用于定期资产巡检与逾期率异动归因。基期与对比期必须明确传入；结论类型以工具返回的枚举为准。
license: MIT
allowed-tools:
  - inspect_and_bind_snapshot
  - resolve_metric_and_dimensions
  - run_portfolio_monitoring
  - decompose_and_locate_anomaly
  - verify_and_commit_report
metadata:
  version: "1.5.0"
  contract_version: "0.4"
---

# 存量资产监控与风险异动归因规程 (`portfolio-anomaly-attribution`)

本规程基于《自动化信用管理的理论与实践》中的资产组合管理、账龄（Vintage）与滚动率（Roll Rate）分析体系制定。用于**逐期盯住已放款存量组合的逾期与迁徙异动，拆解“客群结构变化”与“客群内部风险恶化”，定位满足统计显著性的恶化切片，并输出可复核的调查报告**。

详细数学分解公式、恶化客群定位四条件与辛普森反转判据见 [references/attribution-rules-and-formulas.md](references/attribution-rules-and-formulas.md)。

---

## 何时使用 / 何时不使用

**四规程优先级**：任何存量监控、异常归因、策略或建模任务必须先经 `credit-data-admission` 完成数据准入体检；本规程只在已取得合法 `dataset_snapshot_id` 与 `metric_version` 后启动。

**何时使用（触发条件）**：需要逐期盯住已放款存量组合的逾期与迁徙异动、做同账龄 Vintage 对比、Shift-Share 结构与组内风险效应分解，或定位满足统计显著性的恶化切片并输出可复核调查报告时使用。基期 `base_periods` 与对比期 `comp_periods` 必须明确传入且非空。

**何时不使用（不得引用本规程结论的情形）**：
- 快照未过准入、目标账龄没有成熟批次，或只有 latest_observed 最新可观察样本时，不得据本规程做目标表现期坏账率归因；**latest_observed 最新可观察样本不得用于建模或目标坏账率结论**。
- 期次与数据不匹配（工具返回 `PERIOD_MISMATCH`）时，不得静默换期或拼接不同口径版本的结果；`observe_only` 名单不得截断后当作已定位根因。
- 处置研究未签收（`signoff_status` 未确认）时，不得写成「已立项/已批准」；无策略版本字段时不得出现「策略失效」结论。

---

## 一、核心原则（不可违背的红线）

1. **同账龄（MOB）对齐红线**：
   - 比较不同放款批次（Cohort）的风险水平时，**必须在相同账龄（Months on Book, MOB）下对比**。分母只能包含已完整走完该 MOB 的成熟借据，严禁拿新批次的低 MOB 逾期率与老批次的高 MOB 逾期率直接作差。
2. **结构效应与组内风险效应必须分离**：
   - 组合整体逾期率上升时，必须先做 Shift-Share 分解（拆为**客群结构效应 Mix Effect**、**组内风险效应 Rate Effect** 和**交互项 Interaction**），严禁在未做结构分解前直接宣称“某渠道风控失效”。
3. **固定分档与最小样本量门槛**：
   - 归因下钻只允许使用维度登记表中的**决策时点冻结字段**。
   - 连续变量（如评分、金额、期限）必须使用事先登记的固定分档刀口，**严禁为了凑出显著差异而临时修改切分点**。
   - 任何单元格（基期或本期）若样本量 $N < 50$、期望坏样本数 $< 5$ 或期望好样本数 $< 5$，仅列入“小样本观察”，**严禁定性为恶化根因或提出处置建议**。基期和本期都要达到这三条。
4. **因果与外推克制**：
   - 关联与贡献度不等于唯一根因。无策略版本字段时不判断“策略失效”；无外部宏观序列时不判断“外部冲击”。

---

## 二、工具返回与停机

监控与归因数字只来自 `run_portfolio_monitoring` 和 `decompose_and_locate_anomaly` 的摘要。完整曲线、迁徙矩阵和分群表在产物文件中，对话里只引用摘要已经给出的汇总数。allowed-tools 只是工具声明，实际门禁由工具层返回的 envelope 与审批账本保证。

| status | 动作 |
| --- | --- |
| `ok` | 按 `allowed_conclusions` 撰写步骤 4 的对应句式 |
| `degraded` | 只写摘要仍然允许的结论类型 |
| `needs_human` | 展示待确认的处置研究后停止 |
| `blocked` | 按下表停机，不更换期次或维度重试来绕过 |

任何工具异常统一返回 `blocked` + `block_code`（`RESOURCE_UNAVAILABLE` / `UNSUPPORTED_SCOPE` 等）的结构化 envelope，不再抛裸异常。

| block_code | 停机时怎么写 |
| --- | --- |
| `ADMISSION_REQUIRED` | 说明该快照还没有准入记录，先执行 `credit-data-admission` |
| `LABEL_IMMATURE` | 说明目标账龄没有成熟批次，不拿未成熟样本比较 |
| `METRIC_UNSIGNED` | 说明口径版本未签收，不拼接两个版本的结果 |
| `TIME_POINT_VIOLATION` | 说明该列是观察时点字段，不能当作恶化来源 |
| `DIMENSION_UNREGISTERED` | 说明该维度不在登记表内，不临时加列 |
| `REPORT_UNTRACED` | 删掉或改正文中对不上的数字，再重新核验 |
| `PERIOD_MISMATCH` | 说明传入期次与数据实际期次不符，绝不静默换期，请用户重新指定 `base_periods` / `comp_periods` |
| `RESOURCE_UNAVAILABLE` | 说明视图、维表或运行环境暂不可用，写出缺口后停止，不臆造数值重试 |
| `SIGNOFF_REQUIRED` | 说明处置研究尚未签收，报告不得写「已立项/已批准」，停在待确认并等待审批卡 |

`base_periods` 与 `comp_periods` 必填且非空，必须是调用前已经写明的期次列表；期次与数据实际期次不匹配时返回 `PERIOD_MISMATCH`，绝不静默换期。`dimensions` 可选，但一旦传入必须全部在已签收登记表内，否则返回 `DIMENSION_UNREGISTERED`。用户没指定期次时先询问，不省略这两个参数。

`located`、`observe_only`、`stop_rule`、辛普森标记和 `allowed_conclusions` 以摘要为准，不在报告里重算后改名单；`observe_only` 名单不截断，不把被截断的切片写成已定位根因。`closure_error` ≥ 1e-9 时结论降级为 `degraded`；视图「未配置」保持 `ok` 并在 `notes` 说明，只有「已声明却缺列/不可比」才降级。`allowed_conclusions` 不含 `policy_version_gap` 时，全文不出现“策略失效”。五个枚举与句式的对应关系：

| 枚举 | 可用句式 |
| --- | --- |
| `mix_shift` | 恶化主要来自客群结构变化 |
| `within_group_deterioration` | 同一客群内部风险普遍变差 |
| `slice_contribution` | 特定切片贡献了主要恶化 |
| `policy_version_gap` | 某策略版本上线后同一客群表现变差 |
| `unexplained_residual` | 现有维度解释后仍有残差 |

处置研究写成待确认事项。`record_human_decision`（`gate=anomaly_disposition`）只由审批卡调用，不在本规程工具清单内。报告提交时输出 `signoff_status`（`anomaly_disposition`）；未签收却写「已立项/已批准」返回 `SIGNOFF_REQUIRED`。对话里的同意不算已经立项。

定稿调用 `verify_and_commit_report`。每个比率、bp、样本量和贡献占比都提交 `citations`：`path` 指向本次 `run_id` 的摘要字段，`rendered` 是正文写法。比率按展示精度对照产物中的 6 位小数。

定位公式与四条件见 [references/attribution-rules-and-formulas.md](references/attribution-rules-and-formulas.md)。

---

## 三、标准作业步骤（5 步归因法）

### 步骤 1：准入核对与基期/本期对齐
1. 确认已通过 `credit-data-admission` 获取合法的 `dataset_snapshot_id` 与 `metric_version`。工具返回 `ADMISSION_REQUIRED` 时停止，不在本规程内补做成熟度判断。
2. 把**基期**与**本期**写成明确且非空的期次列表（`base_periods` / `comp_periods` 必填），核实分子、分母、加权方式及数据延迟状态，再调用监控工具；期次与数据不匹配返回 `PERIOD_MISMATCH` 时绝不静默换期。
3. 若基期与本期的指标口径版本不一致，停止跨期直接拼接并报告版本冲突。

### 步骤 2：组合全景监控（趋势、Vintage 与逾期迁徙矩阵）
调用 `run_portfolio_monitoring` 生成三大基础视图：
1. **同口径趋势对比**：输出基期水平 $R_0$、本期水平 $R_1$、绝对变动 $\Delta R = R_1 - R_0$（精确到 bp）以及两边的分子、分母样本范围。
2. **同账龄 Vintage 曲线表**：
   - 横轴为账龄 `MOB 1, 2, 3, ...`，纵轴为放款批次 `Cohort`。
   - 判断风险恶化是**早发特征**（`MOB 1~2` 首逾 FPD 即抬升，通常指向获客欺诈或准入偏移）还是**迟发特征**（随 MOB 推移斜率变陡，通常指向共债累积或存量催收衰减）。
3. **逾期迁徙率矩阵（Roll Rate / Migration Matrix）**：
   - 追踪 `C (正常) -> M1 -> M2 -> M3 -> M4+ / Write-off` 的跨期滚动率。
   - 区分恶化源于**前端流入恶化**（`C -> M1` 升高）还是**中后端催收回收恶化**（`M1 -> M2`、`M2 -> M3` 滚动率升高）。

### 步骤 3：Shift-Share 结构分解与受约束下钻定位
当组合整体风险相对基期变差时，调用 `decompose_and_locate_anomaly` 对登记表中的决策时点维度逐一执行。工具暴露 `min_n`、`min_events`、`contribution_floor`、`alpha`、`min_bad_increase` 控制定位门槛；登记表的 `max_cardinality`、`min_cell_n`、`min_expected_bad`、`bins`、`cut_points` 逐维生效，不允许临时改刀口。无成熟批次时返回 `LABEL_IMMATURE`：
1. **Shift-Share 三分法分解**：
   - 计算该维度下的**结构效应（Mix Effect）**、**组内风险效应（Rate Effect）**与**交互项（Interaction）**，核验三者之和严格等于总变化 $\Delta R$（闭合残差 `closure_error` $\approx 0$）。
   - 检查是否存在**辛普森反转（Simpson's Paradox）**：若各子分群内部逾期率均在下降或持平，但高风险分群的放款占比大幅上升导致总逾期率上升，必须定性为**客群结构移动**，不得将任何单格定性为组内恶化。
2. **四条件定位恶化切片**：
   一个切片必须**同时满足**以下四项条件，方可列入“定位到的恶化客群名单”：
   - 基期与本期样本量均达标（默认 $N \ge 50$，且期望坏样本、期望好样本都 $\ge 5$）；
   - 切片比率变差方向与组合一致，且两比例检验显著（$p < 0.05$）；
   - 切片对总变化的贡献度 $\ge 20\%$（或为最大贡献格且新增坏账笔数达到预设门槛）；
   - 该维度未发生辛普森结构反转。
3. **有界停止规则（常见单维均未命中时按序停止，禁止穷举）**：
   - ① **普遍变差**：组内风险效应 $>$ 结构效应，但无单一格显著且够大 $\rightarrow$ 结论为“整本普遍变差”；
   - ② **结构分散**：结构效应 $>$ 组内风险效应，或发生辛普森反转 $\rightarrow$ 列出份额上升最大的格，结论为“客群构成变化”；
   - ③ **预登记二维交叉**：仅运行登记表指定的配对（如 `渠道 × 放款月`、`渠道 × 策略版本`、`评分档 × 渠道`）；
   - ④ **预登记连续字段分档**：按既有分箱查看评分、金额、期限的一维贡献；
   - ⑤ **现有维度无法定位单一客群**：如实报告已测试维度及未解释残差比例，建议“补充维度或按整本处置”。

### 步骤 4：按五类合法句式撰写归因结论
撰写报告结论时，**只能严格依据计算产物使用以下五类标准句式**：

| 结论类别 | 必须具备的计算依据 | 严禁越界表述（红线） |
| --- | --- | --- |
| **1. 恶化主要来自客群结构变化** | 结构效应（Mix Effect）解释了总变化的主要部分，或出现辛普森反转 | 严禁推论“某渠道获客策略已失败”或建议直接关停渠道 |
| **2. 同一客群内部风险普遍变差** | 组内风险效应（Rate Effect）占主导，且样本充足 | 严禁在无外部数据时直接断言“系宏观经济/外部环境冲击” |
| **3. 特定渠道/客群/批次贡献了主要恶化** | 该切片同时通过四项定位条件（样本够、显著、贡献 $\ge 20\%$、无反转） | 严禁将其表述为“唯一根因” |
| **4. 某策略版本上线后同客群表现变差** | 快照含策略版本字段，且新旧版本下同一客群可比、表现显著恶化 | 无策略版本字段时，严禁出现“策略失效”字样 |
| **5. 现有维度与版本解释后仍存残差** | 结构与已登记维度解释后残差仍大，或缺少策略版本/外部序列 | 严禁将未解释残差直接命名为“外部冲击”或“策略漏洞” |

### 步骤 5：数字溯源核验与处置研究立项
1. 调用 `verify_and_commit_report`，为报告中的每一个比率、bp 变动、样本量、贡献占比提交 `citations`。`path` 必须落在本次 `run_id` 的摘要字段上。
2. 对 `located` 中持续恶化的切片，生成一条**待人工确认的处置研究建议**（如启动 `policy-cutoff-simulation` 或 `scorecard-modeling-pipeline`）。建议停在待确认，不写成已经立项，也不自动进入策略或模型变更；报告提交输出的 `signoff_status` 未确认时若写「已立项/已批准」，返回 `SIGNOFF_REQUIRED`。
