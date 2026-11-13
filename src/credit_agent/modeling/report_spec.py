"""Caller-selected scorecard metrics.

The legacy evaluation report always emitted the same columns and always cut
scores on fixed edges. This module returns only the metrics named by the
caller, using the bin count on the spec instead of a hardcoded score scale.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Dict, Optional, Sequence

import numpy as np

from .metrics import auc_score, decile_bad_rates, ks_score, psi


METRIC_NAMES = ("ks", "auc", "psi", "bad_rate", "lift", "decile_bad_rate")


@dataclass(frozen=True)
class ReportSpec:
    """Which metrics to publish for one scored sample.

    ``metrics`` must be a non-empty selection from :data:`METRIC_NAMES`.
    ``n_bins`` is the equal-frequency bin count used by KS, PSI, lift, and
    decile bad rate, and must be an integer from 2 to 20.
    """

    metrics: tuple[str, ...]
    n_bins: int = 10
    sample_name: str = "sample"

    def __post_init__(self) -> None:
        metrics = self.metrics
        if isinstance(metrics, (str, bytes)):
            raise ValueError("指标必须是非空的名称列表")
        try:
            chosen = tuple(metrics)
        except TypeError as exc:
            raise ValueError("指标必须是非空的名称列表") from exc
        if not chosen:
            raise ValueError("指标不能为空")
        if any(not isinstance(name, str) for name in chosen):
            raise ValueError("指标名必须是字符串")
        unknown = [name for name in chosen if name not in METRIC_NAMES]
        if unknown:
            raise ValueError("不支持的指标：" + "、".join(unknown))
        if isinstance(self.n_bins, bool) or not isinstance(self.n_bins, int) or not 2 <= self.n_bins <= 20:
            raise ValueError("分箱数必须在 2 到 20 之间")
        if not isinstance(self.sample_name, str):
            raise ValueError("样本名称必须是字符串")
        object.__setattr__(self, "metrics", chosen)


def build_report(
    y_true: Sequence[Any],
    y_prob: Sequence[Any],
    spec: ReportSpec,
    *,
    reference_prob: Optional[Sequence[Any]] = None,
) -> Dict[str, Any]:
    """Return a dict containing only the metrics named on ``spec``.

    ``ks`` and ``auc`` come from :mod:`credit_agent.modeling.metrics`. ``psi``
    compares ``y_prob`` with ``reference_prob``; when the reference is omitted
    the value is null and ``notes`` says the reference distribution is missing.
    ``bad_rate`` is the mean of ``y_true``. ``lift`` is the bad rate in the
    highest-risk equal-frequency bin divided by the overall bad rate. That bin
    is the first piece of the same descending-score split used by
    ``decile_bad_rates``. Non-finite metric values are returned as null.
    """

    if not isinstance(spec, ReportSpec):
        raise ValueError("报告规格无效")
    y = _as_1d_float(y_true, "标签")
    p = _as_1d_float(y_prob, "预测概率")
    if len(y) != len(p):
        raise ValueError("标签与预测概率的长度不一致")
    if len(y) == 0:
        raise ValueError("样本为空")
    reference: Optional[np.ndarray] = None
    if reference_prob is not None:
        reference = _as_1d_float(reference_prob, "参照分布")

    notes: list[str] = []
    computed: Dict[str, Any] = {}
    requested = set(spec.metrics)
    if "ks" in requested:
        computed["ks"] = _finite_or_none(ks_score(y, p, n_bins=spec.n_bins)[0])
    if "auc" in requested:
        computed["auc"] = _finite_or_none(auc_score(y, p))
    if "psi" in requested:
        computed["psi"] = _population_stability(p, reference, spec.n_bins, notes)
    if "bad_rate" in requested:
        computed["bad_rate"] = float(y.mean())
    if "lift" in requested:
        computed["lift"] = _lift(y, p, spec.n_bins, notes)
    if "decile_bad_rate" in requested:
        computed["decile_bad_rate"] = decile_bad_rates(y, p, n_bins=spec.n_bins)

    report: Dict[str, Any] = {
        "sample_name": spec.sample_name,
        "n": int(len(y)),
        "metrics_requested": spec.metrics,
    }
    seen: set[str] = set()
    for name in spec.metrics:
        if name in seen:
            continue
        seen.add(name)
        report[name] = computed[name]
    if notes:
        report["notes"] = notes
    return report


def _population_stability(
    actual: np.ndarray,
    reference: Optional[np.ndarray],
    n_bins: int,
    notes: list[str],
) -> Optional[float]:
    if reference is None:
        notes.append("缺少参照分布，未计算 psi")
        return None
    value = _finite_or_none(psi(reference, actual, n_bins=n_bins))
    if value is None:
        notes.append("参照分布不足以计算 psi")
    return value


def _lift(y: np.ndarray, p: np.ndarray, n_bins: int, notes: list[str]) -> Optional[float]:
    overall = float(y.mean())
    if not math.isfinite(overall) or overall == 0.0:
        notes.append("总体坏账率为 0，无法计算 lift")
        return None
    order = np.argsort(-p, kind="mergesort")
    top = np.array_split(order, n_bins)[0]
    if len(top) == 0:
        notes.append("最高风险分箱为空，无法计算 lift")
        return None
    return float(y[top].mean()) / overall


def _finite_or_none(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _as_1d_float(values: Any, name: str) -> np.ndarray:
    if values is None or isinstance(values, (str, bytes)):
        raise ValueError(f"{name}必须是一维数值")
    try:
        array = np.asarray(values, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}必须是一维数值") from exc
    if array.ndim != 1:
        raise ValueError(f"{name}必须是一维数值")
    if array.size and not np.isfinite(array).all():
        raise ValueError(f"{name}包含缺失或无穷值")
    return array
