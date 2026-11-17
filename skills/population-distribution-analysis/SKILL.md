---
name: population-distribution-analysis
description: 无标签客群分布分析：先锁定分析口径（总体/粒度/时点/分母/缺失语义/观察点），再输出全量分布与分箱构成，最后用同一口径做两期比较。只回答结构与位置变化，不回答坏账率升降。
license: MIT
allowed-tools:
  - inspect_and_bind_snapshot
  - describe_population
  - compare_population_distribution
  - verify_and_commit_report
metadata:
  version: "1.6.0"
  contract_version: "0.5"
---

# 无标签客群分布分析规程 (`population-distribution-analysis`)

本规程用于**没有目标标签、或标签尚未成熟**时的客群结构分析：申请量构成是否变化、某字段的分布是否整体上移、缺失与零值的口径是否改变。

它与存量监控的分工是硬的：有标签且已成熟 → 走 `portfolio-anomaly-attribution`；无标签或未成熟 → 走本规程。**不要为了做分布分析，临时把「疑似提现」「高息共债」这类代理标记当坏账标签**——那会同时污染结论和模型。

分析纪律（缺失与零值分开、口径先于数字、结论不得越过数据）见
[`references/analysis-discipline.md`](references/analysis-discipline.md)。

## 何时使用

- 用户问「10 月申请客群结构变了没有」「某字段的分布是不是整体上移」。
- 标签未成熟，但业务需要先看规模、构成、缺失情况。
- 需要把「口径」写下来、可复核、可版本化，而不是每次口头约定。

## 何时不使用

- 有成熟标签、要问坏账率或风险升降 → `portfolio-anomaly-attribution`。
- 要做评分卡或特征筛选 → `scorecard-modeling-pipeline`。
- 只是要一段描述性文字、不需要数字 → 不必调用工具。

---

## 一、先定口径，再看任何数字

`describe_population` 的 `spec` 必须在计算前填好。下面八项都要写定；`primary_test` 可以留空，但留空就只能出探索性结论：

| 字段 | 说明 | 本次事故里的对应错误 |
| --- | --- | --- |
| `spec_id` / `spec_version` | 口径身份；修订就升版本，旧 run 不覆盖 | 口径改了口头没说，新旧数字混用 |
| `population_label` | 这批记录到底是谁，用业务语言写 | 按申请记录统计，却围绕放款客群解释 |
| `grain` | 记录粒度，必须与快照一致（留空取快照值） | 申请与放款粒度假定一致 |
| `time_column` | 切期用的决策时点字段，必须等于快照的 `decision_time_column` | 拿观察时点字段切期 |
| `denominator_rule` | `all_records` / `approved` / `disbursed` / `matured_only` | 「全部申请进分母」没有写下来 |
| `denominator_filter` | 非 `all_records` 时必须指明哪些记录算数 | 让工具猜哪些算放款 |
| `field_semantics` | 每个用到的列：缺失是什么、零是什么 | `org_num` 缺失被直接剔除，占比从 36.49% 变 33.85% |
| `observation_points` | 观察点与已应用策略阈值，**分开标** | 把 `≥10` 当成整体多头变化的代表 |
| `primary_test` | 两期比较时预先指定的主检验；须是五种检验之一 | 五个检验一起看，挑显著的讲 |

四条硬规则：

1. **`population_label` 与 `denominator_rule` 必须自洽。** 写「已放款客群」就必须是 `disbursed`，否则返回 `POPULATION_MISMATCH`。这条不是文案检查，而是防止用申请的分布解释放款的结论。
2. **缺失与零值必须逐字段声明。** 有缺失或有零值、却没声明含义的列，会让工具直接返回 `needs_human` 并**不落 run**——因为「缺失当 0」还是「缺失当未知」会直接改分母和中位数。
   声明本身也真的会改数字：`missing: "zero_equivalent"` 会把缺失计入零值档并进入均值/分位数（摘要里 `n_imputed_zero` 给出补入条数），声明 `unknown` / `no_record` 则缺失只留在分母里。两种都同时给出 `without_zero` 块，供你判断零值是否属于该字段。
   **字面缺失码按缺失处理**：`N/A`、`na`、`null`、`none`、`nan`、`-`、`--`、`?`、空串、`未知`、`空`、`缺失`、`未填`、`不适用`、`未提供`、`待补充`、`无数据` 都会被算成缺失，`n_missing` 与 `missing_share` 包含它们，`top_values` 里也不会出现这一档。否则一个 87.5% 都是 `"N/A"` 的列会报 `n_missing = 0`，而它剩下的那一档占比被当成业务指标读。
