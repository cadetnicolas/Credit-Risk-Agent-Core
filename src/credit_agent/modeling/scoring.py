"""Probability to score (PDO / base score), scorecard points.

Convention (industry standard): higher score = lower risk.
``base_odds`` is bad:good odds at ``base_score``; every ``pdo`` points the
bad:good odds halve.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

import numpy as np


def pdo_factor(pdo: float) -> float:
    return pdo / math.log(2)


def score_offset(*, base_score: float, pdo: float, base_odds: float) -> float:
    """``score = offset - factor * ln(bad_odds)``."""
    return base_score + pdo_factor(pdo) * math.log(base_odds)


def prob_to_score(
    prob: Sequence[float],
    *,
    base_score: float = 600.0,
    pdo: float = 50.0,
    base_odds: float = 1 / 20,
    eps: float = 1e-6,
) -> np.ndarray:
    """Map bad probability to score; higher score = lower risk."""
    p = np.clip(np.asarray(prob, dtype=float), eps, 1.0 - eps)
    log_odds = np.log(p / (1.0 - p))
    return score_offset(base_score=base_score, pdo=pdo, base_odds=base_odds) - pdo_factor(pdo) * log_odds


def scorecard_points_from_lr(
    woe_profile: Dict[str, Any],
    coefficients: List[Dict[str, Any]],
    *,
    intercept: Optional[float] = None,
    pdo: float = 50.0,
    base_score: float = 600.0,
    base_odds: float = 1 / 20,
) -> Dict[str, Any]:
    """Scorecard table from an LR fitted on WOE features (raw WOE space).

    ``ln(bad_odds) = intercept + sum(coef_i * woe_i)``, so
    ``score = [offset - factor*intercept] + sum(-factor * coef_i * woe_i)``.
    The sum of base points and one bin per feature equals :func:`prob_to_score`.
    """
    coef_map = {c["feature"]: float(c["coef"]) for c in coefficients if c.get("feature")}
    factor = pdo_factor(pdo)
    offset = score_offset(base_score=base_score, pdo=pdo, base_odds=base_odds)
    # WoEBinningModel.to_dict() 落的是 feature_specs，早期调用方传的是 features。
    # 只认一个键会让 bins 恒为空、评分卡表不可落地，这里两种键名都接受。
    specs = woe_profile.get("features") or woe_profile.get("feature_specs") or {}
    features_out: List[Dict[str, Any]] = []
    for feat, coef in coef_map.items():
        spec = specs.get(feat) or {}
        bins = []
        for b in spec.get("bins") or []:
            woe = float(b.get("woe") or 0)
            bins.append({**b, "points": round(-factor * coef * woe, 4)})
        features_out.append({"feature": feat, "coef": coef, "bins": bins, "iv": spec.get("iv")})
    b0 = float(intercept) if intercept is not None else 0.0
    return {
        "base_score": base_score,
        "pdo": pdo,
        "base_odds": base_odds,
        "direction": "higher_score_lower_risk",
        "lr_intercept": b0,
        "base_points": round(offset - factor * b0, 4),
        "intercept_score_component": round(offset - factor * b0, 4),
        "features": features_out,
    }
