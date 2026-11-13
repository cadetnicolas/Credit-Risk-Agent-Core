"""Development / test split shared by feature selection and training.

Every target-dependent statistic (IV, WOE, binning, medians, encoder maps)
must be fitted on the development part only, so the test part stays a
genuine holdout. With a ``time_column`` the latest rows become the
out-of-time (OOT) test set; otherwise a stratified random split is used.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np
import pandas as pd
from sklearn.model_selection import train_test_split


@dataclass(frozen=True)
class DevTestSplit:
    dev_mask: np.ndarray
    test_mask: np.ndarray
    method: str  # oot | stratified_random | none
    description: str


def _time_order(series: pd.Series) -> pd.Series:
    if pd.api.types.is_datetime64_any_dtype(series):
        return pd.to_datetime(series, errors="coerce", utc=True)
    values = series.astype("string").str.strip()
    if values.str.fullmatch(r"\d{6}", na=False).all():
        return pd.to_datetime(values, format="%Y%m", errors="coerce", utc=True)
    if values.str.fullmatch(r"\d{8}", na=False).all():
        return pd.to_datetime(values, format="%Y%m%d", errors="coerce", utc=True)
    if pd.api.types.is_numeric_dtype(series):
        raise ValueError("观察时间列为无法解释的数值，请先明确实际日期格式")
    return pd.to_datetime(values, errors="coerce", utc=True, format="mixed")


def dev_test_split(work: pd.DataFrame, y: np.ndarray, *, test_size: float, random_state: int,
                   time_column: Optional[str] = None, group_column: Optional[str] = None) -> DevTestSplit:
    n = len(work)
    if n == 0 or not 0 <= test_size < 1:
        raise ValueError("需要非空样本和 0 到 1 之间的留出比例")
    if len(y) != n or not np.isin(y, [0, 1]).all() or len(np.unique(y)) != 2:
        raise ValueError("开发与留出切分需要已明确好坏含义的两类样本")
    groups = None
    if group_column:
        if group_column not in work.columns or work[group_column].isna().any():
            raise ValueError("实体编号缺失或列不存在，不能证明实体隔离")
        from .frame import NULL_MARKERS
        if work[group_column].astype("string").str.strip().isin(NULL_MARKERS).any():
            raise ValueError("实体编号包含数据库空值标记，不能证明实体隔离")
        groups = work[group_column].astype(str).to_numpy()
    if test_size == 0:
        return DevTestSplit(np.ones(n, dtype=bool), np.zeros(n, dtype=bool),
                            "cv_only", "development-only cross-validation; no independent holdout")
    if time_column:
        if time_column not in work.columns:
            raise ValueError("指定的观察时间列不存在，不能退回随机切分")
        order = _time_order(work[time_column])
        if order.isna().any() or order.nunique() < 2:
            raise ValueError("观察时间包含缺失、无效日期或只有一个时点，无法形成跨期验证；未退回随机切分")
        distinct = sorted(order.unique())
        boundary_index = min(len(distinct) - 1, max(1, int(len(distinct) * (1 - test_size))))
        cutoff = distinct[boundary_index]
        test = (order >= cutoff).to_numpy(dtype=bool)
        dev = ~test
        purged = 0
        if groups is not None:
            overlaps = np.isin(groups, np.unique(groups[test])) & dev
            purged = int(overlaps.sum())
            dev = dev & ~overlaps
        if dev.sum() < 10 or test.sum() < 10 or len(np.unique(y[test])) != 2 or len(np.unique(y[dev])) != 2:
            raise ValueError("跨期且实体隔离后的开发/留出样本不足或缺少某类标签，请补充样本；未退回随机切分")
        return DevTestSplit(dev, test, "oot", f"out-of-time split on {time_column}: test >= {cutoff}; purged overlapping entity rows: {purged}")
    if groups is not None:
        from sklearn.model_selection import GroupShuffleSplit
        for seed in range(random_state, random_state + 20):
            a, b = next(GroupShuffleSplit(n_splits=1, test_size=test_size, random_state=seed).split(work, y, groups))
            if len(np.unique(y[a])) == 2 and len(np.unique(y[b])) == 2:
                dev, test = np.zeros(n, dtype=bool), np.zeros(n, dtype=bool)
                dev[a], test[b] = True, True
                return DevTestSplit(dev, test, "group_random", "entity-disjoint random holdout; not out-of-time evidence")
        raise ValueError("实体隔离后无法形成含好坏两类的开发与留出集")
    idx = np.arange(n)
    if min(np.bincount(y.astype(int), minlength=2)) < 2:
        raise ValueError("每类至少需要两条样本才能形成分层留出")
    _, te_idx = train_test_split(idx, test_size=test_size, random_state=random_state, stratify=y)
    test = np.zeros(n, dtype=bool)
    test[te_idx] = True
    return DevTestSplit(~test, test, "stratified_random", f"stratified random holdout {int(round((1-test_size)*100))}/{int(round(test_size*100))}; not OOT")
