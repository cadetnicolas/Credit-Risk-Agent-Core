"""Bind a dataset snapshot, judge label maturity, and attach a signed metric.

Row-level frames and maturity masks stay in the ledger. Callers receive an
envelope whose summary contains counts, rates, column names, and cohort
labels only.
"""

from __future__ import annotations

import math
from numbers import Integral
from typing import Any, Dict, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from credit_agent.tools.envelope import envelope
from credit_agent.tools.ledger import Ledger, new_id

_GRAINS = ("loan", "application", "loan_month")
_EXPLICIT_ROLES = ("decision_time", "observation_outcome", "suspect_leakage")
_ROLE_KEYS = (
    "id",
    "decision_time",
    "observation_time",
    "observation_outcome",
    "suspect_leakage",
    "unclassified",
)
# 贷后/观察时点快照类字段仍属疑似穿越；正常表现结果字段不算嫌疑。
_SUSPECT_LEAKAGE_TOKENS = ("current_", "collect", "repay")
_OBSERVATION_OUTCOME_TOKENS = (
    "dpd",
    "overdue",
    "delq",
    "writeoff",
    "bucket",
    "bad",
    "label",
    "target",
    "mob",
)
_LEAKAGE_TOKENS = _SUSPECT_LEAKAGE_TOKENS + _OBSERVATION_OUTCOME_TOKENS
_DIMENSION_FIELDS = ("max_cardinality", "min_cell_n", "min_expected_bad", "bins")
_METRIC_COLUMN_KEYS = (
    "flag_column",
    "weight_column",
    "period_column",
    "loan_id",
    "cohort",
    "mob",
    "bucket",
    "dpd",
    "policy_version_column",
)
_MATURITY_OPTIONS = (
    {"method": "shorter_signed_window", "input": "较短且已签收的指标与表现期；重新执行成熟度核查"},
    {"method": "unlabeled_descriptive", "input": "只查看近期规模和字段质量，不计算目标坏账率"},
    {"method": "wait_for_maturity", "input": "等待目标表现期成熟后再验证"},
)
_LATEST_NOTES = (
    "仅可做描述性测算（规模、分布、命中率），不产出目标表现期成熟坏账率。",
    "不视未成熟样本为 Good，也不做拒绝推断。",
    "不可用于建模、上线收益测算或目标坏账率验证。",
)
_MATURITY_CHOICE_METHODS = ("shorter_signed_window", "unlabeled_descriptive", "wait_for_maturity")
_MIN_MATURE_ROWS = 50
_MIN_CLASS_ROWS = 10


