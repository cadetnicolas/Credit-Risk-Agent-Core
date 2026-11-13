"""Constrained cut-off and limit optimisation with a trade-off curve.

Both functions only use loans whose outcome is observed (the approved book),
so results are subject to selection bias on the reject side; the output says
so explicitly (``caveats``) rather than pretending swap-in bad rates are known.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from .columns import as_flag
from .profit import breakeven_bad_rate, loan_pnl, portfolio_pnl


def _direction(score: pd.Series, bad: pd.Series) -> str:
    """'high_good' when a higher score means lower risk (the usual credit-score convention)."""
    corr = pd.Series(score).corr(pd.Series(bad), method="spearman")
    return "high_good" if (corr is None or np.isnan(corr) or corr <= 0) else "low_good"


def _frame(df: pd.DataFrame, score: str, bad: str, amount: Optional[str], approved: Optional[str], econ: Dict[str, float]) -> pd.DataFrame:
    d = pd.DataFrame({"score": pd.to_numeric(df[score], errors="coerce"), "bad": as_flag(df[bad])})
    d["amount"] = pd.to_numeric(df[amount], errors="coerce") if amount else econ["default_amount"]
    d["amount"] = d["amount"].fillna(econ["default_amount"])
    if approved:
        d["approved"] = as_flag(df[approved]).fillna(0)
    return d


def cutoff_optimize(
    df: pd.DataFrame,
    *,
    score: str,
    bad: str,
    econ: Dict[str, float],
    amount: Optional[str] = None,
    approved: Optional[str] = None,
    constraints: Optional[Dict[str, Any]] = None,
    grid: int = 19,
) -> Dict[str, Any]:
    cons = dict(constraints or {})
    max_bad = cons.get("max_bad_rate")
    min_appr = cons.get("min_approval_rate")
    d = _frame(df, score, bad, amount, approved, econ)
    scored_n = int(d["score"].notna().sum())
    book = d.dropna(subset=["score", "bad"])
    if approved and "approved" in book:
        book = book[book["approved"] > 0]
    if len(book) < 100:
        raise ValueError(f"cutoff: 有表现样本仅 {len(book)} 条，不足以回测 cut-off")
    direction = _direction(book["score"], book["bad"])

    def keep_mask(frame: pd.DataFrame, t: float) -> pd.Series:
        return frame["score"] >= t if direction == "high_good" else frame["score"] <= t

    base = portfolio_pnl(book["amount"], book["bad"], econ)
    baseline = {
        "policy": "current(全部已放款)",
        "threshold": None,
        "approved": base["loans"],
        "approval_rate": 1.0,
        "bad_cnt": int(book["bad"].sum()),
        "bad_rate": base["bad_rate"],
        "principal": base["principal"],
        "profit": base["profit"],
        "el_rate": base["el_rate"],
    }

    qs = np.linspace(0.0, 0.6, grid) if direction == "high_good" else np.linspace(1.0, 0.4, grid)
    thresholds = sorted({float(book["score"].quantile(q)) for q in qs})
    curve: List[Dict[str, Any]] = []
    for t in thresholds:
        sel = book[keep_mask(book, t)]
        if sel.empty:
            continue
        pnl = portfolio_pnl(sel["amount"], sel["bad"], econ)
        appr = len(sel) / len(book)
        ok = (max_bad is None or pnl["bad_rate"] <= float(max_bad)) and (min_appr is None or appr >= float(min_appr))
        curve.append(
            {
                "policy": f"score{'>=' if direction == 'high_good' else '<='}{t:.4g}",
                "threshold": t,
                "approved": int(len(sel)),
                "approval_rate": round(appr, 6),
                "bad_cnt": int(sel["bad"].sum()),
                "bad_rate": round(pnl["bad_rate"], 6),
                "principal": pnl["principal"],
                "profit": round(pnl["profit"], 2),
                "el_rate": round(pnl["el_rate"], 6) if pnl["el_rate"] is not None else None,
                "constraint_ok": bool(ok),
            }
        )
    feasible = [c for c in curve if c["constraint_ok"]]
    best = max(feasible, key=lambda c: c["profit"]) if feasible else None
    candidates = sorted(curve, key=lambda c: (not c["constraint_ok"], -c["profit"]))

    swap = {}
    if best is not None:
        kept = keep_mask(book, best["threshold"])
        out = book[~kept]
        swap = {
            "swap_out_count": int(len(out)),
            "swap_out_bad_rate": round(float(out["bad"].mean()), 6) if len(out) else None,
            "kept_bad_rate": best["bad_rate"],
            "swap_out_profit": round(float(loan_pnl(out["amount"].to_numpy(float), out["bad"].to_numpy(float), econ)["profit"].sum()), 2) if len(out) else 0.0,
        }
        if approved and "approved" in d:
            rej = d[(d["approved"] <= 0) & d["score"].notna()]
            swap["swap_in_count"] = int(keep_mask(rej, best["threshold"]).sum())
            swap["swap_in_bad_rate"] = None
    return {
        "analysis": "cutoff_optimize",
        "definition": {
            "objective": "最大化组合利润（利息收入 − 资金成本 − 信用损失 − 运营/获客成本）",
            "constraints": {"max_bad_rate": max_bad, "min_approval_rate": min_appr},
            "bad_rate": f"通过客群中 {bad}=1 的借据占比",
            "approval_rate": "新阈值下保留的已放款样本 / 全部已放款样本",
            "direction": "分数越高风险越低" if direction == "high_good" else "分数越低风险越低",
        },
        "score_column": score,
        "direction": direction,
        "baseline": baseline,
        "candidates": candidates[:10],
        "tradeoff_curve": sorted(curve, key=lambda c: c["approval_rate"]),
        "best": best,
        "constraint_satisfied": best is not None,
        "breakeven_bad_rate": breakeven_bad_rate(econ),
        "swap": swap,
        "scored_rows": scored_n,
        "caveats": [
            "仅使用已放款有表现样本，拒绝客群（swap-in）的真实坏账率未知，需拒绝推断或小流量测试验证",
            "利润按经济参数测算，参数来源见 economics_source",
        ],
    }


def limit_optimize(
    df: pd.DataFrame,
    *,
    score: str,
    bad: str,
    econ: Dict[str, float],
    amount: Optional[str] = None,
    bands: int = 5,
    multipliers: Optional[List[float]] = None,
    pd_elasticity: float = 0.3,
    max_el_rate: Optional[float] = None,
) -> Dict[str, Any]:
    """Per score band, pick the limit multiplier with the best expected profit.

    Higher limits raise exposure linearly and PD by ``pd_elasticity`` per +100%
    of limit (an industry rule of thumb, overridable)."""
    mults = multipliers or [0.5, 0.8, 1.0, 1.2, 1.5]
    if any(not np.isfinite(m) or m <= 0 for m in mults):
        raise ValueError("limit: 额度倍数必须为有限正数")
    if not np.isfinite(pd_elasticity) or pd_elasticity < 0:
        raise ValueError("limit: PD 弹性必须为有限非负数")
    if max_el_rate is not None and (not np.isfinite(max_el_rate) or not 0 <= max_el_rate <= 1):
        raise ValueError("limit: max_el_rate 必须位于 [0, 1]")
    d = _frame(df, score, bad, amount, None, econ).dropna(subset=["score", "bad"])
    if len(d) < 100:
        raise ValueError(f"limit: 有表现样本仅 {len(d)} 条，不足以做额度测算")
    direction = _direction(d["score"], d["bad"])
    d["band"] = pd.qcut(d["score"].rank(method="first"), q=bands, labels=False)
    if direction == "high_good":
        d["band"] = bands - 1 - d["band"]
    rows: List[Dict[str, Any]] = []
    for b, g in d.groupby("band"):
        pd0 = float(g["bad"].mean())
        amt = float(g["amount"].mean())
        options = []
        for m in mults:
            pdm = float(np.clip(pd0 * (1 + pd_elasticity * (m - 1)), 0.0, 1.0))
            p = loan_pnl(np.array([amt * m]), np.array([pdm]), econ)
            el_rate = float(p["loss"][0] / (amt * m)) if amt else None
            ok = max_el_rate is None or (el_rate is not None and el_rate <= max_el_rate)
            options.append({"multiplier": m, "pd": round(pdm, 6), "unit_profit": round(float(p["profit"][0]), 2), "el_rate": round(el_rate, 6) if el_rate is not None else None, "constraint_ok": ok})
        feas = [o for o in options if o["constraint_ok"]]
        best = max(feas, key=lambda o: o["unit_profit"]) if feas else None
        # The current policy is always 1x, including grids which omit 1x.
        current_profit = float(loan_pnl(np.array([amt]), np.array([pd0]), econ)["profit"][0])
        rows.append(
            {
                "band": int(b) + 1,
                "score_range": [float(g["score"].min()), float(g["score"].max())],
                "loans": int(len(g)),
                "bad_rate": round(pd0, 6),
                "avg_amount": round(amt, 2),
                "recommended_multiplier": best["multiplier"] if best else None,
                "constraint_satisfied": best is not None,
                "unit_profit_current": round(current_profit, 2),
                "unit_profit_recommended": best["unit_profit"] if best else None,
                "uplift_total": round((best["unit_profit"] - current_profit) * len(g), 2) if best else None,
                "options": options,
            }
        )
    return {
        "analysis": "limit_optimize",
        "definition": {
            "objective": "各评分段选择期望利润最高的额度倍数",
            "pd_response": f"额度每 +100%，PD 相对上升 {pd_elasticity:.0%}（行业经验值，可覆盖）",
            "constraints": {"max_el_rate": max_el_rate},
            "band_order": "band 1 风险最低",
        },
        "score_column": score,
        "bands": rows,
        "total_uplift": round(sum(r["uplift_total"] for r in rows), 2) if all(r["constraint_satisfied"] for r in rows) else None,
        "constraint_satisfied": all(
            any(o["constraint_ok"] and o["multiplier"] == r["recommended_multiplier"] for o in r["options"]) for r in rows
        ),
        "caveats": ["额度弹性为经验假设，建议通过 A/B 小流量验证后再全量调整"],
    }
