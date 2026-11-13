"""Portfolio monitoring and anomaly location.

Numbers come from the existing analytics functions. This module only binds a
signed metric, applies the admission gate, and keeps the envelopes small.
"""

from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import pandas as pd

from credit_agent.analytics.columns import as_flag
from credit_agent.analytics.decompose import shift_share
from credit_agent.analytics.drilldown import drilldown
from credit_agent.analytics.migration import roll_rates
from credit_agent.analytics.stats import enough_sample
from credit_agent.analytics.trend import trend
from credit_agent.analytics.vintage import vintage_curves
from credit_agent.tools import Ledger, envelope, new_id

TOOL_MONITOR = "run_portfolio_monitoring"
TOOL_DECOMPOSE = "decompose_and_locate_anomaly"

_NEXT_BUCKET = {"M0": "M1", "M1": "M2", "M2": "M3"}
_OBS_POINT = {
    "observe",
    "observation",
    "observed",
    "obs",
    "obs_time",
    "observation_time",
    "observe_time",
    "snapshot",
    "观察",
    "观察时点",
}
_CONCLUSIONS = (
    "mix_shift",
    "within_group_deterioration",
    "slice_contribution",
    "policy_version_gap",
    "unexplained_residual",
)
# 维度登记表可逐维携带的下钻口径字段。
_SPEC_FIELDS = ("max_cardinality", "min_cell_n", "min_expected_bad", "bins", "cut_points")
# 逐维参数优先级：MCP 显式参数 > 登记表 spec > 默认值。
_DEFAULT_MIN_N = 50
_DEFAULT_MIN_EVENTS = 5
_DEFAULT_FLOOR = 0.20
_DEFAULT_ALPHA = 0.05
_DEFAULT_MIN_BAD_INCREASE = 5
_CLOSURE_TOLERANCE = 1e-9


