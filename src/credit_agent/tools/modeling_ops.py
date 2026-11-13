"""Modeling sample, feature screen, and candidate training tools.

Row-level frames stay in the ledger. These functions only return envelopes.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from credit_agent.modeling.binning import normalize_method
from credit_agent.modeling.config import (
    ExportPolicy,
    FeatureSelectPolicy,
    ModelingJobSpec,
    ScorePolicy,
    TrainPolicy,
)
from credit_agent.modeling.schema import (
    ColumnBinding,
    binary_label_series,
    filter_modeling_rows,
    norm_label_value,
    resolve_schema,
)
from credit_agent.modeling.split import dev_test_split
from credit_agent.modeling.workflows.feature_selection import run_feature_selection
from credit_agent.modeling.workflows.training import train_classifier
from credit_agent.tools import envelope, new_id


def _blocked(tool: str, code: str, message: str, *, snapshot_id: Optional[str] = None) -> Dict[str, Any]:
    return envelope(
        tool=tool,
        status="blocked",
        block_code=code,
        snapshot_id=snapshot_id,
        summary={"message": message},
    )


def _require_admission(ledger: Any, snapshot_id: str, tool: str) -> Optional[Dict[str, Any]]:
    admission = ledger.get_admission(snapshot_id)
    if admission is None or admission.get("status") == "blocked":
        return _blocked(tool, "ADMISSION_REQUIRED", "该快照没有可用的准入记录", snapshot_id=snapshot_id)
    return None


def _ready(value: Any) -> Any:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, np.ndarray):
        return _ready(value.tolist())
    if isinstance(value, dict):
        return {str(key): _ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_ready(item) for item in value]
    return str(value)


def _load_mature(ledger: Any, snapshot_id: str, tool: str):
    frame = ledger.load_frame(snapshot_id)
    try:
        mask = ledger.load_mask(snapshot_id)
    except KeyError:
        return None, _blocked(tool, "LABEL_IMMATURE", "没有成熟样本掩码", snapshot_id=snapshot_id)
    values = mask.to_numpy(dtype=bool)
    if len(values) != len(frame) or not bool(values.any()):
        return None, _blocked(tool, "LABEL_IMMATURE", "没有成熟样本", snapshot_id=snapshot_id)
    mature = frame.iloc[np.flatnonzero(values)].reset_index(drop=True)
    return mature, None


def _binding_from_summary(summary: Dict[str, Any], *, features: Optional[Sequence[str]] = None) -> ColumnBinding:
    allow = list(features) if features is not None else list(summary.get("feature_allowlist") or [])
    split_time = summary.get("split_time_column", summary.get("time_column"))
    exclude = list(summary.get("exclude_columns") or [])
    original_time = summary.get("time_column")
    if original_time and split_time is None and original_time not in exclude:
        exclude.append(original_time)
    return ColumnBinding(
        label=summary.get("label"),
        positive_values=tuple(summary.get("positive_values") or (1,)),
        gray_values=tuple(summary.get("gray_values") or ()),
        gray_policy=str(summary.get("gray_policy") or "exclude"),
        id_columns=list(summary.get("id_columns") or []),
        exclude_columns=exclude,
        feature_allowlist=allow,
        time_column=split_time,
        allow_label_inference=False,
        allow_id_inference=False,
    )


def _count_gray(frame: pd.DataFrame, label: str, gray_values: Sequence[Any], gray_policy: str) -> int:
    if gray_policy != "exclude" or not gray_values or label not in frame.columns:
        return 0
    gray = {norm_label_value(value) for value in gray_values}
    return int(frame[label].map(norm_label_value).isin(gray).sum())


def _split_or_block(work: pd.DataFrame, y: np.ndarray, *, test_size: float, random_state: int,
                    time_column: str, group_column: Optional[str], tool: str, snapshot_id: str):
    try:
        split = dev_test_split(
            work,
            y,
            test_size=test_size,
            random_state=random_state,
            time_column=time_column,
            group_column=group_column,
        )
    except ValueError as exc:
        if "无法解释的数值" in str(exc):
            raise
        return None, _blocked(tool, "RANDOM_SPLIT_FORBIDDEN", str(exc), snapshot_id=snapshot_id)
    if split.method != "oot":
        return None, _blocked(tool, "RANDOM_SPLIT_FORBIDDEN", "切分不是时间外样本", snapshot_id=snapshot_id)
    return split, None


def _validation_choice(ledger: Any, snapshot_id: str, request: Dict[str, Any],
                       review_id: Optional[str], reason: str):
    if review_id:
        review = ledger.get_run(review_id)
        if not review or review["tool"] != "validation_fallback" or review["snapshot_id"] != snapshot_id:
            return None, _blocked("prepare_modeling_sample", "VALIDATION_CHOICE_REQUIRED",
                                  "回退选择与当前快照不匹配", snapshot_id=snapshot_id)
        original = review["summary"]
        if any(original.get(key) != request.get(key) for key in
               ("label", "time_column", "test_size", "random_state", "id_columns",
                "exclude_columns", "positive_values", "gray_values", "gray_policy")):
            return None, _blocked("prepare_modeling_sample", "VALIDATION_CHOICE_REQUIRED",
                                  "回退选择与当前样本定义不匹配", snapshot_id=snapshot_id)
        decision = ledger.get_decision("validation_fallback", review_id)
        if decision and decision.get("channel") == "approval_card":
            method = (decision.get("payload") or {}).get("method")
            if method == "stop":
                return None, _blocked("prepare_modeling_sample", "USER_CHOSE_STOP",
                                      "用户选择等待更多数据", snapshot_id=snapshot_id)
            if method in ("random_holdout", "cv_only"):
                return method, None
    else:
        review_id = new_id("review")
        ledger.save_run(review_id, "validation_fallback", snapshot_id, request)
    return None, envelope(
        tool="prepare_modeling_sample", status="needs_human", snapshot_id=snapshot_id,
        summary={"reason": reason, "standard_method": "oot",
                 "options": [
                     {"method": "random_holdout", "meaning": "独立随机留出；不能证明跨期稳定性"},
                     {"method": "cv_only", "meaning": "只做开发集交叉验证；没有独立留出集"},
                     {"method": "stop", "meaning": "等待更多数据后再建模"},
                 ]},
        human_gate={"gate": "validation_fallback", "review_id": review_id},
    )


def prepare_modeling_sample(
    snapshot_id: str,
    *,
    ledger: Any,
    label: str,
    time_column: Optional[str],
    positive_values: Sequence[Any] = (1,),
    gray_values: Sequence[Any] = (),
    gray_policy: str = "exclude",
    id_columns: Optional[Sequence[str]] = None,
    exclude_columns: Optional[Sequence[str]] = None,
    test_size: float = 0.3,
    random_state: int = 0,
    fallback_review_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Prefer OOT; use weaker validation only after a trusted choice."""

    blocked = _require_admission(ledger, snapshot_id, "prepare_modeling_sample")
    if blocked:
        return blocked
    mature, blocked = _load_mature(ledger, snapshot_id, "prepare_modeling_sample")
    if blocked:
        return blocked
    binding = ColumnBinding(
        label=label,
        positive_values=tuple(positive_values),
        gray_values=tuple(gray_values),
        gray_policy=gray_policy,
        id_columns=list(id_columns or []),
        exclude_columns=list(exclude_columns or []),
        time_column=time_column,
        allow_label_inference=False,
        allow_id_inference=False,
    )
    gray_excluded = _count_gray(mature, label, binding.gray_values, binding.gray_policy)
    work = filter_modeling_rows(mature, binding).reset_index(drop=True)
    if work.empty or label not in work.columns:
        return _blocked("prepare_modeling_sample", "LABEL_IMMATURE", "成熟样本里没有可用的好坏标签", snapshot_id=snapshot_id)
    y = binary_label_series(work, binding)
    keep = y.notna().to_numpy(dtype=bool)
    work = work.iloc[np.flatnonzero(keep)].reset_index(drop=True)
    y_arr = y.iloc[np.flatnonzero(keep)].to_numpy(dtype=int)
    if len(work) == 0:
        return _blocked("prepare_modeling_sample", "LABEL_IMMATURE", "成熟样本里没有可用的好坏标签", snapshot_id=snapshot_id)
    group = binding.id_columns[0] if binding.id_columns else None
    request = {"label": label, "time_column": time_column, "test_size": float(test_size),
               "random_state": int(random_state), "exclude_columns": list(binding.exclude_columns),
               "id_columns": list(binding.id_columns), "positive_values": list(binding.positive_values),
               "gray_values": list(binding.gray_values), "gray_policy": binding.gray_policy}
    fallback_method = None
    try:
        if not time_column:
            raise ValueError("没有观察时间列，无法形成时间外样本")
        split = dev_test_split(work, y_arr, test_size=float(test_size),
                               random_state=int(random_state), time_column=time_column,
                               group_column=group)
        if split.method != "oot":
            raise ValueError("未形成时间外样本")
    except ValueError as exc:
        if "无法解释的数值" in str(exc):
            raise
        fallback_method, response = _validation_choice(ledger, snapshot_id, request,
                                                        fallback_review_id, str(exc))
        if response:
            return response
        try:
            split = dev_test_split(work, y_arr,
                                   test_size=0.0 if fallback_method == "cv_only" else float(test_size),
                                   random_state=int(random_state), time_column=None,
                                   group_column=group)
        except ValueError as fallback_error:
            return _blocked("prepare_modeling_sample", "VALIDATION_UNAVAILABLE",
                            str(fallback_error), snapshot_id=snapshot_id)
    dev = split.dev_mask
    oot = split.test_mask
    bad = int(y_arr.sum())
    summary = {
        "sample_size": int(len(work)),
        "bad": bad,
        "good": int(len(y_arr) - bad),
        "gray_excluded": int(gray_excluded),
        "split_method": split.method,
        "validation_mode": "oot" if fallback_method is None else fallback_method,
        "fallback_review_id": fallback_review_id if fallback_method else None,
        "split_time_column": time_column if fallback_method is None else None,
        "cutoff": split.description,
        "dev_rows": int(dev.sum()),
        "oot_rows": int(oot.sum()),
        "dev_bad_rate": round(float(y_arr[dev].mean()), 6),
        "oot_bad_rate": round(float(y_arr[oot].mean()), 6) if bool(oot.any()) else None,
        "label": label,
        "time_column": time_column,
        "exclude_columns": list(binding.exclude_columns),
        "positive_values": list(binding.positive_values),
        "gray_values": list(binding.gray_values),
        "gray_policy": binding.gray_policy,
        "id_columns": list(binding.id_columns),
        "test_size": 0.0 if fallback_method == "cv_only" else float(test_size),
        "random_state": int(random_state),
        "split": {
            "method": split.method,
            "cutoff": split.description,
            "time_column": time_column if fallback_method is None else None,
        },
    }
    body = _ready(summary)
    run_id = new_id("run")
    artifact = ledger.save_run(run_id, "prepare_modeling_sample", snapshot_id, body)
    return envelope(
        tool="prepare_modeling_sample",
        status="degraded" if fallback_method else "ok",
        summary=body,
        snapshot_id=snapshot_id,
        run_id=run_id,
        artifacts=[artifact],
    )


