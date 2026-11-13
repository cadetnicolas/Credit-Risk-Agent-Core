"""Synthetic checks for approval records and report citation tracing."""

from __future__ import annotations

import pytest

from credit_agent.tools import Ledger
from credit_agent.tools.evidence import record_human_decision, verify_and_commit_report


def test_record_human_decision_only_accepts_approval_cards(tmp_path):
    ledger = Ledger(tmp_path)
    with pytest.raises(ValueError):
        record_human_decision(
            ledger=ledger,
            gate="feature_list",
            review_id="review",
            actor="agent",
            channel="approval_card",
            payload={"selected_features": ["f1"]},
        )
    with pytest.raises(ValueError):
        record_human_decision(
            ledger=ledger,
            gate="feature_list",
            review_id="review",
            actor="reviewer",
            channel="chat",
            payload={},
        )
    with pytest.raises(ValueError):
        record_human_decision(
            ledger=ledger,
            gate="other",
            review_id="review",
            actor="reviewer",
            channel="approval_card",
            payload={},
        )
    saved = record_human_decision(
        ledger=ledger,
        gate="policy_trial",
        review_id="review",
        actor="reviewer",
        channel="approval_card",
        payload={"trial": "cutoff"},
    )
    assert saved["status"] == "ok"
    assert saved["summary"] == {"gate": "policy_trial", "review_id": "review"}
    stored = ledger.get_decision("policy_trial", "review")
    assert stored["actor"] == "reviewer"
    assert stored["channel"] == "approval_card"


