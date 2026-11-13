"""Run a caller-supplied function on each value of a caller-supplied column.

Slices that are too small, or that do not have enough of each class when a
label column is supplied, are skipped. A failure inside one value is recorded
and does not stop the other values.
"""

from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Optional

import numpy as np
import pandas as pd


def run_by_segment(
    frame: pd.DataFrame,
    segment: str,
    runner: Callable[[pd.DataFrame], Dict[str, Any]],
    *,
    min_rows: int = 50,
    min_positive: int = 10,
    min_negative: int = 10,
    label: Optional[str] = None,
) -> Dict[str, Any]:
    """Evaluate ``runner`` once per distinct value of ``segment``.

    ``segment`` and ``label`` are column names chosen by the caller. A value
    with fewer than ``min_rows`` rows is skipped. When ``label`` is given, a
    value is also skipped when the count of 1s is below ``min_positive`` or the
    count of 0s is below ``min_negative``. Skipped values do not call
    ``runner``. An exception from ``runner`` is stored on that value and the
    remaining values still run.

    The result dict contains ``segment``, ``ran`` (sorted by value, each item
    ``value`` / ``rows`` / ``result``), ``skipped``, ``failed``, and
    ``n_values``.
    """

    min_rows = _nonnegative_int(min_rows, "最小行数必须是非负整数")
    min_positive = _nonnegative_int(min_positive, "最小坏样本数必须是非负整数")
    min_negative = _nonnegative_int(min_negative, "最小好样本数必须是非负整数")
    if not isinstance(frame, pd.DataFrame):
        raise ValueError("样本表必须是数据框")
    if not isinstance(segment, str) or segment == "":
        raise ValueError("分群列名必须是非空字符串")
    _require_column(frame, segment, "分群列")
    if label is not None:
        if not isinstance(label, str) or label == "":
            raise ValueError("标签列名必须是非空字符串")
        _require_column(frame, label, "标签列")
    if not callable(runner):
        raise ValueError("分群执行函数必须可调用")

    buckets: Dict[Any, List[int]] = {}
    for position, raw in enumerate(frame[segment].tolist()):
        key = _group_key(raw)
        buckets.setdefault(key, []).append(position)

    ran: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    failed: List[Dict[str, Any]] = []
    for key in sorted(buckets, key=_sort_key):
        positions = buckets[key]
        part = frame.iloc[positions]
        rows = int(len(part))
        reasons: List[str] = []
        positive: Optional[int] = None
        negative: Optional[int] = None
        if rows < min_rows:
            reasons.append("行数不足")
        if label is not None:
            positive, negative = _class_counts(part[label])
            if positive < min_positive:
                reasons.append("坏样本不足")
            if negative < min_negative:
                reasons.append("好样本不足")
        if reasons:
            item: Dict[str, Any] = {"value": key, "rows": rows, "reason": reasons}
            if label is not None:
                item["positive"] = positive
                item["negative"] = negative
            skipped.append(item)
            continue
        try:
            result = runner(part.copy())
        except Exception as exc:
            failed.append(
                {
                    "value": key,
                    "rows": rows,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue
        ran.append({"value": key, "rows": rows, "result": result})

    return {
        "segment": segment,
        "ran": ran,
        "skipped": skipped,
        "failed": failed,
        "n_values": len(buckets),
    }


def _require_column(frame: pd.DataFrame, name: str, title: str) -> None:
    if name not in frame.columns:
        raise ValueError(f"{title}不存在：{name}")
    if isinstance(frame[name], pd.DataFrame):
        raise ValueError(f"{title}不唯一：{name}")


def _nonnegative_int(value: Any, message: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(message)
    return value


def _class_counts(series: pd.Series) -> tuple[int, int]:
    numeric = pd.to_numeric(series, errors="coerce")
    positive = int((numeric == 1).sum())
    negative = int((numeric == 0).sum())
    return positive, negative


def _group_key(value: Any) -> Any:
    if value is None or value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, np.generic):
        if isinstance(value, np.floating) and not np.isfinite(float(value)):
            return None
        return _group_key(value.item())
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return value
    if isinstance(value, int):
        return value
    try:
        hash(value)
    except TypeError:
        return str(value)
    return value


def _sort_key(value: Any) -> tuple:
    if value is None:
        return (2, 0.0, "")
    if isinstance(value, bool):
        return (1, 0.0, str(value))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return (0, float(value), "")
    return (1, 0.0, str(value))
