"""Runtime root resolution.

All user-level state lives under a single runtime root so the whole platform is
back-up-able, deletable and relocatable with one path. Mirrors the convention
used by the reference trading platform, adapted for credit risk.

Priority:
    1. explicit ``config_path.parent`` (used by tests / embedded callers)
    2. ``CREDIT_AGENT_HOME`` environment variable
    3. ``~/.credit-agent``
"""

from __future__ import annotations

import os
from pathlib import Path

_HOME_ENV_VAR = "CREDIT_AGENT_HOME"
_DEFAULT_DIR_NAME = ".credit-agent"


def get_runtime_root(config_path: Path | None = None) -> Path:
    """Resolve the runtime root directory."""
    if config_path is not None:
        return config_path.expanduser().parent
    env_root = os.environ.get(_HOME_ENV_VAR, "").strip()
    if env_root:
        return Path(env_root).expanduser()
    return Path.home() / _DEFAULT_DIR_NAME


def get_sessions_dir() -> Path:
    return get_runtime_root() / "sessions"


def get_runs_dir() -> Path:
    return get_runtime_root() / "runs"


def get_topics_dir() -> Path:
    return get_runtime_root() / "topics"


def get_uploads_dir() -> Path:
    return get_runtime_root() / "uploads"


def get_data_dir() -> Path:
    return get_runtime_root() / "data"


def get_db_path() -> Path:
    return get_runtime_root() / "credit_agent.db"


def get_knowledge_dir() -> Path:
    return get_runtime_root() / "knowledge"


def get_datasets_dir() -> Path:
    """Where user-supplied query results are snapshotted for analysis."""
    return get_data_dir() / "datasets"


def ensure_runtime_dirs() -> None:
    """Create every runtime directory. Idempotent."""
    for path in (
        get_sessions_dir(),
        get_runs_dir(),
        get_topics_dir(),
        get_uploads_dir(),
        get_data_dir(),
        get_datasets_dir(),
        get_knowledge_dir(),
    ):
        path.mkdir(parents=True, exist_ok=True)
