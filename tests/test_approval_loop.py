"""Governance regression: one-time approvals, signoff and citation coverage."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from credit_agent.tools import Ledger, record_human_decision, verify_and_commit_report
from credit_agent.tools.codes import BLOCK_CODES

ROOT = Path(__file__).resolve().parents[1]


def _decision(ledger, gate, review_id, payload=None, actor="risk_owner", channel="approval_card"):
    return record_human_decision(
        ledger=ledger, gate=gate, review_id=review_id, actor=actor, channel=channel,
        payload=payload or {},
    )


def test_decision_is_consumed_only_once(tmp_path):
    ledger = Ledger(tmp_path / "ledger")
    result = _decision(ledger, "feature_list", "run_abc", {"selected_features": ["score"]})
    assert result["status"] == "ok"
    first = ledger.consume_decision("feature_list", "run_abc")
    assert first and first["payload"]["selected_features"] == ["score"]
    assert ledger.consume_decision("feature_list", "run_abc") is None
    assert ledger.get_decision("feature_list", "run_abc") is not None  # audit trail remains readable


def test_record_human_decision_rejects_invalid_approvals(tmp_path):
    ledger = Ledger(tmp_path / "ledger")
    with pytest.raises(ValueError):
        _decision(ledger, "feature_list", "run_x", actor="agent")
    with pytest.raises(ValueError):
        _decision(ledger, "feature_list", "run_x", channel="chat")
    with pytest.raises(ValueError):
        _decision(ledger, "unknown_gate", "run_x")
    with pytest.raises(ValueError):
        _decision(ledger, "validation_fallback", "run_x", {"method": "not_a_method"})
    with pytest.raises(ValueError):
        _decision(ledger, "feature_list", "run_x", {"selected_features": "score"})
    assert _decision(ledger, "maturity_choice", "run_y",
                     {"method": "wait_for_maturity"})["status"] == "ok"


def test_report_signoff_gate_is_enforced(tmp_path):
    ledger = Ledger(tmp_path / "ledger")
    run_id = "run_model_1"
    ledger.save_run(run_id, "train_and_evaluate_candidate", "snap_1",
                    {"ks": 0.42, "status": "candidate"})
    citations = [{"path": "ks", "rendered": "0.42"}]
    report = "KS 为 0.42，模型卡建议上线。"

    blocked = verify_and_commit_report(ledger=ledger, run_ids=[run_id],
                                       report_markdown=report, citations=citations)
    assert blocked["status"] == "blocked"
    assert blocked["block_code"] == "SIGNOFF_REQUIRED"

    _decision(ledger, "model_signoff", run_id, {"approved": True})
    committed = verify_and_commit_report(ledger=ledger, run_ids=[run_id],
                                         report_markdown=report, citations=citations)
    assert committed["status"] == "ok", committed
    assert committed["summary"]["signoff_status"]["model_signoff"] == "signed"


def test_report_accepts_date_and_integer_citations_and_rejects_uncited_integers(tmp_path):
    ledger = Ledger(tmp_path / "ledger")
    run_id = "run_summary_1"
    ledger.save_run(run_id, "inspect_and_bind_snapshot", "snap_1",
                    {"cutoff": "2024-01-31", "n": 2000})
    citations = [
        {"path": "cutoff", "rendered": "2024-01-31"},
        {"path": "n", "rendered": "2000"},
    ]
    ok = verify_and_commit_report(
        ledger=ledger, run_ids=[run_id],
        report_markdown="截止 2024-01-31，共 2000 笔。", citations=citations,
    )
    assert ok["status"] == "ok", ok
    missing = verify_and_commit_report(
        ledger=ledger, run_ids=[run_id],
        report_markdown="截止 2024-01-31，共 2000 笔，另有 9999 笔待核。", citations=citations,
    )
    assert missing["status"] == "blocked"
    assert missing["block_code"] == "REPORT_UNTRACED"


def test_host_approval_cli_writes_a_consumable_decision(tmp_path):
    ledger_root = tmp_path / "ledger"
    Ledger(ledger_root)
    script = ROOT / "scripts/host_approval.py"
    assert script.is_file()
    completed = subprocess.run(
        [sys.executable, str(script), "--ledger-root", str(ledger_root),
         "--gate", "policy_trial", "--review-id", "run_policy_1",
         "--actor", "risk_owner", "--method", "stop"],
        capture_output=True, text=True, check=False,
        env={**__import__("os").environ, "PYTHONPATH": str(ROOT / "src")},
    )
    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout.strip().splitlines()[-1])
    assert payload["status"] == "ok"
    decision = Ledger(ledger_root).consume_decision("policy_trial", "run_policy_1")
    assert decision and decision["channel"] == "approval_card"
    assert decision["payload"]["method"] == "stop"


def test_all_declared_codes_are_known():
    assert "SIGNOFF_REQUIRED" in BLOCK_CODES
    assert "RESOURCE_UNAVAILABLE" in BLOCK_CODES
    assert "PERIOD_MISMATCH" in BLOCK_CODES
