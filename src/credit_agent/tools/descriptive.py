"""Label-free population description and comparison.

These two tools exist because every other analysis tool here needs a target
label, and a question like "did the applicant mix change in October" does not
have one. Before this module the only way to answer it was a local script,
which produced numbers that no ledger run could vouch for — and a report that
cited them came back ``REPORT_UNTRACED`` with no way to fix it.

The gate is different, not weaker:

* there is no maturity check, because no label is read;
* in exchange, the analysis caliber (population, grain, denominator, missing
  semantics) must be declared *before* the numbers are computed, and every
  column whose blanks or zeros have not been explained stops the run with
  ``needs_human`` instead of being silently dropped;
* conclusions are limited to shape and composition. A bad rate cannot be
  obtained here: columns whose snapshot role is a label/outcome (or whose name
  says so) are refused outright as the described column, as a dimension, and as
  a denominator filter — because ``mean`` of a 0/1 flag *is* a bad rate, and a
  per-channel ``bad_flag`` breakdown is a bad-rate table with a different title.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, List, Mapping, Optional, Sequence

import pandas as pd

from credit_agent.analytics import distribution as dist
from credit_agent.tools import Ledger, envelope, new_id

TOOL_DESCRIBE = "describe_population"
TOOL_COMPARE = "compare_population_distribution"

_DENOMINATOR_RULES = ("all_records", "approved", "disbursed", "matured_only")
_MISSING_MEANINGS = ("unknown", "no_record", "not_applicable", "zero_equivalent")
_ZERO_MEANINGS = ("true_zero", "no_record", "not_applicable", "unknown")
# 标签里的总体措辞必须与分母规则一致：这是本次事故里最贵的一条。
_LABEL_RULES = (
    (("放款", "提款", "支用", "用信", "已放"), "disbursed"),
    (("审批通过", "核准", "通过率"), "approved"),
    (("成熟", "表现期完整"), "matured_only"),
    (("申请", "进件", "审批"), "all_records"),
)
_DESCRIBE_CONCLUSIONS = (
    "distribution_shape",
    "composition_change",
    "level_shift",
    "missing_pattern",
)
_FORBIDDEN_CONCLUSIONS = ("bad_rate_change", "risk_deterioration", "default_prediction")
_DEFAULT_MIN_CELL = 30
_MAX_DIMENSION_CELLS = 20
# 描述工具的唯一硬承诺是「拿不到坏账率」。角色是快照给的，列名是兜底：
# 只有角色声明漏了、名字又明显是表现结果时，才由下面这张表拦住。
_LABEL_ROLES = ("label", "target", "flag", "observation_outcome", "suspect_leakage")
_LABEL_TOKENS = (
    "bad", "default", "fpd", "dpd", "overdue", "delq", "writeoff", "label", "target",
    # 中文列名同样常见，英文标记挡不住「是否坏账」「逾期标记」这类字段。
    "坏账", "好坏", "违约", "逾期", "首逾", "核销", "表现期",
)
# 0/1 列只有被明确声明为决策时点特征，才允许描述：它的均值就是比率。
_DECLARED_ROLES = ("feature",)
_TEST_NAMES = ("chi2_bins", "chi2_categories", "ks_2samp", "mann_whitney", "welch_t")


def describe_population(
    snapshot_id: str,
    *,
    ledger: Ledger,
    spec: Mapping[str, Any],
    period: str,
    column: str,
    dimensions: Optional[Sequence[str]] = None,
    bins: int = 10,
    registry: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Describe one column of one period under a declared caliber.

    ``spec`` carries the caliber: population label, grain, time column,
    denominator rule, per-field missing/zero meaning, and observation points.
    When ``registry`` is supplied the spec must match a signed entry, otherwise
    it is recorded as unsigned — still versioned by ``spec_id`` / ``spec_version``
    and a fingerprint over the normalized content, but not a signed artifact.
    """

    meta = ledger.get_snapshot(snapshot_id)
    if meta is None:
        return _blocked(TOOL_DESCRIBE, "SNAPSHOT_INVALID", snapshot_id, "快照不存在")
    if not str(period or "").strip():
        return _blocked(TOOL_DESCRIBE, "PERIOD_MISMATCH", snapshot_id, "period 不能为空")
    try:
        bin_count = int(bins)
    except (TypeError, ValueError):
        return _blocked(TOOL_DESCRIBE, "UNSUPPORTED_SCOPE", snapshot_id, f"bins 必须是整数：收到 {bins!r}")
    if not 3 <= bin_count <= 50:
        return _blocked(TOOL_DESCRIBE, "UNSUPPORTED_SCOPE", snapshot_id, "bins 需在 3~50 之间")
    try:
        frame = ledger.load_frame(snapshot_id)
    except KeyError:
        return _blocked(TOOL_DESCRIBE, "SNAPSHOT_INVALID", snapshot_id, "快照没有数据帧")

    normalized, error = _normalize_spec(spec, meta, frame)
    if error:
        code, message = error
        return _blocked(TOOL_DESCRIBE, code, snapshot_id, message)
    signed = False
    if registry is not None:
        match = _registry_spec(registry, normalized["spec_id"], normalized["spec_version"])
        if match is None:
            return _blocked(
                TOOL_DESCRIBE,
                "METRIC_UNSIGNED",
                snapshot_id,
                "口径未签收或版本不一致："
                f"{normalized['spec_id']}/{normalized['spec_version']} 不在注册表中",
            )
        # 签收口径以注册表内容为准：否则可以在传入一个合法 spec_id 的同时
        # 偷换分母规则，而头部仍显示「已签收」。
        normalized, error = _normalize_spec(match, meta, frame)
        if error:
            code, message = error
            return _blocked(TOOL_DESCRIBE, code, snapshot_id, f"注册表口径无效：{message}")
        signed = True
    normalized["signed"] = signed
    normalized["fingerprint"] = _fingerprint(normalized)

    if column not in frame.columns:
        return _blocked(TOOL_DESCRIBE, "SNAPSHOT_INVALID", snapshot_id, f"列不在快照中：{column}")
    dims = [str(item) for item in (dimensions or [])]
    unknown = [item for item in dims if item not in frame.columns]
    if unknown:
        return _blocked(TOOL_DESCRIBE, "SNAPSHOT_INVALID", snapshot_id, "维度列不在快照中：" + "、".join(unknown))

    gate_columns = [column] + dims + [str((normalized.get("denominator_filter") or {}).get("column") or "")]
    gate_columns = [name for name in dict.fromkeys(gate_columns) if name]
    refused = _label_refusals(meta, gate_columns)
    if refused:
        return _blocked(
            TOOL_DESCRIBE,
            "UNSUPPORTED_SCOPE",
            snapshot_id,
            "; ".join(refused)
            + "。描述工具不读标签：表现结果列一旦作为描述对象或维度进入，"
            "mean 就会变成坏账率。请改用成熟度核查后的建模/监控工具。",
        )

    gate = _binary_gate(TOOL_DESCRIBE, snapshot_id, meta, frame, gate_columns, str(period))
    if gate is not None:
        return gate

    time_column = normalized["time_column"]
    if dist.is_binary_flag(frame[time_column]):
        # 元数据说它是决策时点，但取值只有 0/1：按它分期等于按结果分期。
        return _blocked(
            TOOL_DESCRIBE,
            "TIME_POINT_VIOLATION",
            snapshot_id,
            f"切期字段 {time_column} 只有 0/1 两个取值，不可能是决策时点："
            "按它分期会把两期分母变成好客户数与坏客户数",
        )
    periods = frame[time_column].astype(str)
    if period not in set(periods.unique()):
        return _blocked(
            TOOL_DESCRIBE,
            "PERIOD_MISMATCH",
            snapshot_id,
            f"期次 {period} 不在快照中；可选：{_brief_periods(periods)}",
        )
    in_period = periods == str(period)
    # 全表预检只拦住一眼可辨的；真正的判据是「这一期、这几个位置」是不是 0/1，
    # 否则别的期混进一个未知码（0/1/2）就能让闸门整体失效。
    gate = _binary_gate(
        TOOL_DESCRIBE, snapshot_id, meta, frame.loc[in_period], gate_columns, str(period)
    )
    if gate is not None:
        return gate

    denominator_mask, denominator_note = _denominator_mask(frame, normalized, in_period)
    if denominator_mask is None:
        return _blocked(TOOL_DESCRIBE, "UNSUPPORTED_SCOPE", snapshot_id, denominator_note or "分母无法解析")

    # 第三次、也是最关键的一次：计算实际发生在「这一期 ∩ 这个分母」的样本上。
    # 整期不是 0/1 不代表分母里不是——放款决策那一列在申请期里通常有 3/4/5 等
    # 拒绝码，切到 disbursed 之后正好只剩 1/0。闸门必须站在同一批行上看。
    gate = _binary_gate(
        TOOL_DESCRIBE, snapshot_id, meta, frame.loc[denominator_mask], gate_columns, str(period)
    )
    if gate is not None:
        return gate

    rows_in_period = int(in_period.sum())
    denominator = int(denominator_mask.sum())
    if denominator == 0:
        return _blocked(
            TOOL_DESCRIBE,
            "UNSUPPORTED_SCOPE",
            snapshot_id,
            f"期次 {period} 在声明的分母规则下没有记录",
        )

    undecided = _undecided_semantics(frame.loc[denominator_mask], gate_columns, normalized)
    if undecided:
        return envelope(
            tool=TOOL_DESCRIBE,
            status="needs_human",
            snapshot_id=snapshot_id,
            summary={
                "message": "有字段的缺失或零值含义未声明，先定口径再计算",
                "spec": normalized,
                "period": str(period),
                "undecided": undecided,
                "options": list(_MISSING_MEANINGS),
                "why": "缺失当 0 还是当未知，会直接改变分母和分位数；工具不替你猜。",
            },
            human_gate={"kind": "field_semantics", "columns": [item["column"] for item in undecided]},
        )

    subset = frame.loc[denominator_mask]
    # 口径不只是文案：声明「缺失等价于 0」之后，缺失必须真的进入 0 那一档。
    missing_as_zero = _missing_as_zero(normalized, column)
    if missing_as_zero and not _is_numeric_column(subset[column]):
        return _blocked(
            TOOL_DESCRIBE,
            "UNSUPPORTED_SCOPE",
            snapshot_id,
            f"字段 {column} 是分类字段，不能声明 missing=zero_equivalent："
            "分类列没有 0 这一档可并入，接受这个口径只会让口径变成一句文案。"
            "请改为 unknown / no_record / not_applicable",
        )
    try:
        profile = dist.describe_series(
            subset[column], column=column, bins=bin_count, missing_as_zero=missing_as_zero
        )
    except ValueError as exc:
        return _blocked(
            TOOL_DESCRIBE,
            "UNSUPPORTED_SCOPE",
            snapshot_id,
            f"字段 {column} 无法按该口径描述：{exc}",
        )
    summary: Dict[str, Any] = {
        "spec": normalized,
        "period": str(period),
        "grain": meta.get("grain"),
        "population": {
            "label": normalized["population_label"],
            "denominator_rule": normalized["denominator_rule"],
            "rows_in_period": rows_in_period,
            "rows_excluded": rows_in_period - denominator,
            "denominator": denominator,
            "weighted": False,
        },
        "column": profile,
        "described_column": _described_column(meta, column),
        "by_dimension": _dimension_breakdown(
            subset,
            dims,
            column,
            denominator,
            missing_as_zero=missing_as_zero,
            feature_declared=str((meta.get("column_roles") or {}).get(column) or "")
            in _DECLARED_ROLES,
        ),
        "observation_points": _observation_points(
            subset[column], normalized, denominator, missing_as_zero=missing_as_zero
        ),
        "allowed_conclusions": list(_DESCRIBE_CONCLUSIONS),
        "forbidden_conclusions": list(_FORBIDDEN_CONCLUSIONS),
        "limitations": _limitations(meta, normalized),
    }

    run_id = new_id("run")
    run_artifact = ledger.save_run(run_id, TOOL_DESCRIBE, snapshot_id, _plain(summary))
    table_artifact = ledger.save_json_artifact(
        run_id,
        "population_profile",
        {
            "spec_id": normalized["spec_id"],
            "spec_version": normalized["spec_version"],
            "fingerprint": normalized["fingerprint"],
            "period": str(period),
            "column": column,
            "bins": profile.get("bins") or [],
            "by_dimension": summary["by_dimension"],
        },
    )
    return envelope(
        tool=TOOL_DESCRIBE,
        status="ok",
        summary=_plain(summary),
        snapshot_id=snapshot_id,
        run_id=run_id,
        artifacts=[run_artifact, table_artifact],
        allowed_conclusions=list(_DESCRIBE_CONCLUSIONS),
    )


