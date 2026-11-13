"""Synthetic checks for the logistic scorecard helpers."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest
from sklearn.linear_model import LogisticRegression

from credit_agent.modeling.logistic import (
    coefficient_signs,
    decile_table,
    fit_logistic,
    stepwise_logistic,
    vif_table,
)
from credit_agent.modeling.scoring import prob_to_score, scorecard_points_from_lr


def _synthetic(n: int = 2000, seed: int = 0):
    rng = np.random.default_rng(seed)
    signal = rng.normal(size=n)
    signal_2 = rng.normal(size=n)
    logit = -0.15 + 1.7 * signal + 1.4 * signal_2
    prob = 1.0 / (1.0 + np.exp(-np.clip(logit, -30.0, 30.0)))
    y = rng.binomial(1, prob).astype(int)
    design = np.column_stack([np.ones(n), signal, signal_2, y.astype(float)])
    noise = rng.normal(size=n)
    beta, *_ = np.linalg.lstsq(design, noise, rcond=None)
    noise = noise - design @ beta
    noise = noise / float(noise.std())
    frame = pd.DataFrame({"signal": signal, "signal_2": signal_2, "noise": noise})
    return frame, y, prob


def test_fit_logistic_positive_coefficients_and_aic():
    frame, y, _prob = _synthetic()
    features = ["signal", "signal_2"]
    fitted = fit_logistic(frame, y, features)
    assert set(fitted) == {"coefficients", "intercept", "n", "pos", "aic", "log_likelihood"}
    assert fitted["n"] == len(y)
    assert fitted["pos"] == int(y.sum())
    coef = {row["feature"]: row["coef"] for row in fitted["coefficients"]}
    assert coef["signal"] > 0
    assert coef["signal_2"] > 0
    magnitudes = [abs(row["coef"]) for row in fitted["coefficients"]]
    assert magnitudes == sorted(magnitudes, reverse=True)
    k = len(features) + 1
    assert fitted["aic"] == pytest.approx(2 * k - 2 * fitted["log_likelihood"])

    model = LogisticRegression(C=1.0, max_iter=2000, random_state=0, solver="lbfgs")
    model.fit(frame[features].to_numpy(dtype=float), y)
    probability = np.clip(model.predict_proba(frame[features].to_numpy(dtype=float))[:, 1], 1e-15, 1 - 1e-15)
    log_likelihood = float(np.sum(y * np.log(probability) + (1 - y) * np.log(1.0 - probability)))
    assert fitted["log_likelihood"] == pytest.approx(log_likelihood)
    assert fitted["intercept"] == pytest.approx(float(model.intercept_[0]))

    named = frame.copy()
    named["target"] = y
    via_column = fit_logistic(named, "target", features)
    assert via_column["aic"] == pytest.approx(fitted["aic"])
    card = scorecard_points_from_lr({"features": {}}, fitted["coefficients"], intercept=fitted["intercept"])
    assert card["lr_intercept"] == pytest.approx(fitted["intercept"])
    assert {item["feature"] for item in card["features"]} == set(features)


def test_stepwise_keeps_signal_and_drops_noise():
    frame, y, _prob = _synthetic()
    features = ["signal", "signal_2", "noise"]
    p_hat = float(np.clip(y.mean(), 1e-15, 1.0 - 1e-15))
    null_llf = float(np.sum(y * np.log(p_hat) + (1 - y) * np.log(1.0 - p_hat)))
    null_aic = 2.0 - 2.0 * null_llf
    for direction in ("forward", "backward", "both"):
        result = stepwise_logistic(frame, y, features, direction=direction)
        assert set(result["selected"]) == {"signal", "signal_2"}
        assert "noise" not in result["selected"]
        assert result["path"][-1]["features"] == result["selected"]
        assert result["final"]["aic"] == pytest.approx(result["path"][-1]["aic"])
        assert all(set(step) == {"features", "aic"} for step in result["path"])
        assert all(step["aic"] > later["aic"] for step, later in zip(result["path"], result["path"][1:]))
        coef = {row["feature"]: row["coef"] for row in result["final"]["coefficients"]}
        assert coef["signal"] > 0
        assert coef["signal_2"] > 0
        if direction in ("forward", "both"):
            assert result["path"][0]["features"] == []
            assert result["path"][0]["aic"] == pytest.approx(null_aic)


def test_coefficient_signs_reports_mismatch_without_dropping():
    rows = [
        {"feature": "good", "coef": 0.4},
        {"feature": "bad_sign", "coef": -0.2},
        {"feature": "flat", "coef": 0.0},
    ]
    snapshot = [dict(row) for row in rows]
    positive = coefficient_signs(rows, expected_sign="positive")
    assert rows == snapshot
    assert {item["feature"] for item in positive["violations"]} == {"bad_sign", "flat"}
    assert positive["n_violations"] == 2
    assert {item["sign"] for item in positive["violations"]} == {"negative", "zero"}
    negative = coefficient_signs(rows, expected_sign="negative")
    assert {item["feature"] for item in negative["violations"]} == {"good", "flat"}
    assert coefficient_signs(rows, expected_sign="any")["violations"] == []
    with pytest.raises(ValueError, match="expected_sign"):
        coefficient_signs(rows, expected_sign="plus")

    rng = np.random.default_rng(1)
    n = 1500
    good = rng.normal(size=n)
    bad_sign = rng.normal(size=n)
    logit = 0.1 + 1.5 * good - 1.7 * bad_sign
    prob = 1.0 / (1.0 + np.exp(-logit))
    y = rng.binomial(1, prob).astype(int)
    frame = pd.DataFrame({"good": good, "bad_sign": bad_sign})
    fitted = fit_logistic(frame, y, ["good", "bad_sign"])
    coef = {row["feature"]: row["coef"] for row in fitted["coefficients"]}
    assert coef["good"] > 0
    assert coef["bad_sign"] < 0
    report = coefficient_signs(fitted["coefficients"], expected_sign="positive")
    assert {item["feature"] for item in report["violations"]} == {"bad_sign"}
    assert any(row["feature"] == "bad_sign" for row in fitted["coefficients"])


def test_vif_reports_collinear_columns():
    rng = np.random.default_rng(2)
    n = 400
    base = rng.normal(size=n)
    frame = pd.DataFrame({"base": base, "copy": 2.0 * base, "other": rng.normal(size=n)})
    table = vif_table(frame, ["base", "copy", "other"])
    by_name = {row["feature"]: row for row in table["rows"]}
    assert by_name["base"]["vif"] is None
    assert by_name["base"]["perfect_collinear"] is True
    assert by_name["copy"]["vif"] is None
    assert by_name["copy"]["perfect_collinear"] is True
    assert by_name["other"]["perfect_collinear"] is False
    assert by_name["other"]["vif"] is not None
    assert by_name["other"]["vif"] < 2
    with pytest.raises(ValueError, match="2 个特征"):
        vif_table(frame, ["base"])


def test_decile_table_fields_and_risk_order():
    _frame, y, prob = _synthetic()
    subset = decile_table(y, prob, fields=["bad_rate", "bin"])
    assert list(subset["rows"][0]) == ["bad_rate", "bin"]
    assert "n" not in subset["rows"][0]
    full = decile_table(y, prob)
    assert list(full["rows"][0]) == ["bin", "n", "bad", "bad_rate", "cum_bad_capture", "ks"]
    assert full["rows"][0]["bin"] == 1
    assert full["rows"][0]["bad_rate"] > full["rows"][-1]["bad_rate"]
    assert full["rows"][-1]["cum_bad_capture"] == pytest.approx(1.0)
    assert full["rows"][0]["ks"] >= 0
    assert full["auc"] is not None and full["auc"] > 0.7
    assert full["ks"] is not None and full["ks"] > 0

    scores = prob_to_score(prob)
    aligned = decile_table(y, scores, higher_score_safer=True, fields=["bin", "bad_rate"])
    flipped = decile_table(y, scores, higher_score_safer=False, fields=["bin", "bad_rate"])
    direct = decile_table(y, prob, fields=["bin", "bad_rate"])
    assert [row["bad_rate"] for row in aligned["rows"]] == pytest.approx(
        [row["bad_rate"] for row in direct["rows"]]
    )
    assert aligned["rows"][0]["bad_rate"] > aligned["rows"][-1]["bad_rate"]
    assert flipped["rows"][0]["bad_rate"] < flipped["rows"][-1]["bad_rate"]
    with pytest.raises(ValueError, match="不支持的列"):
        decile_table(y, prob, fields=["bin", "score"])


def test_rejects_non_binary_y():
    frame, y, _prob = _synthetic(n=80)
    mixed = y.copy()
    mixed[0] = 2
    with pytest.raises(ValueError, match="0/1"):
        fit_logistic(frame, mixed, ["signal"])
    with pytest.raises(ValueError, match="0/1"):
        stepwise_logistic(frame, mixed, ["signal", "signal_2"])
    with pytest.raises(ValueError, match="0/1"):
        fit_logistic(frame, np.zeros(len(frame), dtype=int), ["signal"])
    with pytest.raises(ValueError, match="0/1"):
        decile_table(mixed, np.full(len(mixed), 0.5))


def test_rejects_missing_penalty_and_small_sample():
    frame, y, _prob = _synthetic(n=40)
    missing = frame.copy()
    missing.loc[missing.index[0], "signal"] = np.nan
    with pytest.raises(ValueError, match="缺失"):
        fit_logistic(missing, y, ["signal", "signal_2"])
    with pytest.raises(ValueError, match="l2"):
        fit_logistic(frame, y, ["signal"], penalty="l1")
    with pytest.raises(ValueError, match="不能为空"):
        stepwise_logistic(frame, y, [])
    tiny = pd.DataFrame({f"f{i}": np.arange(5, dtype=float) + i for i in range(4)})
    labels = np.array([0, 1, 0, 1, 0])
    with pytest.raises(ValueError, match="特征数加 2"):
        stepwise_logistic(tiny, labels, list(tiny.columns))
    with pytest.raises(ValueError, match="forward"):
        stepwise_logistic(frame, y, ["signal", "signal_2"], direction="sideways")
