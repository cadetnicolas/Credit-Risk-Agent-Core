"""Label-free distribution profile and period-to-period comparison.

Nothing in this module reads a target label. It answers "what does the
population look like" and "did its shape change", which is a different
question from "did the bad rate move". Keeping it label-free is exactly what
lets it run on immature cohorts without weakening the maturity gate: a caller
cannot obtain a bad rate here, because no flag column is ever passed in.

Two rules that matter more than the maths:

* missing and zero are counted separately and reported *before* any average,
  so a field whose blank means "no institution on record" cannot silently
  become a zero and drag the median down;
* every test carries what it tests, when it applies, and what it may conclude.
  Four tests on the same two samples answer four different questions, and
  reporting only the one that came out significant is how a structure change
  gets written up as a risk deterioration.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

try:  # scikit-learn is a hard dependency, so scipy is present in practice.
    from scipy import stats as _st
except Exception:  # pragma: no cover - exotic install without scipy
    _st = None

QUANTILES = (0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99)
MAX_TOP_VALUES = 12
_COERCE_SHARE = 0.95
# 一个 0/1 标记列只要绝大多数取值仍是 0/1，就按标记列对待：少数脏码
# （2 = 未知）不能把它变成「普通数值字段」从而绕开声明闸门。
_FLAG_SHARE = 0.95
# 近似二值：0/1 占九成以上，剩下的都是零星未知码。
_NEAR_BINARY_SHARE = 0.90


def is_numeric(series: pd.Series) -> bool:
    return bool(pd.api.types.is_numeric_dtype(series)) and not bool(
        pd.api.types.is_bool_dtype(series)
    )


def is_binary_flag(series: pd.Series) -> bool:
    """True for a yes/no marker, whatever dtype and encoding it was written in.

    The gate must look at the data the same way the statistics do. A column of
    ``"0"/"1"`` strings is described as a numeric field with mean 0.05 by
    ``as_numeric``; a gate that only accepted numeric *dtypes* would wave it
    through, and the mean of a bad flag is the bad rate. Pandas 3 reads CSV and
    Excel as StringDtype by default, so this is the common case, not an edge.

    The token check runs first and does not depend on coercion: ``"0"/"1"`` plus
    a few non-numeric dirty codes no longer parses as numeric at all, and the
    column would fall back to a categorical profile whose ``top_values`` shares
    are, once again, exact rates.
    """

    if pd.api.types.is_bool_dtype(series):
        return True
    # 先把字面缺失码当成缺失：`"0"/"1"` 掺 `N/A` 的列归一后就是纯 0/1，
    # 不归一的话两边都判不出来——token 一侧以为还能转数值，数值一侧转不动。
    series = normalize_missing(series)
    if _is_flag_tokens(series):
        return True
    values = _coerced_numeric(series)
    if values is None:
        return False
    values = values.dropna()
    if values.empty:
        return False
    distinct = set(pd.unique(values))
    if any(not float(item).is_integer() for item in distinct):
        return False
    if distinct <= {0.0, 1.0}:
        return True
    if not {0.0, 1.0} <= distinct:
        return False
    if len(distinct) <= 3:
        # 0/1/2 这种「一个未知码」的标记列，脏码占比多高都还是标记列。
        return True
    share = float(values.isin([0.0, 1.0]).mean())
    # 近似二值：0/1 占九成以上、其余取值不超过 4 个（0/1/2/3 各带一点未知码）。
    # 单靠 95% 挡不住 4 个取值 94% 的形态，而那种列的均值仍然是比率。
    if share >= _NEAR_BINARY_SHARE and len(distinct - {0.0, 1.0}) <= 4:
        return True
    return share >= _FLAG_SHARE


# 零/一的各种写法。中英文都要覆盖：中文数据的标签列常写成「是/否」。
_ZERO_TOKENS = frozenset({"0", "0.0", "false", "否", "n", "no"})
_ONE_TOKENS = frozenset({"1", "1.0", "true", "是", "y", "yes"})
# 不写成 0/1 的成对取值：好坏、通过/拒绝、正常/异常。「ok/error」这种写法不需要
# 列名语义就能认出来，所以不能算边界——它的 top_values 里 error 的占比就是坏账率。
# 两张表必须左右对称地写：只收 approved 却不收 rejected，等于把成对判据留给运气。
_PASS_TOKENS = frozenset(
    {
        "good", "ok", "okay", "pass", "passed", "success", "successful", "valid",
        "normal", "fine", "healthy", "approved", "approve", "accept", "accepted",
        "正常", "通过", "合格", "成功", "有效", "良好", "无异常", "好",
    }
)
_FAIL_TOKENS = frozenset(
    {
        "bad", "ng", "fail", "failed", "failure", "error", "invalid", "abnormal",
        "reject", "rejected", "decline", "declined", "deny", "denied", "dead",
        "坏", "差", "异常", "失败", "无效", "失效", "不良", "违约", "逾期",
        "不通过", "未通过", "不合格", "拒绝", "已拒绝", "拒批", "审批拒绝",
        "未成功", "不正常", "黑名单",
    }
)
# 「不/未/非 + 正向词」也是反向词：不成功、未通过、非正常。手工维护这两张表
# 一定会漏半边，所以反向词由正向词派生出来，而不是逐条手写。
_NEGATIVE_PREFIXES = ("不", "未", "非", "没")
_DERIVED_FAIL_TOKENS = frozenset(
    f"{prefix}{word}" for prefix in _NEGATIVE_PREFIXES for word in _PASS_TOKENS
) | frozenset({"无" + word for word in _PASS_TOKENS if len(word) >= 2})
_FAIL_LEXICON = _FAIL_TOKENS | _DERIVED_FAIL_TOKENS
_PASS_LEXICON = _PASS_TOKENS | frozenset({"白名单"})

# 字面写出来的缺失码。工具不把它们当一档取值：既会让 n_missing 报 0（而实际
# 87.5% 都是它），也会让「只剩一个判定词」的列看起来还有第二个取值。
_TEXT_MISSING_TOKENS = frozenset(
    {
        "n/a", "na", "n.a.", "#n/a", "null", "none", "nan", "-", "--", "?", "/",
        "未知", "空", "空白", "缺失", "未填", "未填写", "不适用", "未提供",
        "待补充", "无数据", "没有数据",
    }
)


def _token_kind(token: str) -> Optional[str]:
    """'zero' / 'one' / None — by value first, then by vocabulary.

    ``00``, ``0.00``, ``1.0`` and ``1`` all mean the same thing; matching a
    fixed string list would treat them as four different dirty codes and the
    column would stop looking like a flag.
    """

    if token in _ZERO_TOKENS:
        return "zero"
    if token in _ONE_TOKENS:
        return "one"
    try:
        value = float(token)
    except ValueError:
        return None
    if value == 0.0:
        return "zero"
    if value == 1.0:
        return "one"
    return None


def _token_role(token: str) -> Optional[str]:
    """'zero' / 'one' / 'pass' / 'fail' / None for one normalised token."""

    kind = _token_kind(token)
    if kind is not None:
        return kind
    if token in _PASS_LEXICON:
        return "pass"
    if token in _FAIL_LEXICON:
        return "fail"
    # 长词里带判定词也算：「严重逾期」「审批拒绝」「不合格品」。只对长度 ≥2 的
    # 词做包含匹配，否则单字「差」会把「差异」当成分档。
    if len(token) >= 2:
        if any(len(word) >= 2 and word in token for word in _FAIL_LEXICON):
            return "fail"
        if any(len(word) >= 2 and word in token for word in _PASS_LEXICON):
            return "pass"
    return None


def normalize_missing(series: pd.Series) -> pd.Series:
    """Treat literal missing codes (``N/A``, ``-``, ``未知``) as missing.

    Leaving them as a category makes ``n_missing`` report 0 while most rows carry
    one, and makes a column that is really "50 个 1 + 350 个 N/A" look like a
    two-valued field whose composition can be described.
    """

    if is_numeric(series) or pd.api.types.is_bool_dtype(series):
        return series
    if is_datetime(series):
        return series
    try:
        tokens = series.astype(str).str.strip().str.lower()
    except (TypeError, ValueError):
        return series
    blank = tokens.isin(_TEXT_MISSING_TOKENS) | (tokens == "") | (tokens == "none")
    return series.where(~blank)


def is_datetime(series: pd.Series) -> bool:
    return bool(pd.api.types.is_datetime64_any_dtype(series))


def _is_flag_tokens(series: pd.Series) -> bool:
    """Zero/one encodings judged on raw tokens, before any numeric coercion.

    The discriminator is not "how many dirty codes" but **which profile the
    column will get**. A coercible column is described numerically — the mean of
    a 0..9 field is not a rate, so the numeric rules below decide. A column that
    cannot be coerced falls back to a categorical profile, and a categorical
    profile prints every category's count and share; once both sides of a binary
    outcome are present, one of those shares *is* the rate. No count threshold can
    be safe there, because the analyst just needs one more dirty code to cross it.
    """

    if pd.api.types.is_bool_dtype(series):
        return True
    series = normalize_missing(series)
    tokens = set(series.dropna().astype(str).str.strip().str.lower().unique())
    if not tokens:
        return False
    roles = {token: _token_role(token) for token in tokens}
    sides = set(roles.values())
    # 好坏两侧同时出现，这一点本身就足够定性，与第三、第四个取值无关：
    # 「good/bad/pending」里 bad 的占比仍是坏账率。
    if "pass" in sides and "fail" in sides:
        return True
    # 能整列转成数值的，交给数值判据（0..9 这种小基数分布是合法字段，
    # 均值不是比率）。这一步必须排在 0/1 判定之前：否则任何含 0 和 1 的
    # 数值列都会在这里被当成标记列。
    if _coerced_numeric(series) is not None:
        return False
    if "zero" in sides and "one" in sides:
        return True
    # 分类画像里剩下的全是判定词（哪怕只有一侧，哪怕只剩一个取值）：
    # top_values 里它的计数除以总条数就是比率。
    return all(role is not None for role in roles.values())


def _coerced_numeric(series: pd.Series) -> Optional[pd.Series]:
    try:
        return as_numeric(series)
    except ValueError:
        return None


def as_numeric(series: pd.Series) -> pd.Series:
    """Numbers stored as text are still numbers; junk becomes missing."""

    if is_numeric(series):
        return series.astype(float)
    coerced = pd.to_numeric(series, errors="coerce")
    observed = series.notna().sum()
    if observed and float(coerced.notna().sum()) / float(observed) >= _COERCE_SHARE:
        return coerced.astype(float)
    raise ValueError("column_not_numeric")


def quantile_edges(values: pd.Series, bins: int) -> tuple[List[float], str]:
    """Edges for the bin table, plus how they were chosen.

    Equal-frequency edges collapse on discrete fields: ten quantiles of an
    integer 0..15 field yield two distinct edges, and the "distribution" ends up
    as two bins. Such fields get one bin per observed value instead, which is
    also what a credit analyst expects to read.
    """

    clean = values.dropna()
    if clean.empty:
        return [], "empty"
    unique = np.unique(clean.to_numpy(dtype=float))
    if unique.size <= 1:
        single = float(unique[0]) if unique.size else 0.0
        return [single, single], "constant"
    if unique.size == 2:
        # 0/1 字段若直接用观测值作边界会只剩一个箱；用中点切成两段。
        low, high = float(unique[0]), float(unique[-1])
        return [low, (low + high) / 2.0, high], "binary"
    if unique.size <= max(bins * 2, 20):
        return [float(edge) for edge in unique], "discrete"
    edges = np.unique(np.nanquantile(clean.to_numpy(dtype=float), np.linspace(0.0, 1.0, bins + 1)))
    if edges.size >= 2:
        return [float(edge) for edge in edges], "quantile"
    return [float(unique[0]), float(unique[-1])], "range"


def bin_table(
    values: pd.Series, edges: Sequence[float], *, total: Optional[int] = None
) -> List[Dict[str, Any]]:
    """Counts per fixed bin, with explicit under/overflow rows.

    ``np.histogram`` drops values outside the edges. Silently dropping them and
    then dividing by the in-bin count inflates every remaining share: a period
    whose values moved past the base edges would look like it *shrank* into a
    narrower range. The out-of-range rows are therefore always present (count 0
    when empty), and every share is taken over ``total`` — the number of
    non-missing records — so shares always sum to 1.
    """

    if len(edges) < 2:
        return []
    clean = values.dropna().to_numpy(dtype=float)
    denominator = int(clean.size) if total is None else int(total)
    counts, _ = np.histogram(clean, bins=list(edges))
    under = int((clean < edges[0]).sum())
    over = int((clean > edges[-1]).sum())
    rows: List[Dict[str, Any]] = [
        {
            "kind": "underflow",
            "lower": None,
            "upper": float(edges[0]),
            "count": under,
            "share": _share(under, denominator),
            "cum_share": _share(under, denominator),
        }
    ]
    cumulative = under
    for index, count in enumerate(counts):
        cumulative += int(count)
        rows.append(
            {
                "kind": "bin",
                "lower": float(edges[index]),
                "upper": float(edges[index + 1]),
                "count": int(count),
                "share": _share(count, denominator),
                "cum_share": _share(cumulative, denominator),
            }
        )
    cumulative += over
    rows.append(
        {
            "kind": "overflow",
            "lower": float(edges[-1]),
            "upper": None,
            "count": over,
            "share": _share(over, denominator),
            "cum_share": _share(cumulative, denominator),
        }
    )
    return rows


def _share(count: float, total: float) -> Optional[float]:
    return (float(count) / float(total)) if total else None


def describe_series(
    series: pd.Series,
    *,
    column: str,
    bins: int = 10,
    edges: Optional[Sequence[float]] = None,
    top: int = 8,
    missing_as_zero: bool = False,
) -> Dict[str, Any]:
    """Profile one column: counts first, then location, then shape.

    ``missing_as_zero`` implements the declared caliber: when a field's blanks
    mean "no record" and the analyst counts no-record as zero, the blanks join
    the zero bucket and the location statistics change with them. When they do
    not, blanks leave the statistics but stay in the denominator.
    """

    series = normalize_missing(series)
    n_rows = int(len(series))
    n_missing = int(series.isna().sum())
    observed = series.dropna()
    n_effective = int(observed.size)
    profile: Dict[str, Any] = {
        "column": str(column),
        "n_rows": n_rows,
        "n_missing": n_missing,
        "missing_share": _share(n_missing, n_rows),
        "n_effective": n_effective,
        "distinct": int(observed.nunique()) if n_effective else 0,
    }
    try:
        numeric = as_numeric(series)
        kind = "numeric"
    except ValueError:
        numeric = None
        kind = "categorical"
    if missing_as_zero and kind != "numeric":
        # 分类列没有「0」这一档可并入；接受这个口径又不生效，等于让口径
        # 变成一句文案。宁可直接拒绝。
        raise ValueError("missing_as_zero_requires_numeric")
    profile["kind"] = kind

    if kind == "numeric" and numeric is not None:
        observed_values = numeric.dropna()
        n_zero_observed = int((observed_values == 0).sum())
        imputed = 0
        if missing_as_zero and n_missing:
            # 声明的口径是「缺失等价于 0」时，缺失真的进入 0 这一档，
            # 否则「口径是计算输入」就只是一句文案。
            imputed = n_missing
            values = numeric.fillna(0.0)
        else:
            values = observed_values
        profile["n_zero"] = int((values == 0).sum())
        profile["n_zero_observed"] = n_zero_observed
        profile["n_imputed_zero"] = imputed
        profile["n_effective"] = int(values.size)
        profile["zero_share"] = _share(profile["n_zero"], n_rows)
        profile["n_null_or_zero"] = int(n_missing + n_zero_observed)
        if values.empty:
            profile.update(
                {"mean": None, "std": None, "min": None, "max": None,
                 "quantiles": {}, "bins": [], "top_values": []}
            )
            return profile
        q = {f"p{int(round(level * 100)):02d}": float(values.quantile(level)) for level in QUANTILES}
        if edges is not None:
            used_edges, bin_mode = [float(edge) for edge in edges], "fixed"
        else:
            used_edges, bin_mode = quantile_edges(values, bins)
        profile.update(
            {
                "mean": float(values.mean()),
                "std": float(values.std(ddof=1)) if len(values) > 1 else None,
                "min": float(values.min()),
                "max": float(values.max()),
                "quantiles": q,
                "bins": bin_table(values, used_edges, total=int(values.size)),
                "bin_edges": [float(edge) for edge in used_edges],
                "bin_mode": bin_mode,
                "top_values": _top_counts(values),
            }
        )
        binary = _binary_block(values)
        if binary:
            # 0/1 字段的 mean 就是「1 的占比」。它和坏账率长得一模一样，
            # 所以必须把这一点写在数字旁边，并由调用方保证标签列进不来。
            profile["binary"] = binary
            profile["reading"] = (
                "0/1 字段：mean 等于取值为 1 的占比，不是风险水平；"
                "p01~p99 只会在 0 与 1 之间跳，不提供额外信息。"
            )
        # 零值含义为「无记录」时，含零与不含零的位置统计是两个答案，
        # 都摆出来，让读者自己选，而不是替他选。
        non_zero = values[values != 0]
        if int(profile["n_zero"]) > 0 and int(non_zero.size) > 0:
            profile["without_zero"] = {
                "n": int(non_zero.size),
                "mean": float(non_zero.mean()),
                "p50": float(non_zero.quantile(0.5)),
                "note": "剔除零值后的位置统计；零值是否属于该字段取决于口径",
            }
        return profile

    counts = observed.astype(str).value_counts()
    profile["top_values"] = [
        {"value": str(value), "count": int(count), "share": _share(count, n_rows)}
        for value, count in counts.head(top).items()
    ]
    profile["bins"] = []
    return profile


def _top_counts(values: pd.Series, top: int = 5) -> List[Dict[str, Any]]:
    counts = values.value_counts().head(top)
    total = int(values.size)
    return [
        {"value": float(value), "count": int(count), "share": _share(count, total)}
        for value, count in counts.items()
    ]


def _binary_block(values: pd.Series) -> Optional[Dict[str, Any]]:
    """Describe a 0/1 column as a share, so nobody reads ``mean`` as a rate."""

    unique = set(float(item) for item in np.unique(values.to_numpy(dtype=float)))
    if unique != {0.0, 1.0}:
        return None
    total = int(values.size)
    n_one = int((values == 1).sum())
    return {
        "n_one": n_one,
        "n_zero": int(total - n_one),
        "share_one": _share(n_one, total),
        "note": "该列只有 0/1 两个取值；share_one 就是取值为 1 的占比。",
    }


def _test(
    name: str,
    *,
    statistic: Optional[float],
    p_value: Optional[float],
    tests_what: str,
    applicability: str,
    allowed_conclusions: Sequence[str],
    note: Optional[str] = None,
    df: Optional[int] = None,
) -> Dict[str, Any]:
    record: Dict[str, Any] = {
        "test": name,
        "statistic": statistic,
        "p_value": p_value,
        "df": df,
        "tests_what": tests_what,
        "applicability": applicability,
        "allowed_conclusions": list(allowed_conclusions),
    }
    if note:
        record["note"] = note
    return record


def chi2_bins(
    base_counts: Sequence[int],
    comp_counts: Sequence[int],
    alpha: float = 0.05,
    *,
    spec_overrides: Optional[Dict[str, Any]] = None,
    name: str = "chi2_bins",
) -> Dict[str, Any]:
    """Did the composition across bins change?"""

    base = np.asarray(base_counts, dtype=float)
    comp = np.asarray(comp_counts, dtype=float)
    keep = (base + comp) > 0
    base, comp = base[keep], comp[keep]
    spec = {
        "tests_what": "各分箱的占比构成是否变化（结构差异，不看均值）",
        "applicability": "两期均为独立样本；每箱两期期望频数 ≥ 5",
        "allowed_conclusions": ["composition_change"],
    }
    if spec_overrides:
        spec.update(spec_overrides)
    if base.size < 2 or base.sum() == 0 or comp.sum() == 0:
        return _test(name, statistic=None, p_value=None, note="分箱不足，无法做构成检验", **spec)
    total = base.sum() + comp.sum()
    expected_base = (base + comp) * (base.sum() / total)
    expected_comp = (base + comp) * (comp.sum() / total)
    if (expected_base < 5).any() or (expected_comp < 5).any():
        return _test(
            name, statistic=None, p_value=None,
            note="存在期望频数 < 5 的分箱：构成检验不可靠，请合并分箱或改用描述性对比",
            **spec,
        )
    statistic = float(((base - expected_base) ** 2 / expected_base).sum() + ((comp - expected_comp) ** 2 / expected_comp).sum())
    df = int(base.size - 1)
    if _st is None:
        return _test(name, statistic=statistic, p_value=None, df=df, note="scipy 未安装，只给出统计量", **spec)
    p_value = float(_st.chi2.sf(statistic, df))
    return _test(name, statistic=statistic, p_value=p_value, df=df, **spec)


def ks_two_sample(base: pd.Series, comp: pd.Series) -> Dict[str, Any]:
    """Did the whole distribution move (any difference, any direction)?"""

    spec = {
        "tests_what": "两期整体分布是否相同（对位置、尺度、形状任一差异敏感）",
        "applicability": "连续型数值；样本量 ≥ 50 时渐近 p 值可靠",
        "allowed_conclusions": ["distribution_shift"],
    }
    a = base.dropna().to_numpy(dtype=float)
    b = comp.dropna().to_numpy(dtype=float)
    if a.size < 2 or b.size < 2:
        return _test("ks_2samp", statistic=None, p_value=None, note="样本量不足", **spec)
    if _st is None:
        return _test("ks_2samp", statistic=None, p_value=None, note="scipy 未安装", **spec)
    result = _st.ks_2samp(a, b)
    return _test(
        "ks_2samp", statistic=float(result.statistic), p_value=float(result.pvalue),
        note=None if min(a.size, b.size) >= 50 else "小样本，p 值仅供参考",
        **spec,
    )


def mann_whitney(base: pd.Series, comp: pd.Series) -> Dict[str, Any]:
    """Did the rank position (typical value) move, robust to outliers?"""

    spec = {
        "tests_what": "两期的秩位置是否平移（中位数附近的整体移动，抗极端值）",
        "applicability": "连续型数值；不要求正态，但要求分布形状相近才能解释为位置移动",
        "allowed_conclusions": ["level_shift"],
    }
    a = base.dropna().to_numpy(dtype=float)
    b = comp.dropna().to_numpy(dtype=float)
    if a.size < 2 or b.size < 2:
        return _test("mann_whitney", statistic=None, p_value=None, note="样本量不足", **spec)
    if _st is None:
        return _test("mann_whitney", statistic=None, p_value=None, note="scipy 未安装", **spec)
    result = _st.mannwhitneyu(a, b, alternative="two-sided")
    return _test("mann_whitney", statistic=float(result.statistic), p_value=float(result.pvalue), **spec)


def welch_t(base: pd.Series, comp: pd.Series) -> Dict[str, Any]:
    """Did the mean move? Reported because readers ask for it, not because it decides."""

    spec = {
        "tests_what": "两期均值是否不同（对厚尾与极端值敏感）",
        "applicability": "近似正态或样本量较大；厚尾时结论可能由少数大额记录驱动",
        "allowed_conclusions": ["mean_shift"],
    }
    a = base.dropna().to_numpy(dtype=float)
    b = comp.dropna().to_numpy(dtype=float)
    if a.size < 3 or b.size < 3:
        return _test("welch_t", statistic=None, p_value=None, note="样本量不足", **spec)
    if _st is None:
        return _test("welch_t", statistic=None, p_value=None, note="scipy 未安装", **spec)
    result = _st.ttest_ind(a, b, equal_var=False)
    if not math.isfinite(float(result.statistic)):
        return _test("welch_t", statistic=None, p_value=None, note="方差为 0，均值检验无定义", **spec)
    return _test("welch_t", statistic=float(result.statistic), p_value=float(result.pvalue), **spec)


def compare_series(
    base: pd.Series,
    comp: pd.Series,
    *,
    column: str,
    bins: int = 10,
    alpha: float = 0.05,
    edges: Optional[Sequence[float]] = None,
    missing_as_zero: bool = False,
) -> Dict[str, Any]:
    """Two periods, one column: composition, location, and four named tests.

    ``edges`` must be the edges the describe run already published, and they are
    imposed on *both* periods. Recomputing them for the base period (even with
    the same ``bins``) makes the base bins and the comp bins different
    intervals, and every ``share_delta_pp`` then compares two different ranges.

    ``missing_as_zero`` is applied to the tests as well as to the profile. A
    declared caliber that moves the mean but leaves the tests on the raw
    non-missing sample makes one report quote two populations.
    """

    fixed_edges = [float(edge) for edge in edges] if edges is not None else None
    # 与描述工具同一套缺失码归一：否则「N/A」在两期里是两种东西——描述时算缺失、
    # 比较时算一档，两边的占比对不上。
    base = normalize_missing(base)
    comp = normalize_missing(comp)
    base_profile = describe_series(
        base, column=column, bins=bins, edges=fixed_edges, missing_as_zero=missing_as_zero
    )
    used_edges = fixed_edges if fixed_edges is not None else base_profile.get("bin_edges")
    comp_profile = describe_series(
        comp, column=column, bins=bins, edges=used_edges, missing_as_zero=missing_as_zero
    )
    numeric_kind = base_profile["kind"] == "numeric"
    test_base = _materialize(base, missing_as_zero, numeric_kind=numeric_kind)
    test_comp = _materialize(comp, missing_as_zero, numeric_kind=numeric_kind)
    result: Dict[str, Any] = {
        "column": str(column),
        "kind": base_profile["kind"],
        "base": base_profile,
        "comp": comp_profile,
        "alpha": float(alpha),
        "tests": [],
    }
    result["missing_share_delta"] = _delta(base_profile.get("missing_share"), comp_profile.get("missing_share"))
    if base_profile["kind"] == "numeric":
        base_bins = base_profile.get("bins") or []
        comp_bins = comp_profile.get("bins") or []
        if len(base_bins) != len(comp_bins):
            # 两期箱体对不齐时宁可不出表：zip 会把第 3 个箱和第 4 个箱配对，
            # 数字看起来正常，区间却是错的。
            result["binning_note"] = (
                f"两期分箱行数不一致（{len(base_bins)} vs {len(comp_bins)}）：未输出分箱对比表"
            )
            base_bins, comp_bins = [], []
        rows = []
        for left, right in zip(base_bins, comp_bins):
            share_delta = _delta(left.get("share"), right.get("share"))
            rows.append(
                {
                    "kind": left.get("kind", "bin"),
                    "lower": left.get("lower"),
                    "upper": left.get("upper"),
                    "count_base": left["count"],
                    "count_comp": right["count"],
                    "share_base": left.get("share"),
                    "share_comp": right.get("share"),
                    "share_delta_pp": None if share_delta is None else share_delta * 100.0,
                    "count_delta": int(right["count"] - left["count"]),
                    "cum_share_base": left.get("cum_share"),
                    "cum_share_comp": right.get("cum_share"),
                }
            )
        result["bins"] = rows
        result["mean_delta"] = _delta(base_profile.get("mean"), comp_profile.get("mean"))
        result["median_delta"] = _delta(
            (base_profile.get("quantiles") or {}).get("p50"),
            (comp_profile.get("quantiles") or {}).get("p50"),
        )
        result["quantile_deltas"] = {
            key: _delta(
                (base_profile.get("quantiles") or {}).get(key),
                (comp_profile.get("quantiles") or {}).get(key),
            )
            for key in (base_profile.get("quantiles") or {})
        }
        result["total_variation_distance"] = _tvd(base_bins, comp_bins)
        result["zero_share_delta"] = _delta(base_profile.get("zero_share"), comp_profile.get("zero_share"))
        result["tests"] = [
            chi2_bins([row["count_base"] for row in rows], [row["count_comp"] for row in rows], alpha),
            ks_two_sample(test_base, test_comp),
            mann_whitney(test_base, test_comp),
            welch_t(test_base, test_comp),
        ]
    else:
        result["tests"] = _categorical_tests(base, comp, alpha)
        result["category_deltas"] = _category_deltas(base, comp)
        result["total_variation_distance"] = _tvd_categories(base, comp)
    for test in result["tests"]:
        test["significant"] = bool(
            test.get("p_value") is not None and float(test["p_value"]) < alpha
        )
        test["role"] = "exploratory"
    return result


def _materialize(series: pd.Series, missing_as_zero: bool, *, numeric_kind: bool = False) -> pd.Series:
    """The series the tests should see, after the declared caliber is applied.

    When the profile is numeric, the tests must see the same numbers the profile
    was computed from — numbers stored as text (``"0"``/``"1"`` with a few dirty
    codes) are numeric for the profile and would otherwise reach the tests as
    strings.
    """

    if numeric_kind:
        try:
            values = as_numeric(series)
        except ValueError:
            values = None
        if values is not None:
            return values.fillna(0.0) if missing_as_zero else values
    if not missing_as_zero:
        return series
    if not is_numeric(series):
        return series
    return series.astype(float).fillna(0.0)


def _categorical_tests(
    base: pd.Series,
    comp: pd.Series,
    alpha: float = 0.05,
) -> List[Dict[str, Any]]:
    spec = {
        "tests_what": "各类别占比构成是否变化（缺失作为独立类别参与）",
        "applicability": "两期独立样本；每类期望频数 ≥ 5",
        "allowed_conclusions": ["composition_change"],
    }
    base_counts = _category_counts(base)
    comp_counts = _category_counts(comp)
    # 并集，而不是交集：一个类别在一期消失本身就是构成变化的最大信号。
    labels = sorted(set(base_counts) | set(comp_counts))
    if len(labels) < 2:
        return [
            _test(
                "chi2_categories",
                statistic=None,
                p_value=None,
                note="可比类别不足 2 个",
                **spec,
            )
        ]
    base_vec = [base_counts.get(label, 0) for label in labels]
    comp_vec = [comp_counts.get(label, 0) for label in labels]
    return [
        chi2_bins(base_vec, comp_vec, alpha, spec_overrides=spec, name="chi2_categories")
    ]


# 缺失作为一个显式类别参与构成比较：它的占比变化本身就是构成变化，
# 而且只有把它算进去，各类别占比之和才等于 1。
_MISSING_KEY = "\x00missing"
_MISSING_CATEGORY = "<缺失>"


def _category_counts(series: pd.Series) -> Dict[str, int]:
    filled = series.where(series.notna(), _MISSING_KEY)
    counts = filled.astype(str).value_counts()
    return {str(label): int(count) for label, count in counts.items()}


def _category_deltas(base: pd.Series, comp: pd.Series) -> List[Dict[str, Any]]:
    base_counts = _category_counts(base)
    comp_counts = _category_counts(comp)
    # 分母是全部记录（含缺失），与 describe 的 top_values / missing_share 同一口径。
    n_base = int(base.size)
    n_comp = int(comp.size)
    rows = []
    for value in sorted(set(base_counts) | set(comp_counts)):
        left_n = base_counts.get(value, 0)
        right_n = comp_counts.get(value, 0)
        share_base = _share(left_n, n_base)
        share_comp = _share(right_n, n_comp)
        delta = _delta(share_base, share_comp)
        row = {
            "value": _MISSING_CATEGORY if value == _MISSING_KEY else value,
            "count_base": left_n,
            "count_comp": right_n,
            "share_base": share_base,
            "share_comp": share_comp,
            "share_delta_pp": None if delta is None else delta * 100.0,
        }
        if value == _MISSING_KEY:
            row["is_missing"] = True
        rows.append(row)
    rows.sort(key=lambda row: -abs(row["share_delta_pp"] or 0.0))
    return rows


def _tvd(base_bins: Sequence[Dict[str, Any]], comp_bins: Sequence[Dict[str, Any]]) -> Optional[float]:
    if not base_bins or not comp_bins:
        return None
    total = 0.0
    for left, right in zip(base_bins, comp_bins):
        total += abs(float(left.get("share") or 0.0) - float(right.get("share") or 0.0))
    return total / 2.0


def _tvd_categories(base: pd.Series, comp: pd.Series) -> Optional[float]:
    rows = _category_deltas(base, comp)
    if not rows:
        return None
    return sum(abs(row["share_delta_pp"] or 0.0) for row in rows) / 200.0


def _delta(left: Any, right: Any) -> Optional[float]:
    if left is None or right is None:
        return None
    try:
        return float(right) - float(left)
    except (TypeError, ValueError):
        return None
