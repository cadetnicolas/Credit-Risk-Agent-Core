"""Portfolio monitoring and anomaly location on a synthetic two-period panel."""

from __future__ import annotations

import pandas as pd
import pytest

from credit_agent.tools import Ledger
from credit_agent.tools.portfolio import decompose_and_locate_anomaly, run_portfolio_monitoring

_LEGAL_STOP = {"located", "universal_deterioration", "mix_dispersion", "unlocated"}


def _loans(period: str, channel: str, n: int, n_bad: int) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "loan_id": [f"{period}-{channel}-{i}" for i in range(n)],
            "period": period,
            "channel": channel,
            "bad": [1] * n_bad + [0] * (n - n_bad),
        }
    )


def _binding() -> dict:
    return {
        "flag_column": "bad",
        "period_column": "period",
        "weight_column": "",
        "policy_version_column": "",
        "dimensions": [{"column": "channel", "point_in_time": "decision"}],
    }


def _ledger(tmp_path, frame: pd.DataFrame, *, admission: str | None = "ok") -> tuple[Ledger, str]:
    ledger = Ledger(tmp_path / "ledger")
    snapshot_id = "snap-panel"
    ledger.save_snapshot(snapshot_id, {"source": "synthetic"})
    ledger.save_frame(snapshot_id, frame)
    if admission is not None:
        ledger.save_admission(snapshot_id, "standard", admission)
    ledger.save_metric(snapshot_id, "dpd30", "v1", _binding())
    return ledger, snapshot_id


def _monitor(ledger: Ledger, snapshot_id: str, **overrides):
    params = {
        "ledger": ledger,
        "metric_id": "dpd30",
        "metric_version": "v1",
        "base_periods": ["2024-01"],
        "comp_periods": ["2024-02"],
    }
    params.update(overrides)
    return run_portfolio_monitoring(snapshot_id, **params)


def test_located_slice_or_legal_stop(tmp_path):
    frame = pd.concat(
        [
            _loans("2024-01", "online", 400, 40),
            _loans("2024-02", "online", 400, 160),
            _loans("2024-01", "branch", 400, 40),
            _loans("2024-02", "branch", 400, 40),
        ],
        ignore_index=True,
    )
    ledger, snapshot_id = _ledger(tmp_path, frame)
    monitored = _monitor(ledger, snapshot_id)

    assert monitored["status"] == "ok"
    # C3: vintage/migration 缺列时收窄可下结论，不再恒给出全部结论。
    assert "slice_contribution" not in monitored["allowed_conclusions"]
    assert monitored["run_id"].startswith("run_")
    assert monitored["artifacts"] and monitored["artifacts"][0]["kind"] == "run_portfolio_monitoring"
    summary = monitored["summary"]
    assert set(summary) == {
        "base_rate",
        "comp_rate",
        "delta",
        "base_periods",
        "comp_periods",
        "alarm_level",
        "vintage_comparable_mob",
        "roll_highlights",
        "notes",
    }
    assert summary["vintage_comparable_mob"] is None
    assert summary["roll_highlights"] == []
    assert any("vintage" in note for note in summary["notes"])
    saved = ledger.get_run(monitored["run_id"])["summary"]
    assert saved["vintage"] is None
    for banned in ("series", "curves", "matrix", "segments", "detail", "by_period"):
        assert banned not in saved

    located = decompose_and_locate_anomaly(monitored["run_id"], ledger=ledger)
    assert located["status"] == "ok"
    body = located["summary"]
    assert body["located"] or body["stop_rule"] in _LEGAL_STOP
    assert body["stop_rule"] in _LEGAL_STOP
    if body["located"]:
        assert body["stop_rule"] == "located"
        assert any(item["value"] == "online" for item in body["located"])
    assert "slice_contribution" in body["allowed_conclusions"] or not body["located"]
    assert "policy_version_gap" not in body["allowed_conclusions"]


