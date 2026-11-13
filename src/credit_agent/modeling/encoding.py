"""Categorical feature encoding (label-count style, legacy ``var_dict`` compatible)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd


def detect_categorical_columns(df: pd.DataFrame, columns: Sequence[str], *, max_cardinality: int = 50) -> List[str]:
    out: List[str] = []
    for c in columns:
        if c not in df.columns:
            continue
        s = df[c]
        if pd.api.types.is_numeric_dtype(s) and s.nunique(dropna=True) > max_cardinality:
            continue
        if pd.api.types.is_numeric_dtype(s) and s.nunique(dropna=True) <= 5:
            out.append(c)
        elif (
            s.dtype == object
            or pd.api.types.is_string_dtype(s)
            or pd.api.types.is_bool_dtype(s)
            or isinstance(s.dtype, pd.CategoricalDtype)
        ):
            if s.nunique(dropna=True) <= max_cardinality:
                out.append(c)
    return out


@dataclass
class CategoricalEncoder:
    """Map category -> integer (1..n); unknown -> 0."""

    maps: Dict[str, Dict[str, int]] = field(default_factory=dict)
    categorical_columns: List[str] = field(default_factory=list)

    def fit(
        self,
        df: pd.DataFrame,
        columns: Sequence[str],
        *,
        categorical_columns: Optional[Sequence[str]] = None,
    ) -> "CategoricalEncoder":
        if categorical_columns is not None:
            self.categorical_columns = [c for c in categorical_columns if c in columns and c in df.columns]
        else:
            self.categorical_columns = [c for c in columns if c in detect_categorical_columns(df, [c])]
        self.maps = {}
        for c in self.categorical_columns:
            vals = df[c].astype(str).replace("nan", np.nan).dropna()
            counts = vals.value_counts()
            self.maps[c] = {str(k): int(i + 1) for i, k in enumerate(counts.index)}
        return self

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        for c in self.categorical_columns:
            if c not in out.columns:
                continue
            m = self.maps.get(c, {})
            out[c] = out[c].astype(str).replace("nan", np.nan).map(lambda x: m.get(str(x), 0) if pd.notna(x) else 0)
            out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0)
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {"categorical_columns": list(self.categorical_columns), "maps": self.maps}

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "CategoricalEncoder":
        enc = cls()
        enc.categorical_columns = list(raw.get("categorical_columns") or [])
        enc.maps = {k: dict(v) for k, v in (raw.get("maps") or {}).items()}
        return enc
