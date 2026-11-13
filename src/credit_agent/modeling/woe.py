"""WOE / IV binning for numeric and categorical features."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

# 缺失单独成箱的标签。缺失在本数据里是「无该渠道记录 / 从未逾期」这类业务事实，
# 不是未知值，必须独立成箱而不是被剔除或并入均值。
MISSING_BIN = "MISSING"


def _woe_iv_from_counts(bad: np.ndarray, good: np.ndarray) -> Tuple[np.ndarray, float]:
    tot_bad = max(float(bad.sum()), 0.5)
    tot_good = max(float(good.sum()), 0.5)
    bad_dist = (bad + 0.5) / tot_bad
    good_dist = (good + 0.5) / tot_good
    woe = np.log(bad_dist / good_dist)
    iv = float(((bad_dist - good_dist) * woe).sum())
    return woe, iv


def numeric_profile(
    series: pd.Series,
    target: pd.Series,
    *,
    n_bins: int = 10,
    binning_method: str = "quantile",
    monotonic_direction: str = "increasing",
    min_bin_fraction: float = 0.05,
    max_prebins: int = 20,
) -> Optional[Dict[str, Any]]:
    """单变量分箱档（任意分箱方法）：逐箱明细 + IV / KS / 单调性 + 分箱留痕。

    缺失一律独立成箱并计入 IV —— 把缺失整行剔除会让高缺失特征的 IV 只在少数
    有值样本上计算，样本一小 IV 就会被推到不合理的高位。
    """

    from .binning import (
        bin_table,
        chimerge_profile,
        mapped_ks,
        normalize_method,
        numeric_bin_edges,
        summarize_bins,
    )

    method = normalize_method(binning_method)
    x = pd.to_numeric(series, errors="coerce")
    y = pd.to_numeric(target, errors="coerce")
    valid = x.notna() & y.notna()
    if int(valid.sum()) < 20 or y[valid].nunique() < 2:
        return None

    extra: Dict[str, Any] = {}
    if method == "chi2":
        result = chimerge_profile(
            x,
            y,
            max_bins=n_bins,
            min_bin_fraction=min_bin_fraction,
            max_prebins=max_prebins,
            missing_label=MISSING_BIN,
        )
        if result is None:
            return None
        edges, bins = result.edges, result.bins
        summary = summarize_bins(result.bins, ks=result.ks)
        extra = {
            "prebins": result.prebins,
            "n_valid": result.n_valid,
            "min_bin_size": result.min_bin_size,
            "min_bin_fraction_scope": "valid_only",
            "min_bin_violations": result.min_bin_violations,
            "merge_log": result.merge_log,
        }
    else:
        edges = numeric_bin_edges(
            x,
            y,
            method=method,  # type: ignore[arg-type]
            max_bins=n_bins,
            min_bin_fraction=min_bin_fraction,
            monotonic_direction=(
                monotonic_direction if monotonic_direction in ("increasing", "decreasing") else "increasing"
            ),  # type: ignore[arg-type]
            max_prebins=max_prebins,
        )
        bins = bin_table(x, y, edges, missing_label=MISSING_BIN)
        if not bins:
            return None
        summary = summarize_bins(bins, ks=mapped_ks(x, y, edges, bins))

    entry: Dict[str, Any] = {
        "kind": "numeric",
        "iv": summary["iv"],
        "bins": bins,
        "bin_edges": [float(e) for e in edges],
        "binning_method": method,
        "ks": summary["ks"],
        "monotonic": summary["monotonic"],
        "monotonic_direction": summary["monotonic_direction"],
        "monotonic_strength": summary["monotonic_strength"],
        "n_valid": int(valid.sum()),
        "missing_rate": round(float(series.isna().mean()), 6),
        "min_bin_fraction": float(min_bin_fraction),
    }
    if method == "monotonic":
        entry["monotonic_bad_rate"] = summary["monotonic"]
    entry.update(extra)
    return entry


def woe_table_numeric(
    series: pd.Series,
    target: pd.Series,
    *,
    n_bins: int = 10,
    binning_method: str = "quantile",
    monotonic_direction: str = "increasing",
    min_bin_fraction: float = 0.05,
    max_prebins: int = 20,
) -> Tuple[Optional[float], List[Dict[str, Any]], Optional[List[float]]]:
    """向后兼容的薄封装：返回 (iv, 逐箱明细, 分箱边界)。"""

    entry = numeric_profile(
        series,
        target,
        n_bins=n_bins,
        binning_method=binning_method,
        monotonic_direction=monotonic_direction,
        min_bin_fraction=min_bin_fraction,
        max_prebins=max_prebins,
    )
    if entry is None:
        return None, [], None
    return float(entry["iv"]), list(entry["bins"]), list(entry["bin_edges"])


def woe_table_categorical(series: pd.Series, target: pd.Series) -> Tuple[Optional[float], List[Dict[str, Any]]]:
    mask = series.notna() & target.notna()
    x = series[mask].astype(str)
    y = target[mask].astype(float)
    if len(x) < 20 or y.nunique() < 2:
        return None, []
    grp = pd.DataFrame({"b": x, "y": y}).groupby("b", observed=True)["y"].agg(["sum", "count"])
    bad = grp["sum"].values.astype(float)
    good = (grp["count"] - grp["sum"]).values.astype(float)
    woe, iv = _woe_iv_from_counts(bad, good)
    rows: List[Dict[str, Any]] = []
    for i, (cat, row) in enumerate(grp.iterrows()):
        br = float(row["sum"] / row["count"]) if row["count"] else 0.0
        rows.append(
            {
                "bin": str(cat),
                "woe": round(float(woe[i]), 6),
                "bad_rate": round(br, 6),
                "count": int(row["count"]),
            }
        )
    return iv, rows


def woe_profile(
    df: pd.DataFrame,
    features: Sequence[str],
    label: str,
    *,
    n_bins: int = 10,
    categorical_columns: Optional[Sequence[str]] = None,
    binning_method: str = "quantile",
    monotonic_direction: str = "increasing",
    min_bin_fraction: float = 0.05,
    max_prebins: int = 20,
) -> Dict[str, Any]:
    cats = set(categorical_columns or [])
    y = df[label]
    tables: Dict[str, Any] = {}
    for ft in features:
        if ft not in df.columns:
            continue
        if ft in cats or not pd.api.types.is_numeric_dtype(df[ft]):
            iv, rows = woe_table_categorical(df[ft], y)
            if iv is None:
                continue
            tables[ft] = {
                "kind": "categorical",
                "iv": round(float(iv), 6),
                "bins": rows,
                "bin_edges": None,
                "binning_method": "category",
            }
            continue
        entry = numeric_profile(
            df[ft],
            y,
            n_bins=n_bins,
            binning_method=binning_method,
            monotonic_direction=monotonic_direction,
            min_bin_fraction=min_bin_fraction,
            max_prebins=max_prebins,
        )
        if entry is None:
            continue
        tables[ft] = entry
    return {
        "features": tables,
        "label": label,
        "n_features": len(tables),
        "numeric_binning": binning_method,
        "binning_policy": {
            "method": binning_method,
            "max_bins": n_bins,
            "min_bin_fraction": min_bin_fraction,
            "max_prebins": max_prebins,
            "missing_bin": MISSING_BIN,
        },
    }


@dataclass
class WoEBinningModel:
    """Fit WOE maps on training data; transform features to WOE values."""

    feature_specs: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    categorical_columns: List[str] = field(default_factory=list)

    def fit(
        self,
        df: pd.DataFrame,
        features: Sequence[str],
        label: str,
        *,
        n_bins: int = 10,
        categorical_columns: Optional[Sequence[str]] = None,
        binning_method: str = "quantile",
        monotonic_direction: str = "increasing",
        min_bin_fraction: float = 0.05,
        max_prebins: int = 20,
    ) -> "WoEBinningModel":
        cats = set(categorical_columns or [])
        self.categorical_columns = [f for f in features if f in cats]
        self.feature_specs = {}
        y = df[label]
        for ft in features:
            if ft not in df.columns:
                continue
            if ft in cats or not pd.api.types.is_numeric_dtype(df[ft]):
                iv, rows = woe_table_categorical(df[ft], y)
                self.feature_specs[ft] = {"kind": "categorical", "bins": rows, "iv": iv}
            else:
                entry = numeric_profile(
                    df[ft],
                    y,
                    n_bins=n_bins,
                    binning_method=binning_method,
                    monotonic_direction=monotonic_direction,
                    min_bin_fraction=min_bin_fraction,
                    max_prebins=max_prebins,
                )
                if entry is None:
                    self.feature_specs[ft] = {"kind": "numeric", "bins": [], "bin_edges": None, "iv": None}
                    continue
                self.feature_specs[ft] = entry
        return self

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        out = pd.DataFrame(index=df.index)
        for ft, spec in self.feature_specs.items():
            if ft not in df.columns:
                out[ft] = 0.0
                continue
            woe_map = {str(b["bin"]): b["woe"] for b in spec.get("bins") or []}
            if spec.get("kind") == "categorical":
                labels = df[ft].astype(str)
                if MISSING_BIN in woe_map:
                    labels = labels.mask(df[ft].isna(), MISSING_BIN)
                out[ft] = labels.map(lambda x: woe_map.get(x, 0.0))
            else:
                edges = spec.get("bin_edges")
                if not edges or len(edges) < 2:
                    out[ft] = 0.0
                    continue
                numeric = pd.to_numeric(df[ft], errors="coerce")
                binned = pd.cut(numeric, bins=edges, include_lowest=True, duplicates="drop")
                labels = binned.astype(str)
                # 缺失必须映射到缺失箱的 WOE，而不是统一落到 0（=平均风险）。
                if MISSING_BIN in woe_map:
                    labels = labels.mask(binned.isna(), MISSING_BIN)
                out[ft] = labels.map(lambda x: woe_map.get(x, 0.0))
            out[ft] = pd.to_numeric(out[ft], errors="coerce").fillna(0.0)
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "feature_specs": self.feature_specs,
            "categorical_columns": self.categorical_columns,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "WoEBinningModel":
        m = cls()
        m.feature_specs = dict(raw.get("feature_specs") or {})
        m.categorical_columns = list(raw.get("categorical_columns") or [])
        return m
