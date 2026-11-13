"""Label role coverage, leak-free fitting, exported score direction."""

from __future__ import annotations

import importlib.util

import numpy as np
import pandas as pd

from credit_agent.modeling.config import ColumnBinding, ModelingJobSpec
from credit_agent.modeling.metrics import decile_bad_rates, ks_score
from credit_agent.modeling.schema import (
    binary_label_series,
    filter_modeling_rows,
    label_definition_ready,
    unassigned_label_values,
)
from credit_agent.modeling.workflows.training import training_step


def _ylabel_df(n: int = 400) -> pd.DataFrame:
    rng = np.random.default_rng(3)
    return pd.DataFrame(
        {"ylabel": rng.choice([-99, 0, 1, 2], size=n, p=[0.05, 0.6, 0.15, 0.2]), "x": rng.normal(size=n)}
    )


def test_multiclass_label_requires_every_value_assigned():
    df = _ylabel_df()
    partial = ColumnBinding(label="ylabel", positive_values=(2,), exclude_label_values=(-99,), gray_values=(1,))
    assert unassigned_label_values(partial, df) == [0]
    assert not label_definition_ready(partial, df)
    full = ColumnBinding(
        label="ylabel", positive_values=(2,), negative_values=(0,), exclude_label_values=(-99,), gray_values=(1,)
    )
    assert label_definition_ready(full, df)
    work = filter_modeling_rows(df, full)
    assert set(work["ylabel"].unique()) == {0, 2}
    assert set(binary_label_series(work, full).unique()) == {0.0, 1.0}


def test_binary_label_with_nan_is_dropped_and_string_values_match():
    df = pd.DataFrame({"y": ["1", "0", None, "1", "0"], "x": [1, 2, 3, 4, 5]})
    b = ColumnBinding(label="y", positive_values=(1,))
    assert label_definition_ready(b, df)
    work = filter_modeling_rows(df, b)
    assert len(work) == 4
    assert binary_label_series(work, b).tolist() == [1.0, 0.0, 1.0, 0.0]


def test_positive_value_missing_from_data_is_not_ready():
    df = pd.DataFrame({"y": [0, 1, 0, 1]})
    assert not label_definition_ready(ColumnBinding(label="y", positive_values=(2,)), df)


def test_noise_categorical_does_not_leak_into_test_auc():
    rng = np.random.default_rng(11)
    n = 1200
    df = pd.DataFrame(
        {
            "noise_cat": [f"c{i}" for i in rng.integers(0, 45, size=n)],
            "y": rng.integers(0, 2, size=n),
        }
    )
    spec = ModelingJobSpec.from_mapping(
        {
            "columns": {"label": "y"},
            "feature_select": {"numeric_only": False},
            "train": {"algorithms": ["logistic_regression"], "n_folds": 3},
        }
    )
    body = training_step(df, spec, ["noise_cat"])
    assert body.get("error") is None, body
    assert body["test_auc"] < 0.62
    assert "training rows only" in body["sample"]["fit_scope"]


def test_exported_score_is_lower_for_riskier_rows(tmp_path):
    rng = np.random.default_rng(5)
    n = 800
    x = rng.normal(size=n)
    y = (x + rng.normal(scale=0.8, size=n) > 0.8).astype(int)
    df = pd.DataFrame({"x": x, "x2": rng.normal(size=n), "y": y})
    spec = ModelingJobSpec.from_mapping(
        {"columns": {"label": "y"}, "train": {"algorithms": ["logistic_regression"], "n_folds": 3}}
    )
    body = training_step(df, spec, ["x", "x2"], export_dir=tmp_path)
    assert body.get("model_dir"), body
    spec_mod = importlib.util.spec_from_file_location("score_mod", tmp_path / "score.py")
    mod = importlib.util.module_from_spec(spec_mod)
    spec_mod.loader.exec_module(mod)
    out = mod.predict(df[["x", "x2"]].head(200))
    prob = np.array(out["prob"])
    score = np.array(out["credit_score"])
    assert np.corrcoef(prob, score)[0, 1] < -0.9
    assert body["intercept"] is not None
    # Calibration: mean predicted PD close to the observed bad rate without class re-weighting.
    assert abs(prob.mean() - y[:200].mean()) < 0.1


def test_exact_ks_and_decile_monotonicity():
    y = np.array([0] * 50 + [1] * 50)
    p = np.linspace(0, 1, 100)
    ks, _ = ks_score(y, p)
    assert abs(ks - 1.0) < 1e-9
    d = decile_bad_rates(y, p)
    assert d["monotonic"] is True
    assert d["rows"][0]["bad_rate"] == 1.0


def test_oot_split_on_string_month_column_from_parquet(tmp_path):
    """Copy-on-write frames hand back read-only arrays; the OOT mask must not mutate them."""
    from credit_agent.modeling.split import dev_test_split

    rng = np.random.default_rng(11)
    n = 600
    df = pd.DataFrame(
        {"apply_month": rng.choice([f"2025-{m:02d}" for m in range(1, 13)], n), "y": rng.integers(0, 2, n)}
    )
    path = tmp_path / "s.parquet"
    df.to_parquet(path)
    df = pd.read_parquet(path)
    split = dev_test_split(df, df["y"].to_numpy(), test_size=0.3, random_state=1, time_column="apply_month")
    assert split.method == "oot"
    assert df.loc[split.test_mask, "apply_month"].min() > df.loc[split.dev_mask, "apply_month"].max()


def test_decile_monotonicity_tolerates_sampling_noise():
    y = np.array([1] * 30 + [0] * 70 + [1] * 31 + [0] * 69 + [1] * 5 + [0] * 795, dtype=float)
    p = np.linspace(1, 0, len(y))
    out = decile_bad_rates(y, p)
    assert out["strict_monotonic"] is False
    assert out["monotonic"] is True
    assert out["inversions"] and not out["inversions"][0]["significant"]

    y_bad = np.array([1] * 5 + [0] * 95 + [1] * 60 + [0] * 40 + [0] * 800, dtype=float)
    assert decile_bad_rates(y_bad, np.linspace(1, 0, len(y_bad)))["monotonic"] is False
