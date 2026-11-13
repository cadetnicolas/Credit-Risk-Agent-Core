"""Feature selection workflow (legacy ``f_feature_selection`` without business columns).

IV / WOE / correlation are computed on the development split only, the same
split :mod:`training` uses, so the test rows never influence which features
are kept. Feature PSI compares development vs out-of-time rows and is skipped
when no ``time_column`` is configured (a random split cannot measure drift).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from ..config import ColumnBinding, FeatureSelectPolicy, ModelingJobSpec
from ..encoding import CategoricalEncoder, detect_categorical_columns
from ..feature_psi import feature_psi_value
from ..iv import iv_table
from ..preprocess import drop_high_missing, fill_numeric_median, primary_value_ratio_filter
from ..schema import ResolvedSchema, binary_label_series, filter_modeling_rows
from ..split import dev_test_split
from ..woe import woe_profile


def correlation_prune(
    df: pd.DataFrame,
    ordered_features: List[str],
    *,
    threshold: float,
) -> List[str]:
    if len(ordered_features) <= 1:
        return ordered_features
    mat = fill_numeric_median(df, ordered_features)
    corr = mat.corr().abs()
    selected: List[str] = []
    for f in ordered_features:
        if f not in corr.columns:
            continue
        if any(corr.loc[f, s] >= threshold for s in selected if f != s):
            continue
        selected.append(f)
    return selected


def _psi_table(
    dev_enc: pd.DataFrame, test_enc: pd.DataFrame, feats: List[str], max_psi: float
) -> tuple[List[str], List[Dict[str, Any]]]:
    kept: List[str] = []
    table: List[Dict[str, Any]] = []
    for f in feats:
        val = feature_psi_value(dev_enc[f], test_enc[f])
        table.append({"feature": f, "psi": round(float(val), 6) if np.isfinite(val) else None})
        if not np.isfinite(val) or val <= max_psi:
            kept.append(f)
    table.sort(key=lambda r: -(r.get("psi") or 0))
    return kept, table


def run_feature_selection(
    df: pd.DataFrame,
    schema: ResolvedSchema,
    policy: FeatureSelectPolicy,
    *,
    columns: ColumnBinding,
    test_size: float = 0.3,
    random_state: int = 2019,
) -> Dict[str, Any]:
    label = schema.label
    work = filter_modeling_rows(df, columns).copy()
    work["_y_bin"] = binary_label_series(work, columns)
    work = work.dropna(subset=["_y_bin"]).reset_index(drop=True)
    y = work["_y_bin"].values.astype(int)

    split = dev_test_split(
        work, y, test_size=test_size, random_state=random_state, time_column=columns.time_column, group_column=columns.id_columns[0] if columns.id_columns else None
    )
    dev = work[split.dev_mask].reset_index(drop=True)
    test = work[split.test_mask].reset_index(drop=True)

    feats = list(schema.feature_columns)
    feats = drop_high_missing(feats, dev, policy.max_missing_rate)
    feats = primary_value_ratio_filter(feats, dev, limit=policy.primary_value_ratio_limit)

    cat_cols = detect_categorical_columns(dev, feats)
    encoder = CategoricalEncoder().fit(dev, feats, categorical_columns=cat_cols)
    dev_enc = encoder.transform(dev)

    psi_table: List[Dict[str, Any]] = []
    psi_note: Optional[str] = None
    if policy.max_feature_psi is not None:
        if split.method == "oot" and len(test) >= 30:
            feats, psi_table = _psi_table(dev_enc, encoder.transform(test), feats, float(policy.max_feature_psi))
        else:
            psi_note = "没有可用的独立时间外样本，跳过特征 PSI"

    candidates = iv_table(
        dev_enc,
        list(feats),
        "_y_bin",
        n_bins=policy.woe_bins,
        binning_method=policy.numeric_binning,
        monotonic_direction=policy.monotonic_direction,
        min_bin_fraction=policy.min_bin_fraction,
        max_prebins=policy.max_prebins,
    )
    selected = [r["feature"] for r in candidates if (r.get("iv") or 0) >= policy.min_iv]
    selected = correlation_prune(dev_enc, selected, threshold=policy.correlation_threshold)[
        : policy.max_features
    ]

    corr: Dict[str, Any] = {}
    if len(selected) >= 2:
        corr = fill_numeric_median(dev_enc, selected).corr().round(4).to_dict()

    selected_woe = None
    if policy.emit_woe_profile and selected:
        selected_woe = woe_profile(
            dev_enc,
            selected,
            "_y_bin",
            n_bins=policy.woe_bins,
            categorical_columns=[c for c in cat_cols if c in selected],
            binning_method=policy.numeric_binning,
            monotonic_direction=policy.monotonic_direction,
            min_bin_fraction=policy.min_bin_fraction,
            max_prebins=policy.max_prebins,
        )

    return {
        "candidates": candidates,
        "selected": selected,
        "correlation": corr,
        "label_column": label,
        "positive_values": list(schema.label_positive_values),
        "row_count": int(len(work)),
        "dev_row_count": int(len(dev)),
        "raw_row_count": int(len(df)),
        "split": split.description,
        "fit_scope": "IV / WOE / 相关性仅在开发样本上计算",
        "policy": {
            "min_iv": policy.min_iv,
            "max_missing_rate": policy.max_missing_rate,
            "correlation_threshold": policy.correlation_threshold,
            "max_feature_psi": policy.max_feature_psi,
            "binning_method": policy.numeric_binning,
            "woe_bins": policy.woe_bins,
            "min_bin_fraction": policy.min_bin_fraction,
            "max_prebins": policy.max_prebins,
        },
        "feature_psi": psi_table,
        "feature_psi_note": psi_note,
        "categorical_encoding": encoder.to_dict(),
        "categorical_columns": cat_cols,
        "selected_woe_profile": selected_woe,
    }


def feature_selection_step(df: pd.DataFrame, spec: ModelingJobSpec) -> Dict[str, Any]:
    from ..schema import resolve_schema

    work = filter_modeling_rows(df, spec.columns)
    schema = resolve_schema(work, spec.columns, numeric_only=spec.feature_select.numeric_only)
    if not spec.columns.label:
        spec.columns.label = schema.label
    return run_feature_selection(
        df,
        schema,
        spec.feature_select,
        columns=spec.columns,
        test_size=spec.train.test_size,
        random_state=spec.train.random_state,
    )
