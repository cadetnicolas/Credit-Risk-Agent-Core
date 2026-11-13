"""Configuration package.

Two file sources are loaded, in a fixed order, the moment this package is
imported:

1. a local ``.env`` (see :mod:`credit_agent.config.dotenv`), which must never
   clobber a credential injected by the host;
2. ``config.json`` (see :mod:`credit_agent.config.store`), written by the local
   setup page, which *does* take precedence over ``.env`` — a person who fills in
   a form and presses Save expects the form to win.

Real process environment (``mcp.json`` ``env``, shell exports, CI secrets) always
wins over both files.

Loading here rather than inside :func:`get_env_config` is deliberate: ``paths``
and the upload limits read ``os.environ`` directly, so a lazily loaded
``CREDIT_AGENT_HOME`` could be missed depending on call order.

Set ``CREDIT_AGENT_SKIP_DOTENV=1`` to disable ``.env`` lookup (the test suite
does, so a developer's real credentials never leak into a run), and
``CREDIT_AGENT_CONFIG`` to point at a specific ``config.json``.
"""

from .dotenv import ensure_env_loaded
from .env_schema import EnvConfig, get_env_config, reset_env_config
from .paths import (
    ensure_runtime_dirs,
    get_data_dir,
    get_db_path,
    get_knowledge_dir,
    get_runtime_root,
    get_runs_dir,
    get_sessions_dir,
    get_topics_dir,
    get_uploads_dir,
)
from .store import ensure_config_loaded

ENV_FILE_REPORT = ensure_env_loaded()
CONFIG_FILE_REPORT = ensure_config_loaded(env_report=ENV_FILE_REPORT)

__all__ = [
    "CONFIG_FILE_REPORT",
    "ENV_FILE_REPORT",
    "EnvConfig",
    "ensure_config_loaded",
    "ensure_env_loaded",
    "ensure_runtime_dirs",
    "get_data_dir",
    "get_db_path",
    "get_env_config",
    "get_knowledge_dir",
    "get_runtime_root",
    "get_runs_dir",
    "get_sessions_dir",
    "get_topics_dir",
    "get_uploads_dir",
    "reset_env_config",
]
