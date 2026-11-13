"""Delinquency bucket migration (roll-rate) matrices."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

BUCKETS = ["M0", "M1", "M2", "M3", "M4", "M5", "M6", "M6+"]


def dpd_to_bucket(dpd: pd.Series) -> pd.Series:
    d = pd.to_numeric(dpd, errors="coerce")

    def one(v):
        if pd.isna(v):
            return None
        if v <= 0:
            return "M0"
        idx = int((v - 1) // 30) + 1
        return f"M{idx}" if idx <= 6 else "M6+"

    return d.map(one)


def normalise_bucket(series: pd.Series) -> pd.Series:
    def one(v):
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return None
        s = str(v).strip().upper()
        if s in ("C", "CURRENT", "NORMAL", "正常"):
            return "M0"
        if s.isdigit():
            n = int(s)
            return f"M{n}" if n <= 6 else "M6+"
        if s.startswith("M"):
            tail = s[1:].rstrip("+")
            if tail.isdigit():
                n = int(tail)
                return f"M{n}" if n <= 6 and not s.endswith("+") else "M6+"
        return s

    return series.map(one)


def roll_rates(
    df: pd.DataFrame,
    *,
    loan_id: str,
    period: str,
    bucket: Optional[str] = None,
    dpd: Optional[str] = None,
    balance: Optional[str] = None,
) -> Dict[str, Any]:
    if not bucket and not dpd:
        raise ValueError("migration: 需要逾期阶段列（bucket）或逾期天数列（dpd）")
    cols = [loan_id, period] + [c for c in (bucket, dpd, balance) if c]
    d = df[cols].copy()
    d["_bucket"] = normalise_bucket(d[bucket]) if bucket else dpd_to_bucket(d[dpd])
    d = d.dropna(subset=["_bucket"])
    d[period] = d[period].astype(str)
    periods = sorted(d[period].unique())
    pidx = {p: i for i, p in enumerate(periods)}
    d["_pi"] = d[period].map(pidx)
    d = d.sort_values([loan_id, "_pi"])
    nxt = d.groupby(loan_id).shift(-1)
    d["_next"] = nxt["_bucket"]
    d["_next_pi"] = nxt["_pi"]
    pairs = d[(d["_next_pi"] - d["_pi"]) == 1].copy()
    if pairs.empty:
        raise ValueError("migration: 没有相邻两期都有观测的借据，无法计算迁移")
    weight = balance if balance else None
    pairs["_w"] = pd.to_numeric(pairs[weight], errors="coerce").fillna(0) if weight else 1.0

    order = [b for b in BUCKETS if b in set(pairs["_bucket"]) | set(pairs["_next"])]
    order += sorted((set(pairs["_bucket"]) | set(pairs["_next"])) - set(order))
    mat = pairs.pivot_table(index="_bucket", columns="_next", values="_w", aggfunc="sum", fill_value=0)
    mat = mat.reindex(index=[b for b in order if b in mat.index], columns=order, fill_value=0)
    rowsum = mat.sum(axis=1)
    rate = mat.div(rowsum.where(rowsum > 0), axis=0).fillna(0)

    matrix = {
        frm: {to: round(float(rate.loc[frm, to]), 6) for to in order}
        for frm in rate.index
    }
    roll: List[Dict[str, Any]] = []
    for i, b in enumerate(order[:-1]):
        nb = order[i + 1]
        if b in rate.index:
            roll.append({"from": b, "to": nb, "rate": round(float(rate.loc[b, nb]), 6), "base": float(rowsum.loc[b])})

    by_period: List[Dict[str, Any]] = []
    for p, g in pairs.groupby(period):
        row: Dict[str, Any] = {"period": p}
        for i, b in enumerate(order[:-1]):
            base = g.loc[g["_bucket"] == b, "_w"].sum()
            if base > 0:
                rolled = g.loc[(g["_bucket"] == b) & (g["_next"] == order[i + 1]), "_w"].sum()
                row[f"{b}->{order[i + 1]}"] = round(float(rolled / base), 6)
        by_period.append(row)

    cum = 1.0
    chain = []
    for r in roll:
        cum *= r["rate"]
        chain.append({"to": r["to"], "cumulative_from_M0": round(cum, 8)})

    return {
        "analysis": "migration",
        "definition": {
            "metric": "逾期阶段迁移率（Roll Rate）",
            "numerator": f"本期处于阶段 i 且下一期处于阶段 j 的{'余额' if balance else '借据数'}",
            "denominator": f"本期处于阶段 i 且下一期仍有观测的{'余额' if balance else '借据数'}",
            "time_alignment": f"按统计期 {period} 相邻两期配对；逾期阶段每 30 天一档",
        },
        "columns": {"loan_id": loan_id, "period": period, "bucket": bucket, "dpd": dpd, "balance": balance},
        "buckets": order,
        "matrix": matrix,
        "roll_rates": roll,
        "roll_chain": chain,
        "by_period": by_period,
        "n_pairs": int(len(pairs)),
    }
