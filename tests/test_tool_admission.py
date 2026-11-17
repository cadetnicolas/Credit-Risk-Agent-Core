"""Synthetic-data tests for snapshot binding, maturity masks, and signed metrics."""

from __future__ import annotations

import json

import pandas as pd
import pytest

from credit_agent.tools.admission import (
    check_label_maturity,
    inspect_and_bind_snapshot,
    resolve_metric_and_dimensions,
)
from credit_agent.tools.ledger import Ledger


def _ledger(tmp_path) -> Ledger:
    return Ledger(tmp_path / "ledger")


def _registry(version: str = "v1", flag: str = "event_flag", weight: str = "principal") -> dict:
    return {
        "metrics": [
            {
                "metric_id": "event_rate",
                "version": version,
                "numerator": "sum(event_flag)",
                "denominator": "count(rows)",
                "weight_column": weight,
                "flag_column": flag,
                "maturity_mob": 3,
                "signed_by": "risk",
            }
        ],
        "dimensions": [
            {
                "column": "channel",
                "point_in_time": "decision",
                "max_cardinality": 20,
                "min_cell_n": 5,
                "min_expected_bad": 1,
            }
        ],
    }


def _bind(ledger: Ledger, frame: pd.DataFrame, **kwargs):
    params = {
        "grain": "loan",
        "key_columns": ["loan_id"],
        "decision_time_column": "decision_date",
        "observation_time_column": "obs_date",
        "weight_column": "principal",
    }
    params.update(kwargs)
    return inspect_and_bind_snapshot(frame, ledger=ledger, **params)


def _window(kind: str = "mob", **values) -> dict:
    if kind == "mob":
        return {"kind": "mob", "mob": values.get("mob", 3)}
    return {"kind": "fpd", "days": values["days"]}


def _no_saved_frame(tmp_path) -> None:
    assert list((tmp_path / "ledger" / "frames").glob("*.parquet")) == []


@pytest.mark.parametrize("grain", ["loan", "application"])
def test_duplicate_primary_key_blocks(tmp_path, grain):
    frame = pd.DataFrame(
        {
            "loan_id": ["DUP-LOAN-001", "DUP-LOAN-001"],
            "decision_date": ["2024-01-01", "2024-02-01"],
            "cohort": ["2024-01", "2024-02"],
        }
    )
    result = _bind(_ledger(tmp_path), frame, grain=grain, observation_time_column=None, weight_column=None)
    assert result["tool"] == "inspect_and_bind_snapshot"
    assert result["status"] == "blocked"
    assert result["block_code"] == "SNAPSHOT_INVALID"
    assert result["ok"] is False
    assert result["snapshot_id"] is None
    assert set(result["summary"]) == {"message"}
    assert "DUP-LOAN-001" not in result["summary"]["message"]
    _no_saved_frame(tmp_path)


def test_a_flag_column_cannot_be_the_decision_time(tmp_path):
    """0/1 列会被 pandas 当成 1970 年时间戳，绑定通过了就变成按结果分期。"""

    frame = pd.DataFrame(
        {
            "loan_id": [f"L{i}" for i in range(20)],
            "dec": [1.0] * 8 + [0.0] * 12,
        }
    )
    ledger = _ledger(tmp_path)
    result = _bind(
        ledger,
        frame,
        decision_time_column="dec",
        observation_time_column=None,
        weight_column=None,
    )
    assert result["ok"] is False
    assert result["block_code"] == "SNAPSHOT_INVALID"
    assert "不是日期" in result["summary"]["message"]

    # 数值型的「日期」（如 20240901）会被读成 1970，同样拒绝。
    numeric = pd.DataFrame(
        {"loan_id": [f"L{i}" for i in range(20)], "dec": [20240901] * 20}
    )
    rejected = _bind(
        ledger,
        numeric,
        decision_time_column="dec",
        observation_time_column=None,
        weight_column=None,
    )
    assert rejected["ok"] is False
    assert rejected["block_code"] == "SNAPSHOT_INVALID"