def compare_population_distribution(
    base_run_id: str,
    comp_run_id: str,
    *,
    ledger: Ledger,
    alpha: float = 0.05,
) -> Dict[str, Any]:
    """Compare two described periods of the same caliber.

    Both runs must come from ``describe_population`` with an identical spec
    fingerprint: comparing two different calibers produces a number that looks
    like a trend and is actually a definition change.
    """

    base_run = ledger.get_run(base_run_id)
    comp_run = ledger.get_run(comp_run_id)
    try:
        alpha_value = float(alpha)
    except (TypeError, ValueError):
        return _blocked(TOOL_COMPARE, "UNSUPPORTED_SCOPE", "-", f"alpha 必须是数字：收到 {alpha!r}")
    if not 0.0 < alpha_value < 1.0:
        return _blocked(TOOL_COMPARE, "UNSUPPORTED_SCOPE", "-", "alpha 需在 (0, 1) 之间")
    for run_id, run in ((base_run_id, base_run), (comp_run_id, comp_run)):
        if not run:
            return _blocked(TOOL_COMPARE, "BASELINE_UNAVAILABLE", "-", f"找不到描述运行 {run_id}")
        if run.get("tool") != TOOL_DESCRIBE:
            return _blocked(
                TOOL_COMPARE,
                "BASELINE_UNAVAILABLE",
                str(run.get("snapshot_id") or "-"),
                f"运行 {run_id} 不是 {TOOL_DESCRIBE} 产出，无法比较",
            )
    base_summary = base_run.get("summary") or {}
    comp_summary = comp_run.get("summary") or {}
    base_spec = base_summary.get("spec") or {}
    comp_spec = comp_summary.get("spec") or {}
    if not base_spec or not comp_spec:
        return _blocked(TOOL_COMPARE, "BASELINE_UNAVAILABLE", "-", "描述运行缺少口径记录")

    column = base_summary.get("column", {}).get("column")
    if column != comp_summary.get("column", {}).get("column"):
        return _blocked(
            TOOL_COMPARE,
            "SPEC_MISMATCH",
            str(base_run.get("snapshot_id") or "-"),
            f"两次描述的不是同一列：{column} vs {comp_summary.get('column', {}).get('column')}",
        )
    if base_spec.get("fingerprint") != comp_spec.get("fingerprint"):
        return _blocked(
            TOOL_COMPARE,
            "SPEC_MISMATCH",
            str(base_run.get("snapshot_id") or "-"),
            f"口径指纹不一致（{base_spec.get('spec_id')}/v{base_spec.get('spec_version')} 发生过修订）："
            "请用同一口径重跑两期，再比较",
        )
    base_period = str(base_summary.get("period"))
    comp_period = str(comp_summary.get("period"))
    if base_period == comp_period:
        return _blocked(
            TOOL_COMPARE, "PERIOD_MISMATCH", str(base_run.get("snapshot_id") or "-"),
            f"两期相同（{base_period}），没有可比较的差异",
        )

    snapshot_id = str(base_run.get("snapshot_id") or "")
    if str(comp_run.get("snapshot_id") or "") != snapshot_id:
        return _blocked(
            TOOL_COMPARE, "SPEC_MISMATCH", snapshot_id,
            "两次描述来自不同快照：期次差异与快照差异混在一起，无法归因",
        )
    try:
        frame = ledger.load_frame(snapshot_id)
    except KeyError:
        return _blocked(TOOL_COMPARE, "SNAPSHOT_INVALID", snapshot_id, "快照没有数据帧")

    meta = ledger.get_snapshot(snapshot_id) or {}
    normalized, error = _normalize_spec(base_spec, meta, frame)
    if error:
        code, message = error
        return _blocked(TOOL_COMPARE, code, snapshot_id, message)
    gate_columns = [str(column)] + [
        str((normalized.get("denominator_filter") or {}).get("column") or "")
    ]
    gate_columns = [name for name in dict.fromkeys(gate_columns) if name]
    refused = _label_refusals(meta, gate_columns)
    if refused:
        return _blocked(
            TOOL_COMPARE, "UNSUPPORTED_SCOPE", snapshot_id, "; ".join(refused) + "。描述工具不读标签。"
        )
    undeclared = _undeclared_binary(meta, frame, gate_columns)
    if undeclared:
        # 与描述工具的同一个信封：这是「先给这一列定性」，出路是声明角色，不是范围不支持。
        # 能走到这里说明快照 meta 在两期描述之后被改过，所以两期的 run 不能再拿来比。
        return envelope(
            tool=TOOL_COMPARE,
            status="needs_human",
            snapshot_id=snapshot_id,
            summary={
                "message": f"列 {undeclared[0]['column']} 是 0/1 字段且在快照里未声明为 feature："
                "它的均值就是比率，比较两期均值差等于比较两期坏账率。"
                "请在快照的 column_roles 里声明它，再重新描述两期。",
                "undecided": undeclared,
                "options": ["feature（决策时点已知的特征）", "observation_outcome（表现结果）"],
                "how": "inspect_and_bind_snapshot(..., column_roles={'列名': 'feature'})",
            },
            human_gate={
                "kind": "binary_column_role",
                "columns": [item["column"] for item in undeclared],
            },
        )
    if _missing_as_zero(normalized, str(column)) and not _is_numeric_column(frame[column]):
        return _blocked(
            TOOL_COMPARE,
            "UNSUPPORTED_SCOPE",
            snapshot_id,
            f"字段 {column} 是分类字段，口径里的 missing=zero_equivalent 无法执行",
        )
    # 校验用重新归一化的结果，身份字段沿用落账时的那份：
    # 指纹与签收状态是「哪一版口径」的凭证，不能在比较时被重算掉。
    if base_spec.get("fingerprint") and _fingerprint(normalized) != base_spec["fingerprint"]:
        # 快照元数据在两次调用之间变过（决策时点列被换、粒度被改）：
        # 数字是旧口径算的，指纹却对应新元数据，两者已经对不上。
        return _blocked(
            TOOL_COMPARE,
            "SPEC_MISMATCH",
            snapshot_id,
            "口径指纹与当前快照元数据不一致：快照在两次描述之间被改动过，请用当前快照重新描述两期",
        )
    normalized["fingerprint"] = base_spec.get("fingerprint")
    # 签收状态取两期的交集：一期签收、一期没签收时，结果不能整篇都说「已签收口径」。
    base_signed = bool(base_spec.get("signed"))
    comp_signed = bool(comp_spec.get("signed"))
    normalized["signed"] = base_signed and comp_signed
    time_column = normalized["time_column"]
    periods = frame[time_column].astype(str)
    base_mask, note = _denominator_mask(frame, normalized, periods == base_period)
    comp_mask, note2 = _denominator_mask(frame, normalized, periods == comp_period)
    if base_mask is None or comp_mask is None:
        return _blocked(TOOL_COMPARE, "UNSUPPORTED_SCOPE", snapshot_id, note or note2 or "分母无法解析")
    # 每一期各自的样本里再验一次 0/1：别的期混进一个未知码，不能让本期蒙混过关。
    # 与描述工具回同一个信封：这仍然是「先给这一列定性」，不是范围不支持。
    for label, mask, period_value in (
        ("基期", base_mask, base_period),
        ("对比期", comp_mask, comp_period),
    ):
        gate = _binary_gate(
            TOOL_COMPARE, snapshot_id, meta, frame.loc[mask], gate_columns, period_value
        )
        if gate:
            gate["summary"]["message"] = (
                f"{label}样本里的列 {gate['summary']['undecided'][0]['column']} "
                "是 0/1 字段且未声明为 feature：比较它的两期占比等于比较两期坏账率"
            )
            return gate

    # 分箱必须沿用描述运行发布过的那组边界，否则比较出来的占比
    # 在基期报告里根本找不到。
    published_edges = (base_summary.get("column") or {}).get("bin_edges")
    try:
        result = dist.compare_series(
            frame.loc[base_mask, column],
            frame.loc[comp_mask, column],
            column=column,
            alpha=alpha_value,
            edges=published_edges,
            missing_as_zero=_missing_as_zero(normalized, str(column)),
        )
    except ValueError as exc:
        # 计算层用 ValueError 表达「这一列做不到这件事」（口径要数值但这里是
        # 分类列等）。这是范围问题，不是故障：给一个说清原因的 blocked，
        # 而不是让异常冒到宿主那里变成一个既无原因也无出路的堆栈。
        return _blocked(
            TOOL_COMPARE,
            "UNSUPPORTED_SCOPE",
            snapshot_id,
            f"字段 {column} 无法按该口径比较：{exc}",
        )
    declared_primary = normalized.get("primary_test")
    produced = {test.get("test") for test in result.get("tests") or []}
    # 声明的主检验可能不适用于这一列（数值列声明 chi2_categories）。
    # 那就没有任何检验是预先声明的，必须如实说明，而不是挂着一个
    # 从没跑过的名字让读者以为做过预注册。
    primary = declared_primary if declared_primary in produced else None
    for test in result.get("tests") or []:
        test["role"] = "primary" if primary and test.get("test") == primary else "exploratory"
    allowed = sorted(
        {
            conclusion
            for test in result.get("tests") or []
            if test.get("significant")
            for conclusion in test.get("allowed_conclusions") or []
        }
    )
    trace = _described_column(meta, str(column))
    summary = {
        "spec": normalized,
        "column": column,
        "periods": {"base": base_period, "comp": comp_period},
        "signoff": {"base": base_signed, "comp": comp_signed},
        "denominator": {
            "base": int(base_mask.sum()),
            "comp": int(comp_mask.sum()),
        },
        "base": result["base"],
        "comp": result["comp"],
        "bins": result.get("bins") or [],
        "binning_note": result.get("binning_note"),
        "category_deltas": result.get("category_deltas") or [],
        "missing_share_delta": result.get("missing_share_delta"),
        "zero_share_delta": result.get("zero_share_delta"),
        "mean_delta": result.get("mean_delta"),
        "median_delta": result.get("median_delta"),
        "quantile_deltas": result.get("quantile_deltas") or {},
        "total_variation_distance": result.get("total_variation_distance"),
        "alpha": alpha_value,
        "primary_test": primary,
        "declared_primary_test": declared_primary,
        "tests": result.get("tests") or [],
        "allowed_conclusions": allowed,
        "forbidden_conclusions": list(_FORBIDDEN_CONCLUSIONS),
        "described_column": trace,
        "limitations": _compare_limitations(
            result,
            normalized,
            base_signed,
            comp_signed,
            primary,
            declared_primary,
            trace["role"],
        ),
    }
    run_id = new_id("run")
    run_artifact = ledger.save_run(run_id, TOOL_COMPARE, snapshot_id, _plain(summary))
    table_artifact = ledger.save_json_artifact(
        run_id,
        "distribution_compare",
        {
            "spec_id": normalized["spec_id"],
            "spec_version": normalized["spec_version"],
            "fingerprint": normalized["fingerprint"],
            "periods": summary["periods"],
            "column": column,
            "bins": summary["bins"],
            "tests": summary["tests"],
        },
    )
    return envelope(
        tool=TOOL_COMPARE,
        status="ok",
        summary=_plain(summary),
        snapshot_id=snapshot_id,
        run_id=run_id,
        artifacts=[run_artifact, table_artifact],
        allowed_conclusions=allowed,
    )