3. **口径修订就升版本。** 同一 `spec_version` 的两次结果必须可比；改了口径还用旧版本号，`compare_population_distribution` 会以 `SPEC_MISMATCH` 拒绝。
4. **`all_records` 不接受 `denominator_filter`。** 「全部记录」与「只看某类记录」互相矛盾；要过滤就把分母规则改成 `approved` / `disbursed` / `matured_only`，并让 `population_label` 跟着改。

## 一之一、这些列进不来

**担保范围**：本工具的保证不是「任何输入都拿不到坏账率」，而是——**未声明的 0/1 列、只剩一侧判定词的列、以及词表内的成对措辞，拿不到；词表外的措辞要靠分析者在绑定时声明角色**。这条边界是诚实的，也是可审计的：越过它拿到的数字，责任在那份声明，而不在工具。

`column`、`dimensions`、`denominator_filter.column`、`time_column` 四个位置都会先过一遍标签闸门，命中即 `UNSUPPORTED_SCOPE`：

- 快照 `column_roles` 里角色为 `observation_outcome` / `suspect_leakage` / `label` / `target` / `flag` 的列；
- 快照 `observation_outcomes` 里登记过的列；
- 名字里带 `bad`、`default`、`fpd`、`dpd`、`overdue`、`delq`、`writeoff`、`label`、`target`，或带「坏账 / 好坏 / 违约 / 逾期 / 首逾 / 核销 / 表现期」的列（角色声明漏了时的兜底）。

原因很直接：**0/1 标记列的 `mean` 就是坏账率，按维度拆开就是一张分渠道坏账率表，拿它切期就是按结果分期**（两期分母分别变成好客户数和坏客户数）。标签列既然进不来，本工具就不该产出坏账率。

### 0/1 列必须先定性

**取值为 0/1 的列一律先定性**——不管它是 `int64`、`float`、`bool`、`object`，还是从 CSV/Excel 读进来的文本 `"0"/"1"`。判定看值不看 dtype，因为计算层本来就会把文本 `"0"/"1"` 转成数值去算均值。

判据分两步，第一步是把取值归一成 token：`0` / `0.0` / `00` / `false` / `否` / `n` / `no` 记为「零」，`1` / `1.0` / `true` / `是` / `y` / `yes` 记为「一」。然后按**这一列会走哪条画像**分别判：

**能整列转成数值的列**（走数值画像，输出的是均值与分箱）：

- 取值只有 0 和 1（或只有其中一个）；
- 或全为整数、含 0 含 1、不同取值 ≤ 3（最多一个脏码，如 `2=未知`）；
- 或 0/1 覆盖 ≥90% 且 0/1 之外的取值 ≤4 个（`0/1/2/3` 各带一点未知码）；
- 或 0/1 覆盖 ≥95%（脏码个数不限）。

**转不成数值的列**（走分类画像，输出的是逐档计数与占比）：

- 只要 0 和 1 的写法**同时出现**就算标记列，脏码多少不影响——`890 个 "0" + 50 个 "1" + 60 个 "N/A"` 里，`"1"` 那一档的占比就是比率，多一个脏码就多漏一次；
- 或好坏两侧的写法同时出现：`good`/`bad`、`ok`/`error`、`pass`/`fail`、`success`/`failure`、`valid`/`invalid`、`approved`/`rejected`、`accept`/`decline`、`正常`/`异常`、`通过`/`拒绝`、`成功`/`失败`、`有效`/`无效`、`好`/`坏`、`良好`/`不良`、`逾期`。这一条同样不看第三、第四个取值，`good/bad/pending` 也算；
- 或**非缺失取值全部是判定词**（哪怕只有一侧、哪怕只剩一个）。整列只有 `"是"`、只有 `"bad"`、或 `"1"` 加一堆 `"N/A"`，它们的计数除以总条数同样是比率。只有一个普通取值（全是「线上」）或全是缺失码的列不在此列。

反向词由正向词派生：`不/未/非/没 + 正向词`（不成功、未通过、非正常）与 `无 + 正向词` 自动进反向表，`失效`、`拒批`、`审批拒绝`、`黑名单` 这类固定说法单列。长度 ≥2 的词还做包含匹配，所以「严重逾期」「审批拒绝」也认得出；单字（`好`/`坏`/`差`）只做精确匹配，避免把「差异」当成分档。