def run_portfolio_monitoring(
    snapshot_id: str,
    *,
    ledger: Ledger,
    metric_id: str,
    metric_version: str,
    base_periods: Optional[Sequence[str]] = None,
    comp_periods: Optional[Sequence[str]] = None,
    dimensions: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Trend, and vintage / roll rate when the signed columns are present."""

    if not isinstance(base_periods, (list, tuple)) or not base_periods or not isinstance(comp_periods, (list, tuple)) or not comp_periods:
        return _blocked(
            TOOL_MONITOR, "PERIOD_MISMATCH", snapshot_id,
            "base_periods 与 comp_periods 必须是非空列表", metric_id, metric_version,
        )
    blocked = _admission_block(TOOL_MONITOR, snapshot_id, ledger, metric_id, metric_version)
    if blocked:
        return blocked
    binding = ledger.get_metric(snapshot_id, metric_id, metric_version)
    if not binding:
        return _blocked(TOOL_MONITOR, "METRIC_UNSIGNED", snapshot_id, "口径版本未签收", metric_id, metric_version)

    flag = _role(binding, "flag_column")
    period = _role(binding, "period_column")
    weight = _weight(ledger, snapshot_id, binding)
    if not flag or not period:
        return _blocked(
            TOOL_MONITOR,
            "SNAPSHOT_INVALID",
            snapshot_id,
            "指标绑定缺少 flag_column 或 period_column",
            metric_id,
            metric_version,
        )
    frame = _load_frame(ledger, snapshot_id)
    missing = [name for name in (flag, period, weight) if name and name not in frame.columns]
    if missing:
        return _blocked(
            TOOL_MONITOR,
            "SNAPSHOT_INVALID",
            snapshot_id,
            "快照缺少已绑定列：" + "、".join(missing),
            metric_id,
            metric_version,
        )

    dim_specs = _dimension_specs(binding)
    registered = {
        str(spec.get("column"))
        for spec in dim_specs
        if str(spec.get("point_in_time") or "").strip().lower() == "decision"
    }
    requested_dims: Optional[List[str]] = None
    if dimensions is not None:
        requested_dims = [str(item) for item in dimensions]
        unregistered = [item for item in requested_dims if item not in registered]
        if unregistered:
            return _blocked(
                TOOL_MONITOR, "DIMENSION_UNREGISTERED", snapshot_id,
                "维度未在登记表内：" + "、".join(unregistered), metric_id, metric_version,
            )

    try:
        trend_result = trend(
            frame,
            period=period,
            flag=flag,
            weight=weight,
            base_periods=list(base_periods),
            comp_periods=list(comp_periods),
        )
    except ValueError as exc:
        if "PERIOD_MISMATCH" in str(exc):
            return _blocked(TOOL_MONITOR, "PERIOD_MISMATCH", snapshot_id, str(exc), metric_id, metric_version)
        raise
    notes: List[str] = []
    vintage_result, vintage_note, vintage_cmp = _run_vintage(frame, binding, flag)
    if vintage_note:
        notes.append(vintage_note)
    migration_result, migration_note, migration_cmp = _run_migration(frame, binding, period, weight)
    if migration_note:
        notes.append(migration_note)
    if trend_result.get("seasonality_note"):
        notes.append(str(trend_result["seasonality_note"]))

    # 尝试了但结果不可比才降级；仅缺少绑定列时保留 ok 但收窄可下结论。
    comparable_flags = [value for value in (vintage_cmp, migration_cmp) if value is not None]
    incomparable = any(value is False for value in comparable_flags)
    limitations = bool(vintage_note or migration_note or incomparable)
    monitor_status = "degraded" if incomparable else "ok"
    monitor_allowed = _monitor_conclusions(limitations) if limitations else []

    highlights = _roll_highlights(migration_result)
    comparable = None if vintage_result is None else vintage_result.get("comparable_mob")
    base_l = [str(p) for p in trend_result.get("base_periods") or []]
    comp_l = [str(p) for p in trend_result.get("comp_periods") or []]
    slim = {
        "base_rate": trend_result.get("baseline_bad_rate"),
        "comp_rate": trend_result.get("recent_window_bad_rate"),
        "delta": trend_result.get("delta"),
        "base_periods": base_l,
        "comp_periods": comp_l,
        "alarm_level": trend_result.get("alarm_level"),
        "vintage_comparable_mob": comparable,
        "roll_highlights": highlights,
        "notes": notes,
    }
    full = {
        "metric": {"metric_id": metric_id, "metric_version": metric_version},
        "metric_id": metric_id,
        "metric_version": metric_version,
        "periods": {"base_periods": base_l, "comp_periods": comp_l},
        "base_rate": slim["base_rate"],
        "comp_rate": slim["comp_rate"],
        "delta": slim["delta"],
        "base_periods": base_l,
        "comp_periods": comp_l,
        "alarm_level": slim["alarm_level"],
        "alarm": bool(trend_result.get("alarm")),
        "relative_change": trend_result.get("relative_change"),
        "out_of_control_periods": list(trend_result.get("out_of_control_periods") or []),
        "vintage_comparable_mob": comparable,
        "vintage": _vintage_brief(vintage_result),
        "roll_highlights": highlights,
        "migration": _migration_brief(migration_result, highlights),
        "notes": notes,
        "dimension_specs": _slim_specs(dim_specs, requested_dims),
        "dimensions": requested_dims,
        "monitor_status": monitor_status,
    }
    run_id = new_id("run")
    artifact = ledger.save_run(run_id, TOOL_MONITOR, snapshot_id, _plain(full))
    return envelope(
        tool=TOOL_MONITOR,
        status=monitor_status,
        summary=_plain(slim),
        snapshot_id=snapshot_id,
        run_id=run_id,
        metric_id=metric_id,
        metric_version=metric_version,
        artifacts=[artifact],
        allowed_conclusions=monitor_allowed,
    )


def decompose_and_locate_anomaly(
    monitoring_run_id: str,
    *,
    ledger: Ledger,
    min_n: Optional[int] = None,
    min_events: Optional[int] = None,
    contribution_floor: Optional[float] = None,
    alpha: Optional[float] = None,
    min_bad_increase: Optional[int] = None,
) -> Dict[str, Any]:
    """Shift-share each decision-time dimension and keep slices that pass all four gates.

    Every gate parameter resolves as: explicit MCP argument > that dimension's
    registry spec (``min_cell_n`` / ``min_expected_bad`` / ``alpha``) > module
    default. The other registry spec fields (``max_cardinality`` / ``bins`` /
    ``cut_points``) are recorded per dimension for traceability.
    """

    run = ledger.get_run(monitoring_run_id)
    if not run:
        return _blocked(TOOL_DECOMPOSE, "BASELINE_UNAVAILABLE", "-", f"找不到监控运行 {monitoring_run_id}")
    snapshot_id = run.get("snapshot_id")
    if not snapshot_id:
        return _blocked(TOOL_DECOMPOSE, "BASELINE_UNAVAILABLE", "-", f"监控运行 {monitoring_run_id} 没有 snapshot_id")
    blocked = _admission_block(TOOL_DECOMPOSE, str(snapshot_id), ledger)
    if blocked:
        return blocked

    saved = run.get("summary") or {}
    metric = _metric_from_run(saved)
    periods = _periods_from_run(saved)
    if metric is None or periods is None:
        return _blocked(TOOL_DECOMPOSE, "BASELINE_UNAVAILABLE", str(snapshot_id), f"监控运行 {monitoring_run_id} 缺少指标或期次")
    metric_id, metric_version = metric
    base_periods, comp_periods = periods
    binding = ledger.get_metric(str(snapshot_id), metric_id, metric_version)
    if not binding:
        return _blocked(TOOL_DECOMPOSE, "METRIC_UNSIGNED", str(snapshot_id), "口径版本未签收", metric_id, metric_version)

    flag = _role(binding, "flag_column")
    period = _role(binding, "period_column")
    weight = _weight(ledger, str(snapshot_id), binding)
    if not flag or not period:
        return _blocked(
            TOOL_DECOMPOSE,
            "SNAPSHOT_INVALID",
            str(snapshot_id),
            "指标绑定缺少 flag_column 或 period_column",
            metric_id,
            metric_version,
        )
    frame = _load_frame(ledger, str(snapshot_id))
    if flag not in frame.columns or period not in frame.columns:
        return _blocked(
            TOOL_DECOMPOSE,
            "SNAPSHOT_INVALID",
            str(snapshot_id),
            "快照缺少已绑定的标志列或期次列",
            metric_id,
            metric_version,
        )
    if weight and weight not in frame.columns:
        weight = None

    dim_specs, violation, missing_dim = _decision_dimension_specs(binding, period, set(map(str, frame.columns)))
    if violation:
        return _blocked(
            TOOL_DECOMPOSE,
            "TIME_POINT_VIOLATION",
            str(snapshot_id),
            f"观察时点字段不能作为恶化来源：{violation}",
            metric_id,
            metric_version,
        )
    if missing_dim:
        return _blocked(
            TOOL_DECOMPOSE,
            "SNAPSHOT_INVALID",
            str(snapshot_id),
            f"快照缺少已登记维度：{missing_dim}",
            metric_id,
            metric_version,
        )

    base_df, comp_df = _split_periods(frame, period, base_periods, comp_periods)
    if base_df.empty or comp_df.empty:
        return _blocked(
            TOOL_DECOMPOSE,
            "SNAPSHOT_INVALID",
            str(snapshot_id),
            "基期或对比期在快照中没有样本",
            metric_id,
            metric_version,
        )

    global_params = {
        "min_n": _DEFAULT_MIN_N if min_n is None else min_n,
        "min_events": _DEFAULT_MIN_EVENTS if min_events is None else min_events,
        "contribution_floor": _DEFAULT_FLOOR if contribution_floor is None else contribution_floor,
        "alpha": _DEFAULT_ALPHA if alpha is None else alpha,
        "min_bad_increase": _DEFAULT_MIN_BAD_INCREASE if min_bad_increase is None else min_bad_increase,
    }
    portfolio_delta = saved.get("delta")
    per_dimension: List[Dict[str, Any]] = []
    located: List[Dict[str, Any]] = []
    observe: List[str] = []
    any_sig_large = False
    for spec in dim_specs:
        col = str(spec["column"])
        params = _effective_params(spec, min_n, min_events, contribution_floor, alpha, min_bad_increase)
        shared = shift_share(base_df, comp_df, segment=col, flag=flag, weight=weight)
        drilled = drilldown(base_df, comp_df, dims=[col], flag=flag, min_sample=params["min_n"], alpha=params["alpha"])
        simpson = _simpson(drilled, col)
        per_dimension.append(
            {
                "dimension": col,
                "mix_effect": shared.get("mix_effect"),
                "rate_effect": shared.get("rate_effect"),
                "interaction": shared.get("interaction"),
                "closure_error": shared.get("closure_error"),
                "dominant": shared.get("dominant"),
                "simpson": simpson,
                "dimension_spec": {key: spec[key] for key in _SPEC_FIELDS if key in spec},
                "effective_params": params,
            }
        )
        total_change = shared.get("total_change")
        direction_delta = portfolio_delta if portfolio_delta not in (None, "") else total_change
        counts = _group_counts(base_df, col, flag)
        comp_counts = _group_counts(comp_df, col, flag)
        rows = list((drilled.get("detail") or {}).get(col) or [])
        contributions = [row.get("contribution") for row in rows if row.get("contribution") is not None]
        max_contribution = max(contributions) if contributions else None
        for row in rows:
            value = str(row.get("value"))
            n0, k0 = _count_at(counts, value)
            n1, k1 = _count_at(comp_counts, value)
            rate0 = (k0 / n0) if n0 else None
            rate1 = (k1 / n1) if n1 else None
            base_ok = _sample_ok(n0, rate0, params["min_n"], params["min_events"])
            comp_ok = _sample_ok(n1, rate1, params["min_n"], params["min_events"])
            if not (base_ok and comp_ok):
                observe.append(f"{col}={value}")
            significant = bool(row.get("significant"))
            if base_ok and comp_ok and significant:
                any_sig_large = True
            if simpson or not (base_ok and comp_ok):
                continue
            delta = row.get("delta")
            contribution = row.get("contribution")
            bad_increase = k1 - k0
            is_max = (
                max_contribution is not None
                and contribution is not None
                and abs(float(contribution) - float(max_contribution)) <= 1e-9
            )
            if not _same_direction(delta, direction_delta):
                continue
            if not significant:
                continue
            if not _contribution_ok(
                contribution, total_change, is_max, bad_increase,
                params["contribution_floor"], params["min_bad_increase"],
            ):
                continue
            located.append(
                {
                    "dimension": col,
                    "value": value,
                    "n_base": int(n0),
                    "n_comp": int(n1),
                    "bad_base": int(round(k0)),
                    "bad_comp": int(round(k1)),
                    "delta": delta,
                    "contribution": contribution,
                    "p_value": row.get("p_value"),
                    "bad_increase": int(round(bad_increase)),
                }
            )

    located.sort(key=lambda item: -abs(float(item["contribution"] or 0)))
    stop_rule = _stop_rule(per_dimension, bool(located), any_sig_large)
    policy_column = _role(binding, "policy_version_column")
    allowed = _conclusions(per_dimension, located, policy_column)
    max_closure = max((abs(float(item["closure_error"] or 0)) for item in per_dimension), default=0.0)
    decompose_status = "degraded" if max_closure >= _CLOSURE_TOLERANCE else "ok"
    summary = {
        "per_dimension": per_dimension,
        "located": located,
        "observe_only": {"count": len(observe), "names": observe},
        "stop_rule": stop_rule,
        "allowed_conclusions": allowed,
        "closure_residual": max_closure,
    }
    if decompose_status == "degraded":
        summary["status_note"] = f"shift-share 闭合残差 {max_closure:.3g} >= 1e-9，结论降级"
    full = {
        **summary,
        "monitoring_run_id": monitoring_run_id,
        "metric": {"metric_id": metric_id, "metric_version": metric_version},
        "periods": {"base_periods": [str(p) for p in base_periods], "comp_periods": [str(p) for p in comp_periods]},
        **global_params,
    }
    run_id = new_id("run")
    artifact = ledger.save_run(run_id, TOOL_DECOMPOSE, str(snapshot_id), _plain(full))
    return envelope(
        tool=TOOL_DECOMPOSE,
        status=decompose_status,
        summary=_plain(summary),
        snapshot_id=str(snapshot_id),
        run_id=run_id,
        metric_id=metric_id,
        metric_version=metric_version,
        artifacts=[artifact],
        allowed_conclusions=allowed,
    )


def _effective_params(
    spec: Dict[str, Any],
    min_n: Optional[int],
    min_events: Optional[int],
    contribution_floor: Optional[float],
    alpha: Optional[float],
    min_bad_increase: Optional[int],
) -> Dict[str, Any]:
    """MCP explicit > registry spec > default, resolved per dimension."""

    return {
        "min_n": min_n if min_n is not None else spec.get("min_cell_n", _DEFAULT_MIN_N),
        "min_events": min_events if min_events is not None else spec.get("min_expected_bad", _DEFAULT_MIN_EVENTS),
        "contribution_floor": _DEFAULT_FLOOR if contribution_floor is None else contribution_floor,
        "alpha": alpha if alpha is not None else spec.get("alpha", _DEFAULT_ALPHA),
        "min_bad_increase": _DEFAULT_MIN_BAD_INCREASE if min_bad_increase is None else min_bad_increase,
    }


def _admission_block(
    tool: str,
    snapshot_id: str,
    ledger: Ledger,
    metric_id: Optional[str] = None,
    metric_version: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    admission = ledger.get_admission(snapshot_id)
    if admission is None or admission.get("status") == "blocked":
        maturity = ledger.get_maturity(snapshot_id)
        if maturity and maturity.get("mature_row_count") == 0:
            return _blocked(tool, "LABEL_IMMATURE", snapshot_id, "目标表现期下没有成熟批次", metric_id, metric_version)
        return _blocked(tool, "ADMISSION_REQUIRED", snapshot_id, "该快照没有可用的准入记录", metric_id, metric_version)
    return None


def _monitor_conclusions(limitations: bool) -> List[str]:
    if not limitations:
        return list(_CONCLUSIONS)
    return ["mix_shift", "within_group_deterioration", "unexplained_residual"]


def _blocked(
    tool: str,
    code: str,
    snapshot_id: str,
    message: str,
    metric_id: Optional[str] = None,
    metric_version: Optional[str] = None,
) -> Dict[str, Any]:
    return envelope(
        tool=tool,
        status="blocked",
        block_code=code,
        snapshot_id=snapshot_id,
        metric_id=metric_id,
        metric_version=metric_version,
        summary={"message": message},
    )


def _role(binding: Dict[str, Any], key: str) -> Optional[str]:
    """Resolve a column role from columns, then the binding root, then metric."""

    sources = []
    columns = binding.get("columns")
    if isinstance(columns, dict):
        sources.append(columns)
    sources.append(binding)
    metric = binding.get("metric")
    if isinstance(metric, dict):
        sources.append(metric)
    for source in sources:
        if key not in source:
            continue
        value = source[key]
        if value is None or value == "":
            continue
        return str(value)
    return None


def _weight(ledger: Ledger, snapshot_id: str, binding: Dict[str, Any]) -> Optional[str]:
    """Prefer the metric binding's weight; fall back to the snapshot meta weight."""

    name = _role(binding, "weight_column")
    if name:
        return name
    meta = ledger.get_snapshot(snapshot_id)
    if isinstance(meta, dict):
        candidate = meta.get("weight_column")
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _load_frame(ledger: Ledger, snapshot_id: str) -> pd.DataFrame:
    try:
        return ledger.load_frame(snapshot_id)
    except KeyError as exc:
        raise ValueError(f"快照 {snapshot_id} 没有数据帧") from exc


def _present(binding: Dict[str, Any], frame: pd.DataFrame, key: str) -> Optional[str]:
    name = _role(binding, key)
    if name and name in frame.columns:
        return name
    return None


def _run_vintage(
    frame: pd.DataFrame, binding: Dict[str, Any], flag: str
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[bool]]:
    needed = ("loan_id", "cohort", "mob")
    found = {key: _present(binding, frame, key) for key in needed}
    missing = [key for key, name in found.items() if not name]
    if missing:
        return None, "vintage 未计算，缺少列：" + "、".join(missing), None
    try:
        result = vintage_curves(frame, loan_id=found["loan_id"], cohort=found["cohort"], mob=found["mob"], bad=flag)
    except ValueError as exc:
        return None, "vintage 不可比：" + str(exc), False
    return result, None, bool(result.get("comparable_mob") is not None)


def _run_migration(
    frame: pd.DataFrame,
    binding: Dict[str, Any],
    period: str,
    weight: Optional[str],
) -> Tuple[Optional[Dict[str, Any]], Optional[str], Optional[bool]]:
    loan_id = _present(binding, frame, "loan_id")
    bucket = _present(binding, frame, "bucket")
    dpd = _present(binding, frame, "dpd")
    missing: List[str] = []
    if not loan_id:
        missing.append("loan_id")
    if not bucket and not dpd:
        missing.append("bucket 或 dpd")
    if missing:
        return None, "migration 未计算，缺少列：" + "、".join(missing), None
    try:
        result = roll_rates(frame, loan_id=loan_id, period=period, bucket=bucket, dpd=dpd, balance=weight)
    except ValueError as exc:
        return None, "migration 不可比：" + str(exc), False
    return result, None, bool(result.get("n_pairs"))


def _roll_highlights(migration: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not migration:
        return []
    rolls = migration.get("roll_rates")
    if not isinstance(rolls, list):
        rolls = next((migration.get(key) for key in ("roll", "rolls", "highlights") if isinstance(migration.get(key), list)), [])
    highlights = []
    for row in rolls:
        if not isinstance(row, dict):
            continue
        source = row.get("from", row.get("source", row.get("from_bucket")))
        target = row.get("to", row.get("target", row.get("to_bucket")))
        if source in _NEXT_BUCKET and target == _NEXT_BUCKET[source]:
            highlights.append({"from": source, "to": target, "rate": row.get("rate")})
    return highlights


def _vintage_brief(result: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if result is None:
        return None
    immature = result.get("immature_cohorts") or []
    return {
        "comparable_mob": result.get("comparable_mob"),
        "n_loans": result.get("n_loans"),
        "n_immature_cohorts": len(immature),
    }


def _migration_brief(result: Optional[Dict[str, Any]], highlights: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if result is None:
        return None
    return {"n_pairs": result.get("n_pairs"), "buckets": list(result.get("buckets") or []), "roll_highlights": highlights}


def _metric_from_run(summary: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    metric = summary.get("metric")
    if isinstance(metric, dict) and metric.get("metric_id") and metric.get("metric_version"):
        return str(metric["metric_id"]), str(metric["metric_version"])
    if summary.get("metric_id") and summary.get("metric_version"):
        return str(summary["metric_id"]), str(summary["metric_version"])
    return None


def _periods_from_run(summary: Dict[str, Any]) -> Optional[Tuple[List[Any], List[Any]]]:
    periods = summary.get("periods")
    if isinstance(periods, dict):
        base = periods.get("base_periods", periods.get("base"))
        comp = periods.get("comp_periods", periods.get("comp"))
        if isinstance(base, list) and isinstance(comp, list) and base and comp:
            return base, comp
    base = summary.get("base_periods")
    comp = summary.get("comp_periods")
    if isinstance(base, list) and isinstance(comp, list) and base and comp:
        return base, comp
    return None


def _decision_dimension_specs(
    binding: Dict[str, Any], period_column: str, columns: Iterable[str]
) -> Tuple[List[Dict[str, Any]], Optional[str], Optional[str]]:
    """Decision-time dimension specs, or the observation column / missing column that stops the scan."""

    known = set(columns)
    specs = _dimension_specs(binding)
    for spec in specs:
        column = spec.get("column")
        if not column:
            continue
        point = str(spec.get("point_in_time") or "").strip().lower()
        if point in _OBS_POINT or column == period_column:
            return [], column, None
    chosen: List[Dict[str, Any]] = []
    seen: set = set()
    for spec in specs:
        column = spec.get("column")
        if not column:
            continue
        point = str(spec.get("point_in_time") or "").strip().lower()
        if point != "decision":
            continue
        if column not in known:
            return [], None, column
        if column not in seen:
            seen.add(column)
            chosen.append(spec)
    return chosen, None, None


def _dimension_specs(binding: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = binding.get("dimensions")
    if raw is None:
        raw = binding.get("dimension_registry")

    def build(column: Any, meta: Any) -> Dict[str, Any]:
        spec: Dict[str, Any] = {"column": None if column is None else str(column)}
        if isinstance(meta, dict):
            spec["point_in_time"] = meta.get("point_in_time")
            for field in _SPEC_FIELDS:
                if field in meta:
                    spec[field] = meta[field]
            if "alpha" in meta:
                spec["alpha"] = meta["alpha"]
        else:
            spec["point_in_time"] = meta
        return spec

    specs: List[Dict[str, Any]] = []
    if isinstance(raw, dict):
        for key, meta in raw.items():
            if isinstance(meta, dict):
                specs.append(build(meta.get("column") or key, meta))
            else:
                specs.append(build(key, meta))
        return specs
    if not isinstance(raw, list):
        return []
    for item in raw:
        if isinstance(item, str):
            specs.append(build(item, None))
        elif isinstance(item, dict):
            column = item.get("column") or item.get("name") or item.get("field")
            specs.append(build(column, item))
    return specs


def _slim_specs(specs: List[Dict[str, Any]], requested: Optional[Sequence[str]]) -> List[Dict[str, Any]]:
    chosen = []
    wanted = set(requested) if requested is not None else None
    for spec in specs:
        column = str(spec.get("column") or "")
        if str(spec.get("point_in_time") or "").strip().lower() != "decision":
            continue
        if wanted is not None and column not in wanted:
            continue
        kept = {"column": column, "point_in_time": "decision"}
        for field in _SPEC_FIELDS:
            if field in spec:
                kept[field] = spec[field]
        chosen.append(kept)
    return chosen


def _split_periods(
    frame: pd.DataFrame, period: str, base_periods: Sequence[Any], comp_periods: Sequence[Any]
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    labels = frame[period].astype(str)
    base = frame.loc[labels.isin({str(p) for p in base_periods})].copy()
    comp = frame.loc[labels.isin({str(p) for p in comp_periods})].copy()
    return base, comp


def _group_counts(df: pd.DataFrame, dim: str, flag: str) -> pd.DataFrame:
    grouped = pd.DataFrame({"v": df[dim].astype(str), "k": as_flag(df[flag])}).dropna(subset=["k"])
    if grouped.empty:
        return pd.DataFrame(columns=["n", "k"])
    return grouped.groupby("v").agg(n=("k", "size"), k=("k", "sum"))


def _count_at(counts: pd.DataFrame, value: str) -> Tuple[float, float]:
    if counts.empty or value not in counts.index:
        return 0.0, 0.0
    return float(counts.loc[value, "n"]), float(counts.loc[value, "k"])


def _sample_ok(n: float, rate: Optional[float], min_n: int, min_events: int) -> bool:
    return bool(n >= min_n and enough_sample(n, rate, min_n=min_n, min_events=min_events))


def _same_direction(slice_delta: Any, portfolio_delta: Any) -> bool:
    if slice_delta is None or portfolio_delta is None:
        return False
    try:
        left, right = float(slice_delta), float(portfolio_delta)
    except (TypeError, ValueError):
        return False
    if left == 0 or right == 0 or math.isnan(left) or math.isnan(right):
        return False
    return (left > 0 and right > 0) or (left < 0 and right < 0)


def _contribution_ok(
    contribution: Any,
    total_change: Any,
    is_max: bool,
    bad_increase: float,
    floor: float,
    min_bad_increase: int,
) -> bool:
    if contribution is None or total_change is None:
        return False
    try:
        change = float(total_change)
        share = abs(float(contribution) / change) if change else 0.0
    except (TypeError, ValueError, ZeroDivisionError):
        return False
    if change == 0:
        return False
    if share >= floor:
        return True
    return bool(is_max and bad_increase >= min_bad_increase)


def _simpson(drilled: Dict[str, Any], dimension: str) -> bool:
    for item in drilled.get("simpson_checks") or []:
        if item.get("dimension") == dimension:
            return bool(item.get("risk"))
    return False


def _stop_rule(per_dimension: List[Dict[str, Any]], has_located: bool, any_sig_large: bool) -> str:
    if has_located:
        return "located"
    simpson = any(bool(item.get("simpson")) for item in per_dimension)
    rate_dominates = bool(per_dimension) and all(
        abs(float(item.get("rate_effect") or 0)) > abs(float(item.get("mix_effect") or 0)) for item in per_dimension
    )
    mix_dominates = any(
        abs(float(item.get("mix_effect") or 0)) > abs(float(item.get("rate_effect") or 0)) for item in per_dimension
    )
    if rate_dominates and not any_sig_large and not simpson:
        return "universal_deterioration"
    if mix_dominates or simpson:
        return "mix_dispersion"
    return "unlocated"


def _conclusions(
    per_dimension: List[Dict[str, Any]], located: List[Dict[str, Any]], policy_column: Optional[str]
) -> List[str]:
    structure = any(item.get("dominant") == "结构效应" or item.get("simpson") for item in per_dimension)
    within = any(item.get("dominant") == "风险效应" for item in per_dimension)
    chosen = {
        "mix_shift": structure,
        "within_group_deterioration": within,
        "slice_contribution": bool(located),
        "policy_version_gap": bool(policy_column) and any(item.get("dimension") == policy_column for item in located),
        "unexplained_residual": not located,
    }
    return [name for name in _CONCLUSIONS if chosen[name]]


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if hasattr(value, "item") and not isinstance(value, (str, bytes)):
        try:
            value = value.item()
        except (ValueError, AttributeError):
            return str(value)
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
        return value
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return value