def _normalize_spec(
    spec: Mapping[str, Any], meta: Mapping[str, Any], frame: pd.DataFrame
) -> tuple[Dict[str, Any], Optional[tuple[str, str]]]:
    """Validate the caliber. Every rejection names the field that is wrong."""

    if not isinstance(spec, Mapping):
        return {}, ("UNSUPPORTED_SCOPE", "spec 必须是对象")
    required = ("spec_id", "spec_version", "population_label", "time_column", "denominator_rule")
    missing = [name for name in required if not str(spec.get(name) or "").strip()]
    if missing:
        return {}, ("UNSUPPORTED_SCOPE", "口径缺少字段：" + "、".join(missing))
    normalized: Dict[str, Any] = {name: str(spec[name]) for name in required}
    rule = normalized["denominator_rule"]
    if rule not in _DENOMINATOR_RULES:
        return {}, ("UNSUPPORTED_SCOPE", f"denominator_rule 只接受 {'、'.join(_DENOMINATOR_RULES)}")
    declared_grain = str(spec.get("grain") or meta.get("grain") or "").strip()
    if declared_grain and meta.get("grain") and declared_grain != str(meta.get("grain")):
        return {}, (
            "POPULATION_MISMATCH",
            f"口径声明的粒度 {declared_grain} 与快照粒度 {meta.get('grain')} 不一致："
            "描述的不是同一批记录",
        )
    normalized["grain"] = declared_grain or str(meta.get("grain") or "")
    time_column = normalized["time_column"]
    if time_column not in frame.columns:
        return {}, ("SNAPSHOT_INVALID", f"时间字段不在快照中：{time_column}")
    refused = _label_refusals(meta, [time_column])
    if refused:
        # 拿标签列切期，就是按结果分期：两期分母分别变成好客户数和坏客户数。
        return {}, ("UNSUPPORTED_SCOPE", "; ".join(refused) + "，不能作为切期字段")
    observation_column = meta.get("observation_time_column")
    if observation_column and time_column == observation_column:
        return {}, (
            "TIME_POINT_VIOLATION",
            f"时间字段 {time_column} 是观察时点列：用它切期会把表现期内的信息带进总体",
        )
    decision_column = meta.get("decision_time_column")
    if not decision_column:
        return {}, (
            "SNAPSHOT_INVALID",
            "快照未登记决策时点列：无法确认切期用的是不是决策时点，"
            "请用 inspect_and_bind_snapshot 重新绑定后再分析",
        )
    if str(time_column) != str(decision_column):
        return {}, (
            "TIME_POINT_VIOLATION",
            f"time_column 必须是快照的决策时点列 {decision_column}；{time_column} 不是。"
            "换一个字段切期会产生快照里并不存在的「期次」",
        )
    label_rule = _label_rule(normalized["population_label"])
    if label_rule and label_rule != rule:
        return {}, (
            "POPULATION_MISMATCH",
            f"总体写着「{normalized['population_label']}」，但分母规则是 {rule}（按措辞应为 {label_rule}）："
            "先统一口径再计算",
        )
    raw_filter = spec.get("denominator_filter")
    if raw_filter is not None:
        if not isinstance(raw_filter, Mapping) or not raw_filter.get("column"):
            return {}, ("UNSUPPORTED_SCOPE", "denominator_filter 必须是 {column, values}")
        column = str(raw_filter["column"])
        if column not in frame.columns:
            return {}, ("SNAPSHOT_INVALID", f"分母过滤列不在快照中：{column}")
        refused = _label_refusals(meta, [column])
        if refused:
            # 用标签列做分母过滤，等于按结果挑样本：留下全是好客户的一期，
            # 描述出来当然「结构变好了」。
            return {}, ("UNSUPPORTED_SCOPE", "; ".join(refused) + "，不能作为分母过滤条件")
        values = raw_filter.get("values")
        if not isinstance(values, (list, tuple)) or not values:
            return {}, ("UNSUPPORTED_SCOPE", "denominator_filter.values 必须是非空列表")
        declared = [str(item) for item in values]
        missing_values = [item for item in declared if not _value_present(frame[column], item)]
        if missing_values:
            return {}, (
                "UNSUPPORTED_SCOPE",
                f"分母过滤值在数据中不存在：{'、'.join(missing_values[:3])}；"
                f"该列实际取值示例：{_sample_values(frame[column])}",
            )
        normalized["denominator_filter"] = {"column": column, "values": declared}
    if rule == "all_records" and "denominator_filter" in normalized:
        # 过滤条件与「全部记录」互相矛盾。以前是静默忽略过滤条件、
        # 按全量算，然后把这批数字冠上 all_records 的名字。
        return {}, (
            "UNSUPPORTED_SCOPE",
            "denominator_rule 为 all_records 时不接受 denominator_filter："
            "二者矛盾。要么去掉过滤条件，要么把分母规则改成 approved / disbursed / matured_only，"
            "并让 population_label 与之一致",
        )
    if rule != "all_records" and "denominator_filter" not in normalized:
        return {}, (
            "UNSUPPORTED_SCOPE",
            f"分母规则 {rule} 需要 denominator_filter 指明哪些记录算数；工具不会替你猜",
        )

    semantics: Dict[str, Dict[str, str]] = {}
    raw_semantics = spec.get("field_semantics") or {}
    if not isinstance(raw_semantics, Mapping):
        return {}, ("UNSUPPORTED_SCOPE", "field_semantics 必须是 {列名: 含义} 对象")
    for name, value in raw_semantics.items():
        if isinstance(value, str):
            semantics[str(name)] = {"missing": value, "zero": value}
        elif isinstance(value, Mapping):
            semantics[str(name)] = {
                "missing": str(value.get("missing") or ""),
                "zero": str(value.get("zero") or ""),
            }
        else:
            return {}, ("UNSUPPORTED_SCOPE", f"field_semantics.{name} 必须是字符串或对象")
    normalized["field_semantics"] = semantics

    points: List[Dict[str, Any]] = []
    for item in spec.get("observation_points") or []:
        if not isinstance(item, Mapping):
            return {}, ("UNSUPPORTED_SCOPE", "observation_points 的每一项必须是对象")
        try:
            threshold = float(item.get("threshold"))
        except (TypeError, ValueError):
            return {}, ("UNSUPPORTED_SCOPE", "observation_points.threshold 必须是数字")
        kind = str(item.get("kind") or "observation_point")
        if kind not in ("observation_point", "applied_policy_threshold"):
            return {}, (
                "UNSUPPORTED_SCOPE",
                "observation_points.kind 只接受 observation_point 或 applied_policy_threshold",
            )
        points.append(
            {
                "name": str(item.get("name") or f"≥{threshold:g}"),
                "threshold": threshold,
                "kind": kind,
                "note": str(item.get("note") or ""),
            }
        )
    normalized["observation_points"] = points
    primary = str(spec.get("primary_test") or "").strip()
    if primary and primary not in _TEST_NAMES:
        # 拼错一个字母，全部检验就悄悄变成探索性，而负责人以为自己声明过主检验。
        return {}, (
            "UNSUPPORTED_SCOPE",
            f"primary_test 只接受 {'、'.join(_TEST_NAMES)}；收到 {primary!r}",
        )
    normalized["primary_test"] = primary or None
    normalized["note"] = str(spec.get("note") or "")
    return normalized, None