def test_admission_required(tmp_path):
    ledger = Ledger(tmp_path / "ledger")
    missing = _monitor(ledger, "snap-missing")
    assert missing["status"] == "blocked"
    assert missing["block_code"] == "ADMISSION_REQUIRED"
    assert missing["ok"] is False
    assert "base_rate" not in missing["summary"]

    ledger.save_admission("snap-blocked", "standard", "blocked")
    blocked = _monitor(ledger, "snap-blocked")
    assert blocked["block_code"] == "ADMISSION_REQUIRED"

    ledger.save_run(
        "run_saved",
        "run_portfolio_monitoring",
        "snap-open",
        {
            "metric": {"metric_id": "dpd30", "metric_version": "v1"},
            "periods": {"base_periods": ["2024-01"], "comp_periods": ["2024-02"]},
            "delta": 0.1,
        },
    )
    decomposed = decompose_and_locate_anomaly("run_saved", ledger=ledger)
    assert decomposed["status"] == "blocked"
    assert decomposed["block_code"] == "ADMISSION_REQUIRED"


def test_missing_or_mismatched_periods_are_blocked(tmp_path):
    """C1: 空期次或期次与数据不匹配都返回 blocked/PERIOD_MISMATCH，不静默回退。"""

    ledger = Ledger(tmp_path / "ledger")
    empty = run_portfolio_monitoring(
        "snap", ledger=ledger, metric_id="dpd30", metric_version="v1",
        base_periods=["2024-01"], comp_periods=[],
    )
    assert empty["status"] == "blocked" and empty["block_code"] == "PERIOD_MISMATCH"
    none_base = run_portfolio_monitoring(
        "snap", ledger=ledger, metric_id="dpd30", metric_version="v1",
        base_periods=None, comp_periods=["2024-02"],
    )
    assert none_base["status"] == "blocked" and none_base["block_code"] == "PERIOD_MISMATCH"
    # 负例：显式期次不在数据中，不得静默回退到「最后 3 期 vs 之前」。
    frame = pd.concat(
        [_loans("2024-01", "online", 20, 2), _loans("2024-02", "online", 20, 4)],
        ignore_index=True,
    )
    ledger2, snapshot_id2 = _ledger(tmp_path / "mismatch", frame)
    mismatched = _monitor(ledger2, snapshot_id2, base_periods=["2099-01"], comp_periods=["2099-02"])
    assert mismatched["status"] == "blocked" and mismatched["block_code"] == "PERIOD_MISMATCH"


def test_dimensions_must_be_registered(tmp_path):
    """C2: 显式 dimensions 必须在登记表内，否则 blocked/DIMENSION_UNREGISTERED。"""

    frame = pd.concat(
        [_loans("2024-01", "online", 20, 2), _loans("2024-02", "online", 20, 4)],
        ignore_index=True,
    )
    ledger, snapshot_id = _ledger(tmp_path, frame)
    blocked = _monitor(ledger, snapshot_id, dimensions=["not_registered"])
    assert blocked["status"] == "blocked" and blocked["block_code"] == "DIMENSION_UNREGISTERED"
    ok = _monitor(ledger, snapshot_id, dimensions=["channel"])
    assert ok["status"] == "ok"


def test_small_sample_cell_is_not_located(tmp_path):
    frame = pd.concat(
        [
            _loans("2024-01", "big", 200, 20),
            _loans("2024-02", "big", 200, 80),
            _loans("2024-01", "tiny", 20, 2),
            _loans("2024-02", "tiny", 20, 16),
        ],
        ignore_index=True,
    )
    ledger, snapshot_id = _ledger(tmp_path, frame)
    monitored = _monitor(ledger, snapshot_id)
    located = decompose_and_locate_anomaly(monitored["run_id"], ledger=ledger)
    body = located["summary"]
    values = {item["value"] for item in body["located"]}
    assert "tiny" not in values
    for item in body["located"]:
        assert item["n_base"] >= 50
        assert item["n_comp"] >= 50
    assert body["observe_only"]["count"] >= 1
    assert any("tiny" in name for name in body["observe_only"]["names"])
    # C3: observe_only 名单不再截断为前 10 个。
    assert len(body["observe_only"]["names"]) == body["observe_only"]["count"]
    assert body["closure_residual"] < 1e-9
