"""Fit a logistic scorecard on the development split and score the holdout.

Stepwise selection, coefficient signs, and VIF use the development rows only.
Holdout probabilities reuse that fit. Nothing here refits on the full sample.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional, Sequence

import numpy as np
import pandas as pd

from .by_segment import run_by_segment
from .logistic import coefficient_signs, stepwise_logistic, vif_table
from .report_spec import ReportSpec, build_report
from .split import dev_test_split


def fit_scorecard(
    frame: pd.DataFrame,
    *,
    label: str,
    features: Sequence[Any],
    time_column: Optional[str] = None,
    id_column: Optional[str] = None,
    test_size: float = 0.3,
    random_state: int = 0,
    max_features: int = 20,
    report_metrics: Sequence[str] = ("ks", "auc", "bad_rate", "lift", "decile_bad_rate"),
    C: float = 1.0,
    class_weight: Any = None,
) -> Dict[str, Any]:
    """Select a logistic scorecard on the development sample and report both parts.

    Rows with a missing label, a non-numeric feature, or a missing time or id
    value are removed and counted in ``dropped_rows``. ``label`` values that
    remain must be 0/1. With ``time_column`` the split is out-of-time; without
    it, :func:`dev_test_split` is called with no time column. ``id_column`` is
    the entity column for that split.

    Probabilities are ``1 / (1 + exp(-(intercept + Xb)))`` using the development
    intercept and coefficients matched by feature name. Holdout ``psi`` uses the
    development probabilities as ``reference_prob`` only when the caller asked
    for ``psi``.
    """

    if not isinstance(frame, pd.DataFrame):
        raise ValueError("frame 必须是 DataFrame")
    names = _feature_names(features)
    if label not in frame.columns:
        raise ValueError(f"数据中缺少目标变量：{label}")
    missing_columns = [name for name in names if name not in frame.columns]
    if missing_columns:
        raise ValueError("数据中缺少特征：" + "、".join(str(name) for name in missing_columns))
    metrics = ReportSpec(report_metrics).metrics

    missing = _missing_rows(frame, names, label, time_column, id_column)
    dropped_rows = int(missing.sum())
    work = frame.iloc[np.flatnonzero(~missing)].reset_index(drop=True)
    y = _binary_labels(work[label]) if len(work) else np.array([], dtype=int)

    split_kwargs: Dict[str, Any] = {"test_size": test_size, "random_state": random_state}
    if time_column:
        split_kwargs["time_column"] = time_column
    if id_column:
        split_kwargs["group_column"] = id_column
    split = dev_test_split(work, y, **split_kwargs)

    dev = work.iloc[np.flatnonzero(split.dev_mask)].reset_index(drop=True)
    holdout = work.iloc[np.flatnonzero(split.test_mask)].reset_index(drop=True)
    y_dev = y[split.dev_mask]
    y_holdout = y[split.test_mask]
    chosen = stepwise_logistic(
        dev,
        y_dev,
        names,
        direction="both",
        max_features=max_features,
        C=C,
        class_weight=class_weight,
        random_state=random_state,
    )
    fitted = chosen["final"]
    selected = list(chosen["selected"])
    coefficients = [
        {"feature": row["feature"], "coef": float(row["coef"])} for row in fitted["coefficients"]
    ]
    intercept = float(fitted["intercept"])
    signs = coefficient_signs(coefficients, expected_sign="positive")
    vif = vif_table(dev, selected)
    dev_prob = _positive_probability(dev, selected, coefficients, intercept)
    holdout_prob = _positive_probability(holdout, selected, coefficients, intercept)
    dev_report = build_report(y_dev, dev_prob, ReportSpec(metrics, sample_name="dev"))
    if "psi" in metrics:
        holdout_report = build_report(
            y_holdout,
            holdout_prob,
            ReportSpec(metrics, sample_name="holdout"),
            reference_prob=dev_prob,
        )
    else:
        holdout_report = build_report(
            y_holdout,
            holdout_prob,
            ReportSpec(metrics, sample_name="holdout"),
        )
    return {
        "selected": selected,
        "coefficients": coefficients,
        "intercept": intercept,
        "signs": signs,
        "vif": vif,
        "split_method": split.method,
        "split_description": split.description,
        "dropped_rows": dropped_rows,
        "dev_report": dev_report,
        "holdout_report": holdout_report,
        "aic": float(fitted["aic"]),
    }


def fit_scorecard_by_segment(
    frame: pd.DataFrame,
    *,
    segment: str,
    label: str,
    features: Sequence[Any],
    min_rows: int = 50,
    min_positive: int = 10,
    min_negative: int = 10,
    **scorecard_kwargs: Any,
) -> Dict[str, Any]:
    """Run :func:`fit_scorecard` on each value of ``segment``.

    The segment column is not a feature. Slices below the row or class floors
    are skipped by :func:`run_by_segment` and do not call the scorecard fit.
    """

    names = _feature_names(features)
    if segment in names:
        raise ValueError("分群列不能进入特征")

    def runner(part: pd.DataFrame) -> Dict[str, Any]:
        return fit_scorecard(part, label=label, features=names, **scorecard_kwargs)

    return run_by_segment(
        frame,
        segment,
        runner,
        min_rows=min_rows,
        min_positive=min_positive,
        min_negative=min_negative,
        label=label,
    )


def _feature_names(features: Sequence[Any]) -> list:
    if isinstance(features, (str, bytes)) or features is None:
        raise ValueError("features 必须是特征名列表")
    try:
        names = list(features)
    except TypeError as exc:
        raise ValueError("features 必须是特征名列表") from exc
    if not names:
        raise ValueError("特征列表不能为空")
    if len(set(names)) != len(names):
        raise ValueError("特征列表存在重复列名")
    return names


def _missing_rows(
    frame: pd.DataFrame,
    names: Sequence[Any],
    label: str,
    time_column: Optional[str],
    id_column: Optional[str],
) -> np.ndarray:
    missing = frame[label].isna().to_numpy(dtype=bool)
    parsed = frame.loc[:, list(names)].apply(pd.to_numeric, errors="coerce")
    values = parsed.to_numpy(dtype=float)
    missing = missing | ~np.isfinite(values).all(axis=1)
    if time_column and time_column in frame.columns:
        missing = missing | frame[time_column].isna().to_numpy(dtype=bool)
    if id_column and id_column in frame.columns:
        missing = missing | frame[id_column].isna().to_numpy(dtype=bool)
    return missing


def _binary_labels(series: pd.Series) -> np.ndarray:
    numeric = pd.to_numeric(series, errors="coerce")
    values = numeric.to_numpy(dtype=float)
    if values.size == 0:
        return np.array([], dtype=int)
    whole = np.isfinite(values) & np.isclose(values, np.rint(values), rtol=0.0, atol=1e-8)
    if not whole.all():
        raise ValueError("目标变量必须是 0/1 两类")
    labels = np.rint(values).astype(int)
    if not np.isin(labels, [0, 1]).all():
        raise ValueError("目标变量必须是 0/1 两类")
    return labels


def _positive_probability(
    part: pd.DataFrame,
    names: Sequence[Any],
    coefficients: Sequence[Mapping[str, Any]],
    intercept: float,
) -> np.ndarray:
    weight = {row["feature"]: float(row["coef"]) for row in coefficients}
    ordered = list(names)
    beta = np.array([weight[name] for name in ordered], dtype=float)
    matrix = part.loc[:, ordered].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
    linear = float(intercept) + matrix @ beta
    with np.errstate(over="ignore"):
        return 1.0 / (1.0 + np.exp(-linear))
