"""Per-feature PSI between reference and current samples."""

from __future__ import annotations

from typing import Dict, List, Sequence, Tuple

import numpy as np
import pandas as pd

from .metrics import psi


def reference_current_split(
    n_rows: int,
    *,
    reference_fraction: float = 0.7,
    random_state: int = 2019,
) -> Tuple[np.ndarray, np.ndarray]:
    """Return boolean masks (ref, cur) for row-wise PSI."""
    rng = np.random.default_rng(random_state)
    idx = np.arange(n_rows)
    rng.shuffle(idx)
    cut = max(1, int(n_rows * reference_fraction))
    ref_idx = np.zeros(n_rows, dtype=bool)
    cur_idx = np.zeros(n_rows, dtype=bool)
    ref_idx[idx[:cut]] = True
    cur_idx[idx[cut:]] = True
    if not cur_idx.any():
        cur_idx[-1] = True
        ref_idx[-1] = False
    return ref_idx, cur_idx


def feature_psi_value(ref: pd.Series, cur: pd.Series, *, n_bins: int = 10) -> float:
    r = pd.to_numeric(ref, errors="coerce")
    c = pd.to_numeric(cur, errors="coerce")
    r = r[np.isfinite(r)]
    c = c[np.isfinite(c)]
    if len(r) < n_bins or len(c) < n_bins:
        return float("nan")
    return psi(r, c, n_bins=n_bins)


def filter_by_feature_psi(
    df: pd.DataFrame,
    features: Sequence[str],
    *,
    max_psi: float,
    reference_fraction: float = 0.7,
    random_state: int = 2019,
) -> Tuple[List[str], List[Dict[str, float]]]:
    ref_m, cur_m = reference_current_split(len(df), reference_fraction=reference_fraction, random_state=random_state)
    kept: List[str] = []
    table: List[Dict[str, float]] = []
    for f in features:
        if f not in df.columns:
            continue
        val = feature_psi_value(df.loc[ref_m, f], df.loc[cur_m, f])
        row = {"feature": f, "psi": round(float(val), 6) if np.isfinite(val) else None}
        table.append(row)
        if np.isfinite(val) and val <= max_psi:
            kept.append(f)
        elif not np.isfinite(val):
            kept.append(f)
    table.sort(key=lambda r: -(r.get("psi") or 0))
    return kept, table
