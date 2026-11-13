"""Preprocessing helpers (from legacy ``f_preprocessing`` / ``f_feature_selection``)."""

from __future__ import annotations

from typing import List, Sequence, Tuple

import numpy as np
import pandas as pd


def drop_high_missing(columns: Sequence[str], df: pd.DataFrame, max_missing_rate: float = 0.95) -> List[str]:
    keep: List[str] = []
    n = len(df)
    if n == 0:
        return list(columns)
    for c in columns:
        if c not in df.columns:
            continue
        rate = float(df[c].isna().sum()) / n
        if rate < max_missing_rate:
            keep.append(c)
    return keep


def primary_value_ratio_filter(columns: Sequence[str], df: pd.DataFrame, *, limit: float = 0.99) -> List[str]:
    """Drop features whose mode share exceeds ``limit`` (legacy ``select_primaryvalue_ratio``)."""
    n = len(df)
    if n == 0:
        return list(columns)
    keep: List[str] = []
    for c in columns:
        if c not in df.columns:
            continue
        top = df[c].fillna("__NA__").value_counts().iloc[0]
        if float(top) / n < limit:
            keep.append(c)
    return keep


def fill_numeric_median(df: pd.DataFrame, columns: Sequence[str]) -> pd.DataFrame:
    out = df[list(columns)].copy()
    for c in columns:
        med = out[c].median()
        out[c] = out[c].fillna(med)
    return out