def test_object_dtype_flag_cannot_be_the_decision_time(tmp_path):
    """object 存的 0/1 也是 0/1：dtype 不是判据，值才是。"""

    frame = pd.DataFrame(
        {
            "loan_id": [f"L{i}" for i in range(20)],
            "dec": pd.Series([1] * 8 + [0] * 12, dtype=object),
        }
    )
    ledger = _ledger(tmp_path)
    result = _bind(
        ledger,
        frame,
        decision_time_column="dec",
        observation_time_column=None,
        weight_column=None,
    )
    assert result["ok"] is False
    assert result["block_code"] == "SNAPSHOT_INVALID"


@pytest.mark.parametrize(
    "values, label",
    [
        ([1] * 9 + [2] * 10 + ["2024-10-01"], "1/2 编码混一个文本日期"),
        ([2] * 9 + [3] * 10 + ["2024-10-01"], "2/3 编码混一个文本日期"),
        ([20240901] * 19 + ["2024-10-01"], "yyyymmdd 混一个文本日期"),
    ],
)
def test_a_mixed_numeric_decision_time_cannot_hide_behind_one_text_date(
    tmp_path, values, label
):
    """一个文本日期会把 max(year) 抬到 2024，纯数值行照样是 1970 时间戳。

    只要整个年份区间里有 1970，这一列就不是日期列：要么是编码，要么是
    没转干净的日期。按最大年份判断会放过混在一起的编码行，两期分母于是
    变成好客户数与坏客户数。
    """

    frame = pd.DataFrame(
        {"loan_id": [f"L{i}" for i in range(20)], "dec": pd.Series(values, dtype=object)}
    )
    ledger = _ledger(tmp_path)
    result = _bind(
        ledger,
        frame,
        decision_time_column="dec",
        observation_time_column=None,
        weight_column=None,
    )
    assert result["ok"] is False, label
    assert result["block_code"] == "SNAPSHOT_INVALID", label
    assert "不是日期" in result["summary"]["message"], label


def test_a_real_date_column_is_still_accepted(tmp_path):
    """收紧判定不能把正常日期列一起拒掉。"""

    frame = pd.DataFrame(
        {
            "loan_id": [f"L{i}" for i in range(20)],
            "decision_date": pd.to_datetime(["2024-09-01"] * 10 + ["2024-10-01"] * 10),
            "principal": [1000.0] * 20,
        }
    )
    result = _bind(
        _ledger(tmp_path), frame, observation_time_column=None, weight_column="principal"
    )
    assert result["status"] != "blocked", result.get("summary")


def test_missing_key_and_decision_time_block(tmp_path):
    ledger = _ledger(tmp_path)
    missing_key = pd.DataFrame({"apply_id": ["A1"], "decision_date": ["2024-01-01"]})
    blocked_key = _bind(ledger, missing_key, observation_time_column=None, weight_column=None)
    assert blocked_key["block_code"] == "SNAPSHOT_INVALID"
    assert blocked_key["summary"]["message"] == "主键列缺失"

    missing_time = pd.DataFrame({"loan_id": ["L-100"], "cohort": ["2024-01"]})
    blocked_time = _bind(ledger, missing_time, observation_time_column=None, weight_column=None)
    assert blocked_time["status"] == "blocked"
    assert blocked_time["block_code"] == "SNAPSHOT_INVALID"
    assert blocked_time["summary"]["message"] == "缺少决策时点列"
    assert "L-100" not in blocked_time["summary"]["message"]
    _no_saved_frame(tmp_path)


def test_single_null_key_is_reported_and_repeated_nulls_block(tmp_path):
    ledger = _ledger(tmp_path)
    one_null = pd.DataFrame(
        {
            "loan_id": ["L1", "L2", "L3", None],
            "decision_date": ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"],
        }
    )
    reported = _bind(ledger, one_null, observation_time_column=None, weight_column=None)
    assert reported["status"] == "ok"
    assert reported["summary"]["key_null_rate"] == pytest.approx(0.25)
    assert reported["summary"]["key_duplicate_rate"] == 0.0

    two_nulls = pd.DataFrame(
        {
            "loan_id": [None, None],
            "decision_date": ["2024-01-01", "2024-01-02"],
        }
    )
    blocked = _bind(ledger, two_nulls, observation_time_column=None, weight_column=None)
    assert blocked["status"] == "blocked"
    assert blocked["block_code"] == "SNAPSHOT_INVALID"