def inspect_and_bind_snapshot(
    frame: pd.DataFrame,
    *,
    ledger: Ledger,
    grain: str,
    key_columns: Sequence[str],
    decision_time_column: str,
    observation_time_column: Optional[str] = None,
    weight_column: Optional[str] = None,
    column_roles: Optional[Mapping[str, str]] = None,
    as_of_date: Optional[str] = None,
) -> Dict[str, Any]:
    """Profile a frame and store an immutable snapshot.

    ``grain`` is ``loan``, ``application``, or ``loan_month``. Repeated keys
    block a loan or application snapshot. A loan-month snapshot may repeat a
    loan across months. The cutoff is the latest observation date, or the
    latest decision date when no observation column is available (in that
    case the future-date check is explicitly marked unavailable). Decision
    dates after that cutoff degrade the snapshot but do not discard it.

    A column is constant when its most common non-null value covers more than
    99% of the non-null entries. A column is high-missing when more than 95%
    of its entries are null.
    """

    frame = _require_frame(frame)
    _require_ledger(ledger)
    if grain not in _GRAINS:
        raise ValueError("grain 只接受 loan、application 或 loan_month")
    keys = _key_names(key_columns)
    decision_time_column = _text(decision_time_column, "decision_time_column")
    observation_time_column = _optional_text(observation_time_column, "observation_time_column")
    weight_column = _optional_text(weight_column, "weight_column")
    explicit_roles = _explicit_roles(column_roles)

    missing_keys = [column for column in keys if column not in frame.columns]
    if not keys or missing_keys:
        return _blocked("inspect_and_bind_snapshot", "SNAPSHOT_INVALID", "主键列缺失")
    if decision_time_column not in frame.columns:
        return _blocked("inspect_and_bind_snapshot", "SNAPSHOT_INVALID", "缺少决策时点列")
    if observation_time_column and observation_time_column not in frame.columns:
        return _blocked("inspect_and_bind_snapshot", "SNAPSHOT_INVALID", "观察时点列不在数据中")
    if weight_column and weight_column not in frame.columns:
        return _blocked("inspect_and_bind_snapshot", "SNAPSHOT_INVALID", "权重列不在数据中")
    missing_roles = [column for column in explicit_roles if column not in frame.columns]
    if missing_roles:
        return _blocked("inspect_and_bind_snapshot", "SNAPSHOT_INVALID", "列角色中的列不在数据中")
    if len(frame) == 0:
        return _blocked("inspect_and_bind_snapshot", "SNAPSHOT_INVALID", "数据表没有行")

    null_rate, duplicate_rate = _key_rates(frame, keys)
    if grain in ("loan", "application") and duplicate_rate > 0:
        return _blocked("inspect_and_bind_snapshot", "SNAPSHOT_INVALID", "主键存在重复")

    decision_days = _day_series(frame[decision_time_column])
    if not bool(decision_days.notna().any()):
        return _blocked("inspect_and_bind_snapshot", "SNAPSHOT_INVALID", "决策时点无法解析，不能确定截止日")
    as_of = None
    if as_of_date is not None:
        as_of = _parse_day(as_of_date)
        if as_of is None:
            return _blocked(
                "inspect_and_bind_snapshot",
                "SNAPSHOT_INVALID",
                "as_of_date 无法解析为日期，请用 YYYY-MM-DD 传入数据获取日",
            )
    if observation_time_column:
        observation_days = _day_series(frame[observation_time_column])
        observed_max = observation_days.max() if bool(observation_days.notna().any()) else decision_days.max()
    else:
        observed_max = decision_days.max()
    if as_of is not None:
        # The performance window runs from the observation time forward to the
        # date the extract was taken, not to the largest date inside the file.
        # A file pulled today can hold rows whose last observation is years old
        # and still be fully mature; keying the cutoff off the file maximum
        # would wrongly report every cohort as immature.
        cutoff = as_of
        cutoff_source = "as_of_date"
        if as_of < pd.Timestamp(observed_max).normalize():
            cutoff_source = "as_of_date_before_data_max"
        future_date_check = (
            "available: 以数据获取日 as_of_date 作为表现期截止日"
            if cutoff_source == "as_of_date"
            else "degraded: as_of_date 早于数据内最大观察日，成熟度不可靠"
        )
        future_rows = int((decision_days.notna() & (decision_days > cutoff)).sum())
    else:
        cutoff = observed_max
        if observation_time_column:
            cutoff_source = "data_max_observation"
            future_date_check = "available: 以观察时点列最大值作为截止日（未传 as_of_date）"
        else:
            cutoff_source = "data_max_decision"
            future_date_check = "unavailable: 缺少观察时点列，截止日取自决策时点最大值，无法判定未来日期"
        future_rows = 0 if not observation_time_column else int(
            (decision_days.notna() & (decision_days > cutoff)).sum()
        )
    cutoff = pd.Timestamp(cutoff).normalize()
    observed_decision = decision_days.dropna()
    roles = _assign_roles(
        list(frame.columns),
        keys=keys,
        decision_time_column=decision_time_column,
        observation_time_column=observation_time_column,
        explicit_roles=explicit_roles,
    )
    high_missing, constant = _quality_columns(frame)
    leakage = [column for column, role in roles.items() if role == "suspect_leakage"]
    observation_outcomes = [column for column, role in roles.items() if role == "observation_outcome"]
    summary = {
        "row_count": int(len(frame)),
        "time_min": _iso(observed_decision.min()),
        "time_max": _iso(observed_decision.max()),
        "cutoff": _iso(cutoff),
        "cutoff_source": cutoff_source,
        "as_of_date": _iso(as_of) if as_of is not None else None,
        "data_observed_max": _iso(observed_max),
        "key_duplicate_rate": duplicate_rate,
        "key_null_rate": null_rate,
        "future_date_rows": future_rows,
        "future_date_check": future_date_check,
        "high_missing_columns": high_missing,
        "constant_columns": constant,
        "role_counts": _role_counts(roles),
        "suspect_leakage": leakage,
        "observation_outcomes": observation_outcomes,
    }
    snapshot_id = new_id("snap")
    meta = {
        "grain": grain,
        "column_roles": roles,
        "cutoff": summary["cutoff"],
        "cutoff_source": summary["cutoff_source"],
        "as_of_date": summary["as_of_date"],
        "data_observed_max": summary["data_observed_max"],
        "columns": [str(column) for column in frame.columns],
        "key_columns": list(keys),
        "decision_time_column": decision_time_column,
        "observation_time_column": observation_time_column,
        "weight_column": weight_column,
        "suspect_leakage": leakage,
        "observation_outcomes": observation_outcomes,
        "time_min": summary["time_min"],
        "time_max": summary["time_max"],
    }
    ledger.save_frame(snapshot_id, frame)
    ledger.save_snapshot(snapshot_id, meta)
    return envelope(
        tool="inspect_and_bind_snapshot",
        status="degraded" if (future_rows > 0 or cutoff_source == "as_of_date_before_data_max") else "ok",
        snapshot_id=snapshot_id,
        summary=summary,
    )


