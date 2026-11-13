"""Multi-dimension drill-down with contribution and significance.

Output ``drivers`` keep the shape the hypothesis plugin already consumes
(``dimension / value / bad_rate / base_rate / delta / contribution / sample``)
plus ``p_value`` / ``significant`` / ``sample_ok`` so weak slices are flagged
instead of being promoted to root causes.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

from .columns import as_flag
from .stats import enough_sample, two_prop_z


def _agg(df: pd.DataFrame, dim: str, flag: str) -> pd.DataFrame:
    d = pd.DataFrame({"v": df[dim].astype(str), "k": as_flag(df[flag])}).dropna(subset=["k"])
    return d.groupby("v").agg(n=("k", "size"), k=("k", "sum"))


def drilldown(
    base: Optional[pd.DataFrame],
    comp: pd.DataFrame,
    *,
    dims: Sequence[str],
    flag: str,
    min_sample: int = 50,
    alpha: float = 0.05,
    top_n: int = 10,
) -> Dict[str, Any]:
    """With ``base``: change of each slice vs its own base window.
    Without ``base``: each slice vs the rest of the portfolio (cross-section)."""
    if not dims:
        raise ValueError("drilldown: 需要至少一个下钻维度")
    comp_flag = as_flag(comp[flag]).dropna()
    total_n = len(comp_flag)
    overall = float(comp_flag.mean()) if total_n else None
    base_overall = None
    if base is not None:
        bf = as_flag(base[flag]).dropna()
        base_overall = float(bf.mean()) if len(bf) else None

    detail: Dict[str, List[Dict[str, Any]]] = {}
    drivers: List[Dict[str, Any]] = []
    simpson: List[Dict[str, Any]] = []
    for dim in dims:
        c = _agg(comp, dim, flag)
        b = _agg(base, dim, flag) if base is not None else None
        rows: List[Dict[str, Any]] = []
        for v, r in c.iterrows():
            n1, k1 = float(r["n"]), float(r["k"])
            rate1 = k1 / n1 if n1 else None
            if b is not None and v in b.index:
                n0, k0 = float(b.loc[v, "n"]), float(b.loc[v, "k"])
            elif b is None:
                n0, k0 = total_n - n1, float(comp_flag.sum()) - k1
            else:
                n0, k0 = 0.0, 0.0
            rate0 = k0 / n0 if n0 else None
            delta = (rate1 - rate0) if rate1 is not None and rate0 is not None else None
            test = two_prop_z(k1, n1, k0, n0) if n0 else {"z": None, "p_value": None}
            share = n1 / total_n if total_n else 0
            rows.append(
                {
                    "dimension": dim,
                    "value": v,
                    "sample": int(n1),
                    "bad_cnt": int(k1),
                    "bad_rate": round(rate1, 6) if rate1 is not None else None,
                    "base_rate": round(rate0, 6) if rate0 is not None else None,
                    "delta": round(delta, 6) if delta is not None else None,
                    "share": round(share, 6),
                    "bad_share": round(k1 / comp_flag.sum(), 6) if comp_flag.sum() else None,
                    "contribution": round(delta * share, 6) if delta is not None else None,
                    "p_value": round(test["p_value"], 6) if test["p_value"] is not None else None,
                    "significant": bool(test["p_value"] is not None and test["p_value"] < alpha),
                    "sample_ok": bool(n1 >= min_sample and enough_sample(n1, rate1, min_n=min_sample)),
                }
            )
        rows.sort(key=lambda x: -(x["contribution"] or 0))
        detail[dim] = rows
        drivers.extend(rows[:3])
        if base is not None and base_overall is not None and overall is not None:
            simpson.append(_simpson_flag(dim, rows, overall - base_overall))

    drivers = [d for d in drivers if d["contribution"] is not None]
    drivers.sort(key=lambda x: (not (x["significant"] and x["sample_ok"]), -(x["contribution"] or 0)))
    return {
        "analysis": "drilldown",
        "definition": {
            "metric": f"{flag} 比率",
            "numerator": f"{flag}=1 的样本数",
            "denominator": "切片内全部样本数",
            "comparison": "同切片对比期 vs 基期" if base is not None else "切片 vs 组合内其余样本",
            "contribution": "delta × 切片在对比期的样本占比",
            "significance": f"两比例 z 检验，α={alpha}；样本 < {min_sample} 或期望事件 < 5 记为样本不足",
        },
        "dims": list(dims),
        "overall_rate": round(overall, 6) if overall is not None else None,
        "base_overall_rate": round(base_overall, 6) if base_overall is not None else None,
        "drivers": drivers[:top_n],
        "detail": detail,
        "simpson_checks": simpson,
        "min_sample": min_sample,
    }


def _simpson_flag(dim: str, rows: List[Dict[str, Any]], overall_delta: float) -> Dict[str, Any]:
    """Every slice moves one way while the total moves the other: a mix effect, not a risk change."""
    deltas = [r["delta"] for r in rows if r["delta"] is not None and r["sample_ok"]]
    if not deltas:
        return {"dimension": dim, "risk": False}
    all_down = all(d <= 0 for d in deltas)
    all_up = all(d >= 0 for d in deltas)
    risk = (overall_delta > 0 and all_down) or (overall_delta < 0 and all_up)
    return {
        "dimension": dim,
        "risk": bool(risk),
        "overall_delta": round(overall_delta, 6),
        "note": "各切片同向而整体反向，整体变化主要来自结构（mix）变化" if risk else "",
    }