def test_loan_month_duplicate_keys_are_allowed(tmp_path):
    frame = pd.DataFrame(
        {
            "loan_id": ["L1", "L1"],
            "decision_date": ["2024-01-01", "2024-02-01"],
            "cohort": ["2024-01", "2024-02"],
            "channel": ["zz-sample-cell", "zz-sample-cell"],
        }
    )
    result = _bind(
        _ledger(tmp_path),
        frame,
        grain="loan_month",
        observation_time_column=None,
        weight_column=None,
    )
    assert result["status"] == "ok"
    assert result["block_code"] is None
    assert result["snapshot_id"].startswith("snap_")
    assert result["summary"]["row_count"] == 2
    assert result["summary"]["key_duplicate_rate"] == pytest.approx(0.5)
    assert "zz-sample-cell" not in json.dumps(result["summary"], ensure_ascii=False)


def test_snapshot_profile_roles_and_degraded_future_dates(tmp_path):
    frame = pd.DataFrame(
        {
            "loan_id": [f"L{i}" for i in range(10)],
            "decision_date": ["2024-01-15"] * 9 + ["2024-08-01"],
            "obs_date": ["2024-06-30"] * 10,
            "cohort": ["2024-01"] * 10,
            "channel": ["zz-sample-cell"] * 10,
            "note": [None] * 10,
            "current_dpd": [0, 1] * 5,
            "repay_status": [0, 1] * 5,
            "score": list(range(10)),
            "principal": list(range(10, 20)),
        }
    )
    ledger = _ledger(tmp_path)
    result = _bind(
        ledger,
        frame,
        column_roles={
            "decision_date": "suspect_leakage",
            "repay_status": "observation_outcome",
            "score": "suspect_leakage",
        },
    )
    summary = result["summary"]
    assert result["status"] == "degraded"
    assert result["ok"] is True
    assert summary["row_count"] == 10
    assert summary["time_min"] == "2024-01-15"
    assert summary["time_max"] == "2024-08-01"
    assert summary["cutoff"] == "2024-06-30"
    assert summary["future_date_rows"] == 1
    assert summary["key_duplicate_rate"] == 0.0
    assert summary["key_null_rate"] == 0.0
    assert summary["high_missing_columns"] == ["note"]
    assert summary["constant_columns"] == ["obs_date", "cohort", "channel"]
    assert summary["suspect_leakage"] == ["current_dpd", "score"]
    # B3: 正常表现字段与疑似穿越列区分呈现，不再混为一谈。
    assert summary["observation_outcomes"] == ["repay_status"]
    assert summary["role_counts"] == {
        "id": 1,
        "decision_time": 1,
        "observation_time": 1,
        "observation_outcome": 1,
        "suspect_leakage": 2,
        "feature": 0,
        "unclassified": 4,
    }
    assert "zz-sample-cell" not in json.dumps(summary, ensure_ascii=False)
    meta = ledger.get_snapshot(result["snapshot_id"])
    assert meta["grain"] == "loan"
    assert meta["cutoff"] == "2024-06-30"
    assert meta["columns"] == list(frame.columns)
    assert meta["column_roles"]["decision_date"] == "decision_time"
    assert meta["column_roles"]["loan_id"] == "id"
    assert meta["suspect_leakage"] == ["current_dpd", "score"]
    assert len(ledger.load_frame(result["snapshot_id"])) == 10