def _drop_reason(iv: Any, psi: Any, missing: Optional[float], min_iv: float, max_psi: Optional[float]) -> str:
    if psi is not None and max_psi is not None and float(psi) > float(max_psi):
        return "PSI 超过上限"
    if missing is not None and missing >= 0.95:
        return "缺失率过高"
    if iv is None:
        return "无有效分箱"
    if float(iv) < float(min_iv):
        return "IV 低于下限"
    return "相关或数量上限"


def _feature_names(features: Sequence[str]) -> List[str]:
    if isinstance(features, (str, bytes)) or not isinstance(features, Sequence):
        raise TypeError("features 必须是字段名列表")
    return [str(name) for name in features]


def screen_features(
    prepare_run_id: str,
    *,
    ledger: Any,
    features: Sequence[str],
    min_iv: Optional[float] = None,
    max_feature_psi: Optional[float] = None,
    max_features: Optional[int] = None,
    woe_bins: Optional[int] = None,
    binning_method: Optional[str] = None,
    monotonic_direction: Optional[str] = None,
    min_bin_fraction: Optional[float] = None,
    max_prebins: Optional[int] = None,
) -> Dict[str, Any]:
    """Screen features on the prepared mature sample and stop for human review.

    分箱口径必须与后续训练一致，否则筛选结论不可复现：
    - woe_bins：最多分几箱（ChiMerge 建议 5~6，等频默认 10）
    - binning_method：quantile | chi2（ChiMerge）| monotonic
    - min_bin_fraction：最小箱样本占比，分母为该特征有值样本数
    - max_prebins：ChiMerge 预分箱数（默认 20）
    """

    defaults = FeatureSelectPolicy()
    if min_iv is None:
        min_iv = defaults.min_iv
    if max_feature_psi is None:
        max_feature_psi = defaults.max_feature_psi if defaults.max_feature_psi is not None else 0.25
    if max_features is None:
        max_features = defaults.max_features
    try:
        binning = str(binning_method or defaults.numeric_binning)
        method = normalize_method(binning)
    except ValueError as exc:
        return _blocked("screen_features", "UNSUPPORTED_SCOPE", str(exc))
    woe_bins = int(defaults.woe_bins if woe_bins is None else woe_bins)
    if not 2 <= woe_bins <= 20:
        return _blocked("screen_features", "UNSUPPORTED_SCOPE", "woe_bins 需在 2~20 之间")
    min_bin_fraction = float(defaults.min_bin_fraction if min_bin_fraction is None else min_bin_fraction)
    if not 0.0 < min_bin_fraction < 0.5:
        return _blocked("screen_features", "UNSUPPORTED_SCOPE", "min_bin_fraction 需在 (0, 0.5) 之间")
    max_prebins = int(defaults.max_prebins if max_prebins is None else max_prebins)
    if not woe_bins <= max_prebins <= 100:
        return _blocked("screen_features", "UNSUPPORTED_SCOPE", "max_prebins 需不小于 woe_bins 且不超过 100")
    direction = str(monotonic_direction or defaults.monotonic_direction)
    if direction not in ("increasing", "decreasing"):
        return _blocked("screen_features", "UNSUPPORTED_SCOPE",
                        f"不支持的单调方向 {direction}，可选 increasing / decreasing")

    run = ledger.get_run(prepare_run_id)
    if not run:
        return _blocked("screen_features", "SNAPSHOT_INVALID", "引用的运行不存在")
    snapshot_id = run["snapshot_id"]
    blocked = _require_admission(ledger, snapshot_id, "screen_features")
    if blocked:
        return blocked
    prepared = run["summary"]
    mature, blocked = _load_mature(ledger, snapshot_id, "screen_features")
    if blocked:
        return blocked
    names = _feature_names(features)
    # Known observation-time / leakage columns live in the immutable snapshot
    # meta written by inspect_and_bind_snapshot. Offering one of them as a
    # candidate feature is a point-in-time violation and is blocked outright.
    meta = ledger.get_snapshot(snapshot_id) or {}
    if not isinstance(meta, dict):
        meta = {}
    leakage_names = set()
    raw_leak = meta.get("suspect_leakage")
    if isinstance(raw_leak, (list, tuple)):
        leakage_names.update(str(name) for name in raw_leak)
    roles = meta.get("column_roles")
    if isinstance(roles, dict):
        for column, role in roles.items():
            if role in ("suspect_leakage", "observation_outcome", "observation_time"):
                leakage_names.add(str(column))
    obs_time = meta.get("observation_time_column")
    if isinstance(obs_time, str) and obs_time:
        leakage_names.add(obs_time)
    if set(names) & leakage_names:
        return _blocked("screen_features", "TIME_POINT_VIOLATION",
                        "候选特征包含观察时点/疑似穿越列", snapshot_id=snapshot_id)
    binding = _binding_from_summary(prepared, features=names)
    work = filter_modeling_rows(mature, binding).reset_index(drop=True)
    if work.empty:
        return _blocked("screen_features", "LABEL_IMMATURE", "成熟样本里没有可用的好坏标签", snapshot_id=snapshot_id)
    policy = FeatureSelectPolicy(
        min_iv=min_iv,
        max_feature_psi=max_feature_psi,
        max_features=max_features,
        emit_woe_profile=True,
        numeric_binning=method,
        woe_bins=woe_bins,
        monotonic_direction=direction,
        min_bin_fraction=min_bin_fraction,
        max_prebins=max_prebins,
    )
    spec = ModelingJobSpec(
        columns=binding,
        feature_select=policy,
        train=TrainPolicy(
            test_size=float(prepared.get("test_size", 0.3)),
            random_state=int(prepared.get("random_state", 0)),
        ),
        export=ExportPolicy(export_model=False),
        score=ScorePolicy(),
    )
    schema = resolve_schema(work, binding, numeric_only=spec.feature_select.numeric_only)
    result = run_feature_selection(
        work,
        schema,
        spec.feature_select,
        columns=spec.columns,
        test_size=spec.train.test_size,
        random_state=spec.train.random_state,
    )
    candidates = result.get("candidates") or []
    iv_map = {row["feature"]: row.get("iv") for row in candidates}
    psi_map = {row["feature"]: row.get("psi") for row in result.get("feature_psi") or []}
    cand_map = {row["feature"]: row for row in candidates}
    selected = [str(name) for name in result.get("selected") or []]
    selected_set = set(selected)
    profile = ((result.get("selected_woe_profile") or {}).get("features") or {})
    rows: List[Dict[str, Any]] = []
    suspect_names: List[str] = []
    for name in names:
        iv = iv_map.get(name)
        psi = psi_map.get(name)
        cand = cand_map.get(name) or {}
        missing = round(float(work[name].isna().mean()), 6) if name in work.columns and len(work) else None
        suspect = (iv is not None and float(iv) > 0.5) or name in leakage_names
        if suspect:
            suspect_names.append(name)
        if name in selected_set:
            decision, reason = "keep", "通过筛选"
        else:
            decision, reason = "drop", _drop_reason(iv, psi, missing, min_iv, max_feature_psi)
        rows.append(
            {
                "name": name,
                "iv": None if iv is None else float(iv),
                "psi": None if psi is None else float(psi),
                "missing": missing,
                "ks": cand.get("ks"),
                "n_bins": cand.get("n_bins"),
                "monotonic": cand.get("monotonic"),
                "monotonic_strength": cand.get("monotonic_strength"),
                "decision": decision,
                "reason": reason,
                "suspect_leakage": bool(suspect),
            }
        )
    suspect_set = set(suspect_names)
    full_selected = []
    for name in selected:
        item: Dict[str, Any] = {
            "name": name,
            "iv": None if iv_map.get(name) is None else float(iv_map[name]),
            "psi": None if psi_map.get(name) is None else float(psi_map[name]),
        }
        if name in profile:
            item["woe"] = profile[name]
            # 分箱档与 WOE 档同源：交付报告直接渲染这张表，不再二次分箱。
            item["bin_profile"] = profile[name]
        item["suspect_leakage"] = name in suspect_set
        full_selected.append(item)
    # The ledger summary and the agent-facing envelope now share one structure:
    # ``selected`` is always a plain name list while ``selected_details`` holds
    # the richer per-feature information (IV / PSI / WOE / leakage flag).
    shared = {
        "prepare_run_id": prepare_run_id,
        "label": prepared.get("label"),
        "time_column": prepared.get("time_column"),
        "split_time_column": prepared.get("split_time_column", prepared.get("time_column")),
        "validation_mode": prepared.get("validation_mode", "oot"),
        "fallback_review_id": prepared.get("fallback_review_id"),
        "exclude_columns": list(prepared.get("exclude_columns") or []),
        "positive_values": list(prepared.get("positive_values") or [1]),
        "gray_values": list(prepared.get("gray_values") or []),
        "gray_policy": prepared.get("gray_policy") or "exclude",
        "id_columns": list(prepared.get("id_columns") or []),
        "test_size": prepared.get("test_size", 0.3),
        "random_state": prepared.get("random_state", 0),
        "features": rows,
        "selected": selected,
        "selected_details": full_selected,
        "suspect_leakage": suspect_names,
        "binning_policy": {
            "method": method,
            "max_bins": int(woe_bins),
            "min_bin_fraction": float(min_bin_fraction),
            "min_bin_fraction_scope": "valid_only",
            "max_prebins": int(max_prebins),
            "monotonic_direction": direction,
            "missing_bin": "MISSING",
        },
    }
    body = _ready(shared)
    run_id = new_id("run")
    artifact = ledger.save_run(run_id, "screen_features", snapshot_id, body)
    return envelope(
        tool="screen_features",
        status="needs_human",
        summary=body,
        snapshot_id=snapshot_id,
        run_id=run_id,
        artifacts=[artifact],
        human_gate={"gate": "feature_list", "review_id": run_id},
    )


