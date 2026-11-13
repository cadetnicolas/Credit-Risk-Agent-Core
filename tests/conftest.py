"""Shared test fixtures.

The runtime root is redirected to a throwaway directory *at import time*,
before any module under test is collected - several modules resolve their paths
when first imported, so a pytest fixture would run too late.
"""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Keep pytest's tmp_path inside the workspace: the system temp root already
# exists and re-creating it is denied in a sandboxed shell. A fresh basetemp per
# session - pytest would otherwise try to re-create `pytest-of-<user>`, which the
# same sandbox rule rejects.
_TMP_ROOT = ROOT / ".pytest-tmp"
if not _TMP_ROOT.exists():
    _TMP_ROOT.mkdir()
os.environ["TMPDIR"] = str(_TMP_ROOT)
_BASE_TEMP = _TMP_ROOT / ("run-" + uuid.uuid4().hex[:8])
_BASE_TEMP.mkdir(parents=True)
os.environ["PYTEST_DEBUG_TEMPROOT"] = str(_BASE_TEMP)
tempfile.tempdir = str(_TMP_ROOT)

_SANDBOX_HOME = Path(tempfile.mkdtemp(prefix="credit-agent-test-")).resolve()
os.environ.pop("CREDIT_AGENT_HOME", None)
os.environ["CREDIT_AGENT_HOME"] = str(_SANDBOX_HOME)

# A developer's real `.env` (warehouse credentials, a production backend) must
# never influence a test run: tests assert on explicit monkeypatched values.
os.environ["CREDIT_AGENT_SKIP_DOTENV"] = "1"


@pytest.fixture(scope="session")
def warehouse():
    """Build the mock warehouse once per test session."""
    from credit_agent.config.paths import get_data_dir
    from credit_agent.data.mock import build_duckdb

    return build_duckdb(get_data_dir() / "credit.duckdb")


@pytest.fixture(autouse=True)
def _reset_config():
    from credit_agent.config.env_schema import reset_env_config
    from credit_agent.config.store import reset_state

    saved = dict(os.environ)
    reset_env_config()
    reset_state()
    yield
    os.environ.clear()
    os.environ.update(saved)
    reset_env_config()
    reset_state()
