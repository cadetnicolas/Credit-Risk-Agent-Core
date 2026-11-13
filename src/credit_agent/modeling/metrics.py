"""Model metrics (from legacy ``m_evaluation.py``)."""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

import numpy as np
from sklearn.metrics import roc_auc_score, roc_curve


def ks_score(y_true: Sequence, y_pred: Sequence, *, n_bins: int = 10) -> Tuple[float, List[float]]:
    """Exact KS = max(TPR - FPR) over all thresholds; curve is per score decile."""
    y = np.asarray(y_true, dtype=float)
    p = np.asarray(y_pred, dtype=float)
    if len(y) != len(p) or len(y) < n_bins or len(np.unique(y)) < 2:
        return float("nan"), []
    fpr, tpr, _ = roc_curve(y, p)
    exact_ks = float(np.max(tpr - fpr))
    _, curve = _decile_ks_curve(y, p, n_bins=n_bins)
    return exact_ks, curve


def _decile_ks_curve(y: np.ndarray, p: np.ndarray, *, n_bins: int = 10) -> Tuple[float, List[float]]:
    order = np.argsort(-p)
    y = y[order]
    p = p[order]
    n = len(y)
    bin_size = max(1, n // n_bins)
    ks_curve: List[float] = []
    cum_bad = 0.0
    cum_good = 0.0
    tot_bad = max(float(y.sum()), 1.0)
    tot_good = max(float(len(y) - y.sum()), 1.0)
    max_ks = 0.0
    for i in range(0, n, bin_size):
        chunk = y[i : i + bin_size]
        b = float(chunk.sum())
        g = float(len(chunk) - chunk.sum())
        cum_bad += b
        cum_good += g
        ks_val = abs(cum_bad / tot_bad - cum_good / tot_good)
        ks_curve.append(float(ks_val))
        max_ks = max(max_ks, ks_val)
    return float(max_ks), ks_curve


def auc_score(y_true: Sequence, y_pred: Sequence) -> float:
    y = np.asarray(y_true, dtype=float)
    p = np.asarray(y_pred, dtype=float)
    if len(np.unique(y)) < 2:
        return float("nan")
    return float(roc_auc_score(y, p))


def decile_bad_rates(y_true: Sequence, y_pred: Sequence, *, n_bins: int = 10) -> Dict[str, Any]:
    """Bad rate per predicted-risk decile (1 = riskiest) and strict monotonicity."""
    y = np.asarray(y_true, dtype=float)
    p = np.asarray(y_pred, dtype=float)
    if len(y) != len(p) or len(y) < n_bins * 5:
        return {"rows": [], "monotonic": None}
    order = np.argsort(-p, kind="mergesort")
    rows: List[Dict[str, Any]] = []
    for i, chunk in enumerate(np.array_split(order, n_bins)):
        if not len(chunk):
            continue
        rows.append(
            {
                "decile": i + 1,
                "count": int(len(chunk)),
                "bad_rate": round(float(y[chunk].mean()), 6),
                "min_prob": round(float(p[chunk].min()), 6),
                "max_prob": round(float(p[chunk].max()), 6),
            }
        )
    # An adjacent inversion within ~2 standard errors is sampling noise on a
    # holdout of this size; only significant reversals fail the check.
    inversions: List[Dict[str, Any]] = []
    for a, b in zip(rows, rows[1:]):
        if b["bad_rate"] <= a["bad_rate"]:
            continue
        pooled = (a["bad_rate"] * a["count"] + b["bad_rate"] * b["count"]) / (a["count"] + b["count"])
        se = float(np.sqrt(max(pooled * (1 - pooled), 1e-12) * (1 / a["count"] + 1 / b["count"])))
        diff = b["bad_rate"] - a["bad_rate"]
        inversions.append(
            {"between": [a["decile"], b["decile"]], "diff": round(diff, 6), "z": round(diff / se, 3), "significant": bool(diff > 1.96 * se)}
        )
    return {
        "rows": rows,
        "monotonic": not any(i["significant"] for i in inversions),
        "strict_monotonic": not inversions,
        "inversions": inversions,
    }


def psi(expected: Sequence, actual: Sequence, *, n_bins: int = 10) -> float:
    """Population Stability Index on two score distributions."""
    e = np.asarray(expected, dtype=float)
    a = np.asarray(actual, dtype=float)
    e = e[np.isfinite(e)]
    a = a[np.isfinite(a)]
    if len(e) < n_bins or len(a) < n_bins:
        return float("nan")
    qs = np.linspace(0, 1, n_bins + 1)
    breaks = np.unique(np.quantile(e, qs))
    if len(breaks) < 3:
        return float("nan")
    # Fixed reference bins must include current values outside the development
    # range; otherwise the largest shifts disappear before normalisation.
    breaks[0], breaks[-1] = -np.inf, np.inf
    e_hist, _ = np.histogram(e, bins=breaks)
    a_hist, _ = np.histogram(a, bins=breaks)
    e_pct = e_hist / max(e_hist.sum(), 1)
    a_pct = a_hist / max(a_hist.sum(), 1)
    eps = 1e-6
    return float(np.sum((a_pct - e_pct) * np.log((a_pct + eps) / (e_pct + eps))))