def _woe_lookup(entry: Dict[str, Any]) -> Dict[str, float]:
    return {str(b.get("bin")): float(b.get("woe") or 0.0) for b in entry.get("bins") or []}


def _risk_score(series: pd.Series, entry: Dict[str, Any]) -> np.ndarray:
    """按已拟合的分箱把原始值映射成风险分（WOE 正向，越高越坏）。"""

    edges = entry.get("bin_edges") or []
    lookup = _woe_lookup(entry)
    if not edges or len(edges) < 2 or not lookup:
        return np.zeros(len(series))
    numeric = pd.to_numeric(series, errors="coerce")
    binned = pd.cut(numeric, bins=edges, include_lowest=True, duplicates="drop")
    labels = binned.astype(str)
    if "MISSING" in lookup:
        labels = labels.mask(binned.isna(), "MISSING")
    return labels.map(lambda x: lookup.get(x, 0.0)).astype(float).to_numpy()


def profile_binning(
    prepare_run_id: str,
    *,
    ledger: Any,
    features: Sequence[str],
    woe_bins: int = 6,
    binning_method: str = "chi2",
    monotonic_direction: str = "increasing",
    min_bin_fraction: float = 0.05,
    max_prebins: int = 20,
    include_merge_log: bool = True,
) -> Dict[str, Any]:
    """单特征分箱档：把 ChiMerge 的逐箱结果与合并过程作为工具能力输出。

    与 `screen_features` / `train_and_evaluate_candidate` 共用同一套分箱内核，
    因此这里看到的箱边界就是训练时真正使用的箱边界，不存在「审查一套、训练另一套」。
    """

    tool = "profile_binning"
    try:
        method = normalize_method(binning_method)
    except ValueError as exc:
        return _blocked(tool, "UNSUPPORTED_SCOPE", str(exc))
    if not 2 <= int(woe_bins) <= 20:
        return _blocked(tool, "UNSUPPORTED_SCOPE", "woe_bins 需在 2~20 之间")
    if not 0.0 < float(min_bin_fraction) < 0.5:
        return _blocked(tool, "UNSUPPORTED_SCOPE", "min_bin_fraction 需在 (0, 0.5) 之间")
    if not int(woe_bins) <= int(max_prebins) <= 100:
        return _blocked(tool, "UNSUPPORTED_SCOPE", "max_prebins 需不小于 woe_bins 且不超过 100")
    if monotonic_direction not in ("increasing", "decreasing"):
        return _blocked(tool, "UNSUPPORTED_SCOPE",
                        f"不支持的单调方向 {monotonic_direction}，可选 increasing / decreasing")

    run = ledger.get_run(prepare_run_id)
    if not run:
        return _blocked(tool, "SNAPSHOT_INVALID", "引用的运行不存在")
    snapshot_id = run["snapshot_id"]
    blocked = _require_admission(ledger, snapshot_id, tool)
    if blocked:
        return blocked
    prepared = run["summary"]
    names = _feature_names(features)
    if not names:
        return _blocked(tool, "UNSUPPORTED_SCOPE", "features 不能为空", snapshot_id=snapshot_id)
    mature, blocked = _load_mature(ledger, snapshot_id, tool)
    if blocked:
        return blocked
    binding = _binding_from_summary(prepared, features=names)
    work = filter_modeling_rows(mature, binding).reset_index(drop=True)
    if work.empty or not binding.label:
        return _blocked(tool, "LABEL_IMMATURE", "成熟样本里没有可用的好坏标签", snapshot_id=snapshot_id)
    y_series = binary_label_series(work, binding)
    keep = y_series.notna().to_numpy(dtype=bool)
    work = work.iloc[np.flatnonzero(keep)].reset_index(drop=True)
    y = y_series.iloc[np.flatnonzero(keep)].to_numpy(dtype=int)
    if len(work) == 0:
        return _blocked(tool, "VALIDATION_UNAVAILABLE", "剔除标签缺失后没有可用样本", snapshot_id=snapshot_id)

    time_column = prepared.get("split_time_column") or prepared.get("time_column")
    group_column = (prepared.get("id_columns") or [None])[0]
    dev_mask = np.ones(len(work), dtype=bool)
    test_mask = np.zeros(len(work), dtype=bool)
    split_note = "只有开发集：未配置观察时点列，跨期指标不可用"
    if time_column and time_column in work.columns:
        try:
            split = dev_test_split(
                work,
                y,
                test_size=float(prepared.get("test_size", 0.3)),
                random_state=int(prepared.get("random_state", 0)),
                time_column=time_column,
                group_column=group_column,
            )
            if split.method == "oot":
                dev_mask, test_mask = split.dev_mask, split.test_mask
                split_note = split.description
        except ValueError:
            split_note = "只有开发集：时间切分失败，跨期指标不可用"

    dev = work[dev_mask].reset_index(drop=True)
    oot = work[test_mask].reset_index(drop=True)
    y_dev = y[dev_mask]
    y_oot = y[test_mask]
    from credit_agent.modeling.feature_psi import feature_psi_value
    from credit_agent.modeling.metrics import auc_score, ks_score
    from credit_agent.modeling.woe import numeric_profile

    out: List[Dict[str, Any]] = []
    for name in names:
        if name not in dev.columns:
            out.append({"name": name, "usable": False, "reason": "字段不在样本中"})
            continue
        entry = numeric_profile(
            dev[name],
            pd.Series(y_dev, index=dev.index),
            n_bins=int(woe_bins),
            binning_method=method,
            monotonic_direction=monotonic_direction,
            min_bin_fraction=float(min_bin_fraction),
            max_prebins=int(max_prebins),
        )
        if entry is None:
            out.append({"name": name, "usable": False, "reason": "取值或标签不足以分箱"})
            continue
        score_dev = _risk_score(dev[name], entry)
        ks_dev, _ = ks_score(y_dev, score_dev)
        item: Dict[str, Any] = {
            "name": name,
            "usable": True,
            "iv": entry.get("iv"),
            "kind": entry.get("kind"),
            "n_bins": len(entry.get("bins") or []),
            "n_valid": entry.get("n_valid"),
            "missing_rate": entry.get("missing_rate"),
            "ks_dev": None if not np.isfinite(ks_dev) else round(float(ks_dev), 6),
            "auc_dev": None,
            "monotonic": entry.get("monotonic"),
            "monotonic_strength": entry.get("monotonic_strength"),
            "bin_edges": entry.get("bin_edges"),
            "bins": entry.get("bins"),
            "min_bin_violations": entry.get("min_bin_violations"),
        }
        auc_dev = auc_score(y_dev, score_dev)
        item["auc_dev"] = None if not np.isfinite(auc_dev) else round(float(auc_dev), 6)
        if len(oot) >= 30 and 0 < int(y_oot.sum()) < len(y_oot):
            score_oot = _risk_score(oot[name], entry)
            ks_oot, _ = ks_score(y_oot, score_oot)
            auc_oot = auc_score(y_oot, score_oot)
            item["ks_oot"] = None if not np.isfinite(ks_oot) else round(float(ks_oot), 6)
            item["auc_oot"] = None if not np.isfinite(auc_oot) else round(float(auc_oot), 6)
            psi_value = feature_psi_value(dev[name], oot[name])
            item["psi"] = round(float(psi_value), 6) if np.isfinite(psi_value) else None
        else:
            item["ks_oot"] = None
            item["auc_oot"] = None
            item["psi"] = None
        if include_merge_log:
            item["merge_log"] = entry.get("merge_log") or []
            item["prebins"] = entry.get("prebins")
            item["min_bin_size"] = entry.get("min_bin_size")
        out.append(item)

    summary = _ready(
        {
            "prepare_run_id": prepare_run_id,
            "label": prepared.get("label"),
            "split": split_note,
            "dev_rows": int(dev_mask.sum()),
            "dev_bad": int(y_dev.sum()),
            "oot_rows": int(test_mask.sum()),
            "oot_bad": int(y_oot.sum()) if len(y_oot) else 0,
            "binning_policy": {
                "method": method,
                "max_bins": int(woe_bins),
                "min_bin_fraction": float(min_bin_fraction),
                "min_bin_fraction_scope": "valid_only",
                "max_prebins": int(max_prebins),
                "monotonic_direction": monotonic_direction,
                "missing_bin": "MISSING",
            },
            "features": out,
            "usable": [row["name"] for row in out if row.get("usable")],
        }
    )
    run_id = new_id("run")
    artifact = ledger.save_run(run_id, tool, snapshot_id, summary)
    return envelope(
        tool=tool,
        status="ok" if test_mask.any() else "degraded",
        summary=summary,
        snapshot_id=snapshot_id,
        run_id=run_id,
        artifacts=[artifact],
    )


