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


def test_blocked_report_says_which_run_which_path_which_value(tmp_path):
    """REPORT_UNTRACED must be diagnosable: the caller has to know what to fix."""

    ledger = Ledger(tmp_path)
    ledger.save_run("run_auc", "demo", "snap", {"holdout_auc": 0.812345, "n": 400})

    no_runs = verify_and_commit_report(
        ledger=ledger, run_ids=[], report_markdown="AUC 0.8123。",
        citations=[{"path": "holdout_auc", "rendered": "0.8123"}],
    )
    assert no_runs["details"]["reason"] == "no_runs_referenced"
    # checked.citations 记「传了几条」，checked.runs_resolved 记「读到了几个运行」。
    assert no_runs["details"]["checked"] == {"runs": 0, "runs_resolved": 0, "citations": 1, "numbers": 1}
    assert "run" in no_runs["summary"]["message"]

    ghost = verify_and_commit_report(
        ledger=ledger, run_ids=["run_ghost"], report_markdown="AUC 0.8123。",
        citations=[{"path": "holdout_auc", "rendered": "0.8123"}],
    )
    assert ghost["details"]["reason"] == "run_not_found"
    assert ghost["details"]["failures"][0]["missing_run_ids"] == ["run_ghost"]

    path = verify_and_commit_report(
        ledger=ledger, run_ids=["run_auc"], report_markdown="AUC 0.8123。",
        citations=[{"path": "test_auc", "rendered": "0.8123"}],
    )
    failure = path["details"]["failures"][0]
    assert failure["reason"] == "path_not_found"
    assert failure["path"] == "test_auc"
    assert set(failure["available_paths"]) >= {"holdout_auc", "n"}

    value = verify_and_commit_report(
        ledger=ledger, run_ids=["run_auc"], report_markdown="AUC 0.90。",
        citations=[{"path": "holdout_auc", "rendered": "0.90"}],
    )
    failure = value["details"]["failures"][0]
    assert failure["reason"] == "value_mismatch"
    assert failure["rendered"] == "0.90"
    assert failure["artifact_values"] == [0.812345]
    assert failure["expected"] == ["0.81"], "按当前精度渲染产物的正确写法"


