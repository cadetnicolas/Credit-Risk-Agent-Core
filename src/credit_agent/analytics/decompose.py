"""Shift-share decomposition of a portfolio rate change into mix / rate / interaction.

    ΔR = Σ (w1 - w0)·r0  [结构效应]  +  Σ w0·(r1 - r0)  [风险效应]  +  Σ (w1 - w0)·(r1 - r0)  [交互]

where ``w`` is a segment's share of the denominator and ``r`` its rate. The
three effects sum exactly to the overall change; ``closure_error`` reports the
numerical residual so the validator can check it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

from .columns import as_flag


def _segment_table(df: pd.DataFrame, segment: str, flag: str, weight: Optional[str]) -> pd.DataFrame:
    d = pd.DataFrame({"seg": df[segment].astype(str), "k": as_flag(df[flag])})
    d["w"] = pd.to_numeric(df[weight], errors="coerce").fillna(0) if weight else 1.0
    d = d.dropna(subset=["k"])
    d["kw"] = d["k"] * d["w"]
    g = d.groupby("seg").agg(n=("w", "sum"), k=("kw", "sum"))
    g["rate"] = g["k"] / g["n"].where(g["n"] > 0)
    return g


def shift_share(
    base: pd.DataFrame,
    comp: pd.DataFrame,
    *,
    segment: str,
    flag: str,
    weight: Optional[str] = None,
) -> Dict[str, Any]:
    g0 = _segment_table(base, segment, flag, weight)
    g1 = _segment_table(comp, segment, flag, weight)
    segs = sorted(set(g0.index) | set(g1.index))
    g0 = g0.reindex(segs).fillna({"n": 0, "k": 0})
    g1 = g1.reindex(segs).fillna({"n": 0, "k": 0})
    n0, n1 = g0["n"].sum(), g1["n"].sum()
    if not n0 or not n1:
        raise ValueError("decompose: 基期或对比期样本为空")
    w0, w1 = g0["n"] / n0, g1["n"] / n1
    # a segment absent from one window has no rate there; borrow the other window's
    r0 = g0["rate"].fillna(g1["rate"]).fillna(0)
    r1 = g1["rate"].fillna(g0["rate"]).fillna(0)
    R0, R1 = g0["k"].sum() / n0, g1["k"].sum() / n1

    mix = (w1 - w0) * r0
    rate = w0 * (r1 - r0)
    inter = (w1 - w0) * (r1 - r0)
    total = R1 - R0
    explained = float(mix.sum() + rate.sum() + inter.sum())

    rows: List[Dict[str, Any]] = []
    for s in segs:
        rows.append(
            {
                "segment": s,
                "share_base": round(float(w0[s]), 6),
                "share_comp": round(float(w1[s]), 6),
                "rate_base": round(float(r0[s]), 6),
                "rate_comp": round(float(r1[s]), 6),
                "mix_effect": round(float(mix[s]), 6),
                "rate_effect": round(float(rate[s]), 6),
                "interaction": round(float(inter[s]), 6),
                "total_effect": round(float(mix[s] + rate[s] + inter[s]), 6),
                "n_base": float(g0["n"][s]),
                "n_comp": float(g1["n"][s]),
            }
        )
    rows.sort(key=lambda r: -abs(r["total_effect"]))
    unit = "余额" if weight else "借据数"
    return {
        "analysis": "decompose",
        "definition": {
            "metric": f"{flag} 比率（{unit}加权）",
            "numerator": f"{flag}=1 的{unit}",
            "denominator": f"全部{unit}",
            "method": "Shift-share：结构效应 Σ(w1−w0)·r0 + 风险效应 Σw0·(r1−r0) + 交互 Σ(w1−w0)(r1−r0)",
        },
        "dimension": segment,
        "rate_base": round(float(R0), 6),
        "rate_comp": round(float(R1), 6),
        "total_change": round(float(total), 6),
        "mix_effect": round(float(mix.sum()), 6),
        "rate_effect": round(float(rate.sum()), 6),
        "interaction": round(float(inter.sum()), 6),
        "closure_error": round(abs(float(total) - explained), 10),
        "segments": rows,
        "dominant": "结构效应" if abs(mix.sum()) > abs(rate.sum()) else "风险效应",
    }
