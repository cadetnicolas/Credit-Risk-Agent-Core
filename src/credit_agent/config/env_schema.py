"""Typed environment configuration (legacy platform / research scope).

Every ``os.getenv`` in the codebase should resolve through :func:`get_env_config`
so that defaults live in exactly one place and tests can reset state wholesale.

Scope note: the analysis tools never read this configuration. Their
``--data-root`` / ``--registry-root`` / ``--ledger-root`` are host-supplied CLI
arguments (or generated ``mcp.json`` args), and the three roots must be
mutually disjoint. The **acquisition** tools (``download_sample``) are the one
exception: they need warehouse credentials, so they read ``DataConfig`` from the
process environment, which the host populates via ``mcp.json`` ``env``.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _parse_env_bool(value: Any) -> Any:
    """Accept the usual truthy spellings found in .env files."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _blank_to_none(value: Any) -> Any:
    """Treat ``KEY=`` in a .env file as "unset", not as a validation error.

    Ports and other optional numerics are declared ``int | None``; without this
    a blank line would raise instead of falling back to the backend default.
    """
    if isinstance(value, str) and not value.strip():
        return None
    return value


EnvBool = Annotated[bool, BeforeValidator(_parse_env_bool)]
OptionalInt = Annotated[int | None, BeforeValidator(_blank_to_none)]


