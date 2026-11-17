# 口径 spec 与分布检验参考 (`population-distribution-analysis`)

本文件是 `describe_population` / `compare_population_distribution` 的字段契约与检验说明。**口径是计算输入，不是报告里的注释**：同一个字段写成 `missing: unknown` 还是 `missing: zero_equivalent`，会得到不同的分母和不同的中位数。

## 一、spec 完整结构

```json
{
  "spec_id": "population_monthly",
  "spec_version": "v1",
  "population_label": "已放款客群",
  "grain": "application",
  "time_column": "apply_month",
  "denominator_rule": "disbursed",
  "denominator_filter": {"column": "loan_status", "values": ["已放款"]},
  "field_semantics": {
    "org_num": {"missing": "no_record", "zero": "no_record"},
    "apply_amount": {"missing": "unknown", "zero": "true_zero"},
    "channel": {"missing": "unknown"}
  },
  "observation_points": [
    {"name": "≥10 家机构", "threshold": 10, "kind": "observation_point"},
    {"name": "策略阈值 ≥12", "threshold": 12, "kind": "applied_policy_threshold"}
  ],
  "primary_test": "mann_whitney",
  "note": "口径由风险管理部 2026-11 修订；旧版本为 v0"
}
```

`denominator_filter` 只在非 `all_records` 时出现，二者互斥（见下）。

### 必填

| 字段 | 取值 | 说明 |
| --- | --- | --- |
| `spec_id` | 字符串 | 口径身份；同类分析复用同一 id |
| `spec_version` | 字符串 | 任何修改都升版本。同版本号必须可比 |
| `population_label` | 业务语言 | 与 `denominator_rule` 必须自洽 |
| `time_column` | 列名 | 必须等于快照的 `decision_time_column`；观察时点列、别的字段、标签列都会被拒。该列必须是真日期：取值为 0/1、小编码（`1/2`、`2/3`）或纯数值（`20240901`）在绑定时就拒；**混有数值与日期的列同样拒**——一个文本日期会把年份区间抬到 2024，纯数值行仍是 1970 时间戳，按最大年份判断会放过它们 |
| `denominator_rule` | `all_records` / `approved` / `disbursed` / `matured_only` | 非 `all_records` 时必须给 `denominator_filter`；`all_records` 时给了反而报错 |

`all_records` 与 `denominator_filter` 是互斥的：「全部记录」加上「只看某类记录」自相矛盾。以前是静默忽略过滤条件按全量算、再把数字冠上 `all_records` 的名字；现在直接 `UNSUPPORTED_SCOPE`，要么去掉过滤，要么换成 `approved` / `disbursed` / `matured_only`。

### `primary_test` 取值

只接受 `chi2_bins` / `chi2_categories` / `ks_2samp` / `mann_whitney` / `welch_t`。拼错会被拒（`UNSUPPORTED_SCOPE`），而不是悄悄让所有检验变成探索性。

### 描述对象不能是标签列

`column`、`dimensions`、`denominator_filter.column`、`time_column` 四处都会先过标签闸门，命中即 `UNSUPPORTED_SCOPE`：

| 判据 | 例子 |
| --- | --- |
| 快照 `column_roles` 角色为 `observation_outcome` / `suspect_leakage` / `label` / `target` / `flag` | `bad_flag` 声明为观察期结果 |
| 快照 `observation_outcomes` 登记过 | 准入门已识别的表现字段 |
| 列名含 `bad` / `default` / `fpd` / `dpd` / `overdue` / `delq` / `writeoff` / `label` / `target` | `fpd30`、`is_default` |
| 列名含「坏账 / 好坏 / 违约 / 逾期 / 首逾 / 核销 / 表现期」 | `是否坏账`、`逾期标记` |

原因：**0/1 标记列的 `mean` 就是坏账率；按维度拆开就是分渠道坏账率表；拿它切期就是按结果分期。** 标签进不来，工具才守得住「不产出坏账率」。

### 0/1 列必须先声明为 `feature`

