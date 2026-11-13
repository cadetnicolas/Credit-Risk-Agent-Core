"""Vintage (cohort x MOB) cumulative bad-rate curves.

Data must be a panel: one row per loan per MOB. The rate at MOB k is
``loans that have ever been bad by MOB k / loans observed at MOB k`` — the
denominator only counts loans that have actually reached MOB k, so young
cohorts are never diluted by loans that could not yet have gone bad.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

from .columns import as_flag
from .stats import wilson

MATURE_SHARE = 0.9


def vintage_curves(
    df: pd.DataFrame,
    *,
    loan_id: str,
    cohort: str,
    mob: str,
    bad: str,
    segment: Optional[str] = None,
    max_mob: Optional[int] = None,
    mature_share: float = MATURE_SHARE,
) -> Dict[str, Any]:
    cols = [loan_id, cohort, mob, bad] + ([segment] if segment else [])
    d = df[cols].copy()
    d[mob] = pd.to_numeric(d[mob], errors="coerce")
    if d[loan_id].isna().any() or d[cohort].isna().any():
        raise ValueError("vintage: 借据号与放款批次不能为空")
    if ((d[mob].dropna() < 0) | (d[mob].dropna() % 1 != 0)).any():
        raise ValueError("vintage: MOB 必须为非负整数")
    if d.duplicated([loan_id, mob]).any():
        raise ValueError("vintage: 借据×MOB 存在重复记录，请先按业务口径去重")
    if d.groupby(loan_id)[cohort].nunique().gt(1).any():
        raise ValueError("vintage: 同一借据的放款批次不一致")
    if segment and d.groupby(loan_id)[segment].nunique(dropna=False).gt(1).any():
        raise ValueError("vintage: 分群随账龄变化，请绑定放款时的固定分群")
    if not 0 < mature_share <= 1:
        raise ValueError("vintage: mature_share 必须位于 (0, 1]")
    d["_bad"] = as_flag(d[bad])
    d = d.dropna(subset=[mob, "_bad"])
    d[cohort] = d[cohort].astype(str)
    if max_mob is not None:
        d = d[d[mob] <= int(max_mob)]
    if d.empty:
        raise ValueError("vintage: 有效样本为空（检查 MOB / 坏标记列）")
    if d.groupby(loan_id)[mob].nunique().max() <= 1 and d[mob].nunique() > 1:
        raise ValueError("vintage: 数据不是借据×MOB 面板（每笔借据只有一行），无法计算累计坏账曲线")

    d = d.sort_values([loan_id, mob])
    d["_ever_bad"] = d.groupby(loan_id)["_bad"].cummax()
    keys = ([segment] if segment else []) + [cohort]
    cohort_size = d.groupby(keys)[loan_id].nunique().rename("cohort_loans")
    agg = (
        d.groupby(keys + [mob])
        .agg(observed=(loan_id, "nunique"), ever_bad=("_ever_bad", "sum"))
        .reset_index()
        .merge(cohort_size.reset_index(), on=keys)
    )
    agg["rate"] = agg["ever_bad"] / agg["observed"]
    agg["observed_share"] = agg["observed"] / agg["cohort_loans"]
    agg["mature"] = agg["observed_share"] >= mature_share

    curves: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    cohort_summary: List[Dict[str, Any]] = []
    for gkey, g in agg.groupby(keys):
        gkey = gkey if isinstance(gkey, tuple) else (gkey,)
        seg_val = str(gkey[0]) if segment else "ALL"
        coh = str(gkey[-1])
        pts = []
        for _, r in g.sort_values(mob).iterrows():
            lo, hi = wilson(r["ever_bad"], r["observed"])
            pts.append(
                {
                    "mob": int(r[mob]),
                    "observed": int(r["observed"]),
                    "ever_bad": int(r["ever_bad"]),
                    "rate": round(float(r["rate"]), 6),
                    "ci95": [round(lo, 6), round(hi, 6)] if lo is not None else None,
                    "observed_share": round(float(r["observed_share"]), 4),
                    "mature": bool(r["mature"]),
                }
            )
        curves.setdefault(seg_val, {})[coh] = pts
        mature_pts = [p for p in pts if p["mature"]]
        cohort_summary.append(
            {
                "segment": seg_val,
                "cohort": coh,
                "loans": int(g["cohort_loans"].iloc[0]),
                "max_mob_observed": int(g[mob].max()),
                "max_mature_mob": max((p["mob"] for p in mature_pts), default=None),
                "latest_mature_rate": mature_pts[-1]["rate"] if mature_pts else None,
            }
        )

    comparable = _comparable_mob(cohort_summary)
    compare_table = []
    if comparable is not None:
        for seg_val, by_coh in curves.items():
            for coh, pts in sorted(by_coh.items()):
                hit = next((p for p in pts if p["mob"] == comparable), None)
                if hit and hit["mature"]:
                    compare_table.append({"segment": seg_val, "cohort": coh, "mob": comparable, "rate": hit["rate"], "observed": hit["observed"]})

    immature = [c["cohort"] for c in cohort_summary if c["max_mature_mob"] is None]
    return {
        "analysis": "vintage",
        "definition": {
            "metric": f"MOB{{k}} 累计坏账率（坏标记列 {bad}）",
            "numerator": f"截至 MOB k 曾经 {bad}=1 的借据数",
            "denominator": "在 MOB k 仍有观测的借据数（仅统计已到达该账龄的借据）",
            "time_alignment": f"按放款月 {cohort} 对齐账龄 {mob}",
            "maturity_rule": f"某账龄观测借据占 cohort 比例 ≥ {mature_share:.0%} 视为成熟",
        },
        "columns": {"loan_id": loan_id, "cohort": cohort, "mob": mob, "bad": bad, "segment": segment},
        "curves": curves,
        "cohort_summary": cohort_summary,
        "comparable_mob": comparable,
        "compare_table": compare_table,
        "immature_cohorts": immature,
        "n_loans": int(d[loan_id].nunique()),
    }


def _comparable_mob(summary: List[Dict[str, Any]]) -> Optional[int]:
    """Largest MOB at which a majority of cohorts are mature — the fair comparison point."""
    mm = sorted(c["max_mature_mob"] for c in summary if c["max_mature_mob"] is not None)
    if not mm:
        return None
    return int(mm[len(mm) // 2]) if len(mm) > 1 else int(mm[0])
