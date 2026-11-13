"""Configurable score-band measures: a two-score grid, marginal bands, and a swap table.

Bin edges and thresholds are always supplied by the caller. Every bad rate is
the observed flag rate on rows that already have a bad flag. Rejected
applicants are not assigned an inferred bad rate, and nothing here prices a
cutoff or writes a file.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from .columns import as_flag
from .stats import safe_rate


def _flag(series: pd.Series) -> pd.Series:
    """Bad or approval flag.

    ``as_flag`` reads 0/1 and the usual text labels. A numeric column with
    missing outcomes can fall through that helper and then fail to match the
    ``"1.0"`` text, so when it recognizes nothing and the values are numeric,
    keep the numeric 0/1 reading and leave missing rows missing.
    """
    parsed = as_flag(series)
    numeric = pd.to_numeric(series, errors="coerce")
    if not bool(parsed.notna().any()) and bool(numeric.notna().any()):
        return (numeric > 0).astype(float).where(numeric.notna())
    return parsed


def score_grid(
    frame: pd.DataFrame,
    score_a: str,
    score_b: str,
    bad: str,
    *,
    edges_a: Sequence[float],
    edges_b: Sequence[float],
    weight: Optional[str] = None,
) -> Dict[str, Any]:
    """Cross two scores with right-closed bins and summarize each cell.

    ``edges_a`` and ``edges_b`` are the ``pandas.cut(..., right=True)`` breaks.
    A row counts only when both scores fall inside those breaks and ``bad``
    already has a flag. ``weight`` defaults to one per row. ``bad_rate`` is the
    flagged weight over the cell weight; ``share`` uses the same weights over
    every cell that entered the grid.

    The input frame is not modified. No path is read or written.
    """
    name = "score_grid"
    _require_frame(frame, name)
    needed = [score_a, score_b, bad] if weight is None else [score_a, score_b, bad, weight]
    _require_columns(frame, needed, name)
    parsed_a = _parse_edges(edges_a, name)
    parsed_b = _parse_edges(edges_b, name)
    scores_a = pd.to_numeric(frame[score_a], errors="coerce")
    scores_b = pd.to_numeric(frame[score_b], errors="coerce")
    if not bool(scores_a.notna().any()) or not bool(scores_b.notna().any()):
        raise ValueError("score_grid: 分数全空")
    flags = _flag(frame[bad])
    weights = _weights(frame, weight)
    binned_a = pd.cut(scores_a, bins=parsed_a, right=True)
    binned_b = pd.cut(scores_b, bins=parsed_b, right=True)
    counts, bad_weights = _cell_totals(binned_a, binned_b, flags, weights)
    total_n = float(sum(counts.values()))
    total_k = float(sum(bad_weights.values()))
    weighted = weight is not None
    cells: List[Dict[str, Any]] = []
    for band_a in binned_a.cat.categories:
        for band_b in binned_b.cat.categories:
            key = (str(band_a), str(band_b))
            n_value = float(counts.get(key, 0.0))
            k_value = float(bad_weights.get(key, 0.0))
            cells.append(
                {
                    "band_a": str(band_a),
                    "band_b": str(band_b),
                    "left_a": float(band_a.left),
                    "right_a": float(band_a.right),
                    "left_b": float(band_b.left),
                    "right_b": float(band_b.right),
                    "n": _as_count(n_value, weighted=weighted),
                    "bad_cnt": _as_count(k_value, weighted=weighted),
                    "bad_rate": safe_rate(k_value, n_value),
                    "share": safe_rate(n_value, total_n) if total_n else None,
                }
            )
    return {
        "analysis": "score_grid",
        "definition": {
            "metric": "两分交叉格子的坏账率",
            "numerator": "分子是格子内坏账标记为 1 的权重；未传权重时每笔权重为 1",
            "denominator": "分母是格子内已有坏账标记、且两个分数都落在调用方边界内的权重",
            "share": "占比的分子是格子权重，分母是进入交叉表的全部权重",
            "bins": "pandas.cut 右闭（right=True，左开右闭）；分箱边界由调用方传入",
        },
        "score_a": score_a,
        "score_b": score_b,
        "edges_a": parsed_a,
        "edges_b": parsed_b,
        "weight": weight,
        "n": _as_count(total_n, weighted=weighted),
        "bad_cnt": _as_count(total_k, weighted=weighted),
        "bad_rate": safe_rate(total_k, total_n),
        "cells": cells,
        "caveats": [
            "只描述已经有坏账标记的样本，没有坏账结果的申请不进入任何格子的分子或分母。",
            "等于最左边界或落在区间外的分数不进入交叉表。",
        ],
    }


def marginal_bands(
    frame: pd.DataFrame,
    score: str,
    bad: str,
    *,
    edges: Sequence[float],
    higher_is_safer: bool = True,
) -> Dict[str, Any]:
    """Bin one score and report each band's own bad rate plus the cumulative book.

    ``bad_rate`` is the marginal bad rate of that band. When ``higher_is_safer``
    is true, ``approval_rate`` is the share of flagged rows whose score is at
    least the band's left edge, and ``approved_bad_rate`` is the bad rate on
    that same cumulative set. When it is false, the cumulative set is every
    flagged row at or below the band's right edge.

    No economic parameter is accepted and no profit is calculated.
    """
    name = "marginal_bands"
    _require_frame(frame, name)
    _require_columns(frame, [score, bad], name)
    parsed = _parse_edges(edges, name)
    raw_score = pd.to_numeric(frame[score], errors="coerce")
    if not bool(raw_score.notna().any()):
        raise ValueError("marginal_bands: 分数全空")
    flagged = _flag(frame[bad])
    base = pd.DataFrame({"score": raw_score.to_numpy(), "bad": flagged.to_numpy(dtype="float64")})
    base = base.loc[base["score"].notna() & base["bad"].notna()].reset_index(drop=True)
    total = int(len(base))
    total_bad = float(base["bad"].sum()) if total else 0.0
    binned = pd.cut(base["score"], bins=parsed, right=True) if total else None
    intervals = list(pd.IntervalIndex.from_breaks(parsed, closed="right"))
    bands: List[Dict[str, Any]] = []
    for interval in intervals:
        if binned is None:
            in_band = pd.Series(False, index=base.index)
            approved = in_band
        else:
            in_band = binned.eq(interval)
            if higher_is_safer:
                approved = base["score"] >= float(interval.left)
            else:
                approved = base["score"] <= float(interval.right)
        band_n = int(in_band.sum())
        band_k = float(base.loc[in_band, "bad"].sum()) if band_n else 0.0
        approved_n = int(approved.sum())
        approved_k = float(base.loc[approved, "bad"].sum()) if approved_n else 0.0
        bands.append(
            {
                "band": str(interval),
                "lower": float(interval.left),
                "upper": float(interval.right),
                "n": band_n,
                "bad_cnt": _as_count(band_k, weighted=False),
                "bad_rate": safe_rate(band_k, band_n),
                "approval_rate": safe_rate(approved_n, total),
                "approved_n": approved_n,
                "approved_bad_cnt": _as_count(approved_k, weighted=False) if approved_n else 0,
                "approved_bad_rate": safe_rate(approved_k, approved_n),
            }
        )
    if higher_is_safer:
        approval = "通过率的分子是分数大于等于该档下沿（区间左端点）的已标记样本数，分母是全部分数非空且已有坏账标记的样本数"
    else:
        approval = "通过率的分子是分数小于等于该档上沿（区间右端点）的已标记样本数，分母是全部分数非空且已有坏账标记的样本数"
    return {
        "analysis": "marginal_bands",
        "definition": {
            "metric": "分档边际坏账率与累计通过坏账率",
            "numerator": "分子是该档内坏账标记为 1 的样本数；bad_rate 就是该档边际坏账率",
            "denominator": "分母是该档内已有坏账标记的样本数",
            "approval_rate": approval,
            "approved_bad_rate": "通过坏账率的分子是上述通过样本中坏账标记为 1 的样本数，分母是上述通过样本数",
            "bins": "pandas.cut 右闭；分箱边界由调用方传入。右闭区间里等于下沿的分数落在更低一档，但在 higher_is_safer 时仍计入该下沿的累计通过",
        },
        "score": score,
        "edges": parsed,
        "higher_is_safer": bool(higher_is_safer),
        "n": total,
        "bad_cnt": _as_count(total_bad, weighted=False),
        "bad_rate": safe_rate(total_bad, total),
        "bands": bands,
        "caveats": [
            "只描述已经有坏账标记的样本。bad_rate 是该档自身的边际坏账率，不是累计通过样本的坏账率。",
            "不计算利润，也不使用任何经济参数。",
        ],
    }


def swap_table(
    frame: pd.DataFrame,
    score: str,
    bad: str,
    *,
    current_threshold: float,
    candidate_threshold: float,
    higher_is_safer: bool = True,
    approved: Optional[str] = None,
) -> Dict[str, Any]:
    """Compare two score thresholds on rows that already have a bad flag.

    Passing means ``score >= threshold`` when ``higher_is_safer`` is true, and
    ``score <= threshold`` otherwise. ``keep_approved`` passes both thresholds,
    ``swap_out`` passes only the current one, and ``swap_in`` passes only the
    candidate. If ``approved`` is set, rows with a false approval flag are
    dropped before that comparison.

    A block that contains scores but no bad flag keeps the observed row count
    and a null ``bad_rate``. That null is not filled in from the booked book.
    A reported rate is only the flagged rows' own rate.
    """
    name = "swap_table"
    _require_frame(frame, name)
    _require_columns(frame, [score], name)
    if approved is not None:
        _require_columns(frame, [approved], name)
    current = _finite_number(current_threshold, name, "现行阈值")
    candidate = _finite_number(candidate_threshold, name, "候选阈值")
    data = frame
    if approved is not None:
        data = frame.loc[_flag(frame[approved]).eq(1)]
    scores = pd.to_numeric(data[score], errors="coerce")
    if not bool(scores.notna().any()):
        raise ValueError("swap_table: 分数全空")
    if bad in frame.columns:
        flags = _flag(data[bad])
    else:
        flags = pd.Series(np.nan, index=data.index, dtype="float64")
    work = pd.DataFrame({"score": scores.to_numpy(), "bad": flags.to_numpy(dtype="float64")})
    work = work.loc[np.isfinite(work["score"].to_numpy(dtype="float64"))].reset_index(drop=True)
    current_pass = _passes(work["score"], current, higher_is_safer)
    candidate_pass = _passes(work["score"], candidate, higher_is_safer)
    blocks = {
        "keep_approved": _outcome_block(work.loc[current_pass & candidate_pass, "bad"]),
        "swap_out": _outcome_block(work.loc[current_pass & ~candidate_pass, "bad"]),
        "swap_in": _outcome_block(work.loc[~current_pass & candidate_pass, "bad"]),
    }
    # 矩阵下半行：现行拒绝且候选也拒绝的客群不在已放款样本内，只给笔数。
    keep_rejected = {
        "n": int((~current_pass & ~candidate_pass).sum()),
        "bad_cnt": None,
        "bad_rate": None,
    }
    caveats = [
        "坏账率只在已经有坏账标记的样本上计算；没有坏账结果时 bad_rate 为空，不做填充。",
        "swap_in 若给出坏账率，只是这些已观察样本自身的坏账率，不是被拒申请人的坏账率。",
        "不做拒绝推断，也不给出放宽阈值后的预期收益。",
        "矩阵下半行（现行拒绝客群）不在已放款样本内，keep_rejected 只给笔数，不计算坏账率。",
    ]
    if approved is not None:
        caveats.append("已先只保留审批标记为真的行，被拒申请人不在本次比较范围内。")
    if higher_is_safer:
        pass_rule = "分数大于等于阈值视为通过"
    else:
        pass_rule = "分数小于等于阈值视为通过"
    labeled_n = int(work["bad"].notna().sum())
    return {
        "analysis": "swap_table",
        "definition": {
            "metric": "两个分数阈值的置换",
            "numerator": "分子是该块中坏账标记为 1 的样本数",
            "denominator": "分母是该块中已有坏账标记的样本数；没有坏账标记时 bad_rate 为空",
            "n": "有坏账标记时 n 为标记样本数；没有坏账标记时 n 为该块中分数非空的已观察样本数",
            "keep_approved": "同时通过现行阈值和候选阈值",
            "swap_out": "通过现行阈值但未通过候选阈值",
            "swap_in": "未通过现行阈值但通过候选阈值；没有坏账结果时 n 可以是候选阈值相对现行阈值多放进来的已观察样本数",
            "keep_rejected": "现行与候选阈值都未通过（矩阵下半行）；只给笔数，bad_rate/bad_cnt 恒为空",
            "pass_rule": pass_rule,
            "sample_filter": "先只保留审批标记为真的行" if approved is not None else "未按审批标记过滤",
        },
        "score": score,
        "current_threshold": current,
        "candidate_threshold": candidate,
        "higher_is_safer": bool(higher_is_safer),
        "approved": approved,
        "n": int(len(work)),
        "labeled_n": labeled_n,
        "keep_approved": blocks["keep_approved"],
        "swap_out": blocks["swap_out"],
        "swap_in": blocks["swap_in"],
        "keep_rejected": keep_rejected,
        "caveats": caveats,
    }


def _require_frame(frame: pd.DataFrame, name: str) -> None:
    if not isinstance(frame, pd.DataFrame):
        raise ValueError(f"{name}: 输入必须是 DataFrame")


def _require_columns(frame: pd.DataFrame, columns: Sequence[str], name: str) -> None:
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise ValueError(f"{name}: 缺少列 {'、'.join(missing)}")


def _parse_edges(edges: Sequence[float], name: str) -> List[float]:
    if isinstance(edges, (str, bytes)):
        raise ValueError(f"{name}: 分箱边界必须是数值")
    try:
        values = [float(value) for value in edges]
    except (TypeError, ValueError):
        raise ValueError(f"{name}: 分箱边界必须是数值") from None
    if len(values) < 2:
        raise ValueError(f"{name}: 分箱边界至少需要 2 个")
    if any(math.isnan(value) for value in values):
        raise ValueError(f"{name}: 分箱边界不能是空值")
    if any(values[index] >= values[index + 1] for index in range(len(values) - 1)):
        raise ValueError(f"{name}: 分箱边界必须严格递增")
    return values


def _weights(frame: pd.DataFrame, weight: Optional[str]) -> pd.Series:
    if weight is None:
        return pd.Series(1.0, index=frame.index)
    values = pd.to_numeric(frame[weight], errors="coerce")
    array = values.to_numpy(dtype="float64")
    if bool(np.any(array < 0) or np.any(np.isinf(array))):
        raise ValueError("score_grid: 权重必须为有限非负数")
    return values


def _cell_totals(
    binned_a: pd.Series,
    binned_b: pd.Series,
    flags: pd.Series,
    weights: pd.Series,
) -> tuple[Dict[tuple, float], Dict[tuple, float]]:
    finite_weight = pd.Series(np.isfinite(weights.to_numpy(dtype="float64")), index=weights.index)
    valid = binned_a.notna() & binned_b.notna() & flags.notna() & finite_weight
    if not bool(valid.any()):
        return {}, {}
    part = pd.DataFrame(
        {
            "a": binned_a.loc[valid].astype(str).to_numpy(),
            "b": binned_b.loc[valid].astype(str).to_numpy(),
            "bad": flags.loc[valid].to_numpy(dtype="float64"),
            "w": weights.loc[valid].to_numpy(dtype="float64"),
        }
    )
    part["kw"] = part["bad"] * part["w"]
    grouped = part.groupby(["a", "b"], sort=False)
    counts = {(str(left), str(right)): float(value) for (left, right), value in grouped["w"].sum().to_dict().items()}
    bad_weights = {
        (str(left), str(right)): float(value) for (left, right), value in grouped["kw"].sum().to_dict().items()
    }
    return counts, bad_weights


def _as_count(value: float, *, weighted: bool) -> int | float:
    number = float(value)
    if weighted or not number.is_integer():
        return number
    return int(number)


def _finite_number(value: float, name: str, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name}: {label}必须是有限数值") from None
    if not math.isfinite(number):
        raise ValueError(f"{name}: {label}必须是有限数值")
    return number


def _passes(score: pd.Series, threshold: float, higher_is_safer: bool) -> pd.Series:
    if higher_is_safer:
        return score >= threshold
    return score <= threshold


def _outcome_block(bad: pd.Series) -> Dict[str, Any]:
    """Count a swap block without inventing a rate for unlabeled rows.

    When at least one row has a bad flag, ``n`` and ``bad_rate`` use only those
    rows. When none do, ``n`` stays the observed score count and ``bad_rate``
    is null.
    """
    labeled = bad.notna()
    if not bool(labeled.any()):
        return {"n": int(len(bad)), "bad_cnt": None, "bad_rate": None}
    bad_count = float(bad.loc[labeled].sum())
    labeled_n = int(labeled.sum())
    return {
        "n": labeled_n,
        "bad_cnt": _as_count(bad_count, weighted=False),
        "bad_rate": safe_rate(bad_count, labeled_n),
    }