def _label_rule(label: str) -> Optional[str]:
    for keywords, rule in _LABEL_RULES:
        if any(word in label for word in keywords):
            return rule
    return None


def _registry_spec(registry: Mapping[str, Any], spec_id: str, version: str) -> Optional[Mapping[str, Any]]:
    entries = registry.get("populations") or registry.get("population_specs") or []
    if not isinstance(entries, (list, tuple)):
        return None
    for entry in entries:
        if not isinstance(entry, Mapping):
            continue
        if str(entry.get("spec_id")) == spec_id and str(entry.get("version") or entry.get("spec_version")) == version:
            merged = dict(entry)
            merged.pop("version", None)
            merged["spec_version"] = version
            return merged
    return None


def _denominator_mask(
    frame: pd.DataFrame, spec: Mapping[str, Any], in_period: pd.Series
) -> tuple[Optional[pd.Series], Optional[str]]:
    rule = spec["denominator_rule"]
    if rule == "all_records":
        return in_period, None
    column = spec["denominator_filter"]["column"]
    values = list(spec["denominator_filter"]["values"])
    unknown = [value for value in values if not _value_present(frame[column], value)]
    if unknown:
        # 过滤值一个都不匹配时，分母会变成 0 而没人发现。
        return None, f"分母过滤值在数据中不存在：{'、'.join(unknown[:3])}"
    matched = pd.Series(False, index=frame.index)
    for value in values:
        matched = matched | _value_mask(frame[column], value)
    return in_period & matched, None


