"""Small-sample-aware statistics for rates."""

from __future__ import annotations

import math
from typing import Dict, Optional, Tuple


def wilson(k: float, n: float, z: float = 1.96) -> Tuple[Optional[float], Optional[float]]:
    if not n:
        return None, None
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def two_prop_z(k1: float, n1: float, k2: float, n2: float) -> Dict[str, Optional[float]]:
    """Pooled two-proportion z test; ``p_value`` is two-sided."""
    if not n1 or not n2:
        return {"z": None, "p_value": None}
    p = (k1 + k2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    if se == 0:
        return {"z": 0.0, "p_value": 1.0}
    z = (k1 / n1 - k2 / n2) / se
    return {"z": z, "p_value": math.erfc(abs(z) / math.sqrt(2))}


def enough_sample(n: float, p: Optional[float], min_n: int = 30, min_events: int = 5) -> bool:
    """Normal approximation is usable: enough rows and expected events on both sides."""
    if not n or n < min_n or p is None:
        return False
    return n * p >= min_events and n * (1 - p) >= min_events


def safe_rate(k: float, n: float) -> Optional[float]:
    return (k / n) if n else None