def check_label_maturity(
    snapshot_id: str,
    *,
    ledger: Ledger,
    cohort_column: str,
    decision_time_column: str,
    performance_window: Mapping[str, Any],
    bad_flag_column: Optional[str] = None,
    observation_mode: str = "monthly_cohort",
    maturity_choice_review_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Judge label maturity under a chosen observation mode.

    ``monthly_cohort`` (modelling / monitoring default) keeps the industry
    Vintage/MOB convention: a cohort is mature when its month-end plus ``W``
    whole account months is on or before the snapshot cutoff. It is NOT a
    per-loan T0 rule. A FPD window switches to natural days: the latest T0 in
    the cohort plus ``days`` must be on or before the cutoff, without folding
    days into months.

    ``latest_observed`` (strategy "use the newest possible sample") writes a
    separate latest-observable mask (every row with T0 <= cutoff, including
    immature cohorts), never a maturity mask, and never refreshes admission.
    Its output is descriptive only.
    """

    _require_ledger(ledger)
    snapshot_id = _text(snapshot_id, "snapshot_id")
    cohort_column = _text(cohort_column, "cohort_column")
    decision_time_column = _text(decision_time_column, "decision_time_column")
    bad_flag_column = _optional_text(bad_flag_column, "bad_flag_column")
    if observation_mode not in ("monthly_cohort", "latest_observed"):
        return _blocked(
            "check_label_maturity",
            "UNSUPPORTED_SCOPE",
            "observation_mode 只接受 monthly_cohort 或 latest_observed",
            snapshot_id=snapshot_id,
        )
    window = _window_info(performance_window)

    selected_option = None
    if maturity_choice_review_id:
        decision = ledger.get_decision("maturity_choice", maturity_choice_review_id)
        method = (
            (decision or {}).get("payload", {}).get("method")
            if decision and decision.get("channel") == "approval_card"
            else None
        )
        if method not in _MATURITY_CHOICE_METHODS:
            return _blocked(
                "check_label_maturity",
                "VALIDATION_UNAVAILABLE",
                "成熟度选项决定缺失或 method 非法",
                snapshot_id=snapshot_id,
            )
        selected_option = method

    def finish(result: Dict[str, Any]) -> Dict[str, Any]:
        _refresh_admission(ledger, snapshot_id)
        return result

    meta = ledger.get_snapshot(snapshot_id)
    if meta is None:
        return finish(_blocked("check_label_maturity", "SNAPSHOT_INVALID", "快照不存在", snapshot_id=snapshot_id))
    try:
        frame = ledger.load_frame(snapshot_id)
    except KeyError:
        return finish(_blocked("check_label_maturity", "SNAPSHOT_INVALID", "快照明细不存在", snapshot_id=snapshot_id))
    if cohort_column not in frame.columns:
        return finish(_blocked("check_label_maturity", "SNAPSHOT_INVALID", "批次列不在快照中", snapshot_id=snapshot_id))
    if decision_time_column not in frame.columns:
        return finish(_blocked("check_label_maturity", "SNAPSHOT_INVALID", "决策时点列不在快照中", snapshot_id=snapshot_id))
    if bad_flag_column and bad_flag_column not in frame.columns:
        return finish(_blocked("check_label_maturity", "SNAPSHOT_INVALID", "坏标记列不在快照中", snapshot_id=snapshot_id))
    cutoff = _parse_day(meta.get("cutoff"))
    if cutoff is None:
        return finish(_blocked("check_label_maturity", "SNAPSHOT_INVALID", "快照缺少截止日", snapshot_id=snapshot_id))

    if observation_mode == "latest_observed":
        return _latest_observed_result(
            ledger, snapshot_id, frame, cohort_column, decision_time_column, cutoff, window, selected_option
        )

    labels = [_cohort_label(value) for value in frame[cohort_column].tolist()]
    t0 = _day_series(frame[decision_time_column])
    mismatch = _t0_cohort_mismatch(labels, t0)
    mature_labels, cohorts = _cohort_maturity(labels, t0, window, cutoff)
    mask = pd.Series([label in mature_labels for label in labels], index=frame.index, dtype=bool)
    mature_rows = int(mask.sum())
    immature_rows = int(len(frame) - mature_rows)
    if bad_flag_column and mature_rows:
        flags = pd.to_numeric(frame.loc[mask, bad_flag_column], errors="coerce").dropna()
        bad_rate = None if flags.empty else float(flags.mean())
    else:
        bad_rate = None
    if mature_rows == 0:
        status = "blocked"
    elif immature_rows or mismatch > 0:
        status = "degraded"
    else:
        status = "ok"
    if window["kind"] == "fpd":
        days = window["days"]
        maturity_rule = (
            f"FPD 按自然日判定：批次内最晚决策时点 T0 + {days} 天 <= cutoff"
            f"（实际使用 {days} 个自然日，不折算为账龄月）"
        )
    else:
        months = window["window_months"]
        maturity_rule = f"按放款批次月末 + {months} 个完整账龄月 <= cutoff 判定（Vintage/MOB 行业惯例）"
    summary = {
        "mature_cohorts": cohorts,
        "mature_row_count": mature_rows,
        "immature_row_count": immature_rows,
        "bad_rate_on_mature": bad_rate,
        "mask_id": snapshot_id,
        "bad_flag_column": bad_flag_column,
        "observation_mode": "monthly_cohort",
        "maturity_rule": maturity_rule,
        "window": window,
        "cutoff": _iso(cutoff),
        "decision_time_column": decision_time_column,
        "t0_cohort_mismatch": mismatch,
    }
    if selected_option is not None:
        summary["selected_option"] = selected_option
    ledger.save_maturity(snapshot_id, {**summary, "status": status})
    ledger.save_mask(snapshot_id, mask if mature_rows else pd.Series(False, index=frame.index))
    if status == "blocked":
        return finish(
            envelope(
                tool="check_label_maturity", status="needs_human",
                block_code="LABEL_IMMATURE", snapshot_id=snapshot_id,
                summary={
                    "message": "目标表现期下没有成熟样本；当前目标坏账率和建模仍不可计算",
                    "target_window_months": window.get("window_months"),
                    "mature_row_count": 0,
                    "immature_row_count": immature_rows,
                    "observation_mode": "monthly_cohort",
                    "maturity_rule": maturity_rule,
                    "window": window,
                    "options": [dict(item) for item in _MATURITY_OPTIONS],
                },
            )
        )
    return finish(envelope(tool="check_label_maturity", status=status, snapshot_id=snapshot_id, summary=summary))


def _latest_observed_result(
    ledger: Ledger,
    snapshot_id: str,
    frame: pd.DataFrame,
    cohort_column: str,
    decision_time_column: str,
    cutoff: pd.Timestamp,
    window: Mapping[str, Any],
    selected_option: Optional[str],
) -> Dict[str, Any]:
    """Descriptive newest-observable sample: no maturity mask, no admission refresh."""

    if not hasattr(ledger, "save_latest_mask"):
        return _blocked(
            "check_label_maturity",
            "VALIDATION_UNAVAILABLE",
            "账本不支持最新可观察样本掩码（等待 Ledger.save_latest_mask）",
            snapshot_id=snapshot_id,
        )
    t0 = _day_series(frame[decision_time_column])
    latest_mask = (t0.notna() & (t0 <= cutoff)).astype(bool)
    latest_review_id = new_id("review")
    try:
        ledger.save_latest_mask(snapshot_id, latest_mask)
    except (OSError, ValueError, TypeError) as exc:
        return _blocked(
            "check_label_maturity", "VALIDATION_UNAVAILABLE",
            f"最新可观察样本掩码写入失败: {exc}", snapshot_id=snapshot_id,
        )
    latest_row_count = int(latest_mask.sum())
    selected = frame.loc[latest_mask, cohort_column]
    latest_cohorts = sorted({str(_cohort_label(value) or value) for value in selected.tolist()})
    # 记录一次运行，供 simulate_policy_rules 校验 latest_review_id 的来源。
    ledger.save_run(
        latest_review_id,
        "check_label_maturity",
        snapshot_id,
        {
            "observation_mode": "latest_observed",
            "mask_kind": "latest_observed",
            "snapshot_id": snapshot_id,
            "latest_row_count": latest_row_count,
            "latest_cohorts": latest_cohorts,
        },
    )
    summary: Dict[str, Any] = {
        "observation_mode": "latest_observed",
        "label_status": "immature",
        "latest_review_id": latest_review_id,
        "latest_row_count": latest_row_count,
        "latest_cohorts": latest_cohorts,
        "cutoff": _iso(cutoff),
        "decision_time_column": decision_time_column,
        "window": dict(window),
        "maturity_rule": "latest_observed：所有 T0 <= cutoff 的最新可观察样本（含未成熟批次），不写成熟掩码",
        "options": [dict(item) for item in _MATURITY_OPTIONS],
        "notes": list(_LATEST_NOTES),
    }
    if selected_option is not None:
        summary["selected_option"] = selected_option
    return envelope(
        tool="check_label_maturity", status="degraded", snapshot_id=snapshot_id, summary=summary
    )


def resolve_metric_and_dimensions(
    snapshot_id: str,
    *,
    ledger: Ledger,
    metric_id: str,
    metric_version: str,
    registry: Mapping[str, Any],
) -> Dict[str, Any]:
    """Bind one signed metric and keep only decision-time dimensions.

    ``metric_id`` and ``metric_version`` must equal the registry strings
    exactly. Dimensions whose point in time is not ``decision`` are named in
    ``rejected_columns`` and are not returned as usable dimensions.
    """

    _require_ledger(ledger)
    snapshot_id = _text(snapshot_id, "snapshot_id")
    metric_id = _text(metric_id, "metric_id")
    metric_version = _text(metric_version, "metric_version")
    metrics, dimensions = _registry_parts(registry)
    meta = ledger.get_snapshot(snapshot_id)
    if meta is None:
        return _blocked(
            "resolve_metric_and_dimensions",
            "SNAPSHOT_INVALID",
            "快照不存在",
            snapshot_id=snapshot_id,
            metric_id=metric_id,
            metric_version=metric_version,
        )
    matched = _match_metric(metrics, metric_id, metric_version)
    if matched is None:
        return _blocked(
            "resolve_metric_and_dimensions",
            "METRIC_UNSIGNED",
            "指标未签收或版本不一致",
            snapshot_id=snapshot_id,
            metric_id=metric_id,
            metric_version=metric_version,
        )
    known = _snapshot_columns(ledger, snapshot_id, meta)
    if known is None:
        return _blocked(
            "resolve_metric_and_dimensions",
            "SNAPSHOT_INVALID",
            "快照明细不存在",
            snapshot_id=snapshot_id,
            metric_id=metric_id,
            metric_version=metric_version,
        )
    required = _required_columns(matched, dimensions)
    if any(column not in known for column in required):
        return _blocked(
            "resolve_metric_and_dimensions",
            "SNAPSHOT_INVALID",
            "已声明的列不在快照中",
            snapshot_id=snapshot_id,
            metric_id=metric_id,
            metric_version=metric_version,
        )
    accepted, rejected = _split_dimensions(dimensions)
    metric = _plain(matched)
    summary = {
        "metric": metric,
        "bound_columns": _bound_columns(metric, accepted),
        "dimensions": accepted,
        "rejected_columns": rejected,
        "columns": _metric_columns(metric),
    }
    ledger.save_metric(snapshot_id, metric_id, metric_version, summary)
    stored = dict(meta)
    stored["bound_metric_id"] = metric_id
    stored["bound_metric_version"] = metric_version
    ledger.save_snapshot(snapshot_id, stored)
    _refresh_admission(ledger, snapshot_id)
    return envelope(
        tool="resolve_metric_and_dimensions",
        status="ok",
        snapshot_id=snapshot_id,
        metric_id=metric_id,
        metric_version=metric_version,
        summary=summary,
    )


def _refresh_admission(ledger: Ledger, snapshot_id: str) -> None:
    """Write the admission level once snapshot, maturity, and metric all exist.

    Blocked maturity is level C, degraded maturity is level B. A fully mature
    snapshot is level A only when the mature booked sample meets the SKILL bad
    sample gate (mature rows >= 50 and at least 10 positives / 10 negatives).
    Suspect-leakage or observation-outcome columns that were already rejected
    do NOT downgrade the level.
    """

    meta = ledger.get_snapshot(snapshot_id)
    maturity = ledger.get_maturity(snapshot_id)
    if not meta or not maturity:
        return
    metric_id = meta.get("bound_metric_id")
    version = meta.get("bound_metric_version")
    if not isinstance(metric_id, str) or not isinstance(version, str):
        return
    binding = ledger.get_metric(snapshot_id, metric_id, version)
    if binding is None:
        return
    maturity_status = maturity.get("status")
    if maturity_status == "blocked":
        level, status = "C", "blocked"
    elif maturity_status == "degraded":
        level, status = "B", "degraded"
    elif maturity_status == "ok":
        if _mature_bad_sample_ok(ledger, snapshot_id, maturity, binding):
            level, status = "A", "ok"
        else:
            level, status = "B", "degraded"
    else:
        return
    ledger.save_admission(snapshot_id, level, status)


def _mature_bad_sample_ok(
    ledger: Ledger, snapshot_id: str, maturity: Mapping[str, Any], binding: Mapping[str, Any]
) -> bool:
    try:
        frame = ledger.load_frame(snapshot_id)
        mask = ledger.load_mask(snapshot_id).to_numpy(dtype=bool)
    except (KeyError, OSError, ValueError):
        return False
    if len(mask) != len(frame):
        return False
    mature = frame.iloc[np.flatnonzero(mask)]
    if len(mature) < _MIN_MATURE_ROWS:
        return False
    flag = None
    columns = binding.get("columns")
    if isinstance(columns, dict):
        flag = columns.get("flag_column")
    if not flag:
        flag = binding.get("flag_column")
    if not flag:
        flag = maturity.get("bad_flag_column")
    if not flag or flag not in mature.columns:
        return False
    values = pd.to_numeric(mature[flag], errors="coerce").dropna()
    positives = int((values > 0).sum())
    negatives = int((values == 0).sum())
    return positives >= _MIN_CLASS_ROWS and negatives >= _MIN_CLASS_ROWS


def _require_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise ValueError("frame 必须是数据表")
    if not all(isinstance(column, str) for column in frame.columns):
        raise ValueError("列名必须是字符串")
    if frame.columns.duplicated().any():
        raise ValueError("列名不能重复")
    return frame.copy()


def _require_ledger(ledger: Ledger) -> None:
    if not isinstance(ledger, Ledger):
        raise ValueError("ledger 必须是 Ledger")


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or value == "":
        raise ValueError(f"{label} 必须是字符串")
    return value


def _optional_text(value: Any, label: str) -> Optional[str]:
    if value is None:
        return None
    return _text(value, label)


def _key_names(key_columns: Sequence[str]) -> list[str]:
    if isinstance(key_columns, (str, bytes)) or not isinstance(key_columns, Sequence):
        raise ValueError("key_columns 必须是列名序列")
    names = list(key_columns)
    if not names:
        return []
    if not all(isinstance(column, str) and column for column in names):
        raise ValueError("key_columns 必须是列名序列")
    if len(names) != len(set(names)):
        raise ValueError("key_columns 不能包含重复列名")
    return names


def _explicit_roles(column_roles: Optional[Mapping[str, str]]) -> Dict[str, str]:
    if column_roles is None:
        return {}
    if not isinstance(column_roles, Mapping):
        raise ValueError("column_roles 必须是字典")
    explicit: Dict[str, str] = {}
    for column, role in column_roles.items():
        if not isinstance(column, str) or column == "":
            raise ValueError("column_roles 的列名必须是字符串")
        if role not in _EXPLICIT_ROLES:
            raise ValueError("column_roles 只接受 decision_time、observation_outcome 或 suspect_leakage")
        explicit[column] = role
    return explicit


def _assign_roles(
    columns: Sequence[str],
    *,
    keys: Sequence[str],
    decision_time_column: str,
    observation_time_column: Optional[str],
    explicit_roles: Mapping[str, str],
) -> Dict[str, str]:
    key_set = set(keys)
    roles: Dict[str, str] = {}
    for column in columns:
        name = column.lower()
        if column == decision_time_column:
            roles[column] = "decision_time"
        elif observation_time_column and column == observation_time_column:
            roles[column] = "observation_time"
        elif column in key_set:
            roles[column] = "id"
        elif column in explicit_roles:
            roles[column] = explicit_roles[column]
        elif any(token in name for token in _SUSPECT_LEAKAGE_TOKENS):
            roles[column] = "suspect_leakage"
        elif any(token in name for token in _OBSERVATION_OUTCOME_TOKENS):
            roles[column] = "observation_outcome"
        else:
            roles[column] = "unclassified"
    return roles


def _role_counts(roles: Mapping[str, str]) -> Dict[str, int]:
    counts = {role: 0 for role in _ROLE_KEYS}
    for role in roles.values():
        counts[role] = counts.get(role, 0) + 1
    return counts


def _key_rates(frame: pd.DataFrame, key_columns: Sequence[str]) -> tuple[float, float]:
    keys = frame.loc[:, list(key_columns)]
    rows = len(frame)
    null_rate = float(keys.isna().any(axis=1).mean())
    duplicate_rate = float((rows - len(keys.drop_duplicates())) / rows)
    return null_rate, duplicate_rate


def _quality_columns(frame: pd.DataFrame) -> tuple[list[str], list[str]]:
    high_missing = []
    constant = []
    for column in frame.columns:
        series = frame[column]
        if float(series.isna().mean()) > 0.95:
            high_missing.append(str(column))
        observed = series.dropna()
        if observed.empty:
            continue
        top = int(observed.value_counts().iloc[0])
        if top / len(observed) > 0.99:
            constant.append(str(column))
    return high_missing, constant


def _day_series(series: pd.Series) -> pd.Series:
    parsed = pd.to_datetime(series, errors="coerce", format="mixed")
    if isinstance(parsed.dtype, pd.DatetimeTZDtype):
        parsed = parsed.dt.tz_convert(None)
    return parsed.dt.normalize()


def _parse_day(value: Any) -> Optional[pd.Timestamp]:
    if value is None:
        return None
    try:
        parsed = pd.to_datetime(value, errors="coerce", format="mixed")
    except (TypeError, ValueError):
        return None
    if parsed is None or pd.isna(parsed):
        return None
    stamp = pd.Timestamp(parsed)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert(None)
    return stamp.normalize()


def _iso(value: Any) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d")


def _window_info(performance_window: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(performance_window, Mapping):
        raise ValueError("performance_window 必须是包含 kind 的字典")
    kind = performance_window.get("kind")
    if kind == "mob":
        months = _positive_int(performance_window.get("mob"), "mob 必须是不小于 1 的整数")
        return {"kind": "mob", "mob": months, "window_months": months, "days": None}
    if kind == "fpd" or (kind is None and "days" in performance_window):
        days = _positive_int(performance_window.get("days"), "fpd 的 days 必须是不小于 1 的整数")
        return {"kind": "fpd", "days": days, "window_months": None, "mob": None}
    raise ValueError("performance_window 的 kind 只接受 mob 或 fpd")


def _positive_int(value: Any, message: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(message)
    return value


def _t0_cohort_mismatch(labels: Sequence[Optional[str]], t0: pd.Series) -> int:
    """Count rows whose decision month differs from their registered cohort month.

    Unparseable cohorts / decision times are ignored so existing data is not
    accidentally penalised.
    """

    mismatch = 0
    for index, label in enumerate(labels):
        if not _is_month_label(label):
            continue
        stamp = t0.iloc[index]
        if pd.isna(stamp):
            continue
        decision_month = f"{int(stamp.year):04d}-{int(stamp.month):02d}"
        if decision_month != label:
            mismatch += 1
    return int(mismatch)


def _cohort_maturity(
    labels: Sequence[Optional[str]], t0: pd.Series, window: Mapping[str, Any], cutoff: pd.Timestamp
) -> tuple[set, list[Dict[str, Any]]]:
    grouped: Dict[Optional[str], Dict[str, Any]] = {}
    for label in labels:
        bucket = grouped.setdefault(label, {"n": 0, "parsed": _is_month_label(label)})
        bucket["n"] += 1
    # FPD: natural-day deadline per loan, then the most conservative (latest)
    # T0 in each cohort decides the whole cohort.
    day_ok = None
    label_day_ok: Dict[Optional[str], bool] = {}
    if window["kind"] == "fpd":
        day_ok = t0.notna() & ((t0 + pd.Timedelta(days=int(window["days"]))) <= cutoff)
        for index, label in enumerate(labels):
            ok = bool(day_ok.iloc[index])
            label_day_ok[label] = label_day_ok.get(label, True) and ok
    mature_labels: set = set()
    cohorts: list[Dict[str, Any]] = []
    for label, bucket in grouped.items():
        if window["kind"] == "fpd":
            mature = bool(label_day_ok.get(label, False))
        else:
            mature = bool(bucket["parsed"] and _deadline(str(label), int(window["window_months"])) <= cutoff)
        if mature:
            mature_labels.add(label)
        cohorts.append({"cohort": label, "n": int(bucket["n"]), "mature": mature})
    cohorts.sort(key=lambda item: (item["cohort"] is None, "" if item["cohort"] is None else str(item["cohort"])))
    return mature_labels, cohorts


def _missing_cell(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value.strip() == ""
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _month_end(value: Any) -> Optional[pd.Timestamp]:
    if _missing_cell(value):
        return None
    if isinstance(value, pd.Period):
        return _end_of_month(pd.Timestamp(value.to_timestamp(how="start")))
    if isinstance(value, str):
        text = value.strip()
        if len(text) == 6 and text.isdigit():
            return _end_from_parts(int(text[:4]), int(text[4:]))
        if len(text) == 8 and text.isdigit():
            parsed = pd.to_datetime(text, format="%Y%m%d", errors="coerce")
            return None if pd.isna(parsed) else _end_of_month(pd.Timestamp(parsed))
        return _end_of_month(_parse_day(text)) if _parse_day(text) is not None else None
    if isinstance(value, Integral) and not isinstance(value, bool):
        text = str(int(value))
        if len(text) == 6:
            return _end_from_parts(int(text[:4]), int(text[4:]))
        if len(text) == 8:
            parsed = pd.to_datetime(text, format="%Y%m%d", errors="coerce")
            return None if pd.isna(parsed) else _end_of_month(pd.Timestamp(parsed))
        return None
    if isinstance(value, float) and value.is_integer():
        return _month_end(int(value))
    parsed = _parse_day(value)
    return None if parsed is None else _end_of_month(parsed)


def _end_from_parts(year: int, month: int) -> Optional[pd.Timestamp]:
    if not (1900 <= year <= 2199 and 1 <= month <= 12):
        return None
    return _end_of_month(pd.Timestamp(year=year, month=month, day=1))


def _end_of_month(stamp: Optional[pd.Timestamp]) -> Optional[pd.Timestamp]:
    if stamp is None or pd.isna(stamp):
        return None
    start = pd.Timestamp(year=int(stamp.year), month=int(stamp.month), day=1)
    return (start + pd.offsets.MonthEnd(0)).normalize()


def _cohort_label(value: Any) -> Optional[str]:
    month_end = _month_end(value)
    if month_end is None:
        return None if _missing_cell(value) else str(value).strip()
    return month_end.strftime("%Y-%m")


def _is_month_label(label: Optional[str]) -> bool:
    if not isinstance(label, str) or len(label) != 7 or label[4] != "-":
        return False
    year, month = label[:4], label[5:]
    return year.isdigit() and month.isdigit() and 1 <= int(month) <= 12


def _deadline(month_label: str, window_months: int) -> pd.Timestamp:
    month_end = _end_from_parts(int(month_label[:4]), int(month_label[5:]))
    if month_end is None:
        return pd.Timestamp.max.normalize()
    return (month_end + pd.DateOffset(months=window_months)).normalize()


def _registry_parts(registry: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    if not isinstance(registry, Mapping):
        raise ValueError("registry 必须是字典")
    if "metrics" not in registry or "dimensions" not in registry:
        raise ValueError("registry 必须包含 metrics 和 dimensions")
    metrics = registry["metrics"]
    dimensions = registry["dimensions"]
    if not isinstance(metrics, Sequence) or isinstance(metrics, (str, bytes)):
        raise ValueError("metrics 必须是列表")
    if not isinstance(dimensions, Sequence) or isinstance(dimensions, (str, bytes)):
        raise ValueError("dimensions 必须是列表")
    metric_rows = []
    for item in metrics:
        if not isinstance(item, Mapping):
            raise ValueError("metrics 中的每一项必须是字典")
        metric_rows.append(item)
    dimension_rows = []
    for item in dimensions:
        if not isinstance(item, Mapping):
            raise ValueError("维度登记必须是字典")
        column = item.get("column")
        if not isinstance(column, str) or column == "":
            raise ValueError("维度 column 必须是字符串")
        dimension_rows.append(item)
    return metric_rows, dimension_rows


def _match_metric(
    metrics: Sequence[Mapping[str, Any]],
    metric_id: str,
    metric_version: str,
) -> Optional[Mapping[str, Any]]:
    for item in metrics:
        identity = item.get("metric_id")
        version = item.get("version")
        if (
            isinstance(identity, str)
            and isinstance(version, str)
            and identity == metric_id
            and version == metric_version
        ):
            return item
    return None


def _snapshot_columns(ledger: Ledger, snapshot_id: str, meta: Mapping[str, Any]) -> Optional[set[str]]:
    columns = meta.get("columns")
    if isinstance(columns, list) and columns:
        return {str(column) for column in columns}
    try:
        frame = ledger.load_frame(snapshot_id)
    except KeyError:
        return None
    return {str(column) for column in frame.columns}


def _required_columns(metric: Mapping[str, Any], dimensions: Sequence[Mapping[str, Any]]) -> list[str]:
    required = []
    for field in ("weight_column", "flag_column"):
        column = metric.get(field)
        if column is None or column == "":
            continue
        if not isinstance(column, str):
            raise ValueError(f"{field} 必须是字符串")
        if column not in required:
            required.append(column)
    for dimension in dimensions:
        column = str(dimension["column"])
        if column not in required:
            required.append(column)
    return required


def _split_dimensions(dimensions: Sequence[Mapping[str, Any]]) -> tuple[list[Dict[str, Any]], list[str]]:
    accepted = []
    rejected = []
    for dimension in dimensions:
        column = str(dimension["column"])
        if dimension.get("point_in_time") != "decision":
            if column not in rejected:
                rejected.append(column)
            continue
        kept: Dict[str, Any] = {"column": column, "point_in_time": "decision"}
        for field in _DIMENSION_FIELDS:
            if field in dimension:
                kept[field] = _plain(dimension[field])
        accepted.append(kept)
    return accepted, rejected


def _metric_columns(metric: Mapping[str, Any]) -> Dict[str, Any]:
    """Copy declared column roles. Absent or blank roles are left out."""

    columns: Dict[str, Any] = {}
    for key in _METRIC_COLUMN_KEYS:
        if key not in metric:
            continue
        value = metric[key]
        if value is None or value == "":
            continue
        columns[key] = _plain(value)
    return columns


def _bound_columns(metric: Mapping[str, Any], dimensions: Sequence[Mapping[str, Any]]) -> list[str]:
    bound = []
    for field in ("weight_column", "flag_column"):
        column = metric.get(field)
        if isinstance(column, str) and column and column not in bound:
            bound.append(column)
    for dimension in dimensions:
        column = str(dimension["column"])
        if column not in bound:
            bound.append(column)
    return bound


def _plain(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, bool):
        return value
    if isinstance(value, Integral):
        return int(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return float(value)
    if isinstance(value, pd.Timestamp):
        return _iso(value)
    return str(value)


def _blocked(
    tool: str,
    block_code: str,
    message: str,
    *,
    snapshot_id: Optional[str] = None,
    metric_id: Optional[str] = None,
    metric_version: Optional[str] = None,
) -> Dict[str, Any]:
    return envelope(
        tool=tool,
        status="blocked",
        block_code=block_code,
        snapshot_id=snapshot_id,
        metric_id=metric_id,
        metric_version=metric_version,
        summary={"message": message},
    )