def _selected_names(summary: Dict[str, Any]) -> List[str]:
    names: List[str] = []
    for item in summary.get("selected") or []:
        if isinstance(item, dict):
            name = item.get("name")
        else:
            name = item
        if isinstance(name, str):
            names.append(name)
    if not names:
        for item in summary.get("selected_details") or []:
            if isinstance(item, dict) and isinstance(item.get("name"), str):
                names.append(item["name"])
    return names


def _candidate_summary(result: Dict[str, Any]) -> Dict[str, Any]:
    models: Dict[str, Any] = {}
    for name, meta in (result.get("models") or {}).items():
        models[str(name)] = {
            "cv_auc_mean": _ready(meta.get("cv_auc_mean")),
            "cv_auc_std": _ready(meta.get("cv_auc_std")),
            "oof_auc": _ready(meta.get("oof_auc")),
            "oof_ks": _ready(meta.get("oof_ks")),
            "train_auc": _ready(meta.get("train_auc")),
            "train_ks": _ready(meta.get("train_ks")),
            "test_auc": _ready(meta.get("test_auc")),
            "test_ks": _ready(meta.get("test_ks")),
        }
    monotonic = result.get("monotonic_deciles")
    if isinstance(monotonic, (bool, np.bool_)):
        monotonic = bool(monotonic)
    return {
        "status": "candidate",
        "signoff_status": "pending",
        "signoff_gate": "model_signoff",
        "signoff_required": True,
        "algorithm": result.get("algorithm"),
        "models": models,
        "unavailable_algorithms": list(result.get("unavailable_algorithms") or []),
        "candidate_comparison_complete": not bool(result.get("unavailable_algorithms")),
        "score_psi_train_vs_test": _ready(result.get("score_psi_train_vs_test")),
        "monotonic_deciles": monotonic,
    }