def _value_mask(series: pd.Series, value: str) -> pd.Series:
    """Match a declared filter value as text, or numerically when it looks numeric.

    ``1`` in the data and ``"1.0"`` in the spec are the same record; comparing
    their string forms would either miss every record or demand a rewrite of a
    caliber that is already correct.
    """

    mask = series.astype(str) == value
    number = _as_float(value)
    if number is not None:
        mask = mask | (pd.to_numeric(series, errors="coerce") == number)
    return mask


def _value_present(series: pd.Series, value: str) -> bool:
    return bool(_value_mask(series, value).any())


def _as_float(value: Any) -> Optional[float]:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _sample_values(series: pd.Series, limit: int = 5) -> str:
    values = sorted({str(item) for item in series.dropna().unique()})
    return "、".join(values[:limit]) + ("…" if len(values) > limit else "")


def _label_refusals(meta: Mapping[str, Any], columns: Sequence[str]) -> List[str]:
    """Why these columns cannot be described, as sentences naming the column."""

    roles = meta.get("column_roles") or {}
    outcomes = {str(item) for item in (meta.get("observation_outcomes") or [])}
    refusals: List[str] = []
    for name in dict.fromkeys(str(item) for item in columns if item):
        if not name:
            continue
        role = str(roles.get(name) or "")
        if role in _LABEL_ROLES:
            refusals.append(f"列 {name} 在快照里角色为 {role}")
            continue
        if name in outcomes:
            refusals.append(f"列 {name} 在快照里登记为观察期结果字段")
            continue
        lowered = name.lower()
        hit = next((token for token in _LABEL_TOKENS if token in lowered), None)
        if hit:
            refusals.append(f"列 {name} 的名称含「{hit}」，按表现结果字段处理")
    return refusals