def test_every_uncited_number_is_named_with_context(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.save_run("run_auc", "demo", "snap", {"holdout_auc": 0.812345})
    out = verify_and_commit_report(
        ledger=ledger, run_ids=["run_auc"],
        report_markdown="申请量 400 笔，其中 30 天以上占比 12%，AUC 0.8123。",
        citations=[{"path": "holdout_auc", "rendered": "0.8123"}],
    )
    assert out["block_code"] == "REPORT_UNTRACED"
    fragments = [item["fragment"] for item in out["details"]["failures"]]
    assert set(fragments) == {"400", "30", "12%"}
    assert all(item["reason"] == "uncited_number" for item in out["details"]["failures"])
    assert "30" in out["details"]["failures"][1]["context"]
    assert out["details"]["failure_count"] == 3


def test_signoff_claim_reports_pending_gates(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.save_run("run_model_1", "train_and_evaluate_candidate", "snap", {"ks": 0.42})
    out = verify_and_commit_report(
        ledger=ledger, run_ids=["run_model_1"], report_markdown="KS 为 0.42，建议上线。",
        citations=[{"path": "ks", "rendered": "0.42"}],
    )
    assert out["block_code"] == "SIGNOFF_REQUIRED"
    assert out["details"]["reason"] == "signoff_pending"
    assert out["details"]["failures"][0]["gates"] == ["model_signoff"]


def test_commit_separates_reproducible_verified_and_signed(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.save_run("run_auc", "demo", "snap", {"holdout_auc": 0.812345})
    out = verify_and_commit_report(
        ledger=ledger, run_ids=["run_auc"], report_markdown="AUC 0.8123。",
        citations=[{"path": "holdout_auc", "rendered": "0.8123"}],
    )
    assert out["status"] == "ok"
    assert out["summary"]["computation_reproducible"] is True
    assert out["summary"]["citations_verified"] is True
    assert out["summary"]["business_signoff"] == "not_required"
    assert out["summary"]["number_count"] == 1
    assert out["details"] == {}


def test_empty_report_is_not_a_vacuous_pass(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.save_run("run_auc", "demo", "snap", {"holdout_auc": 0.812345})
    for body in ("", "   \n", None):
        out = verify_and_commit_report(
            ledger=ledger, run_ids=["run_auc"], report_markdown=body, citations=[],
        )
        assert out["status"] == "blocked", body
        assert out["details"]["reason"] == "empty_report"
        assert out["details"]["checked"]["numbers"] == 0


def test_number_normalisation_accepts_spaced_and_fullwidth_percent(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.save_run("run_rate", "demo", "snap", {"bad_rate": 0.0345})
    for body in ("坏账率 3.45 %。", "坏账率 3.45％。", "坏账率 3.45%。"):
        out = verify_and_commit_report(
            ledger=ledger, run_ids=["run_rate"], report_markdown=body,
            citations=[{"path": "bad_rate", "rendered": "3.45%"}],
        )
        assert out["status"] == "ok", (body, out["details"])


def test_month_labels_and_thresholds_need_no_citation(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.save_run("run_auc", "demo", "snap", {"holdout_auc": 0.8123})
    out = verify_and_commit_report(
        ledger=ledger, run_ids=["run_auc"],
        report_markdown="9 月表现期 AUC 为 0.8123，观察 ≥10 万客群，2026 年 3 月复核。",
        citations=[{"path": "holdout_auc", "rendered": "0.8123"}],
    )
    assert out["status"] == "ok", out["details"]


def test_citation_can_be_scoped_to_one_run(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.save_run("run_a", "demo", "snap", {"holdout_auc": 0.8})
    ledger.save_run("run_b", "demo", "snap", {"holdout_auc": 0.5})
    # 0.5 只属于 run_b：绑错运行必须被拒，不能「任一命中即通过」。
    wrong = verify_and_commit_report(
        ledger=ledger, run_ids=["run_a", "run_b"], report_markdown="AUC 0.5。",
        citations=[{"path": "holdout_auc", "rendered": "0.5", "run_id": "run_a"}],
    )
    assert wrong["details"]["reason"] == "value_mismatch"
    right = verify_and_commit_report(
        ledger=ledger, run_ids=["run_a", "run_b"], report_markdown="AUC 0.5。",
        citations=[{"path": "holdout_auc", "rendered": "0.5", "run_id": "run_b"}],
    )
    assert right["status"] == "ok", right["details"]


def test_numbers_glued_to_chinese_are_still_scanned(tmp_path):
    """中文与数字之间没有空格，不能成为绕过核验的通道。"""

    ledger = Ledger(tmp_path)
    ledger.save_run("run_a", "demo", "snap", {"holdout_auc": 0.8123})
    cited = [{"path": "holdout_auc", "rendered": "0.8123"}]
    for body, fragment in (
        ("共400笔，AUC 0.8123。", "400"),
        ("坏账率3.45%，AUC 0.8123。", "3.45%"),
        ("余额1200万元，AUC 0.8123。", "1200"),
    ):
        out = verify_and_commit_report(
            ledger=ledger, run_ids=["run_a"], report_markdown=body, citations=cited,
        )
        assert out["status"] == "blocked", body
        assert out["details"]["reason"] == "uncited_number"
        assert out["details"]["failures"][0]["fragment"] == fragment
    # 标识符里的数字仍然不参与扫描，否则版本号会把报告挡死。
    ok = verify_and_commit_report(
        ledger=ledger, run_ids=["run_a"],
        report_markdown="快照 snap_abc123 上的 AUC 为 0.8123，口径 v1.2.3。",
        citations=cited,
    )
    assert ok["status"] == "ok", ok["details"]


def test_durations_and_mob_are_labels_not_figures(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.save_run("run_a", "demo", "snap", {"holdout_auc": 0.8123})
    out = verify_and_commit_report(
        ledger=ledger, run_ids=["run_a"],
        report_markdown="观察期 12 个月、账龄 6.5 个月、第 12 期与 Mob 3 的 AUC 均为 0.8123。",
        citations=[{"path": "holdout_auc", "rendered": "0.8123"}],
    )
    assert out["status"] == "ok", out["details"]


def test_all_label_body_declares_no_figure_scope(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.save_run("run_a", "demo", "snap", {"holdout_auc": 0.8123})
    out = verify_and_commit_report(
        ledger=ledger, run_ids=["run_a"], report_markdown="9 月复核，观察 ≥10 万客群。",
        citations=[],
    )
    assert out["status"] == "ok"
    assert out["summary"]["number_count"] == 0
    assert out["summary"]["verification_scope"] == "no_figures"


def test_citation_run_id_is_bounded_in_the_error(tmp_path):
    ledger = Ledger(tmp_path)
    ledger.save_run("run_a", "demo", "snap", {"holdout_auc": 0.8})
    out = verify_and_commit_report(
        ledger=ledger, run_ids=["run_a"], report_markdown="AUC 0.8。",
        citations=[{"path": "holdout_auc", "rendered": "0.8", "run_id": {"k": "y" * 4000}}],
    )
    failure = out["details"]["failures"][0]
    assert failure["reason"] == "run_not_found"
    assert len(failure["run_id"]) <= 121

    empty = verify_and_commit_report(
        ledger=ledger, run_ids=["run_a"], report_markdown="AUC 0.8。",
        citations=[{"path": "holdout_auc", "rendered": "0.8", "run_id": ""}],
    )
    assert empty["details"]["failures"][0]["run_id"] == "<empty>"


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