下列任一条成立即视为 0/1 标记列（**判定看值，不看 dtype**：`int64` / `float` / `bool` / `object` / 文本 `"0"/"1"` 一视同仁，因为计算层会把文本转成数值再算均值）：

先做两步归一：
1. **字面缺失码 → 缺失**：`N/A`、`na`、`n.a.`、`#N/A`、`null`、`none`、`nan`、`-`、`--`、`?`、`/`、空串、`未知`、`空`、`空白`、`缺失`、`未填`、`未填写`、`不适用`、`未提供`、`待补充`、`无数据`、`没有数据`。它们不计入任何一档，而是进 `n_missing` / `missing_share`。
2. **取值 → 角色**：`0` / `0.0` / `00` / `false` / `否` / `n` / `no` → 零；`1` / `1.0` / `true` / `是` / `y` / `yes` → 一（`bool` dtype 直接算标记列）；正向词表 → pass，反向词表 → fail。

然后按**这一列会走哪条画像**分别判：

| 画像 | 判据 | 例子 |
| --- | --- | --- |
| 数值（整列可转成数值） | 取值只有 0/1（或只有其中之一） | `bad_flag` 全 0 |
| 数值 | 全为整数、含 0 含 1、不同取值 ≤ 3 | `0/1/2`，60 个 `2=未知` |
| 数值 | 0/1 覆盖 ≥ 90%，且 0/1 之外的取值 ≤ 4 个 | `0/1/2/3` 各带一点未知码 |
| 数值 | 0/1 覆盖 ≥ 95%，脏码个数不限 | `0/1` 占 96%，其余零散 |
| 分类（转不成数值） | 0 与 1 的写法同时出现，**脏码个数不限** | `890 "0" + 50 "1" + 60 "N/A"` |
| 分类 | 好坏两侧写法同时出现 | `good/bad`、`ok/error`、`正常/异常`、`good/bad/pending` |
| 分类 | 非缺失取值全部是判定词（只有一侧也算） | 整列只有 `"是"`；`50 "1" + 350 "N/A"`；`"是" + "未知"` |

反向词由正向词派生（`不/未/非/没 + 正向词`、`无 + 正向词`），另收 `失效`、`拒批`、`审批拒绝`、`黑名单`；`白名单` 在正向侧。长度 ≥2 的词做包含匹配（「严重逾期」「审批拒绝」），单字只做精确匹配（「差异」不会被当成「差」）。

为什么要按画像分而不用一个阈值：分类画像会把**每一档**的计数与占比都列出来，所以 0 与 1 同时出现就已经是漏点，设「最多几个脏码」的上限只会被下一个脏码绕过；而数值列里 `0..9` 是合法的小基数分布，均值不是比率，反而不能用同一条规则。`text 0/1 + 5 个脏码` 这种列转不成数值、走分类画像，所以照样拦。

命中且快照没把角色声明为 `feature`，就返回 `needs_human`（`human_gate.kind = "binary_column_role"`），不落 run、不出数字：

```
inspect_and_bind_snapshot(..., column_roles={"has_guarantor": "feature"})
```

声明后可以描述，摘要额外给 `binary = {n_one, n_zero, share_one}` 与一句读法提示（`mean` 是占比，不是风险水平），并记录 `described_column = {name, role, role_source}` 作为声明留痕——`role_source` 读的是快照 meta 的 `explicit_roles`（`column_roles` 每列都有角色，区分不出声明与推断）。

四个位置都受约束：描述列、维度、`denominator_filter.column`、`time_column`。判定站在**计算实际用的那批行**上做，共三次：整表、本期、以及「本期 ∩ 声明的分母」。第三次最容易被忽略——放款决策列在申请期里常有 `3/4/5` 等拒绝码，切到 `disbursed` 后正好只剩 `1/0`，分母里的均值就是坏账率。维度内部同理：整列 `0..9` 不是标记列，但某一档只剩 `0/1` 时，该档的 `metric` 会被抑制并写入 `suppressed_reason`。

