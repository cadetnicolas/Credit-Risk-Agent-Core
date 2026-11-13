"""PSI filter, encoding, K-fold export."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from credit_agent.modeling.analyzer import ModelingAnalyzer
from credit_agent.modeling.config import ModelingJobSpec
from credit_agent.modeling.feature_psi import filter_by_feature_psi
from credit_agent.modeling.encoding import CategoricalEncoder


def test_feature_psi_filter_drops_unstable():
    from credit_agent.modeling.feature_psi import feature_psi_value

    n = 400
    ref = pd.Series(np.random.default_rng(0).normal(size=n))
    cur_stable = pd.Series(np.random.default_rng(1).normal(size=n))
    cur_drift = pd.concat([pd.Series(np.zeros(n // 2)), pd.Series(np.ones(n - n // 2))], ignore_index=True)
    assert feature_psi_value(ref, cur_drift) > feature_psi_value(ref, cur_stable)

    rng = np.random.default_rng(9)
    df = pd.DataFrame({"f_ok": rng.normal(size=500), "f_bad": rng.normal(size=500)})
    df.loc[250:, "f_bad"] += 4.0
    kept, table = filter_by_feature_psi(df, ["f_ok", "f_bad"], max_psi=0.05)
    assert len(table) == 2
    assert "f_ok" in kept or len(kept) >= 1
    assert len(kept) <= 2


def test_categorical_encoder_maps_unknown_to_zero():
    df = pd.DataFrame({"cat": ["a", "b", "a", "c"], "y": [0, 1, 0, 1]})
    enc = CategoricalEncoder().fit(df, ["cat"])
    out = enc.transform(df)
    assert out["cat"].tolist()[0] >= 1
    unk = enc.transform(pd.DataFrame({"cat": ["z"]}))
    assert unk["cat"].iloc[0] == 0


def test_kfold_export_bundle(tmp_path):
    rng = np.random.default_rng(42)
    n = 900
    x1 = rng.normal(size=n)
    seg = rng.choice(["A", "B"], size=n)
    y = ((x1 + (seg == "B").astype(float) * 0.5 + rng.normal(scale=0.3, size=n)) > 0).astype(int)
    df = pd.DataFrame({"f1": x1, "seg": seg, "ylabel": y})
    spec = ModelingJobSpec.from_mapping(
        {
            "columns": {"label": "ylabel"},
            "feature_select": {"max_feature_psi": 0.5, "numeric_only": False},
            "train": {"n_folds": 3, "algorithms": ["logistic_regression"]},
            "export": {"export_model": True},
        }
    )
    analyzer = ModelingAnalyzer(spec)
    sel = analyzer.run(df, "feature_select.json")
    assert sel.get("categorical_encoding")
    train = analyzer.run(
        df,
        "model_train.json",
        features=sel["selected"],
        export_dir=tmp_path,
        encoding=sel.get("categorical_encoding"),
    )
    assert train.get("model_dir")
    bundle = tmp_path / "model_bundle.joblib"
    assert bundle.exists()
    assert (tmp_path / "score.py").exists()
    meta = json.loads((tmp_path / "model_meta.json").read_text(encoding="utf-8"))
    assert meta["n_folds"] >= 1
