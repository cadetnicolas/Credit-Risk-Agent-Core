"""Unit economics for consumer loans: revenue, funding, expected loss, opex.

    EL = PD × LGD × EAD
    profit = interest income − funding cost − credit loss − opex − acquisition cost

Defaults are industry reference values (see the built-in knowledge pack,
``strategy_acquisition.md`` → 盈利测算参考参数). Company-specific values passed
in ``economics`` always override them, and the output records which source
each parameter came from.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd

DEFAULT_ECONOMICS: Dict[str, float] = {
    "apr": 0.18,  # 年化利率
    "term_months": 12,
    "avg_balance_factor": 0.55,  # 等额本息平均占用 / 放款本金
    "funding_cost": 0.05,  # 年化资金成本
    "lgd": 0.65,  # 无抵押消费贷违约损失率
    "ead_factor": 0.75,  # 违约时剩余敞口 / 放款本金
    "opex_rate": 0.015,  # 年化运营成本 / 本金
    "acquisition_cost": 30.0,  # 单笔获客成本
    "default_amount": 10000.0,  # 无金额列时的单笔本金
}

DEFAULT_SOURCE = "行业参考值（内置知识包·策略与盈利测算），公司库口径优先"


def resolve_economics(*overrides: Optional[Dict[str, Any]]) -> Tuple[Dict[str, float], Dict[str, str]]:
    econ = dict(DEFAULT_ECONOMICS)
    source = {k: DEFAULT_SOURCE for k in econ}
    labels = ("课题上下文（人工确认）", "任务参数")
    for label, ov in zip(labels, overrides):
        for k, v in (ov or {}).items():
            if k in econ and v not in (None, ""):
                try:
                    econ[k] = float(v)
                    source[k] = label
                except (TypeError, ValueError):
                    continue
    return econ, source


def loan_pnl(amount: np.ndarray, bad: np.ndarray, econ: Dict[str, float]) -> Dict[str, np.ndarray]:
    """Realised P&L per loan given the observed bad flag (0/1) or a PD (0..1)."""
    years = econ["term_months"] / 12.0
    exposure = amount * econ["avg_balance_factor"] * years
    # a defaulted loan stops paying interest roughly half-way through
    income = amount * econ["apr"] * econ["avg_balance_factor"] * years * (1 - 0.5 * bad)
    funding = exposure * econ["funding_cost"]
    loss = bad * econ["lgd"] * econ["ead_factor"] * amount
    opex = exposure * econ["opex_rate"] + econ["acquisition_cost"]
    profit = income - funding - loss - opex
    return {"income": income, "funding": funding, "loss": loss, "opex": opex, "profit": profit}


def portfolio_pnl(amount: pd.Series, bad: pd.Series, econ: Dict[str, float]) -> Dict[str, Any]:
    a = amount.to_numpy(dtype=float)
    b = bad.to_numpy(dtype=float)
    p = loan_pnl(a, b, econ)
    principal = float(a.sum())
    out = {k: float(v.sum()) for k, v in p.items()}
    out.update(
        {
            "loans": int(len(a)),
            "principal": principal,
            "bad_rate": float(b.mean()) if len(b) else None,
            "el_rate": float(p["loss"].sum() / principal) if principal else None,
            "profit_margin": float(p["profit"].sum() / principal) if principal else None,
        }
    )
    return out


def breakeven_bad_rate(econ: Dict[str, float]) -> Optional[float]:
    """Bad rate at which a loan's expected profit is zero."""
    a = np.array([econ["default_amount"]])
    good = loan_pnl(a, np.array([0.0]), econ)["profit"][0]
    bad = loan_pnl(a, np.array([1.0]), econ)["profit"][0]
    if good <= bad:
        return None
    return float(good / (good - bad))