def _missing_as_zero(spec: Mapping[str, Any], column: str) -> bool:
    declared = (spec.get("field_semantics") or {}).get(column) or {}
    return declared.get("missing") == "zero_equivalent"


def _undeclared_binary(
    meta: Mapping[str, Any], frame: pd.DataFrame, columns: Sequence[str]
) -> List[Dict[str, Any]]:
    """0/1 columns nobody has classified. Their mean is a rate, so we ask first.

    Only the fact that the column is 0/1 leaves this function — never how many
    rows are 1. On a column that turns out to be a bad flag, that count divided
    by the row count *is* the bad rate, so reporting it inside a `needs_human`
    envelope would hand over exactly the number the gate exists to withhold.
    """

    roles = meta.get("column_roles") or {}
    undecided: List[Dict[str, Any]] = []
    for name in dict.fromkeys(str(item) for item in columns if item):
        if name not in frame.columns:
            continue
        role = str(roles.get(name) or "unclassified")
        if role in _DECLARED_ROLES:
            continue
        if not dist.is_binary_flag(frame[name]):
            continue
        undecided.append(
            {
                "column": name,
                "role": role,
                "observed_values": _binary_values(frame[name]),
            }
        )
    return undecided


def _binary_values(series: pd.Series) -> List[str]:
    """The set of values, without saying how many rows carry each one."""

    if pd.api.types.is_bool_dtype(series):
        return ["False", "True"]
    text = series.dropna().astype(str).str.strip()
    try:
        numbers = sorted({float(item) for item in text})
    except ValueError:
        return sorted(set(text.str.lower()))
    return [str(int(item)) if float(item).is_integer() else str(item) for item in numbers]


