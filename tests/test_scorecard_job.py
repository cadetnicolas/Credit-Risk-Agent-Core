"""Synthetic checks for the logistic scorecard job."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from credit_agent.analytics import ROLE_LABELS, marginal_bands, resolve_columns, score_grid, swap_table
from credit_agent.modeling import (
    ModelingAnalyzer,
    ReportSpec,
    build_report,
    coefficient_signs,
    decile_table,
    fit_logistic,
    fit_scorecard,
    fit_scorecard_by_segment,
    run_by_segment,
    stepwise_logistic,
    vif_table,
)
from credit_agent.modeling.metrics import auc_score, psi
from credit_agent.modeling.split import dev_test_split


_FEATURES = ["signal", "signal_2", "noise"]
_STRUCTURAL = {"sample_name", "n", "metrics_requested", "notes"}


def _population(months: int = 10, per_month: int = 40, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    n = months * per_month
    signal = rng.normal(size=n)
    signal_2 = rng.normal(size=n)
    noise = rng.normal(size=n)
    logit = -0.2 + 2.0 * signal + 1.6 * signal_2
    probability = 1.0 / (1.0 + np.exp(-np.clip(logit, -30.0, 30.0)))
    label = rng.binomial(1, probability).astype(int).reshape(months, per_month)
    label[:, 0] = 0
    label[:, 1] = 1
    label = label.ravel()
    design = np.column_stack([np.ones(n), signal, signal_2, label.astype(float)])
    beta, *_ = np.linalg.lstsq(design, noise, rcond=None)
    noise = noise - design @ beta
    scale = float(noise.std())
    if scale > 0:
        noise = noise / scale
    when = np.repeat(pd.date_range("2020-01-01", periods=months, freq="MS"), per_month)
    return pd.DataFrame(
        {
            "signal": signal,
            "signal_2": signal_2,
            "noise": noise,
            "bad": label.astype(int),
            "apply_time": when,
        }
    )


def _with_gaps(frame: pd.DataFrame) -> pd.DataFrame:
    extra = frame.iloc[:2].copy()
    extra.iloc[0, extra.columns.get_loc("signal")] = np.nan
    extra.iloc[1, extra.columns.get_loc("bad")] = np.nan
    return pd.concat([frame, extra], ignore_index=True)


def _probability(part: pd.DataFrame, result: dict) -> np.ndarray:
    coef = {row["feature"]: row["coef"] for row in result["coefficients"]}
    names = list(result["selected"])
    beta = np.array([coef[name] for name in names], dtype=float)
    linear = float(result["intercept"]) + part.loc[:, names].to_numpy(dtype=float) @ beta
    return 1.0 / (1.0 + np.exp(-linear))


def _metric_keys(report: dict) -> set:
    return set(report) - _STRUCTURAL


def test_public_entries_remain_importable():
    assert ModelingAnalyzer is not None
    assert callable(fit_logistic)
    assert callable(stepwise_logistic)
    assert callable(coefficient_signs)
    assert callable(vif_table)
    assert callable(decile_table)
    assert callable(build_report)
    assert callable(run_by_segment)
    assert callable(fit_scorecard)
    assert callable(fit_scorecard_by_segment)
    assert ReportSpec(("ks",)).metrics == ("ks",)
    assert callable(score_grid)
    assert callable(marginal_bands)
    assert callable(swap_table)
    assert callable(resolve_columns)
    assert "bad" in ROLE_LABELS


def test_fit_scorecard_out_of_time_reports_only_requested_metrics():
    raw = _population()
    frame = _with_gaps(raw)
    before = frame.copy()
    metrics = ("ks", "auc")
    result = fit_scorecard(
        frame,
        label="bad",
        features=_FEATURES,
        time_column="apply_time",
        test_size=0.3,
        random_state=0,
        report_metrics=metrics,
    )
    pd.testing.assert_frame_equal(frame, before)
    assert set(result) == {
        "selected",
        "coefficients",
        "intercept",
        "signs",
        "vif",
        "split_method",
        "split_description",
        "dropped_rows",
        "dev_report",
        "holdout_report",
        "aic",
    }
    assert result["dropped_rows"] == 2
    assert result["split_method"] == "oot"
    assert "apply_time" in result["split_description"]
    assert _metric_keys(result["holdout_report"]) == set(metrics)
    assert result["holdout_report"]["metrics_requested"] == metrics
    assert result["holdout_report"]["sample_name"] == "holdout"
    assert "psi" not in result["holdout_report"]
    assert "bad_rate" not in result["holdout_report"]
    assert "lift" not in result["holdout_report"]
    assert "decile_bad_rate" not in result["holdout_report"]
    assert _metric_keys(result["dev_report"]) == set(metrics)
    assert {"signal", "signal_2"} <= set(result["selected"])
    assert set(result["selected"]) <= set(_FEATURES)
    coef = {row["feature"]: row["coef"] for row in result["coefficients"]}
    assert set(coef) == set(result["selected"])
    assert coef["signal"] > 0
    assert coef["signal_2"] > 0
    assert result["signs"]["expected_sign"] == "positive"
    assert {row["feature"] for row in result["vif"]["rows"]} == set(result["selected"])

    work = raw.reset_index(drop=True)
    y = work["bad"].to_numpy(dtype=int)
    split = dev_test_split(work, y, test_size=0.3, random_state=0, time_column="apply_time")
    dev = work.iloc[np.flatnonzero(split.dev_mask)].reset_index(drop=True)
    holdout = work.iloc[np.flatnonzero(split.test_mask)].reset_index(drop=True)
    dev_fit = fit_logistic(dev, y[split.dev_mask], result["selected"])
    assert result["aic"] == pytest.approx(dev_fit["aic"])
    assert result["intercept"] == pytest.approx(dev_fit["intercept"])
    assert result["split_description"] == split.description
    dev_prob = _probability(dev, result)
    holdout_prob = _probability(holdout, result)
    assert result["dev_report"]["auc"] == pytest.approx(auc_score(y[split.dev_mask], dev_prob))
    assert result["holdout_report"]["auc"] == pytest.approx(auc_score(y[split.test_mask], holdout_prob))
    assert result["dev_report"]["n"] + result["holdout_report"]["n"] + result["dropped_rows"] == len(frame)


def test_holdout_psi_uses_development_probabilities_as_reference():
    frame = _population()
    metrics = ("psi", "auc")
    result = fit_scorecard(
        frame,
        label="bad",
        features=_FEATURES,
        time_column="apply_time",
        report_metrics=metrics,
    )
    assert result["split_method"] == "oot"
    assert _metric_keys(result["holdout_report"]) == set(metrics)
    assert result["holdout_report"]["psi"] is not None
    assert "notes" not in result["holdout_report"] or not any(
        "参照" in note for note in result["holdout_report"].get("notes", [])
    )
    assert result["dev_report"]["psi"] is None
    assert any("参照" in note for note in result["dev_report"]["notes"])

    y = frame["bad"].to_numpy(dtype=int)
    split = dev_test_split(frame, y, test_size=0.3, random_state=0, time_column="apply_time")
    dev = frame.iloc[np.flatnonzero(split.dev_mask)]
    holdout = frame.iloc[np.flatnonzero(split.test_mask)]
    assert result["holdout_report"]["psi"] == pytest.approx(
        psi(_probability(dev, result), _probability(holdout, result), n_bins=10)
    )


def test_without_time_column_still_calls_random_split():
    frame = _population().drop(columns=["apply_time"])
    result = fit_scorecard(frame, label="bad", features=_FEATURES, report_metrics=("auc",))
    assert result["split_method"] == "stratified_random"
    assert "70/30" in result["split_description"]
    assert set(result["holdout_report"]) == {"sample_name", "n", "metrics_requested", "auc"}


def test_shared_entity_id_is_passed_to_the_split():
    frame = _population(months=4, per_month=15)
    frame["entity"] = "shared"
    with pytest.raises(ValueError, match="实体"):
        fit_scorecard(
            frame,
            label="bad",
            features=["signal", "signal_2"],
            time_column="apply_time",
            id_column="entity",
        )


def test_scorecard_by_segment_skips_the_small_group():
    large = _population()
    large["segment"] = "large"
    small = _population(months=2, per_month=4, seed=1)
    small["segment"] = "small"
    frame = pd.concat([large, small], ignore_index=True)
    result = fit_scorecard_by_segment(
        frame,
        segment="segment",
        label="bad",
        features=_FEATURES,
        time_column="apply_time",
    )
    assert result["segment"] == "segment"
    assert result["n_values"] == 2
    assert result["failed"] == []
    assert [row["value"] for row in result["skipped"]] == ["small"]
    assert "行数不足" in result["skipped"][0]["reason"]
    assert [row["value"] for row in result["ran"]] == ["large"]
    selected = result["ran"][0]["result"]["selected"]
    assert selected
    assert "segment" not in selected
    assert result["ran"][0]["result"]["split_method"] == "oot"


def test_segment_column_cannot_be_a_feature():
    frame = pd.DataFrame({"segment": ["a"], "bad": [0], "x": [1.0]})
    with pytest.raises(ValueError, match="分群"):
        fit_scorecard_by_segment(
            frame,
            segment="segment",
            label="bad",
            features=["x", "segment"],
        )