def test_feature_role_can_be_declared_explicitly(tmp_path):
    """0/1 字段只有被声明成 feature，描述工具才肯把它当特征来算。"""

    frame = pd.DataFrame(
        {
            "loan_id": [f"L{i}" for i in range(10)],
            "decision_date": ["2024-01-15"] * 10,
            "has_guarantor": [0, 1] * 5,
        }
    )
    ledger = _ledger(tmp_path)
    result = _bind(
        ledger,
        frame,
        observation_time_column=None,
        weight_column=None,
        column_roles={"has_guarantor": "feature"},
    )
    assert result["ok"] is True
    assert result["summary"]["role_counts"]["feature"] == 1
    meta = ledger.get_snapshot(result["snapshot_id"])
    assert meta["column_roles"]["has_guarantor"] == "feature"
    # feature 不是穿越列，也不进 observe 结果清单。
    assert result["summary"]["suspect_leakage"] == []
    assert result["summary"]["observation_outcomes"] == []


def test_immature_cohorts_are_masked_out(tmp_path):
    frame = pd.DataFrame(
        {
            "loan_id": ["A1", "A2", "B1", "B2", "C1"],
            "decision_date": ["2024-01-05", "2024-01-18", "2024-05-02", "2024-05-20", "2024-03-01"],
            "obs_date": ["2024-06-30"] * 5,
            "cohort": ["2024-01", "2024-01", "2024-05", "2024-05", "not-a-month"],
            "channel": ["east", "west", "east", "west", "east"],
            "principal": [10, 10, 10, 10, 10],
            "event_flag": [1, 0, 1, 0, 1],
        }
    )
    ledger = _ledger(tmp_path)
    bound = _bind(ledger, frame)
    assert bound["status"] == "ok"
    snapshot_id = bound["snapshot_id"]
    result = check_label_maturity(
        snapshot_id,
        ledger=ledger,
        cohort_column="cohort",
        decision_time_column="decision_date",
        performance_window=_window(),
        bad_flag_column="event_flag",
    )
    assert result["status"] == "degraded"
    assert result["summary"]["mask_id"] == snapshot_id
    assert result["summary"]["mature_row_count"] == 2
    assert result["summary"]["immature_row_count"] == 3
    assert result["summary"]["bad_rate_on_mature"] == pytest.approx(0.5)
    # B1: monthly_cohort 摘要补齐观察口径、账龄月规则、截止日与 T0 一致性。
    assert result["summary"]["observation_mode"] == "monthly_cohort"
    assert "Vintage/MOB" in result["summary"]["maturity_rule"]
    assert result["summary"]["window"]["window_months"] == 3
    assert result["summary"]["cutoff"] == "2024-06-30"
    assert result["summary"]["decision_time_column"] == "decision_date"
    assert result["summary"]["t0_cohort_mismatch"] == 0
    assert result["summary"]["mature_cohorts"] == [
        {"cohort": "2024-01", "n": 2, "mature": True},
        {"cohort": "2024-05", "n": 2, "mature": False},
        {"cohort": "not-a-month", "n": 1, "mature": False},
    ]
    mask = ledger.load_mask(snapshot_id)
    assert [bool(value) for value in mask.tolist()] == [True, True, False, False, False]
    assert ledger.get_admission(snapshot_id) is None

    resolved = resolve_metric_and_dimensions(
        snapshot_id,
        ledger=ledger,
        metric_id="event_rate",
        metric_version="v1",
        registry=_registry(),
    )
    assert resolved["status"] == "ok"
    assert ledger.get_admission(snapshot_id) == {"level": "B", "status": "degraded"}


