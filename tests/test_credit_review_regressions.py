"""Regression cases for misleading risk monitoring and infeasible policies."""

import numpy as np
import pandas as pd
import pytest

from credit_agent.analytics.optimize import limit_optimize
from credit_agent.analytics.profit import DEFAULT_ECONOMICS, loan_pnl
from credit_agent.analytics.vintage import vintage_curves
from credit_agent.modeling.metrics import psi


def test_psi_counts_current_values_outside_reference_range():
    reference = np.linspace(0, 1, 1000)
    assert psi(reference, reference) == pytest.approx(0)
    assert psi(reference, reference + 10) > 1
    assert psi(reference, reference - 10) > 1


def _vintage(df):
    return vintage_curves(df, loan_id="id", cohort="cohort", mob="mob", bad="bad")


def test_vintage_rejects_duplicate_loan_mob():
    df = pd.DataFrame({"id": [1, 1, 1], "cohort": ["a"] * 3, "mob": [1, 1, 2], "bad": [1, 1, 1]})
    with pytest.raises(ValueError, match="重复"):
        _vintage(df)


def test_vintage_comparison_excludes_immature_point():
    df = pd.DataFrame({"id": [1, 1, 2, 2, 3, 3, 4], "cohort": ["a"] * 4 + ["b"] * 3,
                       "mob": [1, 2, 1, 2, 1, 2, 1], "bad": [0, 1, 0, 0, 0, 1, 0]})
    out = _vintage(df)
    assert out["comparable_mob"] == 2
    assert [r["cohort"] for r in out["compare_table"]] == ["a"]


@pytest.mark.parametrize("mobs", [[-1, 1], [1, 1.5]])
def test_vintage_rejects_invalid_mob(mobs):
    with pytest.raises(ValueError, match="非负整数"):
        _vintage(pd.DataFrame({"id": [1, 1], "cohort": ["a", "a"], "mob": mobs, "bad": [0, 1]}))


def _loans():
    return pd.DataFrame({"score": np.arange(200), "bad": np.tile([0, 1], 100), "amount": 10000.0})


def test_limit_infeasible_has_no_recommendation_or_uplift():
    out = limit_optimize(_loans(), score="score", bad="bad", amount="amount", econ=dict(DEFAULT_ECONOMICS), max_el_rate=0)
    assert out["constraint_satisfied"] is False
    assert out["total_uplift"] is None
    assert all(b["recommended_multiplier"] is None and b["uplift_total"] is None for b in out["bands"])


def test_limit_baseline_is_one_even_when_grid_omits_one():
    econ = dict(DEFAULT_ECONOMICS)
    out = limit_optimize(_loans(), score="score", bad="bad", amount="amount", econ=econ, multipliers=[0.5, 1.5])
    expected = loan_pnl(np.array([10000.0]), np.array([0.5]), econ)["profit"][0]
    assert all(b["unit_profit_current"] == pytest.approx(round(expected, 2)) for b in out["bands"])


@pytest.mark.parametrize("multipliers", [[0], [-1], [float("nan")]])
def test_limit_rejects_invalid_multipliers(multipliers):
    with pytest.raises(ValueError, match="有限正数"):
        limit_optimize(_loans(), score="score", bad="bad", econ=dict(DEFAULT_ECONOMICS), multipliers=multipliers)
