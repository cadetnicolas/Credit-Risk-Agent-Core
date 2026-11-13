"""Period trend with MoM / YoY and a p-chart (3σ) control band.

The control limits use the base window's pooled rate and each period's own
sample size, so a small month is not flagged just for being noisy.
"""

from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional

import pandas as pd

from .columns import as_flag
from .windows import split_windows

_YM = re.compile(r"^(\d{4})[-/]?(\d{2})")


def _yoy_key(period: str) -> Optional[str]:
    m = _YM.match(period)
    if not m:
        return None
    y, mo = int(m.group(1)), m.group(2)
    sep = "-" if "-" in period[:7] else ("/" if "/" in period[:7] else "")
    return f"{y - 1}{sep}{mo}"


def trend(
    df: pd.DataFrame,
    *,
    period: str,
    flag: str,
    weight: Optional[str] = None,
    base_periods: Any = None,
    comp_periods: Any = None,
    window: int = 3,
    sigma: float = 3.0,
    warn_delta: float = 0.005,
    high_delta: float = 0.015,
) -> Dict[str, Any]:
    d = pd.DataFrame({"p": df[period].astype(str), "k": as_flag(df[flag])})
    d["w"] = pd.to_numeric(df[weight], errors="coerce").fillna(0) if weight else 1.0
    d = d.dropna(subset=["k"])
    d["kw"] = d["k"] * d["w"]
    g = d.groupby("p").agg(n=("w", "sum"), k=("kw", "sum"), rows=("k", "size")).sort_index()
    if len(g) < 2:
        raise ValueError("trend: 统计期少于 2 期，无法做趋势与环比")
    g["rate"] = g["k"] / g["n"].where(g["n"] > 0)

    base_l, comp_l = split_windows(g.index, base=base_periods, comp=comp_periods, window=window)
    base_g = g.loc[[p for p in base_l if p in g.index]]
    comp_g = g.loc[[p for p in comp_l if p in g.index]]
    p_bar = float(base_g["k"].sum() / base_g["n"].sum()) if len(base_g) and base_g["n"].sum() else float(g["rate"].mean())
    recent = float(comp_g["k"].sum() / comp_g["n"].sum()) if comp_g["n"].sum() else None

    series: List[Dict[str, Any]] = []
    prev_rate = None
    rates = g["rate"].to_dict()
    for p, r in g.iterrows():
        rate = float(r["rate"]) if pd.notna(r["rate"]) else None
        n_eff = float(r["rows"])
        se = math.sqrt(p_bar * (1 - p_bar) / n_eff) if n_eff else 0.0
        ucl, lcl = p_bar + sigma * se, max(0.0, p_bar - sigma * se)
        yk = _yoy_key(p)
        yoy_base = rates.get(yk) if yk else None
        series.append(
            {
                "period": p,
                "n": float(r["n"]),
                "rate": round(rate, 6) if rate is not None else None,
                "mom": round(rate - prev_rate, 6) if rate is not None and prev_rate is not None else None,
                "yoy": round(rate - float(yoy_base), 6) if rate is not None and yoy_base is not None and pd.notna(yoy_base) else None,
                "ucl": round(ucl, 6),
                "lcl": round(lcl, 6),
                "out_of_control": bool(rate is not None and (rate > ucl or rate < lcl)),
                "window": "comp" if p in comp_l else ("base" if p in base_l else "other"),
            }
        )
        prev_rate = rate

    delta = (recent - p_bar) if recent is not None else None
    ooc = [s["period"] for s in series if s["out_of_control"] and s["window"] == "comp"]
    level = "low"
    if delta is not None and (delta > high_delta or (ooc and delta > warn_delta)):
        level = "high"
    elif delta is not None and (delta > warn_delta or ooc):
        level = "medium"
    seasonal = [
        s["period"]
        for s in series
        if s["window"] == "comp" and (m := _YM.match(s["period"])) and m.group(2) in ("01", "02")
    ]
    return {
        "analysis": "trend",
        "definition": {
            "metric": f"{flag} 比率（{'余额' if weight else '借据数'}加权）",
            "numerator": f"{flag}=1 的{'余额' if weight else '借据数'}",
            "denominator": f"当期全部{'余额' if weight else '借据数'}",
            "time_alignment": f"按 {period} 分期；对比期 {comp_l}，基期 {len(base_l)} 期",
            "control_chart": f"p 图：中心线=基期合并比率，控制限=±{sigma:g}σ（σ 按当期样本量）",
            "alarm_rule": f"对比期均值较基期上升 > {warn_delta:.2%} 为 medium，> {high_delta:.2%} 或越上控制限为 high",
        },
        "series": series,
        "base_periods": base_l,
        "comp_periods": comp_l,
        "baseline_bad_rate": round(p_bar, 6),
        "recent_window_bad_rate": round(recent, 6) if recent is not None else None,
        "delta": round(delta, 6) if delta is not None else None,
        "relative_change": round(delta / p_bar, 6) if delta is not None and p_bar else None,
        "out_of_control_periods": ooc,
        "alarm": level != "low",
        "alarm_level": level,
        "seasonality_note": "对比期含 1-2 月（春节前后），环比波动需剔除季节性后判断" if seasonal else "",
    }