def _binary_gate(
    tool: str,
    snapshot_id: str,
    meta: Mapping[str, Any],
    frame: pd.DataFrame,
    columns: Sequence[str],
    period: str,
) -> Optional[Dict[str, Any]]:
    """0/1 columns must be declared before their share becomes a number."""

    undeclared = _undeclared_binary(meta, frame, columns)
    if not undeclared:
        return None
    return envelope(
        tool=tool,
        status="needs_human",
        snapshot_id=snapshot_id,
        summary={
            "message": "有 0/1 字段未声明是决策时点特征还是表现结果，先定性再计算",
            "period": period,
            "undecided": undeclared,
            "options": ["feature（决策时点已知的特征）", "observation_outcome（表现结果）"],
            "why": "0/1 列的均值就是取值为 1 的占比；如果它是坏账标记，这个均值就是坏账率。"
            "在快照的 column_roles 里声明它，再重跑。",
            "how": "inspect_and_bind_snapshot(..., column_roles={'列名': 'feature'})",
        },
        human_gate={"kind": "binary_column_role", "columns": [item["column"] for item in undeclared]},
    )


def _is_numeric_column(series: pd.Series) -> bool:
    try:
        dist.as_numeric(series)
    except ValueError:
        return False
    return True


def _undecided_semantics(
    subset: pd.DataFrame, columns: Sequence[str], spec: Mapping[str, Any]
) -> List[Dict[str, Any]]:
    semantics = spec.get("field_semantics") or {}
    undecided: List[Dict[str, Any]] = []
    for name in dict.fromkeys(columns):
        if name not in subset.columns:
            continue
        series = subset[name]
        n_missing = int(series.isna().sum())
        n_zero = 0
        numeric = None
        try:
            numeric = dist.as_numeric(series)
        except ValueError:
            numeric = None
        if numeric is not None:
            n_zero = int((numeric.dropna() == 0).sum())
        needs_missing = n_missing > 0
        needs_zero = n_zero > 0
        if not needs_missing and not needs_zero:
            continue
        declared = semantics.get(name) or {}
        # 只缺零值含义时不该要求先声明缺失含义：那会让一个只有零值
        # 的字段永远卡在待确认上。
        missing_ok = declared.get("missing") in _MISSING_MEANINGS
        zero_ok = declared.get("zero") in _ZERO_MEANINGS
        if (not needs_missing or missing_ok) and (not needs_zero or zero_ok):
            continue
        record: Dict[str, Any] = {
            "column": name,
            "n_missing": n_missing,
            "n_zero": n_zero,
            "declared": declared or None,
        }
        if needs_missing:
            record["missing_options"] = list(_MISSING_MEANINGS)
        if needs_zero:
            record["zero_options"] = list(_ZERO_MEANINGS)
        undecided.append(record)
    return undecided


