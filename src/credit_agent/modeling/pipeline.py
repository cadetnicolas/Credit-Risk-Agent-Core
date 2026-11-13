"""Public entry points for sandbox / plugins."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

from .artifacts import feature_select, model_eval, model_train, run_artifact, sample_profile
from .config import ModelingJobSpec


def _spec(
    spec: Optional[ModelingJobSpec] = None,
    spec_dict: Optional[Dict[str, Any]] = None,
    **legacy_kwargs: Any,
) -> ModelingJobSpec:
    if spec_dict is None:
        spec_dict = legacy_kwargs.pop("spec_dict", None)
    else:
        legacy_kwargs.pop("spec_dict", None)
    if spec is not None:
        base = spec
    else:
        base = ModelingJobSpec.from_mapping(spec_dict)
    if legacy_kwargs.get("label_column"):
        base.columns.label = legacy_kwargs["label_column"]
    meta = dict(base.metadata)
    if legacy_kwargs.get("product"):
        meta.setdefault("product", legacy_kwargs["product"])
    base.metadata = meta
    return base


def sample_profile_artifact(df: pd.DataFrame, *, spec: Optional[ModelingJobSpec] = None, **kw: Any) -> Dict[str, Any]:
    spec_dict = kw.pop("spec_dict", None)
    return sample_profile(df, _spec(spec, spec_dict, **kw))


def feature_select_artifact(
    df: pd.DataFrame,
    *,
    spec: Optional[ModelingJobSpec] = None,
    preset_features: Optional[List[str]] = None,
    **kw: Any,
) -> Dict[str, Any]:
    spec_dict = kw.pop("spec_dict", None)
    return feature_select(df, _spec(spec, spec_dict, **kw), preset_features=preset_features)


def model_train_artifact(
    df: pd.DataFrame,
    *,
    spec: Optional[ModelingJobSpec] = None,
    features: Optional[List[str]] = None,
    **kw: Any,
) -> Dict[str, Any]:
    spec_dict = kw.pop("spec_dict", None)
    return model_train(df, _spec(spec, spec_dict, **kw), features=features)


def model_eval_artifact(
    df: pd.DataFrame,
    *,
    spec: Optional[ModelingJobSpec] = None,
    features: Optional[List[str]] = None,
    **kw: Any,
) -> Dict[str, Any]:
    spec_dict = kw.pop("spec_dict", None)
    return model_eval(df, _spec(spec, spec_dict, **kw), features=features)