`needs_human` 的摘要只有 `column` / `role` / `observed_values`，**不含任何计数**：对一个真是坏账标记的列，1 的条数除以总条数就是坏账率，写进信封等于门没关。

`column_roles` 接受四种角色：`decision_time`、`observation_outcome`、`suspect_leakage`、`feature`。前三种都会被标签闸门拒绝，只有 `feature` 放行 0/1 列。

判据的边界有两处：

1. **不含 0 的编码**（`1/2`、`0/2`、`10/20`）不会被识别，会被当作普通数值或分类字段描述——放宽到「两个整数取值就拦」会把所有二值特征都变成待定。切期列是例外：`1/2` 小编码在绑定时就因「不是日期」被拒。
2. **词表之外的两值措辞**。好坏判定依赖词表：`good/ok/okay/pass/passed/success/successful/valid/normal/fine/healthy/approved/approve/accept/accepted` 与 `bad/ng/fail/failed/failure/error/invalid/abnormal/reject/rejected/decline/declined/deny/denied/dead`，中文对应 `正常/通过/合格/成功/有效/良好/好/无异常` 与 `异常/失败/无效/不良/违约/逾期/不通过/未通过/不合格/拒绝/已拒绝/未成功/不正常/坏/差`。`良好`/`极差`、`合格品`/`报废品` 这类不在表内的写法认不出来。

这两类只能靠绑定时声明角色来排除。反过来，普通分类字段被词表误判（如「正常/冻结」）时，声明 `feature` 即可放行。

显式声明优先于命名启发式，因此这也是误判的解药：准入门把 `mob`、`bucket`、`dpd` 这类名字默认判为观察期结果（保守取向），如果某个 `mob` 列确实是决策时点已知的账龄，声明 `column_roles={"mob": "feature"}` 即可放行。

### `population_label` 与分母规则的自洽表

| 措辞包含 | 需要 `denominator_rule` |
| --- | --- |
| 放款 / 提款 / 支用 / 用信 / 已放 | `disbursed` |
| 审批通过 / 核准 / 通过率 | `approved` |
| 成熟 / 表现期完整 | `matured_only` |
| 申请 / 进件 / 审批 | `all_records` |

不一致直接 `POPULATION_MISMATCH`。**这条防止用申请记录的分布去解释放款客群的结论**——两者不是一批记录。

### `field_semantics`

支持两种写法：

- 字符串简写：`"org_num": "no_record"` 表示缺失与零值都是「无记录」；
- 对象写法：`{"missing": "no_record", "zero": "true_zero"}`。

`missing` 取值：`unknown`（不知道）、`no_record`（没有该记录，如未开立机构）、`not_applicable`（该客群不适用）、`zero_equivalent`（等价于 0，计入零值）。

`zero` 取值：`true_zero`（真的是 0）、`no_record`、`not_applicable`、`unknown`。

`zero_equivalent` 只对数值列有效：分类列没有「0」这一档可并入，声明即 `UNSUPPORTED_SCOPE`，避免口径变成一句不生效的文案。

规则：**用到的列只要有缺失或有零值，就必须声明**，否则返回 `needs_human`，不落 run、不出数字。只缺零值含义时不要求声明缺失含义（反之亦然）——那会让一个没有缺失的字段永远卡在待确认上。

声明不是文案，它进计算：

| `org_num` 的 `missing` 声明 | 有效记录数 | 零值数 | 中位数 |
| --- | --- | --- | --- |
| `unknown` / `no_record`（缺失只留在分母里） | 720 | 40 | 6 |
| `zero_equivalent`（缺失并入零值档） | 800 | 120 | 5 |

选哪个是业务问题：`org_num` 缺失若表示「未在任何机构有贷款」，它就等价于 0（33.85%、中位数 5 才对）；若表示「数据没取到」，就当未知。工具把两种结果都摆出来（摘要同时给 `n_imputed_zero` 与 `without_zero` 块），但**只按你声明的那个口径计算**，不替你选。

