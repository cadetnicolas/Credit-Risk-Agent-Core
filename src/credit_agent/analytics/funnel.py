"""Conversion funnel (apply -> approve -> draw -> ...)."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

from .columns import as_flag


def funnel(df: pd.DataFrame, *, stages: Sequence[str], segment: Optional[str] = None) -> Dict[str, Any]:
    if len(stages) < 2:
        raise ValueError("funnel: 至少需要两个阶段标记列（如 applied, approved, drawn）")
    flags = pd.DataFrame({s: as_flag(df[s]).fillna(0) for s in stages})
    # a later stage only counts if every earlier stage is reached
    for i in range(1, len(stages)):
        flags[stages[i]] = flags[stages[i]] * flags[stages[i - 1]]

    def one(f: pd.DataFrame) -> List[Dict[str, Any]]:
        out = []
        top = float(f[stages[0]].sum())
        prev = top
        for s in stages:
            cnt = float(f[s].sum())
            out.append(
                {
                    "stage": s,
                    "count": int(cnt),
                    "step_conversion": round(cnt / prev, 6) if prev else None,
                    "cumulative_conversion": round(cnt / top, 6) if top else None,
                }
            )
            prev = cnt
        return out

    by_segment: Dict[str, List[Dict[str, Any]]] = {}
    if segment:
        for v, g in flags.groupby(df[segment].astype(str)):
            by_segment[str(v)] = one(g)
    overall = one(flags)
    worst = min(overall[1:], key=lambda r: r["step_conversion"] or 1)
    return {
        "analysis": "funnel",
        "definition": {
            "metric": "阶段转化率",
            "numerator": "到达本阶段（且已到达前序全部阶段）的样本数",
            "denominator": "到达上一阶段的样本数（累计转化以首阶段为分母）",
        },
        "stages": list(stages),
        "overall": overall,
        "by_segment": by_segment,
        "bottleneck": worst["stage"],
    }
