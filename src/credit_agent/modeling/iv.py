"""Information Value for numeric features (simplified from legacy ``IV/``)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd


def _strength(iv: float) -> str:
    if iv >= 0.1:
        return "strong"
    if iv >= 0.02:
        return "medium"
    return "weak"


def numeric_iv(
    series: pd.Series,
    target: pd.Series,
    *,
    n_bins: int = 10,
) -> Tuple[Optional[float], List[Dict[str, Any]]]:
    mask = series.notna() & target.notna()
    x = series[mask].astype(float)
    y = target[mask].astype(float)
    if len(x) < 20 or y.nunique() < 2:
        return None, []
    try:
        binned = pd.qcut(x, min(n_bins, x.nunique()), duplicates="drop")
    except ValueError:
        return None, []
    grp = pd.DataFrame({"b": binned, "y": y}).groupby("b", observed=True)["y"].agg(["sum", "count"])
    grp["good"] = grp["count"] - grp["sum"]
    tot_bad = grp["sum"].sum()
    tot_good = grp["good"].sum()
    if tot_bad <= 0 or tot_good <= 0:
        return None, []
    bad_rate = (grp["sum"] + 0.5) / tot_bad
    good_rate = (grp["good"] + 0.5) / tot_good
    woe = np.log(bad_rate / good_rate).to_numpy(dtype=float)
    iv = float(((bad_rate - good_rate) * woe).sum())
    detail = []
    for i, (idx, row) in enumerate(grp.iterrows()):
        br = float(row["sum"] / row["count"]) if row["count"] else 0.0
        detail.append(
            {
                "bin": str(idx),
                "woe": round(float(woe[i]), 6),
                "bad_rate": round(br, 6),
                "count": int(row["count"]),
            }
        )
    return iv, detail


def iv_table(
    df: pd.DataFrame,
    features: List[str],
    label: str,
    *,
    n_bins: int = 10,
    binning_method: str = "quantile",
    monotonic_direction: str = "increasing",
    min_bin_fraction: float = 0.05,
    max_prebins: int = 20,
) -> List[Dict[str, Any]]:
    """逐特征 IV 表。

    三种分箱方法走同一条 ``numeric_profile`` 通道，缺失一律独立成箱并计入 IV。
    早期实现里 quantile 走的是 ``numeric_iv``（整行剔除缺失、无最小箱约束），
    高缺失特征的 IV 只在少数有值样本上计算，会被系统性高估。
    """

    from .woe import numeric_profile

    y = df[label]
    rows: List[Dict[str, Any]] = []
    for ft in features:
        entry = numeric_profile(
            df[ft],
            y,
            n_bins=n_bins,
            binning_method=binning_method,
            monotonic_direction=monotonic_direction,
            min_bin_fraction=min_bin_fraction,
            max_prebins=max_prebins,
        )
        if entry is None:
            continue
        iv = float(entry["iv"])
        rows.append(
            {
                "feature": ft,
                "iv": round(iv, 6),
                "strength": _strength(iv),
                "binning_method": entry.get("binning_method"),
                "n_bins": len(entry.get("bins") or []),
                "ks": entry.get("ks"),
                "monotonic": entry.get("monotonic"),
                "monotonic_strength": entry.get("monotonic_strength"),
                "missing_rate": entry.get("missing_rate"),
                "bin_edges": entry.get("bin_edges"),
                "min_bin_violations": entry.get("min_bin_violations"),
                "woe_detail": entry.get("bins") or [],
            }
        )
    rows.sort(key=lambda r: -(r["iv"] or 0))
    return rows