class _EnvBase(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")


class LLMConfig(_EnvBase):
    """Any OpenAI-compatible chat endpoint works (private deployment preferred)."""

    provider: str = Field(alias="CREDIT_AGENT_LLM_PROVIDER", default="openai")
    base_url: str = Field(alias="CREDIT_AGENT_LLM_BASE_URL", default="https://api.openai.com/v1")
    api_key: str = Field(alias="CREDIT_AGENT_LLM_API_KEY", default="")
    model: str = Field(alias="CREDIT_AGENT_LLM_MODEL", default="gpt-4o-mini")
    temperature: float = Field(alias="CREDIT_AGENT_LLM_TEMPERATURE", default=0.2)
    timeout_seconds: float = Field(alias="CREDIT_AGENT_LLM_TIMEOUT_SECONDS", default=120.0)
    max_retries: int = Field(alias="CREDIT_AGENT_LLM_MAX_RETRIES", default=2)

    @property
    def configured(self) -> bool:
        return bool(self.api_key.strip())


class EmbeddingConfig(_EnvBase):
    provider: str = Field(alias="CREDIT_AGENT_EMBED_PROVIDER", default="hashing")
    base_url: str = Field(alias="CREDIT_AGENT_EMBED_BASE_URL", default="")
    api_key: str = Field(alias="CREDIT_AGENT_EMBED_API_KEY", default="")
    model: str = Field(alias="CREDIT_AGENT_EMBED_MODEL", default="text-embedding-3-small")
    dim: int = Field(alias="CREDIT_AGENT_EMBED_DIM", default=256)


class DataConfig(_EnvBase):
    """Business warehouse access. The agent only ever reads.

    One backend is active at a time, chosen by ``CREDIT_AGENT_DATA_BACKEND``:

    ``duckdb``   local file, used by tests and the bundled demo dataset
    ``odps``     MaxCompute / DataWorks (Hive-like SQL)
    ``postgres`` PostgreSQL
    ``holo``     Hologres — same wire protocol as PostgreSQL, different defaults
    ``mysql``    MySQL / MariaDB

    Credentials are read from the environment only. They are never written back,
    never logged, and never serialised into a report or evidence pack.
    """

    backend: str = Field(alias="CREDIT_AGENT_DATA_BACKEND", default="duckdb")
    duckdb_path: str = Field(alias="CREDIT_AGENT_DUCKDB_PATH", default="")
    max_scan_rows: int = Field(alias="CREDIT_AGENT_MAX_SCAN_ROWS", default=2_000_000)
    connect_timeout_seconds: int = Field(alias="CREDIT_AGENT_CONNECT_TIMEOUT", default=30)

    # --- ODPS / MaxCompute -------------------------------------------------
    odps_endpoint: str = Field(alias="CREDIT_AGENT_ODPS_ENDPOINT", default="")
    odps_access_id: str = Field(alias="CREDIT_AGENT_ODPS_ACCESS_ID", default="")
    odps_access_key: str = Field(alias="CREDIT_AGENT_ODPS_ACCESS_KEY", default="")
    odps_project: str = Field(alias="CREDIT_AGENT_ODPS_PROJECT", default="")

    # --- PostgreSQL / Hologres (shared PG wire protocol) --------------------
    pg_host: str = Field(alias="CREDIT_AGENT_PG_HOST", default="")
    pg_port: OptionalInt = Field(alias="CREDIT_AGENT_PG_PORT", default=None)
    pg_database: str = Field(alias="CREDIT_AGENT_PG_DATABASE", default="")
    pg_user: str = Field(alias="CREDIT_AGENT_PG_USER", default="")
    pg_password: str = Field(alias="CREDIT_AGENT_PG_PASSWORD", default="")
    pg_sslmode: str = Field(alias="CREDIT_AGENT_PG_SSLMODE", default="")
    pg_schema: str = Field(alias="CREDIT_AGENT_PG_SCHEMA", default="")

    # --- MySQL / MariaDB ---------------------------------------------------
    mysql_host: str = Field(alias="CREDIT_AGENT_MYSQL_HOST", default="")
    mysql_port: OptionalInt = Field(alias="CREDIT_AGENT_MYSQL_PORT", default=None)
    mysql_database: str = Field(alias="CREDIT_AGENT_MYSQL_DATABASE", default="")
    mysql_user: str = Field(alias="CREDIT_AGENT_MYSQL_USER", default="")
    mysql_password: str = Field(alias="CREDIT_AGENT_MYSQL_PASSWORD", default="")
    mysql_charset: str = Field(alias="CREDIT_AGENT_MYSQL_CHARSET", default="utf8mb4")

    def backend_configured(self) -> tuple[bool, list[str]]:
        """Whether the active backend has every variable it needs.

        Returns ``(ok, missing_variable_names)``. ``duckdb`` is always
        "configured" because it needs no credentials.
        """
        backend = (self.backend or "duckdb").strip().lower()
        required: dict[str, list[tuple[str, str]]] = {
            "odps": [
                ("CREDIT_AGENT_ODPS_ACCESS_ID", self.odps_access_id),
                ("CREDIT_AGENT_ODPS_ACCESS_KEY", self.odps_access_key),
                ("CREDIT_AGENT_ODPS_PROJECT", self.odps_project),
            ],
            "postgres": [
                ("CREDIT_AGENT_PG_HOST", self.pg_host),
                ("CREDIT_AGENT_PG_DATABASE", self.pg_database),
                ("CREDIT_AGENT_PG_USER", self.pg_user),
                ("CREDIT_AGENT_PG_PASSWORD", self.pg_password),
            ],
            "holo": [
                ("CREDIT_AGENT_PG_HOST", self.pg_host),
                ("CREDIT_AGENT_PG_DATABASE", self.pg_database),
                ("CREDIT_AGENT_PG_USER", self.pg_user),
                ("CREDIT_AGENT_PG_PASSWORD", self.pg_password),
            ],
            "mysql": [
                ("CREDIT_AGENT_MYSQL_HOST", self.mysql_host),
                ("CREDIT_AGENT_MYSQL_DATABASE", self.mysql_database),
                ("CREDIT_AGENT_MYSQL_USER", self.mysql_user),
                ("CREDIT_AGENT_MYSQL_PASSWORD", self.mysql_password),
            ],
        }
        if backend == "duckdb":
            return True, []
        missing = [name for name, value in required.get(backend, []) if not str(value or "").strip()]
        if backend not in required:
            return False, [f"CREDIT_AGENT_DATA_BACKEND={backend} (未知后端)"]
        return not missing, missing


class SandboxConfig(_EnvBase):
    timeout_seconds: int = Field(alias="CREDIT_AGENT_SANDBOX_TIMEOUT_SECONDS", default=300)
    rlimit_as_mb: int = Field(alias="CREDIT_AGENT_SANDBOX_RLIMIT_AS_MB", default=4096)
    rlimit_nofile: int = Field(alias="CREDIT_AGENT_SANDBOX_RLIMIT_NOFILE", default=512)
    drop_privileges: EnvBool = Field(alias="CREDIT_AGENT_SANDBOX_DROP_PRIVILEGES", default=True)
    sandbox_user: str = Field(alias="CREDIT_AGENT_SANDBOX_USER", default="credit-sandbox")
    react_max_steps: int = Field(alias="CREDIT_AGENT_SANDBOX_REACT_MAX_STEPS", default=4)


class APIConfig(_EnvBase):
    max_concurrent_runs: int = Field(alias="CREDIT_AGENT_MAX_CONCURRENT_RUNS", default=2, ge=1, le=32)
    max_queued_runs: int = Field(alias="CREDIT_AGENT_MAX_QUEUED_RUNS", default=32, ge=1, le=1000)
    api_auth_key: str = Field(alias="API_AUTH_KEY", default="")
    host: str = Field(alias="CREDIT_AGENT_API_HOST", default="127.0.0.1")
    port: int = Field(alias="CREDIT_AGENT_API_PORT", default=8890)
    cors_origins: str = Field(alias="CORS_ORIGINS", default="")
    async_topic_runs: EnvBool = Field(
        alias="CREDIT_AGENT_API_ASYNC_TOPICS",
        default=True,
        description="POST /topics 与 decision 在后台跑图，客户端轮询 GET /topics/{id} 看进度",
    )


class AgentConfig(_EnvBase):
    planning_timeout_seconds: float = Field(alias="CREDIT_AGENT_PLANNING_TIMEOUT_SECONDS", default=30, gt=0, le=300)
    max_task_retries: int = Field(alias="CREDIT_AGENT_MAX_TASK_RETRIES", default=1)
    max_graph_steps: int = Field(alias="CREDIT_AGENT_MAX_GRAPH_STEPS", default=200)
    # When no LLM is configured the planner falls back to deterministic baseline
    # templates. This keeps the platform fully runnable offline.
    deterministic_planning: EnvBool = Field(alias="CREDIT_AGENT_DETERMINISTIC_PLANNING", default=False)
    experience_recall_limit: int = Field(alias="CREDIT_AGENT_EXPERIENCE_RECALL_LIMIT", default=3)


class EnvConfig(_EnvBase):
    llm: LLMConfig = Field(default_factory=LLMConfig)
    embedding: EmbeddingConfig = Field(default_factory=EmbeddingConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    api: APIConfig = Field(default_factory=APIConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)

    @property
    def planning_mode(self) -> str:
        """``llm`` when a model is reachable, otherwise ``baseline``."""
        if self.agent.deterministic_planning or not self.llm.configured:
            return "baseline"
        return "llm"


_instance: EnvConfig | None = None
_lock = threading.Lock()

#: Fingerprint of the config files the current instance was built from.
_config_stamp: tuple | None = None
#: ``time.monotonic()`` of the last filesystem look-up (throttles the stat call —
#: this runs on every tool invocation, and a stale credential costs far more
#: than three ``stat`` calls).
_stamp_checked_at: float = 0.0
_STAMP_TTL_SECONDS = 0.5


def _config_signature() -> tuple:
    """Fingerprint every config file that can change the answer.

    ``setup.html`` and ``config.json`` are ordinary files a person edits in a
    browser and saves; nothing tells this process about it. Comparing
    ``mtime``+``size`` is how an already-running MCP server notices — otherwise
    the page says ``odps`` while the process keeps using the ``duckdb`` it read
    at startup, and the only symptom is a confusing "table does not exist".
    """
    from .store import candidate_config_paths  # noqa: PLC0415 - avoids a cycle

    items = []
    for path in candidate_config_paths():
        try:
            st = path.stat()
        except OSError:
            continue
        items.append((str(path), st.st_mtime_ns, st.st_size))
    return tuple(items)


def _build() -> EnvConfig:
    """Validate the current process environment into a typed config."""
    env = dict(os.environ)
    return EnvConfig(
        llm=LLMConfig.model_validate(env),
        embedding=EmbeddingConfig.model_validate(env),
        data=DataConfig.model_validate(env),
        sandbox=SandboxConfig.model_validate(env),
        api=APIConfig.model_validate(env),
        agent=AgentConfig.model_validate(env),
    )


def _load() -> EnvConfig:
    """(Re)build the singleton, re-projecting any saved config first."""
    global _instance, _config_stamp, _stamp_checked_at
    with _lock:
        stamp = _config_signature()
        if _instance is not None and stamp == _config_stamp:
            _stamp_checked_at = time.monotonic()
            return _instance
        if _config_stamp is not None and stamp != _config_stamp:
            # The saved page changed under us. Re-apply it so the new values win
            # over the ones this process injected last time (``_injected`` marks
            # them replaceable, so they are not mistaken for real env vars).
            from .store import ensure_config_loaded  # noqa: PLC0415

            ensure_config_loaded()
        _instance = _build()
        _config_stamp = stamp
        _stamp_checked_at = time.monotonic()
        return _instance


def get_env_config() -> EnvConfig:
    """Process-wide singleton that follows edits to the saved configuration.

    Each section is validated against the process environment. Passing ``{}`` to
    the outer model would silently fall back to defaults for every nested
    section, so the environment is handed to each one explicitly.

    A saved ``setup.html`` edit is picked up within ``_STAMP_TTL_SECONDS`` — no
    restart required. This matters because the MCP server is long-lived: the
    analyst saves the page, the next tool call must already see it.
    """
    global _stamp_checked_at
    if _instance is None:
        return _load()
    now = time.monotonic()
    if now - _stamp_checked_at < _STAMP_TTL_SECONDS:
        return _instance
    _stamp_checked_at = now
    if _config_signature() == _config_stamp:
        return _instance
    return _load()


def reload_env_config() -> bool:
    """Force a re-read. Returns ``True`` when the configuration changed.

    ``_instance`` is dropped but the previous fingerprint is kept on purpose:
    :func:`_load` compares against it to decide whether the saved page needs
    re-projecting into the environment.
    """
    global _instance
    with _lock:
        _instance = None
    changed = _config_signature() != _config_stamp
    _load()
    return changed


def reset_env_config() -> None:
    global _instance, _config_stamp
    with _lock:
        _instance = None
        _config_stamp = None


def get_env_value(name: str, default: str = "") -> str:
    return os.environ.get(name, default)