def _dimension_breakdown(
    subset: pd.DataFrame,
    dimensions: Sequence[str],
    column: str,
    denominator: int,
    *,
    missing_as_zero: bool = False,
    feature_declared: bool = False,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for name in dimensions:
        grouped = subset[name].astype(str)
        counts = grouped.value_counts()
        cells = []
        for value, count in counts.head(_MAX_DIMENSION_CELLS).items():
            cell = grouped == value
            entry: Dict[str, Any] = {
                "value": str(value),
                "count": int(count),
                "share": float(count) / denominator if denominator else None,
            }
            if int(count) >= _DEFAULT_MIN_CELL:
                cell_series = subset.loc[cell, column]
                # 整列不是 0/1，不代表某一档不是：一个总体 1/0/5 的列，到了
                # 「a 档」里可能只剩 1/0，那一档的均值就是这一档的占比。
                # 未声明为 feature 的列，档内是 0/1 就不给数字。
                if not feature_declared and dist.is_binary_flag(cell_series):
                    entry["suppressed"] = True
                    entry["suppressed_reason"] = (
                        "该维度档位内这一列只有 0/1 取值：均值与 share_one 就是占比；"
                        "先在快照 column_roles 里声明它是不是 feature"
                    )
                    cells.append(entry)
                    continue
                series = dist.describe_series(
                    cell_series,
                    column=column,
                    missing_as_zero=missing_as_zero,
                )
                entry["metric"] = {
                    "mean": series.get("mean"),
                    "p50": (series.get("quantiles") or {}).get("p50"),
                    "missing_share": series.get("missing_share"),
                }
                if series.get("binary"):
                    entry["metric"]["share_one"] = series["binary"].get("share_one")
            else:
                entry["suppressed"] = True
                entry["suppressed_reason"] = f"样本量 {int(count)} < {_DEFAULT_MIN_CELL}"
            cells.append(entry)
        rows.append(
            {
                "dimension": str(name),
                "distinct": int(counts.size),
                "cells": cells,
                "truncated": bool(counts.size > len(cells)),
            }
        )
    return rows


def _observation_points(
    series: pd.Series, spec: Mapping[str, Any], denominator: int, *, missing_as_zero: bool = False
) -> List[Dict[str, Any]]:
    points = spec.get("observation_points") or []
    if not points:
        return []
    try:
        numeric = dist.as_numeric(series)
    except ValueError:
        return []
    values = numeric.fillna(0.0) if missing_as_zero else numeric.dropna()
    rows = []
    for point in points:
        threshold = float(point["threshold"])
        count = int((values >= threshold).sum())
        rows.append(
            {
                "name": point["name"],
                "threshold": threshold,
                # 观察点与已应用策略阈值不是一回事：混在一起就会把「看一眼」
                # 写成「策略已经这么卡」。
                "kind": point["kind"],
                "count": count,
                "share": float(count) / denominator if denominator else None,
                "note": point.get("note") or "",
            }
        )
    return rows


def _described_column(meta: Mapping[str, Any], column: str) -> Dict[str, Any]:
    """Which column was described, and on whose authority it was allowed in.

    A 0/1 column that passed the gate did so because someone declared it a
    `feature`. The run has to record that, otherwise the declaration exists
    only in the conversation and cannot be audited afterwards. ``column_roles``
    carries a role for *every* column (inferred ones included), so the source
    has to be read from the narrower ``explicit_roles`` map.
    """

    roles = meta.get("column_roles") or {}
    explicit = meta.get("explicit_roles")
    if explicit is None:
        explicit = roles
    declared = column in explicit
    return {
        "name": str(column),
        "role": str(roles.get(column) or "unclassified"),
        "role_source": "explicit" if declared else "default",
        "label_gate": "passed",
    }


def _limitations(meta: Mapping[str, Any], spec: Mapping[str, Any]) -> List[str]:
    notes = ["本结果不含目标标签，不能据此推断坏账率或风险升降。"]
    if not spec.get("signed"):
        notes.append("口径为本次声明的未签收版本：可用于分析，不可当作已签收的业务口径引用。")
    if meta.get("weight_column"):
        notes.append("未加权：快照登记了权重列，本工具按记录数计算。")
    if meta.get("grain") == "application":
        notes.append("粒度为申请记录：分母是申请量，不代表已放款客群。")
    notes.append(
        "未声明的 0/1 列、只剩一侧判定词的列（哪怕整列只有一个「是」）、以及词表内的成对措辞，"
        "都会被拦在 needs_human；已声明为 feature 的列，其身份由分析者担保，"
        "工具不复核数据本身——这类列的 share_one 能不能当业务指标用，取决于那份声明是否成立。"
    )
    notes.append(
        "闸门按取值形态判断，不看列名语义：不含 0 的编码（1/2、0/2、10/20）与好坏词表之外的"
        "两值措辞（如「良好/极差」）认不出来，需在绑定时用 column_roles 声明角色。"
    )
    return notes


def _compare_limitations(
    result: Mapping[str, Any],
    spec: Mapping[str, Any],
    base_signed: bool = True,
    comp_signed: bool = True,
    primary: Optional[str] = None,
    declared_primary: Optional[str] = None,
    described_role: Optional[str] = None,
) -> List[str]:
    notes = ["比较的是分布与构成，不是风险水平；显著不等于业务重要。"]
    if result.get("binning_note"):
        notes.append(str(result["binning_note"]))
    if result.get("kind") == "categorical":
        notes.append(
            "分类比较把缺失作为独立类别参与构成检验：各类别占比以全部记录为分母，"
            "与描述里的 top_values 一致；缺失占比见 missing_share_delta。"
        )
    if described_role == "feature":
        notes.append(
            "该列被声明为 feature（决策时点已知的特征）：它是不是特征由分析者担保，"
            "工具不复核数据本身；若声明不成立，这里比较的就是两期坏账率。"
        )
    notes.append(
        "比较沿用描述阶段的标记列闸门（按取值形态判断）：不含 0 的编码（1/2、0/2）与好坏词表"
        "之外的两值措辞不会被识别为标记列。"
    )
    if not primary:
        if declared_primary:
            notes.append(
                f"声明的主检验 {declared_primary} 不适用于本列（本次未产出该检验）："
                "全部检验均为探索性，多项同看会放大假阳性"
            )
        else:
            notes.append("未预先声明主检验：多项检验同时看显著性会放大假阳性，结论仅作探索。")
    if base_signed != comp_signed:
        notes.append(
            "两期口径的签收状态不一致（基期%s、对比期%s）：结果按未签收口径对待。"
            % ("已签收" if base_signed else "未签收", "已签收" if comp_signed else "未签收")
        )
    unavailable = [test["test"] for test in result.get("tests") or [] if test.get("p_value") is None]
    if unavailable:
        notes.append("以下检验未能给出 p 值：" + "、".join(unavailable))
    return notes


def _fingerprint(spec: Mapping[str, Any]) -> str:
    """Stable id for one caliber version, safe to paste into a report.

    The ``fp_`` prefix is not decoration: a bare 16-character hex string that
    happens to start with digits is read as a number by the report citation
    scanner, so citing it would fail and the analyst would be told to cite a
    figure that is not a figure.
    """

    payload = {key: value for key, value in spec.items() if key not in ("fingerprint", "signed")}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return "fp_" + hashlib.sha256(raw).hexdigest()[:16]


def _brief_periods(periods: pd.Series, limit: int = 8) -> str:
    values = sorted({str(item) for item in periods.unique()})
    shown = "、".join(values[:limit])
    return shown + ("…" if len(values) > limit else "")


def _blocked(tool: str, code: str, snapshot_id: str, message: str) -> Dict[str, Any]:
    return envelope(
        tool=tool,
        status="blocked",
        block_code=code,
        snapshot_id=snapshot_id,
        summary={"message": message},
    )


def _plain(value: Any) -> Any:
    """numpy scalars and NaN cannot survive JSON, and NaN is not a number here."""

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
    return str(value)
