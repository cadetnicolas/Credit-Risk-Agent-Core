"""WOE binning and PDO score mapping."""

from __future__ import annotations

import numpy as np
import pandas as pd

from credit_agent.modeling.scoring import prob_to_score, scorecard_points_from_lr
from credit_agent.modeling.woe import WoEBinningModel, woe_table_numeric


def test_woe_table_has_woe_values():
    rng = np.random.default_rng(0)
    n = 300
    x = rng.normal(size=n)
    y = (x + rng.normal(scale=0.5, size=n) > 0).astype(int)
    iv, rows, edges = woe_table_numeric(pd.Series(x), pd.Series(y), n_bins=5)
    assert iv is not None and iv > 0
    assert rows[0]["woe"] is not None
    assert edges is not None


def test_woe_transform_and_prob_to_score():
    rng = np.random.default_rng(1)
    n = 400
    df = pd.DataFrame({"f1": rng.normal(size=n), "y": (rng.normal(size=n) > 0).astype(int)})
    m = WoEBinningModel().fit(df, ["f1"], "y", n_bins=5)
    w = m.transform(df)
    assert "f1" in w.columns
    scores = prob_to_score([0.1, 0.5, 0.9], base_score=600, pdo=50, base_odds=0.05)
    assert scores[0] > scores[1] > scores[2]


def test_prob_to_score_anchor_and_pdo():
    base_p = 0.05 / 1.05  # bad:good odds = 1/20
    s_base = prob_to_score([base_p], base_score=600, pdo=50, base_odds=0.05)[0]
    assert abs(s_base - 600) < 1e-6
    doubled_p = 0.1 / 1.1  # odds doubled -> 50 points lower
    s_dbl = prob_to_score([doubled_p], base_score=600, pdo=50, base_odds=0.05)[0]
    assert abs((s_base - s_dbl) - 50) < 1e-6


def test_scorecard_points_reconcile_with_prob_to_score():
    import math

    woe = {"features": {"f1": {"bins": [{"bin": "a", "woe": 0.5}, {"bin": "b", "woe": -0.3}]},
                        "f2": {"bins": [{"bin": "x", "woe": 0.2}]}}}
    coefs = [{"feature": "f1", "coef": 0.8}, {"feature": "f2", "coef": 1.1}]
    b0 = -2.5
    card = scorecard_points_from_lr(woe, coefs, intercept=b0, pdo=50, base_score=600, base_odds=0.05)
    pts = {f["feature"]: f["bins"] for f in card["features"]}
    total = card["base_points"] + pts["f1"][0]["points"] + pts["f2"][0]["points"]
    logit = b0 + 0.8 * 0.5 + 1.1 * 0.2
    p = 1 / (1 + math.exp(-logit))
    expected = prob_to_score([p], base_score=600, pdo=50, base_odds=0.05)[0]
    assert abs(total - expected) < 1e-2


def test_scorecard_points_structure():
    woe = {
        "features": {
            "f1": {
                "bins": [{"bin": "a", "woe": 0.5}, {"bin": "b", "woe": -0.3}],
                "iv": 0.1,
            }
        }
    }
    coefs = [{"feature": "f1", "coef": 0.8}]
    card = scorecard_points_from_lr(woe, coefs, pdo=50, base_score=600, base_odds=0.05)
    assert card["pdo"] == 50
    assert card["features"][0]["bins"][0]["points"] is not None
