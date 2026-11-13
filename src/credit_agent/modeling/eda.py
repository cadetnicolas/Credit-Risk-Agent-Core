"""Exploratory summaries (numeric profile only, no warehouse SQL)."""

from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd


def column_profiles(df: pd.DataFrame, *, max_columns: int = 80) -> List[Dict[str, Any]]:
    """Per-column stats for sample / data-quality review."""
    rows: List[Dict[str, Any]] = []
    for col in list(df.columns)[:max_columns]:
        s = df[col]
        missing = float(s.isna().mean())
        entry: Dict[str, Any] = {
            "column": col,
            "dtype": str(s.dtype),
            "missing_rate": round(missing, 6),
            "nunique": int(s.nunique(dropna=True)),
        }
        if pd.api.types.is_numeric_dtype(s):
            v = s.dropna()
            if len(v):
                entry.update(
                    {
                        "mean": round(float(v.mean()), 6),
                        "std": round(float(v.std()), 6) if len(v) > 1 else 0.0,
                        "min": round(float(v.min()), 6),
                        "max": round(float(v.max()), 6),
                    }
                )
        else:
            top = s.value_counts(dropna=True).head(3)
            entry["top_values"] = {str(k): int(v) for k, v in top.items()}
        rows.append(entry)
    return rows


def enrich_sample_profile(body: Dict[str, Any], df: pd.DataFrame) -> Dict[str, Any]:
    out = dict(body)
    out["columns"] = column_profiles(df)
    out["numeric_column_count"] = int(
        sum(1 for c in df.columns if pd.api.types.is_numeric_dtype(df[c]))
    )
    if "bad_rate" in out and out["bad_rate"] is not None:
        out["bad_rate_pct"] = round(float(out["bad_rate"]) * 100, 4)
    return out
