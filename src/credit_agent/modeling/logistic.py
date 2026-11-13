"""Configurable logistic scorecard helpers.

Pure functions that return dictionaries. They do not plot, write spreadsheets,
or print. Discrimination summaries call
:func:`credit_agent.modeling.metrics.ks_score`,
:func:`credit_agent.modeling.metrics.auc_score`, and
:func:`credit_agent.modeling.metrics.decile_bad_rates`.
Probability-to-score and WOE point tables stay in
:mod:`credit_agent.modeling.scoring` (``prob_to_score``,
``scorecard_points_from_lr``); coefficient rows from :func:`fit_logistic`
are the ``coefficients`` argument those helpers already accept.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
from sklearn.linear_model import LinearRegression, LogisticRegression

from credit_agent.modeling.metrics import auc_score, decile_bad_rates, ks_score

_AIC_TOLERANCE = 1e-6
_LLF_CLIP = 1e-15
_VIF_R2_LIMIT = 1.0 - 1e-8
_DECILE_FIELDS = ("bin", "n", "bad", "bad_rate", "cum_bad_capture", "ks")
_DIRECTIONS = ("forward", "backward", "both")
_SIGNS = ("positive", "negative", "any")
_FIT_MAX_ITER = 2000


def fit_logistic(
    frame: pd.DataFrame,
    y: Any,
    features: Sequence[Any],
    *,
    C: float = 1.0,
    penalty: str = "l2",
    class_weight: Any = None,
    max_iter: int = _FIT_MAX_ITER,
    random_state: int = 0,
) -> Dict[str, Any]:
    """Fit an L2 logistic regression and return coefficients plus AIC.

    ``AIC = 2 * k - 2 * llf``. ``k`` counts every feature and the intercept.
    ``llf`` is the Bernoulli log-likelihood of the fitted probabilities, so the
    penalty changes the fit but is not added back into ``llf``.

    Missing values are rejected. Impute before calling. ``penalty`` accepts
    only ``l2``. Coefficients are sorted by absolute value, descending.
    """
    if penalty != "l2":
        raise ValueError("penalty 只接受 l2")
    _require_positive_c(C)
    _require_max_iter(max_iter)
    matrix, target, names = _prepare_xy(frame, y, features)
    return _fit_arrays(
        matrix,
        target,
        names,
        C=C,
        class_weight=class_weight,
        max_iter=max_iter,
        random_state=random_state,
    )


def stepwise_logistic(
    frame: pd.DataFrame,
    y: Any,
    features: Sequence[Any],
    *,
    direction: str = "both",
    criterion: str = "aic",
    max_features: int = 20,
    min_features: int = 1,
    C: float = 1.0,
    class_weight: Any = None,
    random_state: int = 0,
) -> Dict[str, Any]:
    """Select features by AIC and refit the chosen logistic model.

    ``direction`` is ``forward``, ``backward``, or ``both``. ``both`` runs
    forward selection, then backward elimination on that subset. A feature is
    added or removed only when the step strictly lowers AIC. ``max_features``
    caps the set: forward stops at the cap, and backward drops the removal
    with the lowest AIC while the set is still above the cap. The search never
    keeps fewer than ``min_features``.

    ``path`` records ``features`` and ``aic`` at the start and after every
    accepted step. ``final`` is the :func:`fit_logistic` result on ``selected``.
    """
    if direction not in _DIRECTIONS:
        raise ValueError("direction 只接受 forward、backward、both")
    if criterion != "aic":
        raise ValueError("criterion 只接受 aic")
    _require_feature_bounds(min_features, max_features)
    _require_positive_c(C)
    matrix, target, names = _prepare_xy(frame, y, features)
    if len(target) < len(names) + 2:
        raise ValueError("样本量不能少于特征数加 2")

    fit_kwargs = {
        "C": C,
        "class_weight": class_weight,
        "max_iter": _FIT_MAX_ITER,
        "random_state": random_state,
    }
    if direction == "forward":
        selected, path = _forward(matrix, target, names, max_features=max_features, **fit_kwargs)
    elif direction == "backward":
        selected, path = _backward(
            matrix,
            target,
            names,
            names,
            min_features=min_features,
            max_features=max_features,
            record_start=True,
            **fit_kwargs,
        )
    else:
        selected, path = _forward(matrix, target, names, max_features=max_features, **fit_kwargs)
        selected, removed = _backward(
            matrix,
            target,
            names,
            selected,
            min_features=min_features,
            max_features=max_features,
            record_start=False,
            **fit_kwargs,
        )
        path.extend(removed)
    if len(selected) < min_features:
        raise ValueError("逐步回归选中的特征少于最少特征数")
    final = fit_logistic(
        frame,
        target,
        selected,
        C=C,
        penalty="l2",
        class_weight=class_weight,
        max_iter=_FIT_MAX_ITER,
        random_state=random_state,
    )
    return {"selected": list(selected), "path": path, "final": final}


def coefficient_signs(
    coefficients: Sequence[Mapping[str, Any]],
    *,
    expected_sign: str = "positive",
) -> Dict[str, Any]:
    """List coefficients whose sign disagrees with ``expected_sign``.

    ``expected_sign`` is ``positive``, ``negative``, or ``any``. The input is
    not filtered and no feature is dropped. Zero matches neither ``positive``
    nor ``negative``.
    """
    if expected_sign not in _SIGNS:
        raise ValueError("expected_sign 只接受 positive、negative、any")
    rows = _coefficient_rows(coefficients)
    violations: List[Dict[str, Any]] = []
    if expected_sign != "any":
        for row in rows:
            sign = _sign_label(row["coef"])
            disagrees = (expected_sign == "positive" and row["coef"] <= 0) or (
                expected_sign == "negative" and row["coef"] >= 0
            )
            if disagrees:
                violations.append({"feature": row["feature"], "coef": row["coef"], "sign": sign})
    return {
        "expected_sign": expected_sign,
        "violations": violations,
        "n_violations": len(violations),
    }


def vif_table(frame: pd.DataFrame, features: Sequence[Any]) -> Dict[str, Any]:
    """Variance inflation factors from an auxiliary linear regression.

    Each feature is regressed on the others. ``VIF = 1 / (1 - R^2)``. When
    ``R^2`` is numerically 1, ``vif`` is ``None`` and ``perfect_collinear``
    is true. Fewer than two features raises ``ValueError``.
    """
    matrix, names = _prepare_features(frame, features)
    if len(names) < 2:
        raise ValueError("计算 VIF 至少需要 2 个特征")
    if len(matrix) <= len(names):
        raise ValueError("样本量必须大于特征数才能计算 VIF")
    rows: List[Dict[str, Any]] = []
    for index, name in enumerate(names):
        target = matrix[:, index]
        others = np.delete(matrix, index, axis=1)
        r2 = float(LinearRegression().fit(others, target).score(others, target))
        if not np.isfinite(r2) or r2 >= _VIF_R2_LIMIT:
            rows.append(
                {
                    "feature": name,
                    "r2": 1.0 if np.isfinite(r2) else None,
                    "vif": None,
                    "perfect_collinear": True,
                }
            )
        else:
            rows.append(
                {
                    "feature": name,
                    "r2": r2,
                    "vif": float(1.0 / (1.0 - r2)),
                    "perfect_collinear": False,
                }
            )
    return {"rows": rows}


def decile_table(
    y: Sequence[Any],
    prob: Sequence[Any],
    *,
    n_bins: int = 10,
    higher_score_safer: bool = False,
    fields: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    """Bad-rate table with bin 1 as the riskiest group.

    Default columns are ``bin``, ``n``, ``bad``, ``bad_rate``,
    ``cum_bad_capture``, and ``ks``. ``fields`` keeps a subset, in the order
    given. Higher ``prob`` is riskier. When ``higher_score_safer`` is true the
    values are negated before ranking, so a higher input score still lands in
    a later, safer bin.

    Top-level ``auc`` and ``ks`` come from the shared metric helpers on that
    same risk ranking. Each row's ``ks`` is the cumulative decile KS.
    ``monotonic`` comes from :func:`decile_bad_rates`.
    """
    chosen = _resolve_fields(fields)
    if isinstance(n_bins, bool) or not isinstance(n_bins, int) or n_bins < 2:
        raise ValueError("分箱数至少为 2")
    target = _as_binary(_as_1d(y, "目标变量必须是 0/1 两类"))
    score = _as_float_vector(prob, "预测概率存在缺失值")
    if len(score) != len(target):
        raise ValueError("目标变量与预测概率长度不一致")
    if len(target) < n_bins:
        raise ValueError("样本量不足以分成指定箱数")
    risk = -score if higher_score_safer else score
    order = np.argsort(-risk, kind="mergesort")
    rows = _decile_rows(target, order, n_bins)
    exact_ks, _curve = ks_score(target, risk, n_bins=n_bins)
    diagnostics = decile_bad_rates(target, risk, n_bins=n_bins)
    return {
        "rows": [{key: row[key] for key in chosen} for row in rows],
        "n_bins": n_bins,
        "fields": chosen,
        "higher_score_safer": bool(higher_score_safer),
        "auc": _finite_float(auc_score(target, risk)),
        "ks": _finite_float(exact_ks),
        "monotonic": diagnostics.get("monotonic"),
    }


def _fit_arrays(
    matrix: np.ndarray,
    target: np.ndarray,
    names: Sequence[Any],
    *,
    C: float,
    class_weight: Any,
    max_iter: int,
    random_state: int,
) -> Dict[str, Any]:
    model = LogisticRegression(
        C=float(C),
        class_weight=class_weight,
        max_iter=max_iter,
        random_state=random_state,
        solver="lbfgs",
    )
    model.fit(matrix, target)
    probability = _positive_probability(model, matrix)
    log_likelihood = _log_likelihood(target, probability)
    coefficients = [
        {"feature": name, "coef": float(value)}
        for name, value in zip(names, np.asarray(model.coef_, dtype=float).ravel())
    ]
    coefficients.sort(key=lambda row: (-abs(row["coef"]), str(row["feature"])))
    return {
        "coefficients": coefficients,
        "intercept": float(np.asarray(model.intercept_, dtype=float).ravel()[0]),
        "n": int(len(target)),
        "pos": int(np.sum(target == 1)),
        "aic": float(2.0 * (len(names) + 1) - 2.0 * log_likelihood),
        "log_likelihood": float(log_likelihood),
    }


def _forward(
    matrix: np.ndarray,
    target: np.ndarray,
    names: Sequence[Any],
    *,
    max_features: int,
    C: float,
    class_weight: Any,
    max_iter: int,
    random_state: int,
) -> Tuple[List[Any], List[Dict[str, Any]]]:
    selected: List[Any] = []
    remaining = list(names)
    current = _subset_aic(
        matrix,
        target,
        names,
        selected,
        C=C,
        class_weight=class_weight,
        max_iter=max_iter,
        random_state=random_state,
    )
    path = [{"features": [], "aic": float(current)}]
    while remaining and len(selected) < max_features:
        best_name: Optional[Any] = None
        best_aic: Optional[float] = None
        for name in remaining:
            aic = _subset_aic(
                matrix,
                target,
                names,
                selected + [name],
                C=C,
                class_weight=class_weight,
                max_iter=max_iter,
                random_state=random_state,
            )
            if _prefer(aic, name, best_aic, best_name):
                best_name = name
                best_aic = aic
        if best_name is None or best_aic is None or best_aic >= current - _AIC_TOLERANCE:
            break
        selected.append(best_name)
        remaining.remove(best_name)
        current = best_aic
        path.append({"features": list(selected), "aic": float(current)})
    return selected, path


def _backward(
    matrix: np.ndarray,
    target: np.ndarray,
    names: Sequence[Any],
    initial: Sequence[Any],
    *,
    min_features: int,
    max_features: int,
    record_start: bool,
    C: float,
    class_weight: Any,
    max_iter: int,
    random_state: int,
) -> Tuple[List[Any], List[Dict[str, Any]]]:
    selected = list(initial)
    current = _subset_aic(
        matrix,
        target,
        names,
        selected,
        C=C,
        class_weight=class_weight,
        max_iter=max_iter,
        random_state=random_state,
    )
    path: List[Dict[str, Any]] = []
    if record_start:
        path.append({"features": list(selected), "aic": float(current)})
    while len(selected) > min_features:
        force_drop = len(selected) > max_features
        best_name: Optional[Any] = None
        best_aic: Optional[float] = None
        for name in selected:
            trial = [item for item in selected if item != name]
            aic = _subset_aic(
                matrix,
                target,
                names,
                trial,
                C=C,
                class_weight=class_weight,
                max_iter=max_iter,
                random_state=random_state,
            )
            if _prefer(aic, name, best_aic, best_name):
                best_name = name
                best_aic = aic
        if best_name is None or best_aic is None or (not force_drop and best_aic >= current - _AIC_TOLERANCE):
            break
        selected = [item for item in selected if item != best_name]
        current = best_aic
        path.append({"features": list(selected), "aic": float(current)})
    return selected, path


def _prefer(aic: float, name: Any, best_aic: Optional[float], best_name: Optional[Any]) -> bool:
    if best_aic is None or best_name is None:
        return True
    if aic < best_aic - _AIC_TOLERANCE:
        return True
    return abs(aic - best_aic) <= _AIC_TOLERANCE and str(name) < str(best_name)


def _subset_aic(
    matrix: np.ndarray,
    target: np.ndarray,
    names: Sequence[Any],
    subset: Sequence[Any],
    *,
    C: float,
    class_weight: Any,
    max_iter: int,
    random_state: int,
) -> float:
    chosen = list(subset)
    if not chosen:
        probability = _null_probabilities(target, class_weight)
        return float(2.0 - 2.0 * _log_likelihood(target, probability))
    index = {name: position for position, name in enumerate(names)}
    columns = matrix[:, [index[name] for name in chosen]]
    fitted = _fit_arrays(
        columns,
        target,
        chosen,
        C=C,
        class_weight=class_weight,
        max_iter=max_iter,
        random_state=random_state,
    )
    return float(fitted["aic"])


def _prepare_xy(frame: pd.DataFrame, y: Any, features: Sequence[Any]) -> Tuple[np.ndarray, np.ndarray, List[Any]]:
    matrix, names = _prepare_features(frame, features)
    if isinstance(y, str):
        if y not in frame.columns:
            raise ValueError(f"数据中缺少目标变量：{y}")
        raw = frame[y].to_numpy()
    else:
        raw = _as_1d(y, "目标变量必须是 0/1 两类")
        if len(raw) != len(frame):
            raise ValueError("目标变量与样本行数不一致")
    return matrix, _as_binary(raw), names


def _prepare_features(frame: pd.DataFrame, features: Sequence[Any]) -> Tuple[np.ndarray, List[Any]]:
    if not isinstance(frame, pd.DataFrame):
        raise ValueError("frame 必须是 DataFrame")
    names = _feature_names(features)
    missing = [name for name in names if name not in frame.columns]
    if missing:
        raise ValueError("数据中缺少特征：" + "、".join(str(name) for name in missing))
    values = frame.loc[:, names].apply(pd.to_numeric, errors="coerce").to_numpy(dtype=float)
    if values.ndim != 2 or not np.isfinite(values).all():
        raise ValueError("特征存在缺失值，请先填补后再训练")
    return values, names


def _feature_names(features: Sequence[Any]) -> List[Any]:
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


def _as_binary(raw: Any) -> np.ndarray:
    vector = _as_1d(raw, "目标变量必须是 0/1 两类")
    try:
        values = np.asarray(vector, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError("目标变量必须是 0/1 两类") from exc
    if not np.isfinite(values).all():
        raise ValueError("目标变量存在缺失值，请先处理后再训练")
    if not np.all(np.isclose(values, np.rint(values), rtol=0.0, atol=1e-8)):
        raise ValueError("目标变量必须是 0/1 两类")
    labels = np.rint(values).astype(int)
    if set(np.unique(labels).tolist()) != {0, 1}:
        raise ValueError("目标变量必须是 0/1 两类")
    return labels


def _as_1d(values: Any, message: str) -> np.ndarray:
    if isinstance(values, pd.Series):
        array = values.to_numpy()
    else:
        array = np.asarray(values)
    if array.ndim == 2 and 1 in array.shape:
        array = array.reshape(-1)
    if array.ndim != 1:
        raise ValueError(message)
    return array


def _as_float_vector(values: Any, missing_message: str) -> np.ndarray:
    vector = _as_1d(values, missing_message)
    try:
        array = np.asarray(vector, dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError(missing_message) from exc
    if not np.isfinite(array).all():
        raise ValueError(missing_message)
    return array


def _coefficient_rows(coefficients: Sequence[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    if isinstance(coefficients, Mapping) or isinstance(coefficients, (str, bytes)):
        raise ValueError("coefficients 必须是包含 feature 和 coef 的列表")
    try:
        rows = list(coefficients)
    except TypeError as exc:
        raise ValueError("coefficients 必须是包含 feature 和 coef 的列表") from exc
    parsed: List[Dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, Mapping) or "feature" not in row or "coef" not in row:
            raise ValueError("coefficients 必须是包含 feature 和 coef 的列表")
        try:
            coef = float(row["coef"])
        except (TypeError, ValueError) as exc:
            raise ValueError("coefficients 必须是包含 feature 和 coef 的列表") from exc
        if not np.isfinite(coef):
            raise ValueError("coefficients 必须是包含 feature 和 coef 的列表")
        parsed.append({"feature": row["feature"], "coef": coef})
    return parsed


def _resolve_fields(fields: Optional[Sequence[str]]) -> List[str]:
    if fields is None:
        return list(_DECILE_FIELDS)
    if isinstance(fields, (str, bytes)):
        raise ValueError("fields 必须是列名列表")
    try:
        chosen = list(fields)
    except TypeError as exc:
        raise ValueError("fields 必须是列名列表") from exc
    if not chosen:
        raise ValueError("fields 不能为空")
    unknown = [name for name in chosen if name not in _DECILE_FIELDS]
    if unknown:
        raise ValueError("fields 含有不支持的列：" + "、".join(str(name) for name in unknown))
    if len(set(chosen)) != len(chosen):
        raise ValueError("fields 含有重复列名")
    return [str(name) for name in chosen]


def _decile_rows(target: np.ndarray, order: np.ndarray, n_bins: int) -> List[Dict[str, Any]]:
    chunks = [chunk for chunk in np.array_split(order, n_bins) if len(chunk)]
    total_bad = float(target.sum())
    total_good = float(len(target) - total_bad)
    cum_bad = 0.0
    cum_good = 0.0
    rows: List[Dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        bad = float(target[chunk].sum())
        good = float(len(chunk) - bad)
        cum_bad += bad
        cum_good += good
        rows.append(
            {
                "bin": index + 1,
                "n": int(len(chunk)),
                "bad": int(bad),
                "bad_rate": bad / float(len(chunk)),
                "cum_bad_capture": cum_bad / total_bad,
                "ks": abs(cum_bad / total_bad - cum_good / total_good),
            }
        )
    return rows


def _positive_probability(model: LogisticRegression, matrix: np.ndarray) -> np.ndarray:
    classes = [int(label) for label in model.classes_]
    if 1 not in classes:
        raise ValueError("目标变量必须是 0/1 两类")
    return np.asarray(model.predict_proba(matrix)[:, classes.index(1)], dtype=float)


def _log_likelihood(target: np.ndarray, probability: np.ndarray) -> float:
    clipped = np.clip(probability, _LLF_CLIP, 1.0 - _LLF_CLIP)
    labels = np.asarray(target, dtype=float)
    return float(np.sum(labels * np.log(clipped) + (1.0 - labels) * np.log(1.0 - clipped)))


def _null_probabilities(target: np.ndarray, class_weight: Any) -> np.ndarray:
    weights = _class_weights(target, class_weight)
    rate = float(np.sum(weights * target) / np.sum(weights))
    rate = float(np.clip(rate, _LLF_CLIP, 1.0 - _LLF_CLIP))
    return np.full(len(target), rate, dtype=float)


def _class_weights(target: np.ndarray, class_weight: Any) -> np.ndarray:
    if class_weight is None:
        return np.ones(len(target), dtype=float)
    n_pos = int(np.sum(target == 1))
    n_neg = int(len(target) - n_pos)
    if class_weight == "balanced":
        return np.where(target == 1, len(target) / (2.0 * n_pos), len(target) / (2.0 * n_neg)).astype(float)
    if isinstance(class_weight, Mapping):
        if 0 not in class_weight or 1 not in class_weight:
            raise ValueError("class_weight 只接受 None、balanced 或按类别给出的字典")
        try:
            weight_0 = float(class_weight[0])
            weight_1 = float(class_weight[1])
        except (TypeError, ValueError) as exc:
            raise ValueError("class_weight 只接受 None、balanced 或按类别给出的字典") from exc
        if not np.isfinite(weight_0) or not np.isfinite(weight_1) or weight_0 <= 0 or weight_1 <= 0:
            raise ValueError("class_weight 只接受 None、balanced 或按类别给出的字典")
        return np.where(target == 1, weight_1, weight_0).astype(float)
    raise ValueError("class_weight 只接受 None、balanced 或按类别给出的字典")


def _sign_label(coef: float) -> str:
    if coef > 0:
        return "positive"
    if coef < 0:
        return "negative"
    return "zero"


def _require_positive_c(value: float) -> None:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("C 必须是正数") from exc
    if not np.isfinite(number) or number <= 0:
        raise ValueError("C 必须是正数")


def _require_max_iter(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("max_iter 必须是正整数")


def _require_feature_bounds(min_features: int, max_features: int) -> None:
    if isinstance(min_features, bool) or not isinstance(min_features, int) or min_features < 1:
        raise ValueError("min_features 至少为 1")
    if isinstance(max_features, bool) or not isinstance(max_features, int) or max_features < min_features:
        raise ValueError("max_features 不能小于 min_features")


def _finite_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not np.isfinite(number):
        return None
    return number
