"""Synthetic checks for rule mining scope and cutoff simulation."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from credit_agent.tools import Ledger
from credit_agent.tools.evidence import record_human_decision
from credit_agent.tools.policy import simulate_policy_rules


def _install_latest_mask(ledger: Ledger) -> None:
    """另一个并行代理会在 ledger.py 增加这两个方法；测试内按相同语义独立装入。"""

    def save(snapshot_id, mask):
        path = ledger.frames / f"{snapshot_id}.latest.parquet"
        pd.DataFrame({"latest": mask.astype(bool).to_numpy()}).to_parquet(path, index=False)
        return path

    def load(snapshot_id):
        path = ledger.frames / f"{snapshot_id}.latest.parquet"
        if not path.is_file():
            raise KeyError(snapshot_id)
        return pd.read_parquet(path)["latest"]

    ledger.save_latest_mask = save
    ledger.load_latest_mask = load


def _book(tmp_path, frame: pd.DataFrame, mask: np.ndarray, *, registered=("x",), rejected=(), leakage=()):
    ledger = Ledger(tmp_path)
    snapshot_id = "snap_policy"
    ledger.save_frame(snapshot_id, frame)
    ledger.save_mask(snapshot_id, pd.Series(mask))
    ledger.save_admission(snapshot_id, "A", "ok")
    ledger.save_snapshot(snapshot_id, {
        "bound_metric_id": "m", "bound_metric_version": "v1",
        "cutoff": "2024-12-31", "suspect_leakage": list(leakage),
    })
    ledger.save_metric(snapshot_id, "m", "v1", {
        "dimensions": [{"column": column, "point_in_time": "decision"} for column in registered],
        "rejected_columns": list(rejected),
    })
    _install_latest_mask(ledger)
    return ledger, snapshot_id


def _mature_frame():
    months = np.repeat([202401 + index for index in range(8)], 25)
    x = np.tile(np.array([-3] * 10 + [2] * 15), 8)
    bad = np.tile(np.array([1] * 10 + [0] * 15), 8)
    mature = pd.DataFrame({"month": months, "bad": bad, "x": x})
    immature = pd.DataFrame({"month": np.full(30, 202401), "bad": np.zeros(30, dtype=int), "x": np.full(30, -3)})
    frame = pd.concat([mature, immature], ignore_index=True)
    mask = np.array([True] * len(mature) + [False] * len(immature))
    return frame, mask


def test_depth_and_loosen_benefit_are_out_of_scope(tmp_path):
    ledger = Ledger(tmp_path)
    deep = simulate_policy_rules(
        "missing",
        ledger=ledger,
        mode="rule_mining",
        time_column="month",
        bad="bad",
        depth=2,
    )
    assert deep["status"] == "blocked"
    assert deep["block_code"] == "UNSUPPORTED_SCOPE"
    loosen = simulate_policy_rules(
        "missing",
        ledger=ledger,
        mode="cutoff",
        time_column="month",
        bad="bad",
        score="score",
        edges=[0, 50, 100],
        request_loosen_benefit=True,
    )
    assert loosen["block_code"] == "UNSUPPORTED_SCOPE"


def test_rule_mining_uses_mature_development_rows_only(tmp_path):
    frame, mask = _mature_frame()
    ledger, snapshot_id = _book(tmp_path, frame, mask)

    blocked = simulate_policy_rules(
        snapshot_id,
        ledger=ledger,
        mode="rule_mining",
        time_column=None,
        bad="bad",
        features=["x"],
    )
    assert blocked["block_code"] == "RANDOM_SPLIT_FORBIDDEN"

    mined = simulate_policy_rules(
        snapshot_id,
        ledger=ledger,
        mode="rule_mining",
        time_column="month",
        bad="bad",
        features=["x"],
    )
    assert mined["status"] == "ok"
    summary = mined["summary"]
    assert summary["trials"] >= 1
    assert 1 <= len(summary["recommended"]) <= 10
    assert any("已放款" in item for item in summary["caveats"])
    for rule in summary["recommended"]:
        assert rule["same_direction"] is True
        assert rule["oot_lift"] > 1
        assert {"hit_rate", "bad_rate", "lift", "oot_lift", "same_direction"} <= set(rule)
    assert summary["recommended"][0]["bad_rate"] == 1


def test_rule_mining_requires_registered_decision_features(tmp_path):
    """A3: features 非空且必须是已登记的决策时点维度；不许全表扫描。"""

    frame, mask = _mature_frame()
    ledger, snapshot_id = _book(tmp_path / "a", frame, mask)
    for features in ([], None, ["not_registered"]):
        result = simulate_policy_rules(
            snapshot_id, ledger=ledger, mode="rule_mining", time_column="month", bad="bad", features=features,
        )
        assert result["status"] == "blocked"
        assert result["block_code"] == "DIMENSION_UNREGISTERED"

    ledger2, snapshot_id2 = _book(tmp_path / "b", frame, mask, rejected=("obs_col",))
    violation = simulate_policy_rules(
        snapshot_id2, ledger=ledger2, mode="rule_mining", time_column="month", bad="bad", features=["obs_col"],
    )
    assert violation["status"] == "blocked"
    assert violation["block_code"] == "TIME_POINT_VIOLATION"


def test_error_paths_return_envelopes(tmp_path):
    """A7: 非法 mode / 缺 score / 非法 economics / 非法 observation_mode 都返回 blocked。"""

    frame, mask = _mature_frame()
    ledger, snapshot_id = _book(tmp_path, frame, mask)
    bad_mode = simulate_policy_rules(snapshot_id, ledger=ledger, mode="unknown", time_column="month", bad="bad")
    assert bad_mode["status"] == "blocked" and bad_mode["block_code"] == "UNSUPPORTED_SCOPE"
    no_score = simulate_policy_rules(snapshot_id, ledger=ledger, mode="cutoff", time_column="month", bad="bad", edges=[0, 1])
    assert no_score["status"] == "blocked" and no_score["block_code"] == "UNSUPPORTED_SCOPE"
    bad_econ = simulate_policy_rules(
        snapshot_id, ledger=ledger, mode="cutoff", time_column="month", bad="bad",
        score="x", edges=[0, 1, 2], economics="not-a-dict",
    )
    assert bad_econ["status"] == "blocked" and bad_econ["block_code"] == "VALIDATION_UNAVAILABLE"
    bad_mode2 = simulate_policy_rules(
        snapshot_id, ledger=ledger, mode="cutoff", time_column="month", bad="bad", observation_mode="wrong",
    )
    assert bad_mode2["status"] == "blocked" and bad_mode2["block_code"] == "UNSUPPORTED_SCOPE"


def test_cutoff_keeps_swap_in_rate_unknown_and_does_not_price_loosening(tmp_path):
    """A1 红线：调入集坏账率未知；A5/A6：真实经济核算与冠军挑战者设计单。"""

    score = np.linspace(1, 99, 160)
    frame = pd.DataFrame(
        {
            "month": np.repeat([202401 + index for index in range(8)], 20),
            "bad": (score < 40).astype(int),
            "score": score,
            "x": np.where(score < 40, -3, 2),
        }
    )
    ledger, snapshot_id = _book(tmp_path, frame, np.ones(len(frame), dtype=bool))
    out = simulate_policy_rules(
        snapshot_id,
        ledger=ledger,
        mode="cutoff",
        time_column="month",
        bad="bad",
        score="score",
        edges=[0, 25, 50, 75, 100],
        current_threshold=60,
        candidate_threshold=40,
        higher_is_safer=True,
        economics={"apr": 0.22, "lgd": 0.5},
    )
    assert out["status"] == "ok"
    summary = out["summary"]
    # 原测试意图（不估算放宽收益）保留；新红线把 swap_in 率固定为「未知」。
    assert summary["swap_in_bad_rate"] is None
    assert "bad_rate" not in summary["swap_in"]
    assert "bad_cnt" not in summary["swap_in"]
    assert "n" in summary["swap_in"]
    assert any("未知" in item for item in summary["caveats"])
    assert summary["bands"]
    assert any("未把放宽" in item for item in summary["caveats"])
    assert "profit" not in json.dumps(summary)
    # A5：economics 块真实核算，不估算 swap-in 收益。
    econ = summary["economics"]
    assert econ["break_even_bad_rate"] is not None
    assert len(econ["marginal_net_gain"]) == len(summary["bands"])
    assert "suggested_cutoff_range" in econ
    assert "source" in econ
    # A6：三档候选 + 待审批的试验设计单。
    assert [plan["plan"] for plan in summary["candidate_plans"]] == ["aggressive", "balanced", "conservative"]
    for plan in summary["candidate_plans"]:
        assert {"threshold", "approval_rate", "bad_rate", "bad_capture"} <= set(plan)
    trial = summary["trial_design"]
    assert trial["status"] == "pending_approval"
    assert trial["challenger_traffic"]["min"] >= 0.05 and trial["challenger_traffic"]["max"] <= 0.10
    assert trial["observation_window"] and trial["rollback_triggers"]


def test_rule_fallback_requires_choice_and_never_recommends_without_oot(tmp_path):
    frame = pd.DataFrame({
        "bad": np.tile([1] * 10 + [0] * 15, 8),
        "x": np.tile([-3] * 10 + [2] * 15, 8),
    })
    ledger, snapshot_id = _book(tmp_path, frame, np.ones(len(frame), dtype=bool))
    offered = simulate_policy_rules(snapshot_id, ledger=ledger, mode="rule_mining",
                                    time_column=None, bad="bad", features=["x"])
    assert offered["status"] == "needs_human"
    assert offered["block_code"] == "RANDOM_SPLIT_FORBIDDEN"
    assert offered["summary"]["fallback_review_id"] == offered["human_gate"]["review_id"]
    review_id = offered["human_gate"]["review_id"]
    for method in ("random_holdout", "descriptive_only"):
        record_human_decision(ledger=ledger, gate="validation_fallback", review_id=review_id,
                              actor="strategist", channel="approval_card", payload={"method": method})
        result = simulate_policy_rules(snapshot_id, ledger=ledger, mode="rule_mining",
                                       time_column=None, bad="bad", features=["x"],
                                       fallback_review_id=review_id)
        assert result["status"] == "degraded"
        assert result["summary"]["validation_method"] == method
        assert result["summary"]["fallback_review_id"] == review_id
        assert result["summary"]["recommended"] == []
        assert result["summary"]["candidates"]


def test_latest_observed_is_descriptive_only(tmp_path):
    """B2: 最新可观察样本只做描述性测算，不产出坏账率/收益/推荐。"""

    score = np.linspace(1, 99, 160)
    frame = pd.DataFrame(
        {
            "month": np.repeat([202401 + index for index in range(8)], 20),
            "bad": (score < 40).astype(int),
            "score": score,
            "x": np.where(score < 40, -3, 2),
        }
    )
    ledger, snapshot_id = _book(tmp_path, frame, np.ones(len(frame), dtype=bool))
    ledger.save_latest_mask(snapshot_id, pd.Series([True] * 120 + [False] * 40))
    review = "review_latest"
    ledger.save_run(review, "check_label_maturity", snapshot_id, {
        "observation_mode": "latest_observed", "mask_kind": "latest_observed", "snapshot_id": snapshot_id,
    })
    out = simulate_policy_rules(
        snapshot_id, ledger=ledger, mode="cutoff", time_column="month", bad="bad",
        score="score", edges=[0, 25, 50, 75, 100], features=["x"],
        current_threshold=60, candidate_threshold=40, higher_is_safer=True,
        observation_mode="latest_observed", latest_review_id=review,
    )
    assert out["status"] == "degraded"
    body = out["summary"]
    assert body["observation_mode"] == "latest_observed"
    assert body["latest_row_count"] == 120
    assert body["bad_rate"] is None and body["lift"] is None and body["recommended"] == []
    assert body["bands"] and all("bad_rate" not in band for band in body["bands"])
    assert body["candidates"] and all(c["bad_rate"] is None and c["lift"] is None for c in body["candidates"])
    assert body["swap_set"] and all(set(value) == {"n"} for value in body["swap_set"].values())
    assert "economics" not in body
    assert any("未成熟批次" in note for note in body["caveats"])
    for bad_review in (None, "not_a_run", snapshot_id):
        refused = simulate_policy_rules(
            snapshot_id, ledger=ledger, mode="cutoff", time_column="month", bad="bad",
            observation_mode="latest_observed", latest_review_id=bad_review,
        )
        assert refused["status"] == "blocked" and refused["block_code"] == "ADMISSION_REQUIRED"
