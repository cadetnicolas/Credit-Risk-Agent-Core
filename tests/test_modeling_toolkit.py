"""Modeling toolkit (auto_model_code lineage)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from credit_agent.modeling.config import ModelingJobSpec
from credit_agent.modeling.pipeline import feature_select_artifact, model_train_artifact
from credit_agent.modeling.schema import SchemaResolutionError, resolve_schema
from credit_agent.modeling.analyzer import ModelingAnalyzer


@pytest.fixture
def sample_df() -> pd.DataFrame:
    rng = np.random.default_rng(42)
    n = 800
    x1 = rng.normal(size=n)
    x2 = rng.normal(size=n)
    y = ((x1 + 0.5 * x2 + rng.normal(scale=0.5, size=n)) > 0).astype(int)
    return pd.DataFrame({"f1": x1, "f2": x2, "noise": rng.normal(size=n), "ylabel": y})


def test_feature_select_produces_selected(sample_df):
    out = feature_select_artifact(sample_df, product="测试")
    assert "selected" in out
    assert out.get("label_column") == "ylabel"
    assert len(out["selected"]) >= 1


def test_model_train_produces_auc(sample_df):
    sel = feature_select_artifact(sample_df)
    out = model_train_artifact(sample_df, features=sel["selected"])
    assert out.get("test_auc") is not None
    assert out["best_model"]["valid_auc"] is not None


def test_modeling_analyzer_supported_artifacts():
    assert "feature_select.json" in ModelingAnalyzer.SUPPORTED_ARTIFACTS
    out = ModelingAnalyzer.from_params(
        {"modeling": {"columns": {"label": "ylabel"}}},
        product="p",
    ).run(
        pd.DataFrame({"f1": [0.0, 1.0], "ylabel": [0, 1]}),
        "sample_profile.json",
    )
    assert out.get("artifact") == "sample_profile.json"


def test_schema_ambiguous_labels_raises():
    df = pd.DataFrame({"bad_flag": [0, 1], "mob3_dpd30": [0, 1], "f1": [1.0, 2.0]})
    spec = ModelingJobSpec()
    with pytest.raises(SchemaResolutionError):
        resolve_schema(df, spec.columns)


def test_schema_explicit_label():
    df = pd.DataFrame({"bad_flag": [0, 1], "mob3_dpd30": [0, 1], "f1": [1.0, 2.0]})
    spec = ModelingJobSpec.from_mapping({"columns": {"label": "mob3_dpd30"}})
    resolved = resolve_schema(df, spec.columns)
    assert resolved.label == "mob3_dpd30"
    assert "f1" in resolved.feature_columns


def test_feature_select_with_spec_dict(sample_df):
    out = feature_select_artifact(
        sample_df,
        spec_dict={"columns": {"label": "ylabel", "exclude_columns": ["noise"]}},
    )
    selected = out.get("selected") or []
    if selected and isinstance(selected[0], dict):
        names = {f.get("feature") or f.get("name") for f in selected}
    else:
        names = set(selected)
    assert "noise" not in names