def train_and_evaluate_candidate(
    screen_run_id: str,
    *,
    ledger: Any,
    woe_bins: int = 10,
    binning_method: str = "quantile",
    monotonic_direction: str = "increasing",
    min_bin_fraction: float = 0.05,
    max_prebins: int = 20,
) -> Dict[str, Any]:
    """Train only after an approval card confirms a subset of the screened features.

    分箱口径可配置（此前硬编码 quantile 10 箱，与人工审查用的 ChiMerge 分箱不一致时
    会导致训练结果无法复现审查结论）：
    - woe_bins：最多分几箱（ChiMerge 建议 5~6，等频默认 10）
    - binning_method：quantile（等频）| chi2（ChiMerge）| monotonic（保序单调）
    - monotonic_direction：increasing（数值越大坏率越高）| decreasing
    - min_bin_fraction：每箱最小样本占比，分母为该特征有值样本数
    - max_prebins：ChiMerge 预分箱数
    """

    try:
        method = normalize_method(binning_method)
    except ValueError as exc:
        return _blocked("train_and_evaluate_candidate", "UNSUPPORTED_SCOPE", str(exc))
    if monotonic_direction not in ("increasing", "decreasing"):
        return _blocked("train_and_evaluate_candidate", "UNSUPPORTED_SCOPE",
                        f"不支持的单调方向 {monotonic_direction}，可选 increasing / decreasing")
    if not 2 <= int(woe_bins) <= 20:
        return _blocked("train_and_evaluate_candidate", "UNSUPPORTED_SCOPE",
                        "woe_bins 需在 2~20 之间")
    if not 0.0 < float(min_bin_fraction) < 0.5:
        return _blocked("train_and_evaluate_candidate", "UNSUPPORTED_SCOPE",
                        "min_bin_fraction 需在 (0, 0.5) 之间")
    if not int(woe_bins) <= int(max_prebins) <= 100:
        return _blocked("train_and_evaluate_candidate", "UNSUPPORTED_SCOPE",
                        "max_prebins 需不小于 woe_bins 且不超过 100")

    run = ledger.get_run(screen_run_id)
    if not run:
        return _blocked("train_and_evaluate_candidate", "SNAPSHOT_INVALID", "引用的运行不存在")
    snapshot_id = run["snapshot_id"]
    blocked = _require_admission(ledger, snapshot_id, "train_and_evaluate_candidate")
    if blocked:
        return blocked
    screened = run["summary"]
    allowed = _selected_names(screened)

    def _confirms(candidate: Optional[Dict[str, Any]]) -> bool:
        if not candidate or candidate.get("channel") != "approval_card":
            return False
        payload = candidate.get("payload") or {}
        picked = payload.get("selected_features") if isinstance(payload, dict) else None
        return (isinstance(picked, (list, tuple))
                and len(picked) > 0
                and all(isinstance(name, str) for name in picked)
                and set(picked).issubset(set(allowed)))

    # Peek first: a rejected attempt must not burn the one-time approval card.
    peek = ledger.get_decision("feature_list", screen_run_id)
    if not _confirms(peek):
        return _blocked(
            "train_and_evaluate_candidate",
            "FEATURE_REVIEW_REQUIRED",
            "缺少审批卡确认的入模特征子集",
            snapshot_id=snapshot_id,
        )
    if hasattr(ledger, "consume_decision"):
        decision = ledger.consume_decision("feature_list", screen_run_id)
        if decision is None or not _confirms(decision):
            return _blocked(
                "train_and_evaluate_candidate",
                "FEATURE_REVIEW_REQUIRED",
                "审批卡确认单已被消费，禁止重放",
                snapshot_id=snapshot_id,
            )
    else:
        decision = peek
    chosen = list((decision.get("payload") or {}).get("selected_features"))
    if not screened.get("time_column") and screened.get("validation_mode") == "oot":
        return _blocked(
            "train_and_evaluate_candidate",
            "RANDOM_SPLIT_FORBIDDEN",
            "没有观察时间列，不能随机切分",
            snapshot_id=snapshot_id,
        )
    mature, blocked = _load_mature(ledger, snapshot_id, "train_and_evaluate_candidate")
    if blocked:
        return blocked
    binding = _binding_from_summary(screened, features=[str(name) for name in chosen])
    work = filter_modeling_rows(mature, binding).reset_index(drop=True)
    if work.empty:
        return _blocked("train_and_evaluate_candidate", "LABEL_IMMATURE", "成熟样本里没有可用的好坏标签", snapshot_id=snapshot_id)
    schema = resolve_schema(work, binding, numeric_only=False)
    spec = ModelingJobSpec(
        columns=binding,
        train=TrainPolicy(
            algorithms=("logistic_regression", "lightgbm"),
            test_size=float(screened.get("test_size", 0.3)),
            random_state=int(screened.get("random_state", 0)),
        ),
        export=ExportPolicy(export_model=False),
        score=ScorePolicy(),
    )
    try:
        result = train_classifier(
            work,
            schema,
            [str(name) for name in chosen],
            spec.train,
            spec.export,
            spec.score,
            columns=spec.columns,
            woe_bins=int(woe_bins),
            binning_method=str(method),
            monotonic_direction=str(monotonic_direction),
            min_bin_fraction=float(min_bin_fraction),
            max_prebins=int(max_prebins),
        )
    except ValueError as exc:
        message = str(exc)
        if any(token in message for token in ("随机", "时间", "跨期")):
            return _blocked("train_and_evaluate_candidate", "RANDOM_SPLIT_FORBIDDEN",
                            message, snapshot_id=snapshot_id)
        return _blocked("train_and_evaluate_candidate", "VALIDATION_UNAVAILABLE",
                        message, snapshot_id=snapshot_id)
    if result.get("error"):
        error = result["error"]
        error_codes = {
            "baseline_unavailable": "BASELINE_UNAVAILABLE",
            "no_trainable_algorithm": "BASELINE_UNAVAILABLE",
            "cv_unavailable": "VALIDATION_UNAVAILABLE",
            "insufficient_rows": "VALIDATION_UNAVAILABLE",
            "insufficient_class_balance": "VALIDATION_UNAVAILABLE",
            "insufficient_cv_classes": "VALIDATION_UNAVAILABLE",
            "no_features": "VALIDATION_UNAVAILABLE",
        }
        return _blocked("train_and_evaluate_candidate", error_codes.get(error, "UNSUPPORTED_SCOPE"),
                        str(result.get("message") or error), snapshot_id=snapshot_id)
    body = _ready(_candidate_summary(result))
    body["binning_policy"] = {
        "woe_bins": int(woe_bins),
        "binning_method": str(method),
        "monotonic_direction": str(monotonic_direction),
        "min_bin_fraction": float(min_bin_fraction),
        "min_bin_fraction_scope": "valid_only",
        "max_prebins": int(max_prebins),
        "missing_bin": "MISSING",
    }
    body["validation_mode"] = screened.get("validation_mode", "oot")
    body["oot_validation_complete"] = body["validation_mode"] == "oot"
    body["fallback_review_id"] = screened.get("fallback_review_id")
    run_id = new_id("run")
    details = _ready({
        "sample": result.get("sample"),
        "split_spec": result.get("split_spec"),
        "label": result.get("label"),
        "features": result.get("features"),
        "models": result.get("models"),
        "test_deciles": result.get("test_deciles"),
        "baseline_scorecard": result.get("baseline_scorecard"),
    })
    artifact = ledger.save_run(run_id, "train_and_evaluate_candidate", snapshot_id,
                               {**body, "validation": details})
    detail_artifact = ledger.save_json_artifact(run_id, "model_validation", details)
    card_artifact = ledger.save_json_artifact(run_id, "scorecard", details["baseline_scorecard"])
    return envelope(
        tool="train_and_evaluate_candidate",
        status="degraded" if body["unavailable_algorithms"] or not body["oot_validation_complete"] else "ok",
        summary=body,
        snapshot_id=snapshot_id,
        run_id=run_id,
        artifacts=[artifact, detail_artifact, card_artifact],
    )
