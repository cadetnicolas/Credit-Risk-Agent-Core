"""Caller-selected report metrics and per-value model runs."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from credit_agent.modeling.by_segment import run_by_segment
from credit_agent.modeling.metrics import auc_score, decile_bad_rates, ks_score, psi
from credit_agent.modeling.report_spec import METRIC_NAMES, ReportSpec, build_report


def _separated_scores():
    rng = np.random.default_rng(7)
    y = np.array([0] * 80 + [1] * 80, dtype=float)
    p = np.concatenate([rng.uniform(0.05, 0.35, 80), rng.uniform(0.65, 0.95, 80)])
    return y, p


def _people(value: str, rows: int, positives: int) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "segment_col": [value] * rows,
            "outcome": [1] * positives + [0] * (rows - positives),
        }
    )


def test_unknown_or_empty_metric_is_rejected():
    with pytest.raises(ValueError, match="指标"):
        ReportSpec(("gini",))
    with pytest.raises(ValueError, match="指标"):
        ReportSpec(())
    with pytest.raises(ValueError, match="分箱"):
        ReportSpec(("ks",), n_bins=1)
    with pytest.raises(ValueError, match="分箱"):
        ReportSpec(("ks",), n_bins=21)
    assert ReportSpec(["ks"]).metrics == ("ks",)
    assert ReportSpec(("auc",), n_bins=2).n_bins == 2
    assert ReportSpec(METRIC_NAMES, n_bins=20).n_bins == 20


def test_report_contains_only_requested_ks():
    y, p = _separated_scores()
    report = build_report(y, p, ReportSpec(("ks",), sample_name="holdout"))
    assert set(report) == {"sample_name", "n", "metrics_requested", "ks"}
    assert "auc" not in report
    assert report["sample_name"] == "holdout"
    assert report["n"] == len(y)
    assert report["metrics_requested"] == ("ks",)
    assert report["ks"] == pytest.approx(ks_score(y, p, n_bins=10)[0])


def test_ks_uses_requested_bin_count():
    y = np.array([0, 1] * 8, dtype=float)
    p = np.linspace(0.1, 0.9, 16)
    narrow = build_report(y, p, ReportSpec(("ks",), n_bins=20))
    wide = build_report(y, p, ReportSpec(("ks",), n_bins=10))
    assert narrow["ks"] is None
    assert wide["ks"] == pytest.approx(ks_score(y, p, n_bins=10)[0])


def test_psi_without_reference_is_null():
    y, p = _separated_scores()
    report = build_report(y, p, ReportSpec(("psi", "ks")))
    assert report["psi"] is None
    assert "auc" not in report
    assert any("参照" in note for note in report["notes"])


def test_requested_metrics_match_library():
    y, p = _separated_scores()
    reference = np.clip(p + 0.05, 0, 1)
    report = build_report(
        y,
        p,
        ReportSpec(METRIC_NAMES, n_bins=10, sample_name="dev"),
        reference_prob=reference,
    )
    assert report["sample_name"] == "dev"
    assert report["n"] == len(y)
    assert report["metrics_requested"] == METRIC_NAMES
    assert report["ks"] == pytest.approx(ks_score(y, p, n_bins=10)[0])
    assert report["auc"] == pytest.approx(auc_score(y, p))
    assert report["psi"] == pytest.approx(psi(reference, p, n_bins=10))
    assert report["bad_rate"] == pytest.approx(float(np.mean(y)))
    assert report["decile_bad_rate"] == decile_bad_rates(y, p, n_bins=10)
    assert "notes" not in report


def test_lift_uses_highest_predicted_risk_bin():
    y = np.array([0, 0, 1, 1, 1, 0], dtype=float)
    p = np.array([0.9, 0.8, 0.7, 0.6, 0.2, 0.1])
    report = build_report(y, p, ReportSpec(("lift", "bad_rate"), n_bins=3))
    assert report["bad_rate"] == pytest.approx(0.5)
    assert report["lift"] == pytest.approx(0.0)

    ranked = np.array([1, 1, 1, 1, 0, 0, 0, 0], dtype=float)
    scores = np.array([0.9, 0.8, 0.7, 0.6, 0.4, 0.3, 0.2, 0.1])
    lifted = build_report(ranked, scores, ReportSpec(("lift",), n_bins=4))
    assert lifted["lift"] == pytest.approx(2.0)


def test_lift_null_when_overall_bad_rate_is_zero():
    report = build_report(np.zeros(8), np.linspace(0.1, 0.9, 8), ReportSpec(("lift",), n_bins=2))
    assert report["lift"] is None
    assert any("坏账" in note for note in report["notes"])


def test_label_and_score_length_mismatch():
    with pytest.raises(ValueError, match="长度"):
        build_report([0, 1], [0.2], ReportSpec(("bad_rate",)))


def test_missing_segment_column():
    frame = pd.DataFrame({"outcome": [0, 1]})
    with pytest.raises(ValueError, match="分群列"):
        run_by_segment(frame, "missing", lambda part: {}, label="outcome")


def test_small_segment_is_skipped_without_calling_runner():
    frame = pd.concat([_people("keep", 60, 20), _people("tiny", 9, 4)], ignore_index=True)
    called: list[str] = []

    def runner(part: pd.DataFrame) -> dict:
        called.append(str(part["segment_col"].iloc[0]))
        return {"ok": True}

    out = run_by_segment(frame, "segment_col", runner, label="outcome")
    assert called == ["keep"]
    assert [row["value"] for row in out["ran"]] == ["keep"]
    assert out["ran"][0]["result"] == {"ok": True}
    assert [row["value"] for row in out["skipped"]] == ["tiny"]
    assert out["failed"] == []
    assert out["n_values"] == 2
    assert out["segment"] == "segment_col"


def test_imbalanced_segment_is_skipped():
    frame = pd.concat([_people("few_bad", 60, 4), _people("keep", 60, 20)], ignore_index=True)
    called: list[str] = []

    def runner(part: pd.DataFrame) -> dict:
        called.append(str(part["segment_col"].iloc[0]))
        return {"ok": True}

    out = run_by_segment(
        frame,
        "segment_col",
        runner,
        min_rows=50,
        min_positive=10,
        min_negative=10,
        label="outcome",
    )
    assert called == ["keep"]
    skipped = out["skipped"][0]
    assert skipped["value"] == "few_bad"
    assert skipped["rows"] == 60
    assert "坏样本不足" in skipped["reason"]
    assert "行数不足" not in skipped["reason"]


def test_runner_error_is_isolated():
    frame = pd.concat([_people("b", 60, 20), _people("a", 60, 20)], ignore_index=True)

    def runner(part: pd.DataFrame) -> dict:
        value = part["segment_col"].iloc[0]
        if value == "a":
            raise RuntimeError("a failed")
        return {"ok": True}

    out = run_by_segment(frame, "segment_col", runner, label="outcome")
    assert [row["value"] for row in out["ran"]] == ["b"]
    assert out["ran"][0]["result"] == {"ok": True}
    assert [row["value"] for row in out["failed"]] == ["a"]
    assert "a failed" in out["failed"][0]["error"]
    assert out["skipped"] == []
    assert out["n_values"] == 2


def test_every_eligible_value_has_a_result_in_sorted_order():
    frame = pd.concat([_people("b", 55, 12), _people("a", 70, 30)], ignore_index=True)
    out = run_by_segment(
        frame,
        "segment_col",
        lambda part: {"rows": int(len(part))},
        label="outcome",
    )
    assert [(row["value"], row["rows"], row["result"]) for row in out["ran"]] == [
        ("a", 70, {"rows": 70}),
        ("b", 55, {"rows": 55}),
    ]
    assert out["skipped"] == []
    assert out["failed"] == []
    assert out["n_values"] == 2


def test_exact_thresholds_still_run():
    frame = _people("edge", 50, 10)
    out = run_by_segment(frame, "segment_col", lambda part: {"ok": True}, label="outcome")
    assert [row["value"] for row in out["ran"]] == ["edge"]
    assert out["skipped"] == []
