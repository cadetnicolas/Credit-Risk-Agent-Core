"""Thin wrapper so the page can be generated without installing the package.

``credit-risk-config`` is registered as a console script, but the project is
normally run from a checkout with ``PYTHONPATH=src`` — this keeps the same entry
point available either way.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from credit_agent.config.setup_page import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
