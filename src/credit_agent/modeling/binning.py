"""Numeric binning: quantile, ChiMerge (chi-square merge), monotonic bad-rate pools.

ChiMerge 是评分卡的标准连续变量分箱算法（Kerber 1992）：先把变量等频预切成较细的
区间，再反复合并「相邻两箱卡方值最小」的一对，直到满足箱数上限；样本量不足的箱
必须先与最相似的邻箱合并。本模块给出的是完整实现，不是简化版：

1. 预分箱数可配置（``max_prebins``，默认 20）。
2. 相邻两箱用标准 2×2 列联表卡方统计量（好坏双列都计入），而非只比较坏样本列。
3. 最小箱样本量的分母是**该特征有值样本数**，缺失独立成箱、不参与箱体大小约束。
4. 每次合并都写入 ``merge_log``，分箱过程可回放、可审阅。
5. 同时输出逐箱样本/坏率/WOE/IV 贡献/分箱 KS/单调性，供交付报告直接引用。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

BinningMethod = Literal["quantile", "chi2", "monotonic"]
MonotonicDirection = Literal["increasing", "decreasing"]

MISSING_BIN = "MISSING"

_CHIMERGE_ALIASES = ("chi2", "chimerge", "chi_merge", "chisq")


# --------------------------------------------------------------------------- #
# 卡方统计量
# --------------------------------------------------------------------------- #
def _two_bin_chi2(n1: float, b1: float, n2: float, b2: float) -> float:
    """相邻两箱的 2×2 列联表卡方值（好/坏两列都计入）。

    两箱合计只有单一类别（全好或全坏）时无法比较分布差异，返回 ``inf``
    使其排在合并候选的最后。
    """

    total = n1 + n2
    if total <= 0:
        return float("inf")
    bad = b1 + b2
    good = (n1 - b1) + (n2 - b2)
    if bad <= 0 or good <= 0:
        # 两箱合计只有单一类别：两个箱的分布完全相同，属于最应该合并的一对。
        return 0.0
    value = 0.0
    for count, bad_in_bin in ((n1, b1), (n2, b2)):
        if count <= 0:
            continue
        for observed, column_total in ((bad_in_bin, bad), (count - bad_in_bin, good)):
            expected = count * column_total / total
            if expected > 0:
                value += (observed - expected) ** 2 / expected
    return float(value)


# --------------------------------------------------------------------------- #
# 分箱结果
# --------------------------------------------------------------------------- #
@dataclass
class ChiMergeResult:
    """一次 ChiMerge 的完整留痕。"""

    edges: List[float]
    bins: List[Dict[str, Any]] = field(default_factory=list)
    merge_log: List[Dict[str, Any]] = field(default_factory=list)
    prebins: int = 0
    n_valid: int = 0
    min_bin_size: int = 1
    min_bin_fraction: float = 0.05
    iv: Optional[float] = None
    ks: Optional[float] = None
    monotonic: Optional[bool] = None
    monotonic_direction: Optional[str] = None
    monotonic_strength: Optional[float] = None
    min_bin_violations: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "edges": [float(e) for e in self.edges],
            "bins": self.bins,
            "merge_log": self.merge_log,
            "prebins": int(self.prebins),
            "n_valid": int(self.n_valid),
            "min_bin_size": int(self.min_bin_size),
            "min_bin_fraction": float(self.min_bin_fraction),
            "min_bin_fraction_scope": "valid_only",
            "min_bin_violations": self.min_bin_violations,
            "iv": self.iv,
            "ks": self.ks,
            "monotonic": self.monotonic,
            "monotonic_direction": self.monotonic_direction,
            "monotonic_strength": self.monotonic_strength,
            "n_bins": len(self.bins),
        }


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def _quantile_edges(xv: np.ndarray, max_bins: int) -> List[float]:
    if len(xv) < 2:
        return [-np.inf, np.inf]
    try:
        _, edges = pd.qcut(xv, min(max_bins, max(2, len(np.unique(xv)))), duplicates="drop", retbins=True)
        return [float(edges[0]) - 1e-9] + [float(v) for v in edges[1:-1]] + [float(edges[-1]) + 1e-9]
    except ValueError:
        return [-np.inf, np.inf]


def _prebin_edges(xv: np.ndarray, n_pre: int) -> Optional[np.ndarray]:
    n_unique = len(np.unique(xv))
    if n_unique < 2:
        return None
    try:
        _, edges = pd.qcut(xv, min(n_pre, n_unique), duplicates="drop", retbins=True)
    except ValueError:
        return None
    edges = np.asarray(edges, dtype=float)
    if len(edges) < 3:
        return None
    edges[0], edges[-1] = -np.inf, np.inf
    return edges


def _monotonic_strength(rates: Sequence[float]) -> float:
    """坏率与箱序的秩相关绝对值：1 表示完全单调。"""

    if len(rates) < 3:
        return 1.0 if len(rates) == 2 and rates[0] != rates[1] else 0.0
    rank_rate = pd.Series(rates).rank()
    rank_idx = pd.Series(np.arange(len(rates))).rank()
    if rank_rate.std() == 0 or rank_idx.std() == 0:
        return 0.0
    return float(abs(np.corrcoef(rank_rate, rank_idx)[0, 1]))


def _direction(rates: Sequence[float]) -> Optional[str]:
    if len(rates) < 2:
        return None
    if rates[-1] >= rates[0]:
        return "increasing"
    return "decreasing"


def is_monotonic(rates: Sequence[float], direction: MonotonicDirection) -> bool:
    if len(rates) <= 1:
        return True
    for i in range(len(rates) - 1):
        if direction == "increasing" and rates[i + 1] + 1e-12 < rates[i]:
            return False
        if direction == "decreasing" and rates[i + 1] > rates[i] + 1e-12:
            return False
    return True


_is_monotonic = is_monotonic  # 向后兼容旧调用名


def bin_table(
    x: pd.Series,
    y: pd.Series,
    edges: Sequence[float],
    *,
    missing_label: Optional[str] = MISSING_BIN,
) -> List[Dict[str, Any]]:
    """逐箱样本、坏率、WOE、IV 贡献与占比。缺失单独成箱并排在最后。

    WOE 的分母（全样本坏/好总数）必须包含缺失箱，否则缺失箱的占比会被算到
    100% 以上，IV 被系统性放大。这里一次性把全部箱（含缺失箱）交给
    ``_woe_iv_from_counts``，确保 WOE 口径与训练时完全一致。
    """

    from .woe import _woe_iv_from_counts

    num = pd.to_numeric(x, errors="coerce")
    yv = pd.to_numeric(y, errors="coerce")
    labeled = yv.notna()
    if not bool(labeled.any()):
        return []
    valid = labeled & num.notna()
    binned = pd.cut(num[valid], bins=list(edges), include_lowest=True, duplicates="drop")
    grouped = (
        pd.DataFrame({"bin": binned, "y": yv[valid].astype(float)})
        .groupby("bin", observed=True)["y"]
        .agg(["sum", "count"])
    )
    names: List[str] = []
    bads: List[float] = []
    goods: List[float] = []
    flags: List[bool] = []
    for idx, row in grouped.iterrows():
        names.append(str(idx))
        bads.append(float(row["sum"]))
        goods.append(float(row["count"] - row["sum"]))
        flags.append(False)
    if missing_label:
        miss = labeled & num.isna()
        miss_n = int(miss.sum())
        if miss_n > 0:
            miss_bad = float(yv[miss].sum())
            names.append(str(missing_label))
            bads.append(miss_bad)
            goods.append(float(miss_n) - miss_bad)
            flags.append(True)

    bad_arr = np.asarray(bads, dtype=float)
    good_arr = np.asarray(goods, dtype=float)
    woe_arr, _ = _woe_iv_from_counts(bad_arr, good_arr)
    total_bad = max(float(bad_arr.sum()), 0.5)
    total_good = max(float(good_arr.sum()), 0.5)
    total_n = float((bad_arr + good_arr).sum())

    out: List[Dict[str, Any]] = []
    for i, name in enumerate(names):
        bad_share = (bad_arr[i] + 0.5) / total_bad
        good_share = (good_arr[i] + 0.5) / total_good
        count = float(bad_arr[i] + good_arr[i])
        out.append(
            {
                "bin": name,
                "count": int(count),
                "bad": int(bad_arr[i]),
                "good": int(good_arr[i]),
                # 旧字段名保留：历史交付脚本与产物读取 bad_cnt / good_cnt。
                "bad_cnt": int(bad_arr[i]),
                "good_cnt": int(good_arr[i]),
                "bad_rate": round(float(bad_arr[i] / count) if count else 0.0, 6),
                "woe": round(float(woe_arr[i]), 6),
                "iv_part": round(float((bad_share - good_share) * np.log(bad_share / good_share)), 6),
                "is_missing": bool(flags[i]),
                "n_share": round(count / total_n, 6) if total_n else 0.0,
            }
        )
    iv_total = sum(float(r["iv_part"]) for r in out) or 0.0
    for r in out:
        r["iv_share"] = round(float(r["iv_part"]) / iv_total, 6) if iv_total > 0 else 0.0
    return out


def binned_ks(bins: Sequence[Dict[str, Any]]) -> Optional[float]:
    """按箱计算 KS（累计坏占比 vs 累计好占比的最大差）。"""

    bads = [float(b.get("bad") or 0) for b in bins]
    goods = [float(b.get("good") or 0) for b in bins]
    if not bads or sum(bads) <= 0 or sum(goods) <= 0:
        return None
    # 按 WOE 升序（风险从低到高）排序
    order = sorted(range(len(bins)), key=lambda i: float(bins[i].get("woe") or 0.0))
    total_bad = sum(bads)
    total_good = sum(goods)
    cum_bad = cum_good = 0.0
    ks = 0.0
    for i in order:
        cum_bad += bads[i] / total_bad
        cum_good += goods[i] / total_good
        ks = max(ks, abs(cum_bad - cum_good))
    return float(ks)


def mapped_ks(
    x: pd.Series,
    y: pd.Series,
    edges: Sequence[float],
    bins: Sequence[Dict[str, Any]],
) -> Optional[float]:
    """把每行按箱映射成风险分（WOE 正向：WOE 越高风险越高）后计算行级 KS。

    按箱累计算出的 KS 受箱粒度限制，会系统性低于行级 KS；评分卡审阅用的是行级
    口径，这里保持一致，避免「分箱 KS」与「模型 KS」两套数打架。
    """

    from .metrics import ks_score

    lookup = {str(b.get("bin")): float(b.get("woe") or 0.0) for b in bins}
    if not lookup or len(edges) < 2:
        return None
    num = pd.to_numeric(x, errors="coerce")
    yv = pd.to_numeric(y, errors="coerce")
    binned = pd.cut(num, bins=list(edges), include_lowest=True, duplicates="drop")
    labels = binned.astype(str)
    if any(b.get("is_missing") for b in bins):
        labels = labels.mask(binned.isna(), MISSING_BIN)
    # WOE 在本库里是 log(坏占比 / 好占比)：值越大风险越高，直接当风险分用。
    score = labels.map(lambda s: lookup.get(s, 0.0)).astype(float).to_numpy()
    keep = yv.notna().to_numpy()
    if int(keep.sum()) < 10:
        return None
    ks, _ = ks_score(yv[keep].to_numpy(), score[keep])
    return float(ks) if np.isfinite(ks) else None


def summarize_bins(
    bins: Sequence[Dict[str, Any]],
    *,
    ks: Optional[float] = None,
) -> Dict[str, Any]:
    """由逐箱明细汇总 IV / KS / 单调性，供任意分箱方法共用。"""

    non_missing = [b for b in bins if not b.get("is_missing")]
    rates = [float(b["bad_rate"]) for b in non_missing]
    direction = _direction(rates)
    binned = binned_ks(bins)
    return {
        "iv": round(float(sum(float(b.get("iv_part") or 0.0) for b in bins)), 6),
        "ks": ks if ks is not None else binned,
        "ks_binned": binned,
        "monotonic": is_monotonic(rates, direction) if direction else None,
        "monotonic_direction": direction,
        "monotonic_strength": round(_monotonic_strength(rates), 6),
    }


# --------------------------------------------------------------------------- #
# ChiMerge 主实现
# --------------------------------------------------------------------------- #
def chimerge_profile(
    x: pd.Series,
    y: pd.Series,
    *,
    max_bins: int = 6,
    min_bin_fraction: float = 0.05,
    max_prebins: int = 20,
    missing_label: Optional[str] = MISSING_BIN,
) -> Optional[ChiMergeResult]:
    """规范 ChiMerge，返回分箱边界与可审阅的合并过程。"""

    num = pd.to_numeric(x, errors="coerce")
    yv = pd.to_numeric(y, errors="coerce")
    valid = num.notna() & yv.notna()
    xv = num[valid].astype(float)
    target = yv[valid].astype(float)
    n = len(xv)
    if n < 30 or target.nunique() < 2:
        return None
    max_bins = max(2, int(max_bins))
    n_pre = int(min(max(int(max_prebins), max_bins), max(n, 2)))
    pre = _prebin_edges(xv.values, n_pre)
    if pre is None:
        return None
    n_pre_actual = len(pre) - 1
    idx = np.clip(np.digitize(xv.values, pre[1:-1]), 0, n_pre_actual - 1)
    yv_arr = target.values
    min_size = max(1, int(round(n * float(min_bin_fraction))))

    groups: List[List[int]] = [[i] for i in range(n_pre_actual)]
    merge_log: List[Dict[str, Any]] = []

    def pair_stats(a: List[int], b: List[int]) -> Tuple[float, float, float, float, float]:
        mask_a = np.isin(idx, a)
        mask_b = np.isin(idx, b)
        n_a = float(mask_a.sum())
        n_b = float(mask_b.sum())
        b_a = float(yv_arr[mask_a].sum())
        b_b = float(yv_arr[mask_b].sum())
        return n_a, b_a, n_b, b_b, _two_bin_chi2(n_a, b_a, n_b, b_b)

    def group_count(g: List[int]) -> float:
        return float(np.isin(idx, g).sum())

    # 阶段 1：样本量不足的箱必须优先合并，否则小样本箱的 WOE 会剧烈跳动。
    while len(groups) > 2:
        sizes = [group_count(g) for g in groups]
        small = [i for i, size in enumerate(sizes) if size < min_size]
        if not small:
            break
        i = min(small, key=lambda k: sizes[k])
        cands: List[Tuple[int, float]] = []
        if i > 0:
            cands.append((i - 1, pair_stats(groups[i], groups[i - 1])[4]))
        if i < len(groups) - 1:
            cands.append((i + 1, pair_stats(groups[i], groups[i + 1])[4]))
        if not cands:
            break
        j = min(cands, key=lambda t: t[1])[0]
        lo, hi = min(i, j), max(i, j)
        chi = dict(cands)[j]
        merge_log.append(
            {
                "stage": "min_bin_size",
                "left": _edge_text(pre, groups[lo]),
                "right": _edge_text(pre, groups[hi]),
                "chi2": round(float(chi), 6) if np.isfinite(chi) else None,
                "bins_after": len(groups) - 1,
            }
        )
        groups[lo] = groups[lo] + groups[hi]
        groups.pop(hi)

    # 阶段 2：合并卡方值最小（分布最接近）的相邻对，直到箱数达标。
    while len(groups) > max_bins:
        values = [pair_stats(groups[i], groups[i + 1])[4] for i in range(len(groups) - 1)]
        finite = [v for v in values if np.isfinite(v)]
        pos = int(np.argmin(values)) if finite else 0
        chi = values[pos]
        merge_log.append(
            {
                "stage": "max_bins",
                "left": _edge_text(pre, groups[pos]),
                "right": _edge_text(pre, groups[pos + 1]),
                "chi2": round(float(chi), 6) if np.isfinite(chi) else None,
                "bins_after": len(groups) - 1,
            }
        )
        groups[pos] = groups[pos] + groups[pos + 1]
        groups.pop(pos + 1)

    cuts = sorted({float(pre[g[-1] + 1]) for g in groups[:-1]})
    edges = [-np.inf] + [c for c in cuts if np.isfinite(c)] + [np.inf]
    bins = bin_table(num, yv, edges, missing_label=missing_label)
    if not bins:
        return None

    non_missing = [b for b in bins if not b.get("is_missing")]
    summary = summarize_bins(bins, ks=mapped_ks(x, yv, edges, bins))
    result = ChiMergeResult(
        edges=edges,
        bins=bins,
        merge_log=merge_log,
        prebins=n_pre_actual,
        n_valid=n,
        min_bin_size=min_size,
        min_bin_fraction=float(min_bin_fraction),
        iv=summary["iv"],
        ks=summary["ks"],
        monotonic=summary["monotonic"],
        monotonic_direction=summary["monotonic_direction"],
        monotonic_strength=summary["monotonic_strength"],
        min_bin_violations=[
            {"bin": b["bin"], "count": b["count"]} for b in non_missing if b["count"] < min_size
        ],
    )
    return result


def _edge_text(pre: np.ndarray, group: List[int]) -> str:
    lo = pre[group[0]]
    hi = pre[group[-1] + 1]
    left = "-inf" if not np.isfinite(lo) else f"{lo:.6g}"
    right = "inf" if not np.isfinite(hi) else f"{hi:.6g}"
    return f"({left}, {right}]"


# --------------------------------------------------------------------------- #
# 对外接口
# --------------------------------------------------------------------------- #
def chi2_merge_edges(
    x: pd.Series,
    y: pd.Series,
    *,
    max_bins: int = 10,
    min_bin_fraction: float = 0.05,
    max_prebins: int = 20,
    missing_label: Optional[str] = None,
) -> List[float]:
    """ChiMerge 分箱边界（向后兼容的薄封装）。"""

    result = chimerge_profile(
        x,
        y,
        max_bins=max_bins,
        min_bin_fraction=min_bin_fraction,
        max_prebins=max_prebins,
        missing_label=missing_label,
    )
    if result is None:
        mask = x.notna() & y.notna()
        return _quantile_edges(pd.to_numeric(x[mask], errors="coerce").values, max_bins)
    return result.edges


def _bin_bad_rates(xv: pd.Series, yv: pd.Series, edges: List[float]) -> List[float]:
    binned = pd.cut(xv, bins=edges, include_lowest=True, duplicates="drop")
    order = binned.cat.categories if hasattr(binned, "cat") else sorted(binned.unique())
    out: List[float] = []
    for cat in order:
        m = binned == cat
        if not m.any():
            continue
        out.append(float(yv[m].sum()) / float(m.sum()))
    return out


def monotonic_merge_edges(
    x: pd.Series,
    y: pd.Series,
    *,
    max_bins: int = 10,
    direction: MonotonicDirection = "increasing",
    min_bin_fraction: float = 0.05,
) -> List[float]:
    mask = x.notna() & y.notna()
    xv = x[mask].astype(float)
    yv = y[mask].astype(float)
    n = len(xv)
    if n < 30:
        return _quantile_edges(xv.values, max_bins)

    n_init = min(max(max_bins * 2, 5), 30)
    edges = _quantile_edges(xv.values, n_init)
    min_count = max(1, int(n * min_bin_fraction))

    for _ in range(100):
        rates = _bin_bad_rates(xv, yv, edges)
        if len(rates) <= max_bins and _is_monotonic(rates, direction):
            break
        if len(edges) <= 2:
            break
        merge_at = None
        for i in range(len(rates) - 1):
            if direction == "increasing" and rates[i + 1] < rates[i] - 1e-12:
                merge_at = i
                break
            if direction == "decreasing" and rates[i + 1] > rates[i] + 1e-12:
                merge_at = i
                break
        if merge_at is None:
            if len(rates) <= max_bins:
                break
            merge_at = int(np.argmin([abs(rates[i + 1] - rates[i]) for i in range(len(rates) - 1)]))
        if merge_at + 1 >= len(edges) - 1:
            break
        new_edges = edges[: merge_at + 1] + edges[merge_at + 2 :]
        binned = pd.cut(xv, bins=new_edges, include_lowest=True, duplicates="drop")
        if binned.value_counts().min() < min_count and len(new_edges) > 2:
            break
        edges = new_edges

    return edges


def normalize_method(method: Optional[str]) -> BinningMethod:
    """把外部传入的分箱方法名归一化。"""

    name = str(method or "quantile").strip().lower()
    if name in _CHIMERGE_ALIASES:
        return "chi2"
    if name in ("quantile", "chi2", "monotonic"):
        return name  # type: ignore[return-value]
    raise ValueError(f"不支持的分箱方法 {method}，可选 quantile / chi2(chimerge) / monotonic")


def numeric_bin_edges(
    x: pd.Series,
    y: pd.Series,
    *,
    method: BinningMethod = "quantile",
    max_bins: int = 10,
    min_bin_fraction: float = 0.05,
    monotonic_direction: MonotonicDirection = "increasing",
    max_prebins: int = 20,
) -> List[float]:
    method = normalize_method(method)
    if method == "chi2":
        return chi2_merge_edges(
            x, y, max_bins=max_bins, min_bin_fraction=min_bin_fraction, max_prebins=max_prebins
        )
    if method == "monotonic":
        return monotonic_merge_edges(
            x,
            y,
            max_bins=max_bins,
            direction=monotonic_direction,
            min_bin_fraction=min_bin_fraction,
        )
    mask = x.notna() & y.notna()
    return _quantile_edges(x[mask].astype(float).values, max_bins)


def woe_iv_from_edges(
    x: pd.Series,
    y: pd.Series,
    edges: List[float],
    *,
    missing_label: Optional[str] = None,
) -> Tuple[Optional[float], List[Dict[str, Any]]]:
    """由给定边界算逐箱 WOE/IV。缺失独立成箱并计入 IV。"""

    rows = bin_table(x, y, edges, missing_label=missing_label)
    if not rows:
        return None, []
    iv = float(sum(float(r["iv_part"]) for r in rows))
    return iv, rows
