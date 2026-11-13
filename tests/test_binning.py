"""Chi2 and monotonic binning."""

from __future__ import annotations

import numpy as np
import pandas as pd

import pytest

from credit_agent.modeling.binning import (
    _is_monotonic,
    chi2_merge_edges,
    chimerge_profile,
    monotonic_merge_edges,
    normalize_method,
    numeric_bin_edges,
)
from credit_agent.modeling.config import ModelingJobSpec
from credit_agent.modeling.pipeline import feature_select_artifact


def test_chi2_produces_fewer_bins_than_prebins():
    rng = np.random.default_rng(0)
    n = 600
    x = rng.normal(size=n)
    y = ((x + rng.normal(scale=0.8, size=n)) > 0.3).astype(int)
    edges = chi2_merge_edges(pd.Series(x), pd.Series(y), max_bins=6)
    assert len(edges) >= 3
    assert edges[0] == -np.inf and edges[-1] == np.inf


def test_monotonic_bad_rate_increasing():
    rng = np.random.default_rng(1)
    n = 500
    x = rng.uniform(size=n)
    y = (x > np.quantile(x, 0.7)).astype(int)
    x = x + y * 0.15
    edges = monotonic_merge_edges(pd.Series(x), pd.Series(y), max_bins=8, direction="increasing")
    xv = pd.Series(x)
    yv = pd.Series(y)
    binned = pd.cut(xv, bins=edges, include_lowest=True, duplicates="drop")
    rates = []
    for cat in binned.cat.categories:
        m = binned == cat
        rates.append(float(yv[m].sum()) / max(m.sum(), 1))
    assert _is_monotonic(rates, "increasing") or len(rates) <= 2


def test_feature_select_with_chi2_binning():
    rng = np.random.default_rng(42)
    n = 700
    x1 = rng.normal(size=n)
    y = ((x1 + rng.normal(0.4, size=n)) > 0).astype(int)
    df = pd.DataFrame({"f1": x1, "ylabel": y})
    out = feature_select_artifact(
        df,
        spec_dict={
            "columns": {"label": "ylabel"},
            "feature_select": {"numeric_binning": "chi2", "woe_bins": 6},
        },
    )
    assert out.get("selected")


def test_numeric_bin_edges_methods():
    x = pd.Series(np.linspace(0, 1, 200))
    y = pd.Series((x > 0.5).astype(int))
    e1 = numeric_bin_edges(x, y, method="quantile", max_bins=5)
    e2 = numeric_bin_edges(x, y, method="chi2", max_bins=5)
    e3 = numeric_bin_edges(x, y, method="monotonic", max_bins=5, monotonic_direction="increasing")
    assert len(e1) >= 2 and len(e2) >= 2 and len(e3) >= 2


def test_chimerge_returns_profile_and_merge_log():
    rng = np.random.default_rng(7)
    n = 800
    x = rng.uniform(0, 100, size=n)
    y = (rng.uniform(size=n) < 0.02 + x / 100 * 0.25).astype(int)
    res = chimerge_profile(pd.Series(x), pd.Series(y), max_bins=5)
    assert res is not None
    assert res.edges[0] == -np.inf and res.edges[-1] == np.inf
    assert 2 <= len(res.bins) <= 5
    # 每个箱都被统计到，且 IV 贡献之和等于总 IV
    assert all({"bin", "count", "bad", "good", "bad_rate", "woe", "iv_part"} <= set(b) for b in res.bins)
    assert abs(sum(b["iv_part"] for b in res.bins) - res.iv) < 1e-4
    # 合并过程可回放，且每次合并都让箱数减一
    assert res.merge_log
    assert all(step["bins_after"] < res.prebins for step in res.merge_log)
    assert res.iv > 0.05


def test_chimerge_min_bin_size_uses_valid_rows_only():
    rng = np.random.default_rng(11)
    n = 1000
    x = rng.uniform(0, 1, size=n)
    y = (x > 0.6).astype(int)
    # 70% 缺失：最小箱门槛必须按 300 条有值样本算，而不是 1000 条全量
    x = pd.Series(x).mask(rng.uniform(size=n) < 0.7)
    res = chimerge_profile(x, pd.Series(y), max_bins=5, min_bin_fraction=0.10)
    assert res is not None
    assert res.n_valid < 400
    assert res.min_bin_size == max(1, int(round(res.n_valid * 0.10)))
    assert res.min_bin_size < int(round(len(x) * 0.10))
    # 缺失独立成箱，且计入 IV 分母（各箱 n_share 之和为 1）
    missing = [b for b in res.bins if b["is_missing"]]
    assert missing and missing[0]["bin"] == "MISSING"
    assert abs(sum(b["n_share"] for b in res.bins) - 1.0) < 1e-6


def test_chimerge_edges_reproducible():
    rng = np.random.default_rng(3)
    n = 600
    x = rng.normal(size=n)
    y = ((x + rng.normal(scale=0.7, size=n)) > 0.2).astype(int)
    a = chi2_merge_edges(pd.Series(x), pd.Series(y), max_bins=4)
    b = chimerge_profile(pd.Series(x), pd.Series(y), max_bins=4).edges
    assert list(a) == list(b)
    assert normalize_method("chimerge") == "chi2"
    with pytest.raises(ValueError):
        normalize_method("entropy")