历史事故就在这里：缺失被直接剔除，占比从 36.49% 变成 33.85%、中位数从 6 变成 5，报告却按「口径没变」讲下去。

### `observation_points`

`kind` 必须明确：

- `observation_point`：业务想看的点，**没有**策略含义；
- `applied_policy_threshold`：策略里已经在用的卡点。

`describe_population` 无论有没有 `observation_points`，都先输出全量分箱；阈值只作为附加行。报告里不要把阈值那一段写成总体结论。

## 二、输出字段与引用路径

`describe_population` 的 `summary` 可直接被 `verify_and_commit_report` 的 `citation.path` 引用：

| 路径 | 含义 |
| --- | --- |
| `population.denominator` | 分母（该口径下的记录数） |
| `population.rows_in_period` | 该期原始行数 |
| `population.rows_excluded` | 被分母规则排除的行数 |
| `column.n_missing` / `column.n_zero` | 缺失数 / 零值数 |
| `column.n_imputed_zero` | 因 `zero_equivalent` 被并入零值的缺失数 |
| `column.quantiles.p50` | 中位数 |
| `column.mean` | 均值（受极端值影响） |
| `column.binary.share_one` | 0/1 字段取值为 1 的占比 |
| `column.bins.1.share` | 第 1 个**真实分箱**的占比（分箱表首行是 `underflow`） |
| `by_dimension.0.cells.1.share` | 某维度第 2 个取值的占比 |
| `observation_points.0.count` | 第一个观察点的命中数 |
| `spec.fingerprint` | 口径指纹，形如 `fp_4b1c9e2a7d3f5061`，可原样引用 |
| `described_column.role` / `.role_source` | 该列的角色，以及它是显式声明还是默认判定 |

分箱表结构固定：`kind` 为 `underflow` 的行在首、`overflow` 行在末，中间是普通 `bin`。落在边界外的记录不丢弃，占比分母恒为该期非缺失记录数，占比之和为 1。引用具体分箱时先看 `kind`，别默认第 0 行就是最低区间。

`column.top_values[i].share` 的分母随画像走，两侧不一样：**数值列以该期非缺失记录数为分母**（与 `bins` 同一分母，按 `zero_equivalent` 补入的零也算在内），只列前 5 档；**分类列以该期全部记录数为分母**，缺失不占档、只出现在 `n_missing` 里，所以占比之和小于 1——比较时的类别表会把 `<缺失>` 补成一档，同一个类别的占比因此仍与描述一致，只是那一边之和回到 1。看到 `0.36` 这样的数先确认它属于哪一侧，不要把两个分母的占比放进同一张表里比大小。

`compare_population_distribution` 的路径：

| 路径 | 含义 |
| --- | --- |
| `bins.2.share_delta_pp` | 第 3 个分箱的百分点变化 |
| `bins.2.count_delta` | 该分箱人数变化 |
| `mean_delta` / `median_delta` | 均值 / 中位数差 |
| `total_variation_distance` | 总变差距离（0~1，构成差异的效应量） |
| `tests.0.p_value` / `tests.0.statistic` | 某项检验的 p 值与统计量 |
| `denominator.base` / `denominator.comp` | 两期分母 |
| `category_deltas.0.value` / `.share_delta_pp` | 变化最大的类别（分类列） |
| `total_variation_distance` | 分类列同样给出 |
| `binning_note` | 两期分箱行数对不齐时的说明（此时不出分箱表） |
| `signoff.base` / `signoff.comp` | 两期口径是否已签收 |
| `primary_test` / `declared_primary_test` | 实际生效的主检验 / 口径里声明的那个（不适用时前者为空） |

比较的分箱边界一律取基期描述运行发布的那一组，不重新分箱：否则报告里引用的占比在自己引的那份 profile 里找不到。签收状态取两期交集，`spec.signed` 只有两期都签收才为 `true`。

## 三、检验