这个分界不是随手定的阈值：分类画像会把每一档的计数和占比都列出来，所以「0 与 1 同时出现」就已经是漏点，设一个「最多几个脏码」的上限只等着被下一个脏码绕过；而数值列的 0..9 是合法的小基数分布，其均值不是比率。

命中上述任一条、且快照没把它声明为 `feature`，工具返回 `needs_human`，**不落 run、不出数字**，并给出 `human_gate.kind = "binary_column_role"`：

```
inspect_and_bind_snapshot(..., column_roles={"has_guarantor": "feature"})
```

原因同上：0/1 列的均值就是「1 的占比」。一个叫 `gb_flag`、`是否坏账` 的列如果没人声明它是什么，工具无从判断那 0.1 是特征命中率还是坏账率。

这条闸门对四个位置都生效：描述列、维度、分母过滤列、切期列。判定**站在计算实际用的那批行上做**，而且做三次：整表、本期、以及「本期 ∩ 声明的分母」。第三次最容易被忽略——一个放款决策列在申请期里通常有 `3/4/5` 等拒绝码，切到 `disbursed` 之后正好只剩 `1/0`，分母里的均值就是坏账率。维度内部同理：整列 `0..9` 不是标记列，但「线上」这一档只剩 `0/1` 时，这一档的 `mean` 与 `share_one` 会被抑制并给出原因，而不是照算。

`needs_human` 的摘要只说明「这列是 0/1、角色未定」，**不返回任何计数**——对一个真是坏账标记的列，「1 的条数 ÷ 总条数」就是坏账率，把它写进信封等于门没关。

声明为 `feature`（决策时点已知的特征）之后可以正常描述，摘要会给出 `binary.share_one`、`described_column = {role, role_source: "explicit"}` 与一句读法提示（`mean` 是占比，不是风险水平），limitations 里也会写明「这一列的身份由分析者担保，工具不复核数据本身」——声明落在快照 meta 的 `explicit_roles` 里，事后可审计（`column_roles` 里每列都有角色，含推断出来的，不能用来区分声明与推断）。

## 二、先出全量分布，再看阈值

`describe_population` 默认输出完整分布：行数、缺失/零值、均值、七个分位数、分箱频数与累计占比、Top 取值。`observation_points` 只是**附加行**，不是主视图。

写报告时的顺序必须是：总量与构成 → 分布形状 → 再看业务关心的阈值那一段。反过来写（先给 `≥10` 那一段）会让读者以为那一段代表全体。

阈值用 `kind` 区分：

- `observation_point`：业务想看的一个点，没有策略含义；
- `applied_policy_threshold`：策略里**已经**在用的卡点。

混淆这两者，等于把「看一眼」写成「策略已经这么卡」。

## 三、比较：两个 run，不是一个工具

`compare_population_distribution` 只接受两次 `describe_population` 的 `run_id`，不接受原始期次。这是刻意的：先描述、再比较，才不会被「直接比一下」带过去。

比较会返回：

- 分箱人数、占比与**百分点变化**（箱边界取自基期，不随对比期重算）；
- 均值/中位数/分位数差、总变差距离、缺失率与零值率变化；
- 四项检验，每项都带 `tests_what` / `applicability` / `allowed_conclusions` / `role`。

分箱表两端固定带 `underflow` / `overflow` 行：落在基期边界之外的记录会出现在那里，不会被丢掉，也不会被重新归一化成「看起来没变」。所有占比的分母都是该期有效记录数（含按口径补入的零），占比之和恒为 1。两期分箱行数对不齐时工具不出分箱表，并在 `binning_note` 说明原因，而不是把两个不同的区间配成一行。

分类列比较时，缺失作为一个独立类别（`<缺失>`，带 `is_missing`）参与构成检验与总变差距离：某期整列缺失时总变差距离是 1 而不是 0。类别占比的分母同样是该期全部记录，与描述里的 `top_values` 一致。

签收状态取两期的交集：`summary.signoff` 分别给出 `base` / `comp`，只有两期都签收时 `spec.signed` 才为 `true`。一期签收、一期没签收时，`limitations` 会写明，结果按未签收口径对待。

