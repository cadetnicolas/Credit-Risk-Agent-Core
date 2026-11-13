"""WOE binning profile artifact."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

from ..config import ModelingJobSpec
from ..schema import binary_label_series, filter_modeling_rows
from ..woe import woe_profile


def woe_profile_step(
    df: pd.DataFrame,
    spec: ModelingJobSpec,
    features: Optional[List[str]] = None,
    *,
    categorical_columns: Optional[List[str]] = None,
) -> Dict[str, Any]:
    from ..schema import resolve_schema

    work = filter_modeling_rows(df, spec.columns)
    schema = resolve_schema(work, spec.columns, numeric_only=spec.feature_select.numeric_only)
    feats = list(features or schema.feature_columns)
    work = work.copy()
    work["_y"] = binary_label_series(work, spec.columns)
    from ..split import dev_test_split
    split = dev_test_split(work, work["_y"].to_numpy(dtype=int), test_size=spec.train.test_size, random_state=spec.train.random_state,
                           time_column=spec.columns.time_column, group_column=spec.columns.id_columns[0] if spec.columns.id_columns else None)
    work = work[split.dev_mask]
    profile = woe_profile(
        work,
        feats,
        "_y",
        n_bins=spec.feature_select.woe_bins,
        categorical_columns=categorical_columns,
        binning_method=spec.feature_select.numeric_binning,
        monotonic_direction=spec.feature_select.monotonic_direction,
        min_bin_fraction=spec.feature_select.min_bin_fraction,
    )
    profile["fit_scope"] = "development rows only; final holdout excluded"
    profile["split"] = split.description
    profile["label_column"] = schema.label
    profile["feature_list"] = feats
    return profile