def test_all_immature_is_label_immature(tmp_path):
    frame = pd.DataFrame(
        {
            "loan_id": ["L1", "L2"],
            "decision_date": ["2024-06-05", "2024-06-18"],
            "obs_date": ["2024-06-30", "2024-06-30"],
            "cohort": ["2024-06", "2024-06"],
            "channel": ["east", "west"],
            "principal": [10, 12],
            "event_flag": [1, 0],
            "current_dpd": [3, 0],
        }
    )
    ledger = _ledger(tmp_path)
    snapshot_id = _bind(ledger, frame)["snapshot_id"]
    result = check_label_maturity(
        snapshot_id,
        ledger=ledger,
        cohort_column="cohort",
        decision_time_column="decision_date",
        performance_window=_window(),
        bad_flag_column="event_flag",
    )
    assert result["status"] == "needs_human"
    assert result["block_code"] == "LABEL_IMMATURE"
    assert result["summary"]["mature_row_count"] == 0
    assert {item["method"] for item in result["summary"]["options"]} == {
        "shorter_signed_window", "unlabeled_descriptive", "wait_for_maturity"}
    assert result["ok"] is True
    assert "bad_rate_on_mature" not in result["summary"]
    assert "L1" not in result["summary"]["message"]
    stored = ledger.get_maturity(snapshot_id)
    assert stored["status"] == "blocked"
    assert stored["mature_row_count"] == 0
    assert stored["immature_row_count"] == 2
    assert stored["bad_rate_on_mature"] is None
    assert stored["mask_id"] == snapshot_id
    mask = ledger.load_mask(snapshot_id)
    assert [bool(value) for value in mask.tolist()] == [False, False]
    assert ledger.get_admission(snapshot_id) is None

    resolved = resolve_metric_and_dimensions(
        snapshot_id,
        ledger=ledger,
        metric_id="event_rate",
        metric_version="v1",
        registry=_registry(),
    )
    assert resolved["status"] == "ok"
    assert ledger.get_admission(snapshot_id) == {"level": "C", "status": "blocked"}


def test_metric_version_must_match(tmp_path):
    frame = pd.DataFrame(
        {
            "loan_id": ["L1", "L2"],
            "decision_date": ["2024-01-04", "2024-01-11"],
            "obs_date": ["2024-12-31", "2024-12-31"],
            "cohort": ["2024-01", "2024-01"],
            "channel": ["east", "west"],
            "principal": [10, 20],
            "event_flag": [0, 1],
        }
    )
    ledger = _ledger(tmp_path)
    snapshot_id = _bind(ledger, frame)["snapshot_id"]
    mismatch = resolve_metric_and_dimensions(
        snapshot_id,
        ledger=ledger,
        metric_id="event_rate",
        metric_version="v2",
        registry=_registry(version="v1"),
    )
    assert mismatch["status"] == "blocked"
    assert mismatch["block_code"] == "METRIC_UNSIGNED"
    assert mismatch["metric_id"] == "event_rate"
    assert mismatch["metric_version"] == "v2"
    assert ledger.get_metric(snapshot_id, "event_rate", "v1") is None
    assert ledger.get_metric(snapshot_id, "event_rate", "v2") is None

    other_id = resolve_metric_and_dimensions(
        snapshot_id,
        ledger=ledger,
        metric_id="other_rate",
        metric_version="v1",
        registry=_registry(),
    )
    assert other_id["block_code"] == "METRIC_UNSIGNED"

    wrong_case = resolve_metric_and_dimensions(
        snapshot_id,
        ledger=ledger,
        metric_id="event_rate",
        metric_version="V1",
        registry=_registry(version="v1"),
    )
    assert wrong_case["block_code"] == "METRIC_UNSIGNED"

    missing_column = resolve_metric_and_dimensions(
        snapshot_id,
        ledger=ledger,
        metric_id="event_rate",
        metric_version="v1",
        registry=_registry(flag="missing_flag"),
    )
    assert missing_column["status"] == "blocked"
    assert missing_column["block_code"] == "SNAPSHOT_INVALID"
    assert ledger.get_metric(snapshot_id, "event_rate", "v1") is None
    assert ledger.get_admission(snapshot_id) is None


