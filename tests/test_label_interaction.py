"""Human-in-the-loop label mapping for multi-class ylabel."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from credit_agent.modeling.analyzer import ModelingAnalyzer
from credit_agent.modeling.config import ModelingJobSpec
from credit_agent.modeling.schema import (
    SchemaResolutionError,
    binary_label_series,
    filter_modeling_rows,
    label_probe_payload,
    resolve_schema,
)


@pytest.fixture
def multiclass_df() -> pd.DataFrame:
    rng = np.random.default_rng(7)
    n = 600
    y = rng.choice([-99, 0, 1, 2], size=n, p=[0.08, 0.45, 0.22, 0.25])
    return pd.DataFrame({"f1": rng.normal(size=n), "f2": rng.normal(size=n), "ylabel": y})


def test_multiclass_ylabel_probe_needs_human(multiclass_df):
    probe = label_probe_payload(multiclass_df, ModelingJobSpec().columns)
    assert probe["status"] == "needs_label_definition"
    assert any(c["column"] == "ylabel" for c in probe["label_candidates"])
    assert probe["suggested_defaults"] == {}  # column names do not define enum semantics


def test_multiclass_resolve_fails_without_mapping(multiclass_df):
    spec = ModelingJobSpec.from_mapping({"columns": {"label": "ylabel"}})
    with pytest.raises(SchemaResolutionError):
        resolve_schema(multiclass_df, spec.columns)


def test_multiclass_mapping_and_sample_profile(multiclass_df):
    analyzer = ModelingAnalyzer(
        ModelingJobSpec.from_mapping(
            {
                "columns": {
                    "label": "ylabel",
                    "positive_values": [2],
                    "exclude_label_values": [-99],
                    "gray_values": [1],
                    "gray_policy": "exclude",
                }
            }
        )
    )
    out = analyzer.run(multiclass_df, "sample_profile.json")
    assert out.get("status") == "ok"
    assert out.get("label_column") == "ylabel"
    assert out.get("bad_rate") is not None
    assert out["raw_row_count"] == len(multiclass_df)
    assert out["sample_size"] < len(multiclass_df)


def test_binary_label_series_counts(multiclass_df):
    binding = ModelingJobSpec.from_mapping(
        {
            "columns": {
                "label": "ylabel",
                "positive_values": [2],
                "exclude_label_values": [-99],
                "gray_values": [1],
            }
        }
    ).columns
    work = filter_modeling_rows(multiclass_df, binding)
    y = binary_label_series(work, binding)
    assert set(y.unique()) <= {0.0, 1.0}
    assert int(y.sum()) == int((work["ylabel"] == 2).sum())