def test_untraced_or_mismatched_citations_are_blocked(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.save_run("run_auc", "demo", "snap", {"holdout_auc": 0.812345, "note": "see text"})
    mismatch = verify_and_commit_report(
        ledger=ledger,
        run_ids=["run_auc"],
        report_markdown="留出 AUC 为 0.90。",
        citations=[{"path": "holdout_auc", "rendered": "0.90"}],
    )
    assert mismatch["status"] == "blocked"
    assert mismatch["block_code"] == "REPORT_UNTRACED"

    missing = verify_and_commit_report(
        ledger=ledger,
        run_ids=["run_auc"],
        report_markdown="留出 AUC 为 0.8123。",
        citations=[{"path": "missing_auc", "rendered": "0.8123"}],
    )
    assert missing["block_code"] == "REPORT_UNTRACED"

    text = verify_and_commit_report(
        ledger=ledger,
        run_ids=["run_auc"],
        report_markdown="留出 AUC 为 0.8123。",
        citations=[{"path": "note", "rendered": "0.8123"}],
    )
    assert text["block_code"] == "REPORT_UNTRACED"


def test_matching_citation_commits_when_the_body_only_uses_that_number(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.save_run("run_auc", "demo", "snap", {"holdout_auc": 0.812345})
    good = verify_and_commit_report(
        ledger=ledger,
        run_ids=["run_auc"],
        report_markdown="留出 AUC 为 0.8123。",
        citations=[{"path": "holdout_auc", "rendered": "0.8123"}],
    )
    assert good["status"] == "ok"
    assert good["summary"]["citation_count"] == 1
    assert good["summary"]["signoff_required"] is True
    assert good["summary"]["signoff_status"] == {}
    assert good["tool"] == "verify_and_commit_report"
    stored = ledger.get_run(good["run_id"])
    assert stored["tool"] == "verify_and_commit_report"

    # A bare integer in the body is a number too: without its own citation it
    # must be blocked (the v0.3 integer branch in the citation scanner).
    bare_integer = verify_and_commit_report(
        ledger=ledger,
        run_ids=["run_auc"],
        report_markdown="样本 400 的 AUC 为 0.8123。",
        citations=[{"path": "holdout_auc", "rendered": "0.8123"}],
    )
    assert bare_integer["status"] == "blocked"
    assert bare_integer["block_code"] == "REPORT_UNTRACED"

    extra = verify_and_commit_report(
        ledger=ledger,
        run_ids=["run_auc"],
        report_markdown="留出 AUC 为 0.8123，占比 10%。",
        citations=[{"path": "holdout_auc", "rendered": "0.8123"}],
    )
    assert extra["block_code"] == "REPORT_UNTRACED"


def test_percent_bp_thousands_and_nested_paths(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.save_run(
        "run_metrics",
        "demo",
        None,
        {
            "bad_rate": 0.123456,
            "shift": 0.0015,
            "n": 1234,
            "models": {"logistic_regression": {"test_auc": 0.71}},
        },
    )
    out = verify_and_commit_report(
        ledger=ledger,
        run_ids=["run_metrics"],
        report_markdown="坏账率 12.35%，变动 15bp，样本量 1,234，AUC 0.71。",
        citations=[
            {"path": "bad_rate", "rendered": "12.35%"},
            {"path": "shift", "rendered": "15bp"},
            {"path": "n", "rendered": "1,234"},
            {"path": "models.logistic_regression.test_auc", "rendered": "0.71"},
        ],
    )
    assert out["status"] == "ok"
    assert out["summary"]["citation_count"] == 4


def test_date_and_non_numeric_citations_are_accepted(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.save_run(
        "run_meta", "inspect_and_bind_snapshot", "snap",
        {"cutoff": "2024-01-31", "rows": 2000, "owner": "risk team"},
    )
    # ISO dates are stripped before scanning; a date/string value is matched by
    # exact string comparison; an integer needs its own citation.
    out = verify_and_commit_report(
        ledger=ledger,
        run_ids=["run_meta"],
        report_markdown="截至 2024-01-31，共 2000 笔，由 risk team 维护。",
        citations=[
            {"path": "cutoff", "rendered": "2024-01-31"},
            {"path": "rows", "rendered": "2000"},
            {"path": "owner", "rendered": "risk team"},
        ],
    )
    assert out["status"] == "ok", out
    assert out["summary"]["citation_count"] == 3

    # A non-numeric string that does not match the rendered text is untraced.
    mismatch = verify_and_commit_report(
        ledger=ledger,
        run_ids=["run_meta"],
        report_markdown="截至 2024-01-31，共 2000 笔。",
        citations=[
            {"path": "cutoff", "rendered": "2024-01-31"},
            {"path": "rows", "rendered": "2000"},
            {"path": "owner", "rendered": "wrong owner"},
        ],
    )
    assert mismatch["block_code"] == "REPORT_UNTRACED"

    # An uncited integer (9999) is still rejected even when every other figure
    # is cited.
    uncited = verify_and_commit_report(
        ledger=ledger,
        run_ids=["run_meta"],
        report_markdown="截至 2024-01-31，共 2000 笔，另有 9999 笔待核。",
        citations=[
            {"path": "cutoff", "rendered": "2024-01-31"},
            {"path": "rows", "rendered": "2000"},
        ],
    )
    assert uncited["status"] == "blocked"
    assert uncited["block_code"] == "REPORT_UNTRACED"


def test_report_signoff_claim_needs_a_human_approval_card(tmp_path):
    ledger = Ledger(tmp_path)
    run_id = "run_model_1"
    ledger.save_run(run_id, "train_and_evaluate_candidate", "snap", {"ks": 0.42})
    citations = [{"path": "ks", "rendered": "0.42"}]
    report = "KS 为 0.42，模型卡建议上线。"

    pending = verify_and_commit_report(
        ledger=ledger, run_ids=[run_id], report_markdown=report, citations=citations,
    )
    assert pending["status"] == "blocked"
    assert pending["block_code"] == "SIGNOFF_REQUIRED"

    record_human_decision(
        ledger=ledger, gate="model_signoff", review_id=run_id,
        actor="risk_owner", channel="approval_card", payload={"approved": True},
    )
    signed = verify_and_commit_report(
        ledger=ledger, run_ids=[run_id], report_markdown=report, citations=citations,
    )
    assert signed["status"] == "ok", signed
    assert signed["summary"]["signoff_status"]["model_signoff"] == "signed"