def test_observation_dimensions_are_rejected(tmp_path):
    frame = pd.DataFrame(
        {
            "loan_id": ["L1", "L2"],
            "decision_date": ["2024-01-04", "2024-01-11"],
            "obs_date": ["2024-12-31", "2024-12-31"],
            "cohort": ["2024-01", "2024-01"],
            "channel": ["east", "west"],
            "principal": [10, 20],
            "event_flag": [1, 0],
            "obs_balance": [40, 50],
        }
    )
    ledger = _ledger(tmp_path)
    snapshot_id = _bind(ledger, frame)["snapshot_id"]
    registry = _registry()
    registry["dimensions"].append(
        {
            "column": "not_there",
            "point_in_time": "observation",
            "max_cardinality": 10,
            "min_cell_n": 5,
            "min_expected_bad": 1,
        }
    )
    missing = resolve_metric_and_dimensions(
        snapshot_id,
        ledger=ledger,
        metric_id="event_rate",
        metric_version="v1",
        registry=registry,
    )
    assert missing["block_code"] == "SNAPSHOT_INVALID"

    registry["dimensions"][-1] = {
        "column": "obs_balance",
        "point_in_time": "observation",
        "max_cardinality": 10,
        "min_cell_n": 5,
        "min_expected_bad": 1,
        "bins": [0, 10, 100],
    }
    result = resolve_metric_and_dimensions(
        snapshot_id,
        ledger=ledger,
        metric_id="event_rate",
        metric_version="v1",
        registry=registry,
    )
    summary = result["summary"]
    assert result["status"] == "ok"
    assert summary["rejected_columns"] == ["obs_balance"]
    assert [item["column"] for item in summary["dimensions"]] == ["channel"]
    assert summary["dimensions"][0]["point_in_time"] == "decision"
    assert summary["bound_columns"] == ["principal", "event_flag", "channel"]
    assert summary["metric"]["version"] == "v1"
    assert summary["metric"]["numerator"] == "sum(event_flag)"
    assert summary["metric"]["signed_by"] == "risk"
    assert "bins" not in json.dumps(summary["dimensions"])
    assert ledger.get_metric(snapshot_id, "event_rate", "v1")["rejected_columns"] == ["obs_balance"]