数值列四项、分类列一项，回答的问题互不相同。**择优汇报是本次事故的直接原因**：分箱卡方显著说明结构变了，却很容易被写成「整体风险上移」。`primary_test` 只接受 `chi2_bins` / `chi2_categories` / `ks_2samp` / `mann_whitney` / `welch_t`。

声明 `missing: zero_equivalent` 时，四项检验用补齐零值后的序列（与摘要的 `mean`、分位数同一批数据），不是原始非缺失样本——否则同一份报告里均值与 p 值来自两个总体。

声明的 `primary_test` 在本列没有产出时（例如数值列写 `chi2_categories`），`primary_test` 置 `null`、`declared_primary_test` 保留原值，limitations 写明「该项不适用于本列，全部检验均为探索性」。

p 值可以引用成 `8.39e-16`（按有效数字核验：`8.3912e-16` 四舍五入到三位就是 8.39e-16）或 `0.0000`；正文写法必须与引用的 `rendered` 一致。

### `chi2_bins` — 构成是否变化

$$\chi^2=\sum_{i}\frac{(O_i-E_i)^2}{E_i},\quad df=k-1$$

`k` 为两期合计非零的分箱数。适用条件：两期独立样本、每箱两期期望频数 ≥ 5。不满足时工具返回 `p_value: null` 并说明原因，**不硬算**。允许结论：`composition_change`。

### `chi2_categories` — 分类列构成是否变化

同一个卡方，输入换成类别计数；**缺失作为一个独立类别参与**（`<缺失>`，输出里带 `is_missing`）。各类别占比的分母是该期全部记录，与描述里**分类列**的 `top_values` 一致——否则同一类别在描述与比较里会得到两个占比。某期整列缺失时，总变差距离为 1（构成完全不同），不是 0。

### `ks_2samp` — 整体分布是否移动

$$D=\sup_x|F_1(x)-F_2(x)|$$

对位置、尺度、形状任一差异都敏感，因此显著只说明「不一样」，不说明「变差」。适用：连续型数值；样本量 ≥ 50 时渐近 p 值可靠。允许结论：`distribution_shift`。

### `mann_whitney` — 秩位置是否平移

基于秩和，抗极端值。适用：连续型数值；**只有分布形状相近时**才能把显著解释为「位置移动」。允许结论：`level_shift`。

### `welch_t` — 均值是否不同

不假设方差相等。厚尾或大额少数记录会主导结论，因此它显著常与业务直觉不符。允许结论：`mean_shift`。

### 多重检验与主检验

- 未声明 `primary_test` 时，四项均为 `role: exploratory`，报告必须写明「多项同看会放大假阳性」；
- 声明后，该项为 `role: primary`，其余仍可报告但只能作为支持/反驳证据；
- 显著性必须与效应量同时给出（百分点变化、总变差距离）。样本量大时任何微小差异都会显著，**显著 ≠ 业务重要**；
- 不做 Holm-Bonferroni 式全套校正：在这种样本量下它会制造虚假的安全感，预先声明主检验更有效。

## 四、结论边界

允许：`distribution_shape`、`composition_change`、`level_shift`、`missing_pattern`。

禁止：`bad_rate_change`、`risk_deterioration`、`default_prediction`。

原因不是工具能力不足，而是**输入里没有标签**：分布上移既可能是风险变差，也可能只是客群整体上移。要回答风险问题，回到 `portfolio-anomaly-attribution`（存量、已成熟）或 `scorecard-modeling-pipeline`（建模）。

## 五、与建模的衔接

描述分析的三个结论可以直接变成建模前置检查：

1. **缺失与零值语义** → 建模缺失处理按字段定义，变换规则随模型保存，训练与评分一致；
2. **样本覆盖与分布变化** → 开发样本与未来使用总体的分布是否一致（PSI 之外的逐字段证据）；
3. **申请唯一 ≠ 客户独立** → `grain=application` 只能说明记录级唯一，客户级独立性要在建模侧用客户分组与时间外验证检查。
