"""Single-variable reject-rule mining: slices with high lift and enough coverage."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import pandas as pd

from .columns import as_flag
from .stats import two_prop_z


def _definition(base: float, min_coverage: float, min_lift: float, min_hits: int) -> Dict[str, Any]:
    return {
        "metric": "单变量拒绝规则",
        "lift": f"命中客群坏账率 / 整体坏账率（整体 {base:.2%}）",
        "filters": f"覆盖率 >= {min_coverage:.0%} 且 lift >= {min_lift} 且绝对命中数 >= {min_hits}",
        "trials_total": "全部被评估的候选条件数（在任何过滤与 TopN 截断之前）",
    }


def mine_rules(
    df: pd.DataFrame,
    *,
    bad: str,
    features: Optional[Sequence[str]] = None,
    min_coverage: float = 0.01,
    min_lift: float = 2.0,
    min_hits: int = 50,
    bins: int = 10,
    top_n: int = 15,
    exclude: Sequence[str] = (),
) -> Dict[str, Any]:
    """Mine one-condition rules on an explicit, registered feature list.

    ``features`` must be supplied by the caller. An empty list returns an
    empty rule set with ``trials_total == 0``; the whole table is never scanned
    as a fallback. ``trials_total`` counts every candidate condition that was
    evaluated, before coverage / lift / hit-count filters and before TopN.
    """

    y_all = as_flag(df[bad])
    mask = y_all.notna()
    y = y_all[mask]
    base = float(y.mean()) if len(y) else 0.0
    skip = set(exclude) | {bad}
    feats = [str(f) for f in (features or []) if str(f) not in skip]
    if not feats:
        return {
            "analysis": "rule_mining",
            "definition": _definition(base, min_coverage, min_lift, min_hits),
            "base_bad_rate": round(base, 6),
            "rules": [],
            "n": int(len(y)),
            "trials_total": 0,
        }
    if not len(y) or base <= 0:
        raise ValueError("rule_mining: 坏样本为 0，无法挖掘拒绝规则")
    rules: List[Dict[str, Any]] = []
    trials_total = 0
    for f in feats:
        if f not in df.columns:
            continue
        x = df.loc[mask, f]
        num = pd.to_numeric(x, errors="coerce")
        if num.notna().mean() > 0.9 and num.nunique() > bins:
            try:
                q = pd.qcut(num, q=bins, duplicates="drop")
            except ValueError:
                continue
            cats = list(q.cat.categories)
            if not cats:
                continue
            # tail rules are the deployable ones: x <= low edge or x > high edge
            groups = [
                (f"{f} <= {cats[0].right:.4g}", num <= cats[0].right),
                (f"{f} > {cats[-1].left:.4g}", num > cats[-1].left),
            ]
        elif x.nunique() <= 30:
            groups = [(f"{f} == {v}", x.astype(str) == str(v)) for v in x.dropna().astype(str).unique()]
        else:
            continue
        for name, m in groups:
            trials_total += 1
            m = m.fillna(False)
            n = int(m.sum())
            if n < min_hits:
                continue
            cov = n / len(y)
            if cov < min_coverage or n == 0:
                continue
            k = float(y[m].sum())
            rate = k / n
            lift = rate / base
            if lift < min_lift:
                continue
            rest_n, rest_k = len(y) - n, float(y.sum()) - k
            test = two_prop_z(k, n, rest_k, rest_n)
            rules.append(
                {
                    "rule": name,
                    "feature": f,
                    "coverage": round(cov, 6),
                    "hits": n,
                    "bad_rate": round(rate, 6),
                    "lift": round(lift, 3),
                    "bad_capture": round(k / float(y.sum()), 6),
                    "p_value": round(test["p_value"], 6) if test["p_value"] is not None else None,
                }
            )
    rules.sort(key=lambda r: (-r["lift"] * r["bad_capture"]))
    return {
        "analysis": "rule_mining",
        "definition": _definition(base, min_coverage, min_lift, min_hits),
        "base_bad_rate": round(base, 6),
        "rules": rules[:top_n],
        "n": int(len(y)),
        "trials_total": trials_total,
    }