def test_three_steps_admission_level_a(tmp_path):
    # B3: Level A 现在要求成熟样本 >=50、正负各 >=10，故扩充样本量。
    n = 60
    frame = pd.DataFrame(
        {
            "loan_id": [f"L{i}" for i in range(n)],
            "decision_date": ["2024-01-10"] * n,
            "obs_date": ["2024-12-31"] * n,
            "cohort": ["2024-01"] * n,
            "channel": ["east", "west"] * (n // 2),
            "principal": [100, 200] * (n // 2),
            "event_flag": [1] * (n // 2) + [0] * (n // 2),
        }
    )
    ledger = _ledger(tmp_path)
    bound = _bind(ledger, frame)
    assert bound["status"] == "ok"
    assert bound["summary"]["suspect_leakage"] == []
    assert bound["summary"]["future_date_rows"] == 0
    snapshot_id = bound["snapshot_id"]
    maturity = check_label_maturity(
        snapshot_id,
        ledger=ledger,
        cohort_column="cohort",
        decision_time_column="decision_date",
        performance_window=_window(),
        bad_flag_column="event_flag",
    )
    assert maturity["status"] == "ok"
    assert maturity["summary"]["mature_row_count"] == n
    assert maturity["summary"]["immature_row_count"] == 0
    assert maturity["summary"]["bad_rate_on_mature"] == pytest.approx(0.5)
    assert ledger.load_mask(snapshot_id).astype(bool).all()
    assert ledger.get_admission(snapshot_id) is None
    resolved = resolve_metric_and_dimensions(
        snapshot_id,
        ledger=ledger,
        metric_id="event_rate",
        metric_version="v1",
        registry=_registry(),
    )
    assert resolved["status"] == "ok"
    assert ledger.get_admission(snapshot_id) == {"level": "A", "status": "ok"}


def test_three_steps_admission_level_b_when_leakage(tmp_path):
    frame = pd.DataFrame(
        {
            "loan_id": ["L1", "L2", "L3", "L4"],
            "decision_date": ["2024-01-10", "2024-01-20", "2024-02-05", "2024-02-18"],
            "obs_date": ["2024-12-31"] * 4,
            "cohort": ["2024-01", "2024-01", "2024-02", "2024-02"],
            "channel": ["east", "west", "east", "west"],
            "principal": [100, 200, 150, 180],
            "event_flag": [1, 0, 0, 1],
            "current_dpd": [0, 1, 0, 2],
        }
    )
    ledger = _ledger(tmp_path)
    bound = _bind(ledger, frame)
    assert bound["status"] == "ok"
    assert bound["summary"]["suspect_leakage"] == ["current_dpd"]
    snapshot_id = bound["snapshot_id"]
    resolved = resolve_metric_and_dimensions(
        snapshot_id,
        ledger=ledger,
        metric_id="event_rate",
        metric_version="v1",
        registry=_registry(),
    )
    assert resolved["status"] == "ok"
    assert ledger.get_admission(snapshot_id) is None
    maturity = check_label_maturity(
        snapshot_id,
        ledger=ledger,
        cohort_column="cohort",
        decision_time_column="decision_date",
        performance_window=_window(),
        bad_flag_column="event_flag",
    )
    assert maturity["status"] == "ok"
    assert ledger.get_admission(snapshot_id) == {"level": "B", "status": "degraded"}


def test_fpd_window_uses_natural_days(tmp_path):
    """B1: FPD 按 T0 + days <= cutoff 的自然日判定，不再 ceil(days/30) 折算成月。"""

    frame = pd.DataFrame(
        {
            "loan_id": ["L1", "L2"],
            "decision_date": ["2024-01-08", "2024-01-21"],
            "obs_date": ["2024-02-29", "2024-02-29"],
            "cohort": ["2024-01", "2024-01"],
            "channel": ["east", "west"],
            "principal": [10, 11],
            "event_flag": [1, 0],
        }
    )
    ledger = _ledger(tmp_path)
    snapshot_id = _bind(ledger, frame)["snapshot_id"]
    within = check_label_maturity(
        snapshot_id,
        ledger=ledger,
        cohort_column="cohort",
        decision_time_column="decision_date",
        performance_window=_window("fpd", days=30),
    )
    assert within["status"] == "ok"
    assert within["summary"]["mature_row_count"] == 2
    assert within["summary"]["bad_rate_on_mature"] is None
    # 新契约：按自然日，摘要写明实际日数与判定口径。
    assert within["summary"]["observation_mode"] == "monthly_cohort"
    assert within["summary"]["window"]["days"] == 30
    assert within["summary"]["window"]["window_months"] is None
    assert "自然日" in within["summary"]["maturity_rule"]
    assert within["summary"]["cutoff"] == "2024-02-29"
    assert within["summary"]["decision_time_column"] == "decision_date"
    assert within["summary"]["t0_cohort_mismatch"] == 0

    # 负例：最晚 T0 + 60 天已超过 cutoff，整批不成熟。
    beyond = check_label_maturity(
        snapshot_id,
        ledger=ledger,
        cohort_column="cohort",
        decision_time_column="decision_date",
        performance_window=_window("fpd", days=60),
    )
    assert beyond["status"] == "needs_human"
    assert beyond["block_code"] == "LABEL_IMMATURE"
    assert [bool(value) for value in ledger.load_mask(snapshot_id).tolist()] == [False, False]


def test_missing_snapshot_and_invalid_contracts(tmp_path):
    ledger = _ledger(tmp_path)
    frame = pd.DataFrame({"loan_id": ["L1"], "decision_date": ["2024-01-01"]})
    with pytest.raises(ValueError, match="grain"):
        _bind(ledger, frame, grain="account", observation_time_column=None, weight_column=None)
    with pytest.raises(ValueError, match="key_columns"):
        _bind(ledger, frame, key_columns="loan_id", observation_time_column=None, weight_column=None)
    with pytest.raises(ValueError, match="column_roles"):
        _bind(
            ledger,
            frame,
            observation_time_column=None,
            weight_column=None,
            column_roles={"loan_id": "leak"},
        )

    missing = check_label_maturity(
        "snap_missing",
        ledger=ledger,
        cohort_column="cohort",
        decision_time_column="decision_date",
        performance_window=_window(mob=1),
    )
    assert missing["block_code"] == "SNAPSHOT_INVALID"
    with pytest.raises(ValueError, match="kind"):
        check_label_maturity(
            "snap_missing",
            ledger=ledger,
            cohort_column="cohort",
            decision_time_column="decision_date",
            performance_window={"kind": "days", "days": 30},
        )
    with pytest.raises(ValueError, match="不小于 1"):
        check_label_maturity(
            "snap_missing",
            ledger=ledger,
            cohort_column="cohort",
            decision_time_column="decision_date",
            performance_window={"kind": "mob", "mob": 0},
        )
    _no_saved_frame(tmp_path)


def test_maturity_choice_requires_valid_approval_card(tmp_path):
    """B1: maturity_choice 决定必须来自 approval_card 且 method 合法。"""

    frame = pd.DataFrame(
        {
            "loan_id": ["L1", "L2"],
            "decision_date": ["2024-01-08", "2024-01-21"],
            "obs_date": ["2024-12-31", "2024-12-31"],
            "cohort": ["2024-01", "2024-01"],
            "channel": ["east", "west"],
            "principal": [10, 11],
            "event_flag": [1, 0],
        }
    )
    ledger = _ledger(tmp_path)
    snapshot_id = _bind(ledger, frame)["snapshot_id"]

    def run(review_id):
        return check_label_maturity(
            snapshot_id, ledger=ledger, cohort_column="cohort",
            decision_time_column="decision_date", performance_window=_window(),
            maturity_choice_review_id=review_id,
        )

    # 负例：缺决定 / 非法 method。
    missing = run("review_missing")
    assert missing["status"] == "blocked" and missing["block_code"] == "VALIDATION_UNAVAILABLE"
    ledger.save_decision("maturity_choice", "review_bad", "reviewer", "approval_card", {"method": "unknown"})
    invalid = run("review_bad")
    assert invalid["status"] == "blocked" and invalid["block_code"] == "VALIDATION_UNAVAILABLE"
    # 正例：合法选项只写入 selected_option，仍按 monthly_cohort 写成熟掩码。
    ledger.save_decision("maturity_choice", "review_ok", "reviewer", "approval_card", {"method": "wait_for_maturity"})
    ok = run("review_ok")
    assert ok["status"] in ("ok", "degraded")
    assert ok["summary"]["selected_option"] == "wait_for_maturity"
    assert ok["summary"]["observation_mode"] == "monthly_cohort"
    assert ledger.load_mask(snapshot_id).astype(bool).all()


def test_latest_observed_generates_descriptive_mask(tmp_path, monkeypatch):
    """B1: latest_observed 只写最新样本掩码，不写成熟掩码、不刷新准入。"""

    frame = pd.DataFrame(
        {
            "loan_id": ["L1", "L2", "L3"],
            "decision_date": ["2024-01-05", "2024-05-02", "2024-11-15"],
            "obs_date": ["2024-06-30"] * 3,
            "cohort": ["2024-01", "2024-05", "2024-11"],
            "channel": ["east", "west", "east"],
            "principal": [10, 10, 10],
            "event_flag": [1, 0, 1],
        }
    )
    ledger = _ledger(tmp_path)
    snapshot_id = _bind(ledger, frame)["snapshot_id"]

    def save(sid, mask):
        path = ledger.frames / f"{sid}.latest.parquet"
        pd.DataFrame({"latest": mask.astype(bool).to_numpy()}).to_parquet(path, index=False)
        return path

    monkeypatch.setattr(ledger, "save_latest_mask", save, raising=False)
    out = check_label_maturity(
        snapshot_id, ledger=ledger, cohort_column="cohort",
        decision_time_column="decision_date", performance_window=_window(),
        observation_mode="latest_observed",
    )
    assert out["status"] == "degraded"
    body = out["summary"]
    assert body["observation_mode"] == "latest_observed"
    assert body["label_status"] == "immature"
    assert body["latest_row_count"] == 2  # 2024-11-15 晚于 cutoff，排除
    assert body["latest_review_id"] and body["latest_cohorts"]
    assert any("不可用于建模" in note for note in body["notes"])
    assert (ledger.frames / f"{snapshot_id}.latest.parquet").is_file()
    assert ledger.get_maturity(snapshot_id) is None
    assert ledger.get_admission(snapshot_id) is None
    run = ledger.get_run(body["latest_review_id"])
    assert run and run["summary"]["mask_kind"] == "latest_observed"