检验口径（这四项回答的是不同问题，不能择优汇报）：

| 检验 | 回答什么 | 允许的结论 |
| --- | --- | --- |
| `chi2_bins` | 各分箱的**构成**是否变化（数值列） | `composition_change` |
| `chi2_categories` | 各类别**构成**是否变化（分类列，缺失算一个类别） | `composition_change` |
| `ks_2samp` | 整体分布是否移动（任一差异） | `distribution_shift` |
| `mann_whitney` | **秩位置**（典型值）是否平移 | `level_shift` |
| `welch_t` | **均值**是否不同（对厚尾敏感） | `mean_shift` |

数值列出四项（`chi2_bins` / KS / MW / Welch），分类列出 `chi2_categories`。`primary_test` 只能取这五个名字之一，拼错会被拒而不是静默失效。

**主检验必须在 `spec.primary_test` 预先声明。** 未声明时，各项都是探索性的，报告里必须写明「多项同看会放大假阳性」，不得只挑显著的那一项讲。样本量足够大时，任何微小差异都会显著——显著不等于业务重要，要同时给效应量（百分点变化、总变差距离）。

声明了 `missing: zero_equivalent` 时，四项检验用的是**补齐零值后**的序列，与摘要里的 `mean` / 分位数是同一批数据；否则一份报告会引用两个总体。

声明的主检验如果在本列根本没有产出（数值列写了 `chi2_categories`），摘要的 `primary_test` 会置空、`declared_primary_test` 保留原值，并写明「该项不适用于本列，全部检验均为探索性」——不会挂着一个没跑过的名字让人以为做过预注册。

## 四、结论边界

允许：`distribution_shape`、`composition_change`、`level_shift`、`missing_pattern`。

禁止：`bad_rate_change`、`risk_deterioration`、`default_prediction`。工具每次都会把这些写进 `forbidden_conclusions`，报告里不要越界——分布上移既可能是风险变差，也可能只是客群上移，两者在这里无法区分。

### 闸门识别不到的情形

闸门按取值形态判断，不看列名语义，因此有两类列它认不出来：

1. **不含 0 的编码**：`1/2`、`0/2`、`10/20`。工具无法从数据本身断定一列 `2/3` 是评分档位还是好坏编码，而放宽到「两个整数取值就拦」会把所有二值特征都变成待定。切期列是例外——`1/2` 这种小编码在绑定时就被判为非日期而拒绝；描述列、维度、分母过滤列上没有这层保护。
2. **词表之外的两值措辞**：好坏判定走的是词表（`good/ok/pass/success/valid/approved...` 与对应的反面，中英各一套，反向词由「不/未/非/没/无 + 正向词」派生）。像 `良好`/`极差`、`合格品`/`报废品` 这种词表里没有、也无法由正向词派生的写法，列名又不带标签词时，会被当作普通分类字段描述。词表可以继续补，但它不可能穷尽自然语言——所以这是**文字类列的根本限制**，而不是待修的 bug。

处理方式是**在绑定快照时用 `column_roles` 把它声明为标签**（`observation_outcome` / `suspect_leakage`），而不是依赖闸门事后识别。反过来，如果某个普通分类字段被词表误判（例如「正常/冻结」这类状态字段恰好用了判定词），给它声明 `feature` 即可放行——闸门是保守的，出口永远是这一条声明。

## 五、交付

1. 结论由同一个 run 生成，报告里的每个数字都挂 `citation`（`path` 指向该 run 的 `summary`，例如 `population.denominator`、`column.quantiles.p50`、`bins.0.share_comp`）。
2. 摘要里的 `spec.fingerprint` 要写进报告：它是「这版口径」的凭证。形如 `fp_4b1c9e2a7d3f5061`，可以原样引用（`path: spec.fingerprint`），不会被当成数字要求二次引用。
3. 用 `verify_and_commit_report` 核验。返回 `REPORT_UNTRACED` 时看 `details.reason`：`no_runs_referenced` 说明数字不是工具算的；`uncited_number` 会点名正文里哪个数字没引用。
   p 值可以写成 `8.39e-16`（按有效数字核验）或 `0.0000`；正文的写法必须与引用的 `rendered` 完全一致，写 `8.4e-16` 就要引用 `8.4e-16`。
4. 未签收口径（`spec.signed=false`）可以在分析稿里用，但不得写成「已签收口径」或据此上线策略。
