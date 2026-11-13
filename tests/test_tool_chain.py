"""End-to-end chain on one synthetic loan-month panel.

Column roles come from resolve_metric_and_dimensions. This test does not
hand-write a flat metric binding.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from credit_agent.tools import (
    Ledger,
    check_label_maturity,
    decompose_and_locate_anomaly,
    inspect_and_bind_snapshot,
    prepare_modeling_sample,
    record_human_decision,
    resolve_metric_and_dimensions,
    run_portfolio_monitoring,
    screen_features,
    simulate_policy_rules,
    train_and_evaluate_candidate,
    verify_and_commit_report,
)

_LEGAL_STOP = {"located", "universal_deterioration", "mix_dispersion", "unlocated"}
_COHORTS = [f"2023-{month:02d}" for month in range(1, 7)]


def _panel() -> pd.DataFrame:
    rng = np.random.default_rng(7)
    obs_day = {
        "2023-01": "2023-05-10",
        "2023-02": "2023-06-10",
        "2023-03": "2023-07-10",
        "2023-04": "2023-08-10",
        "2023-05": "2023-08-15",
        "2023-06": "2023-08-15",
    }
    rows = []
    for cohort in _COHORTS:
        for index in range(80):
            channel = "online" if index < 40 else "branch"
            if channel == "online":
                bad = 1 if index < 12 else 0
            else:
                bad = 1 if index < 48 else 0
            rows.append(
                {
                    "loan_id": f"{cohort}-{index:03d}",
                    "cohort": cohort,
                    "decision_time": f"{cohort}-10",
                    "period": cohort,
                    "bad": bad,
                    "channel": channel,
                    "obs_time": obs_day[cohort],
                }
            )
    frame = pd.DataFrame(rows)
    frame["score"] = frame["bad"] * 4 + rng.normal(0, 0.7, size=len(frame))
    frame["decision_time"] = pd.to_datetime(frame["decision_time"])
    frame["obs_time"] = pd.to_datetime(frame["obs_time"])
    return frame[
        ["loan_id", "cohort", "decision_time", "period", "bad", "channel", "score", "obs_time"]
    ]


def _registry() -> dict:
    return {
        "metrics": [
            {
                "metric_id": "bad_rate",
                "version": "v1",
                "flag_column": "bad",
                "period_column": "period",
            }
        ],
        "dimensions": [
            {"column": "channel", "point_in_time": "decision"},
            {"column": "obs_time", "point_in_time": "observation"},
        ],
    }


def test_resolved_binding_drives_the_full_chain(tmp_path):
    ledger = Ledger(tmp_path / "ledger")
    frame = _panel()
    bound = inspect_and_bind_snapshot(
        frame,
        ledger=ledger,
        grain="loan_month",
        key_columns=["loan_id", "period"],
        decision_time_column="decision_time",
        observation_time_column="obs_time",
    )
    assert bound["status"] == "ok"
    snapshot_id = bound["snapshot_id"]

    maturity = check_label_maturity(
        snapshot_id,
        ledger=ledger,
        cohort_column="cohort",
        decision_time_column="decision_time",
        performance_window={"kind": "mob", "mob": 3},
        bad_flag_column="bad",
    )
    assert maturity["status"] == "degraded"
    mature = {item["cohort"]: item["mature"] for item in maturity["summary"]["mature_cohorts"]}
    assert [mature[cohort] for cohort in _COHORTS] == [True, True, True, True, False, False]

    resolved = resolve_metric_and_dimensions(
        snapshot_id,
        ledger=ledger,
        metric_id="bad_rate",
        metric_version="v1",
        registry=_registry(),
    )
    assert resolved["status"] == "ok"
    summary = resolved["summary"]
    assert summary["columns"] == {"flag_column": "bad", "period_column": "period"}
    assert "weight_column" not in summary["columns"]
    assert "loan_id" not in summary["columns"]
    assert "cohort" not in summary["columns"]
    assert "policy_version_column" not in summary["columns"]
    assert summary["rejected_columns"] == ["obs_time"]
    assert [item["column"] for item in summary["dimensions"]] == ["channel"]
    admission = ledger.get_admission(snapshot_id)
    assert admission is not None
    assert admission["level"] != "C"

    monitored = run_portfolio_monitoring(
        snapshot_id,
        ledger=ledger,
        metric_id="bad_rate",
        metric_version="v1",
        base_periods=["2023-01"],
        comp_periods=["2023-04"],
    )
    assert monitored["status"] == "ok"
    assert monitored["summary"]["base_periods"] == ["2023-01"]
    assert monitored["summary"]["comp_periods"] == ["2023-04"]

    located = decompose_and_locate_anomaly(monitored["run_id"], ledger=ledger)
    assert located["status"] == "ok"
    assert located["summary"]["stop_rule"] in _LEGAL_STOP

    prepared = prepare_modeling_sample(
        snapshot_id,
        ledger=ledger,
        label="bad",
        time_column="obs_time",
    )
    assert prepared["status"] == "ok"
    assert prepared["summary"]["split_method"] == "oot"

    screened = screen_features(prepared["run_id"], ledger=ledger, features=["score"])
    assert screened["status"] == "needs_human"
    assert "score" in screened["summary"]["selected"]
    record_human_decision(
        ledger=ledger,
        gate="feature_list",
        review_id=screened["run_id"],
        actor="reviewer",
        channel="approval_card",
        payload={"selected_features": ["score"]},
    )
    trained = train_and_evaluate_candidate(screened["run_id"], ledger=ledger)
    candidate = trained.get("status")
    if candidate != "candidate":
        candidate = (trained.get("summary") or {}).get("status")
    assert candidate == "candidate"

    refused = simulate_policy_rules(
        snapshot_id,
        ledger=ledger,
        mode="cutoff",
        time_column="obs_time",
        bad="bad",
        score="score",
        depth=2,
    )
    assert refused["block_code"] == "UNSUPPORTED_SCOPE"

    rate = ledger.get_run(monitored["run_id"])["summary"]["base_rate"]
    rendered = f"{float(rate):.6f}"
    committed = verify_and_commit_report(
        ledger=ledger,
        run_ids=[monitored["run_id"]],
        report_markdown=f"基期不良率为 {rendered}。",
        citations=[{"path": "base_rate", "rendered": rendered}],
    )
    assert committed["status"] == "ok"
    assert committed["summary"]["citation_count"] == 1
