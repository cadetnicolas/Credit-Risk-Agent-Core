"""Base / comparison window selection shared by trend, drilldown and decomposition."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd


def _as_list(v: Any) -> List[str]:
    if v is None or v == "":
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return [s.strip() for s in str(v).replace("，", ",").split(",") if s.strip()]


def split_windows(
    periods: Sequence[Any],
    *,
    base: Any = None,
    comp: Any = None,
    window: int = 3,
) -> Tuple[List[str], List[str]]:
    """Select base / comparison periods.

    The automatic "last N periods vs everything before them" default is used
    only when the caller did not explicitly request a window. When an
    explicitly supplied base / comp period is absent from the data, do not
    silently fall back to that default: raise ``ValueError("PERIOD_MISMATCH:
    ...")`` so the caller can return a structured ``blocked`` envelope.
    """

    ordered = sorted({str(p) for p in periods})
    requested_comp = _as_list(comp)
    requested_base = _as_list(base)
    comp_l = [p for p in requested_comp if p in ordered]
    base_l = [p for p in requested_base if p in ordered]
    if requested_comp and len(comp_l) < len(requested_comp):
        missing = [p for p in requested_comp if p not in ordered]
        raise ValueError("PERIOD_MISMATCH: 对比期不在数据中：" + "、".join(missing))
    if requested_base and len(base_l) < len(requested_base):
        missing = [p for p in requested_base if p not in ordered]
        raise ValueError("PERIOD_MISMATCH: 基期不在数据中：" + "、".join(missing))
    if not comp_l and not requested_comp:
        w = max(1, min(int(window or 3), max(1, len(ordered) - 1)))
        comp_l = ordered[-w:]
    if not base_l and not requested_base:
        base_l = [p for p in ordered if p not in comp_l and p < min(comp_l)]
    return base_l, comp_l


def window_frames(
    df: pd.DataFrame, period: Optional[str], params: Dict[str, Any]
) -> Tuple[pd.DataFrame, pd.DataFrame, Dict[str, Any]]:
    if not period:
        raise ValueError("需要统计期列（period）来划分基期与对比期")
    p = df[period].astype(str)
    base_l, comp_l = split_windows(
        p.unique(),
        base=params.get("base_periods"),
        comp=params.get("comp_periods"),
        window=int(params.get("window") or 3),
    )
    if not base_l:
        raise ValueError("基期为空：统计期数量不足以划分基期与对比期")
    return df[p.isin(base_l)], df[p.isin(comp_l)], {"base_periods": base_l, "comp_periods": comp_l}
