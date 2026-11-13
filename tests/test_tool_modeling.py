"""Synthetic checks for sample preparation, feature review, and candidate training."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from credit_agent.tools import Ledger
from credit_agent.tools.evidence import record_human_decision
from credit_agent.tools.evidence import verify_and_commit_report
from credit_agent.tools.modeling_ops import (
    prepare_modeling_sample,
    profile_binning,
    screen_features,
    train_and_evaluate_candidate,
)
from credit_agent.modeling.workflows.training import _new_lgbm


def _book(tmp_path, frame: pd.DataFrame, mask: np.ndarray, *, status: str = "ok"):
    ledger = Ledger(tmp_path)
    snapshot_id = "snap_model"
    ledger.save_frame(snapshot_id, frame)
    ledger.save_mask(snapshot_id, pd.Series(mask))
    if status:
        ledger.save_admission(snapshot_id, "A", status)
    return ledger, snapshot_id


def _modeling_frame(n_months: int = 10, per_month: int = 40, seed: int = 0):
    rng = np.random.default_rng(seed)
    months = []
    labels = []
    for index in range(n_months):
        month = np.full(per_month, 202001 + index)
        label = np.zeros(per_month, dtype=int)
        label[:16] = 1
        rng.shuffle(label)
        months.append(month)
        labels.append(label)
    label = np.concatenate(labels)
    n = len(label)
    signal = label + rng.normal(0, 0.55, size=n)
    weak = rng.normal(size=n) + 0.15 * label
    noise = rng.normal(size=n)
    leak = label * 8 + rng.normal(0, 0.05, size=n)
    post_loan = label.copy()
    frame = pd.DataFrame(
        {
            "obs_month": np.concatenate(months),
            "label": label,
            "f_signal": signal,
            "f_weak": weak,
            "f_noise": noise,
            "f_leak": leak,
            "post_loan": post_loan,
        }
    )
    gray = pd.DataFrame(
        {
            "obs_month": np.full(12, 202001),
            "label": np.full(12, 2),
            "f_signal": rng.normal(size=12),
            "f_weak": rng.normal(size=12),
            "f_noise": rng.normal(size=12),
            "f_leak": rng.normal(size=12),
            "post_loan": np.zeros(12, dtype=int),
        }
    )
    immature = pd.DataFrame(
        {
            "obs_month": np.full(12, 202002),
            "label": np.array([0, 1] * 6),
            "f_signal": rng.normal(size=12),
            "f_weak": rng.normal(size=12),
            "f_noise": rng.normal(size=12),
            "f_leak": rng.normal(size=12),
            "post_loan": np.zeros(12, dtype=int),
        }
    )
    full = pd.concat([frame, gray, immature], ignore_index=True)
    mask = np.array([True] * len(frame) + [True] * len(gray) + [False] * len(immature))
    return full, mask


def test_prepare_blocks_without_admission_or_time_or_mask(tmp_path):
    frame, mask = _modeling_frame()
    ledger, snapshot_id = _book(tmp_path / "admit", frame, mask, status="")
    missing = prepare_modeling_sample(snapshot_id, ledger=ledger, label="label", time_column="obs_month")
    assert missing["status"] == "blocked"
    assert missing["block_code"] == "ADMISSION_REQUIRED"

    ledger, snapshot_id = _book(tmp_path / "blocked", frame, mask, status="blocked")
    blocked_admission = prepare_modeling_sample(snapshot_id, ledger=ledger, label="label", time_column="obs_month")
    assert blocked_admission["block_code"] == "ADMISSION_REQUIRED"

    ledger, snapshot_id = _book(tmp_path / "time", frame, mask)
    no_time = prepare_modeling_sample(snapshot_id, ledger=ledger, label="label", time_column=None)
    assert no_time["status"] == "needs_human"
    assert no_time["human_gate"]["gate"] == "validation_fallback"
    assert ledger.get_run(no_time["human_gate"]["review_id"])["tool"] == "validation_fallback"

    ledger, snapshot_id = _book(tmp_path / "mask", frame, mask)
    ledger.frames.joinpath(f"{snapshot_id}.mask.parquet").unlink()
    immature = prepare_modeling_sample(snapshot_id, ledger=ledger, label="label", time_column="obs_month")
    assert immature["block_code"] == "LABEL_IMMATURE"


def test_numeric_seconds_are_not_guessed_as_dates(tmp_path):
    rng = np.random.default_rng(1)
    n = 40
    frame = pd.DataFrame(
        {
            "ts": np.arange(n) + 1_700_000_000,
            "label": np.array([0, 1] * (n // 2)),
            "f1": rng.normal(size=n),
        }
    )
    ledger, snapshot_id = _book(tmp_path, frame, np.ones(n, dtype=bool))
    with pytest.raises(ValueError, match="无法解释的数值"):
        prepare_modeling_sample(snapshot_id, ledger=ledger, label="label", time_column="ts")


@pytest.mark.parametrize("method,split_method", [
    ("random_holdout", "stratified_random"),
    ("cv_only", "cv_only"),
])
def test_no_oot_requires_trusted_user_choice_and_marks_weaker_validation(tmp_path, method, split_method):
    frame, mask = _modeling_frame()
    ledger, snapshot_id = _book(tmp_path, frame, mask)
    options = {"label": "label", "time_column": None, "gray_values": (2,),
               "gray_policy": "exclude", "exclude_columns": ["post_loan"]}
    offered = prepare_modeling_sample(snapshot_id, ledger=ledger, **options)
    assert offered["status"] == "needs_human"
    review_id = offered["human_gate"]["review_id"]
    pending = prepare_modeling_sample(snapshot_id, ledger=ledger, fallback_review_id=review_id, **options)
    assert pending["status"] == "needs_human"
    record_human_decision(ledger=ledger, gate="validation_fallback", review_id=review_id,
                          actor="analyst", channel="approval_card", payload={"method": method})
    prepared = prepare_modeling_sample(snapshot_id, ledger=ledger, fallback_review_id=review_id, **options)
    assert prepared["status"] == "degraded"
    assert prepared["summary"]["split_method"] == split_method
    assert (prepared["summary"]["oot_rows"] == 0) is (method == "cv_only")
    screened = screen_features(prepared["run_id"], ledger=ledger, features=["f_signal"])
    assert screened["status"] == "needs_human"
    record_human_decision(ledger=ledger, gate="feature_list", review_id=screened["run_id"],
                          actor="analyst", channel="approval_card",
                          payload={"selected_features": screened["summary"]["selected"]})
    trained = train_and_evaluate_candidate(screened["run_id"], ledger=ledger)
    assert trained["status"] == "degraded"
    assert trained["summary"]["validation_mode"] == method
    assert trained["summary"]["oot_validation_complete"] is False


def test_screen_needs_human_and_train_requires_approval_card(tmp_path):
    frame, mask = _modeling_frame()
    ledger, snapshot_id = _book(tmp_path, frame, mask)
    prepared = prepare_modeling_sample(
        snapshot_id,
        ledger=ledger,
        label="label",
        time_column="obs_month",
        positive_values=(1,),
        gray_values=(2,),
        gray_policy="exclude",
        exclude_columns=["post_loan"],
    )
    assert prepared["status"] == "ok"
    summary = prepared["summary"]
    assert summary["split_method"] == "oot"
    assert summary["sample_size"] == 400
    assert summary["bad"] + summary["good"] == 400
    assert summary["gray_excluded"] == 12
    assert summary["dev_rows"] + summary["oot_rows"] == 400
    assert "out-of-time" in summary["cutoff"]
    assert summary["label"] == "label"
    assert summary["time_column"] == "obs_month"
    assert summary["exclude_columns"] == ["post_loan"]
    assert summary["positive_values"] == [1]

    screened = screen_features(
        prepared["run_id"],
        ledger=ledger,
        features=["f_signal", "f_weak", "f_noise", "f_leak"],
    )
    assert screened["status"] == "needs_human"
    assert screened["human_gate"] == {"gate": "feature_list", "review_id": screened["run_id"]}
    rows = screened["summary"]["features"]
    assert [row["name"] for row in rows] == ["f_signal", "f_weak", "f_noise", "f_leak"]
    core_keys = {"name", "iv", "psi", "missing", "decision", "reason", "suspect_leakage"}
    binning_keys = {"ks", "n_bins", "monotonic", "monotonic_strength"}
    assert all(core_keys | binning_keys >= set(row) >= core_keys for row in rows)
    assert all(row["decision"] in {"keep", "drop"} for row in rows)
    leaks = [row for row in rows if row["suspect_leakage"]]
    assert leaks and all(row["iv"] > 0.5 for row in leaks)
    # ``selected`` is a clean list of names; the richer WOE details live in
    # ``selected_details``. The ledger and the agent envelope now share one
    # structure, so both carry the same keys and values.
    assert "woe" not in json.dumps(screened["summary"]["selected"])
    stored = ledger.get_run(screened["run_id"])["summary"]
    assert stored["selected"] and any("woe" in item for item in stored["selected_details"])
    assert stored == screened["summary"]
    assert isinstance(screened["summary"]["suspect_leakage"], list)
    assert set(screened["summary"]["selected"]) == {row["name"] for row in rows if row["decision"] == "keep"}
    assert screened["summary"]["selected"]

    blocked = train_and_evaluate_candidate(screened["run_id"], ledger=ledger)
    assert blocked["block_code"] == "FEATURE_REVIEW_REQUIRED"

    ledger.save_decision(
        "feature_list",
        screened["run_id"],
        "reviewer",
        "chat",
        {"selected_features": screened["summary"]["selected"][:1]},
    )
    wrong_channel = train_and_evaluate_candidate(screened["run_id"], ledger=ledger)
    assert wrong_channel["block_code"] == "FEATURE_REVIEW_REQUIRED"

    rejected = record_human_decision(
        ledger=ledger,
        gate="feature_list",
        review_id=screened["run_id"],
        actor="reviewer",
        channel="approval_card",
        payload={"selected_features": screened["summary"]["selected"] + ["not_in_screen"]},
    )
    assert rejected["summary"] == {"gate": "feature_list", "review_id": screened["run_id"]}
    extra = train_and_evaluate_candidate(screened["run_id"], ledger=ledger)
    assert extra["block_code"] == "FEATURE_REVIEW_REQUIRED"

    record_human_decision(
        ledger=ledger,
        gate="feature_list",
        review_id=screened["run_id"],
        actor="reviewer",
        channel="approval_card",
        payload={"selected_features": screened["summary"]["selected"][:1]},
    )
    trained = train_and_evaluate_candidate(screened["run_id"], ledger=ledger)
    assert trained["status"] in {"ok", "degraded"}
    assert trained["summary"]["status"] == "candidate"
    assert trained["summary"]["candidate_comparison_complete"] is (trained["status"] == "ok")
    if trained["status"] == "degraded":
        assert "lightgbm" in trained["summary"]["unavailable_algorithms"]
    if _new_lgbm(0) is not None:
        assert trained["status"] == "ok"
        assert "lightgbm" in trained["summary"]["models"]
    assert "approved" not in trained["summary"]
    assert "已批准" not in str(trained["summary"])
    models = trained["summary"]["models"]
    assert "logistic_regression" in models
    for meta in models.values():
        for key in ("train_auc", "train_ks", "test_auc", "test_ks"):
            assert meta[key] is not None and math.isfinite(meta[key])
    assert "score_psi_train_vs_test" in trained["summary"]
    assert "monotonic_deciles" in trained["summary"]
    details = json.loads(Path(trained["artifacts"][1]["path"]).read_text(encoding="utf-8"))
    assert details["sample"]["split_method"] == "oot"
    assert details["test_deciles"]
    assert details["baseline_scorecard"]["features"]
    assert details["baseline_scorecard"]["pdo"] == 50
    assert "_export" not in details
    card = json.loads(Path(trained["artifacts"][2]["path"]).read_text(encoding="utf-8"))
    assert card["base_points"] == details["baseline_scorecard"]["base_points"]
    rendered = f'{card["base_points"]:.4f}'
    report = verify_and_commit_report(
        ledger=ledger, run_ids=[trained["run_id"]],
        report_markdown=f"评分卡基础分为 {rendered}。",
        citations=[{"path": "validation.baseline_scorecard.base_points", "rendered": rendered}],
    )
    assert report["status"] == "ok"

    # The feature approval card is one-time: replaying the same review_id must
    # be refused so an old confirmation cannot be reused to re-train.
    replay = train_and_evaluate_candidate(screened["run_id"], ledger=ledger)
    assert replay["status"] == "blocked"
    assert replay["block_code"] == "FEATURE_REVIEW_REQUIRED"


def test_screen_rejects_explicit_time_point_columns(tmp_path):
    frame, mask = _modeling_frame()
    ledger, snapshot_id = _book(tmp_path, frame, mask)
    # Simulate the immutable snapshot meta written by inspect_and_bind_snapshot.
    ledger.save_snapshot(snapshot_id, {
        "suspect_leakage": ["post_loan"],
        "column_roles": {"post_loan": "observation_outcome", "obs_month": "observation_time"},
        "observation_time_column": "obs_month",
    })
    prepared = prepare_modeling_sample(
        snapshot_id, ledger=ledger, label="label", time_column="obs_month",
        exclude_columns=["post_loan"],
    )
    assert prepared["status"] == "ok"
    blocked = screen_features(prepared["run_id"], ledger=ledger,
                              features=["f_signal", "post_loan"])
    assert blocked["status"] == "blocked"
    assert blocked["block_code"] == "TIME_POINT_VIOLATION"


def test_profile_binning_returns_chimerge_detail(tmp_path):
    frame, mask = _modeling_frame()
    ledger, snapshot_id = _book(tmp_path / "profile", frame, mask)
    prepared = prepare_modeling_sample(snapshot_id, ledger=ledger, label="label", time_column="obs_month",
                                       exclude_columns=["post_loan"], positive_values=[1])
    assert prepared["status"] == "ok"

    profiled = profile_binning(prepared["run_id"], ledger=ledger,
                               features=["f_signal", "f_leak"], woe_bins=4,
                               binning_method="chi2", include_merge_log=True)
    assert profiled["status"] in {"ok", "degraded"}
    summary = profiled["summary"]
    assert summary["binning_policy"]["method"] == "chi2"
    assert summary["binning_policy"]["min_bin_fraction_scope"] == "valid_only"
    rows = {row["name"]: row for row in summary["features"]}
    assert set(rows) == {"f_signal", "f_leak"}
    for row in rows.values():
        assert row["usable"] is True
        assert row["n_bins"] == len(row["bins"])
        assert row["iv"] > 0
        assert row["ks_dev"] is not None and 0 < row["ks_dev"] <= 1
        assert {"bin", "count", "bad", "bad_rate", "woe", "iv_part"} <= set(row["bins"][0])
        assert row["merge_log"] or row["n_bins"] >= 2
    # 强泄漏特征的单特征 KS 必须明显高于普通信号
    assert rows["f_leak"]["ks_dev"] > rows["f_signal"]["ks_dev"]

    # 分箱方法与参数非法时结构化停机，不抛裸异常
    bad = profile_binning(prepared["run_id"], ledger=ledger, features=["f_signal"],
                          binning_method="entropy")
    assert bad["status"] == "blocked" and bad["block_code"] == "UNSUPPORTED_SCOPE"
    bad_bins = profile_binning(prepared["run_id"], ledger=ledger, features=["f_signal"], woe_bins=1)
    assert bad_bins["block_code"] == "UNSUPPORTED_SCOPE"
    empty = profile_binning(prepared["run_id"], ledger=ledger, features=["f_signal"],
                            max_prebins=2, woe_bins=6)
    assert empty["block_code"] == "UNSUPPORTED_SCOPE"
