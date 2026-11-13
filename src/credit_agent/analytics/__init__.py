"""Deterministic business-analysis toolkit for credit portfolios.

Every function returns a plain dict with a ``definition`` block stating the
numerator, denominator and time alignment, so a reviewer (and the validator)
can tell exactly what a rate means. Nothing here calls an LLM.
"""

from .columns import ROLE_LABELS, resolve_columns
from .strategy import marginal_bands, score_grid, swap_table

__all__ = [
    "ROLE_LABELS",
    "resolve_columns",
    "score_grid",
    "marginal_bands",
    "swap_table",
]
