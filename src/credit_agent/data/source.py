"""Read-only warehouse access across every supported backend.

One interface, five backends:

============  =====================================================  ==================
backend       driver                                                 read-only enforced
============  =====================================================  ==================
``duckdb``    ``duckdb``                                             connection flag
``odps``      ``pyodps``                                             SQL text audit
``postgres``  ``psycopg2``                                           **session** flag
``holo``      ``psycopg2`` (Hologres speaks the PG protocol)         **session** flag
``mysql``     ``pymysql``                                            **session** flag
============  =====================================================  ==================

The distinction matters. ``SET SESSION ... READ ONLY`` / ``set_session(readonly=True)``
is enforced by the server, so a write statement fails even if the text audit
misses it. MaxCompute has no equivalent session flag, so ``odps`` relies on
:func:`assert_read_only` alone — the weakest of the three, and worth knowing
before pointing it at production.

Credentials are read from the environment only (see ``config.env_schema``).
They are never hard-coded, never logged, and :meth:`DataSource.safe_target`
returns a host/database label with the password structurally absent.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional

from ..config.env_schema import get_env_config
from ..config.paths import get_data_dir

# Statement kinds a read-only session may start with. `describe`/`show`/`explain`
# are metadata reads; `pragma` is DuckDB's.
_ALLOWED_STATEMENT_PREFIXES = ("select", "with", "describe", "show", "explain", "pragma", "desc")

# Anything that writes, changes schema, or moves data. Matched on the whole
# statement, not just the prefix, so `select 1; drop table t` is caught.
_WRITE_KEYWORDS = re.compile(
    r"(?i)\b("
    r"insert|update|delete|merge|upsert|replace\s+into|"
    r"drop|alter|create|truncate|rename|comment\s+on|"
    r"grant|revoke|attach|detach|copy|unload|export|"
    r"call|execute|prepare|deallocate|vacuum|analyze|"
    r"load\s+data|set\s+global|lock\s+tables"
    r")\b"
)

# `SET` is legal inside a read-only session for session-local knobs, but
# `SET GLOBAL` / `SET PASSWORD` are not. Handled separately from the list above
# so that DataWorks-style `SET odps.sql.*` preambles are stripped, not rejected.
_SET_STATEMENT = re.compile(r"(?i)^\s*set\s+(?!session\s+transaction\s+read\s+only)")

SUPPORTED_BACKENDS = ("duckdb", "odps", "postgres", "holo", "mysql")


class DataAccessError(RuntimeError):
    """Raised when a query is rejected or the backend is unavailable."""


# --------------------------------------------------------------------------- #
# SQL text audit
# --------------------------------------------------------------------------- #
def _scan(sql: str) -> tuple[list[int], str]:
    """Return top-level ``;`` positions and the SQL with comments blanked out.

    Comments are replaced by spaces rather than removed so that character
    offsets stay aligned; string and identifier literals are respected, which is
    what makes ``select ';' as sep`` a single statement.
    """
    out: list[str] = []
    semicolons: list[int] = []
    in_single = in_double = in_backtick = False
    i, n = 0, len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if not in_single and not in_double and not in_backtick:
            if ch == "-" and nxt == "-":
                while i < n and sql[i] not in "\r\n":
                    out.append(" ")
                    i += 1
                continue
            if ch == "/" and nxt == "*":
                out.append(" ")
                out.append(" ")
                i += 2
                while i < n and not (sql[i] == "*" and i + 1 < n and sql[i + 1] == "/"):
                    out.append(" ")
                    i += 1
                if i < n:
                    out.append(" ")
                    out.append(" ")
                    i += 2
                continue

        if ch == "'" and not in_double and not in_backtick:
            if in_single and nxt == "'":
                out.append(ch)
                out.append(nxt)
                i += 2
                continue
            in_single = not in_single
        elif ch == '"' and not in_single and not in_backtick:
            in_double = not in_double
        elif ch == "`" and not in_single and not in_double:
            if in_backtick and nxt == "`":
                out.append(ch)
                out.append(nxt)
                i += 2
                continue
            in_backtick = not in_backtick
        elif ch == ";" and not in_single and not in_double and not in_backtick:
            semicolons.append(len(out))

        out.append(ch)
        i += 1
    return semicolons, "".join(out)


def split_statements(sql: str) -> List[str]:
    """Split on top-level semicolons, dropping empty fragments."""
    positions, flat = _scan(sql or "")
    if not positions:
        return [flat] if flat.strip() else []
    parts: List[str] = []
    start = 0
    for pos in positions:
        fragment = flat[start:pos]
        if fragment.strip():
            parts.append(fragment)
        start = pos + 1
    tail = flat[start:]
    if tail.strip():
        parts.append(tail)
    return parts


def count_statements(sql: str) -> int:
    return len(split_statements(sql))


def assert_single_statement(sql: str) -> None:
    """Reject multi-statement input — the single most common paste accident."""
    if count_statements(sql) > 1:
        raise DataAccessError(
            "只允许一次执行一条 SQL：检测到多条语句。请删掉额外的语句（或分段提交），"
            "DML/DDL 一律不允许。"
        )


def _drop_leading_set_lines(sql: str) -> str:
    lines = [line for line in sql.splitlines() if not _SET_STATEMENT.match(line.strip())]
    return "\n".join(lines).strip()


def assert_read_only(sql: str) -> None:
    """Reject anything that is not a plain read, before a driver sees it."""
    statements = split_statements(sql)
    if not statements:
        raise DataAccessError("empty SQL statement")
    for statement in statements:
        body = statement.strip()
        # A leading `SET ...` preamble is tolerated (DataWorks habits); the
        # statement itself is still validated after the preamble is dropped.
        if _SET_STATEMENT.match(body):
            body = _drop_leading_set_lines(body)
            if not body:
                continue
        head = re.match(r"[a-z_]+", body.lower())
        if not head:
            raise DataAccessError("cannot determine the statement type; only SELECT is permitted")
        if head.group(0) not in _ALLOWED_STATEMENT_PREFIXES:
            raise DataAccessError(
                f"only read-only statements are permitted; got {head.group(0)!r}"
            )
        if _WRITE_KEYWORDS.search(body):
            raise DataAccessError("write or DDL statements are forbidden")


# --------------------------------------------------------------------------- #
# Interface
# --------------------------------------------------------------------------- #
class DataSource(ABC):
    """Minimal read-only query interface shared by every backend."""

    name: ClassVar[str] = "abstract"
    #: True when the server itself refuses writes; False when only the SQL text
    #: audit stands between a caller and a destructive statement.
    server_side_read_only: ClassVar[bool] = False

    @abstractmethod
    def query(self, sql: str, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Execute a read-only query and return rows as plain dicts."""

    @abstractmethod
    def tables(self) -> List[str]:
        """List table names visible to the configured credentials."""

    def schema(self, table: str) -> List[Dict[str, str]]:
        """Column name/type pairs for ``table``. Best effort, never raises."""
        return []

    def query_df(self, sql: str, *, limit: Optional[int] = None):
        """Execute a read-only query and return a DataFrame."""
        import pandas as pd  # noqa: PLC0415

        return pd.DataFrame(self.query(sql, limit=limit))

    def safe_target(self) -> str:
        """A host/database label safe to show in reports. No credentials."""
        return self.name

    def test_connection(self) -> Dict[str, Any]:
        """Ping the backend and report capabilities without leaking secrets."""
        info: Dict[str, Any] = {
            "backend": self.name,
            "target": self.safe_target(),
            "server_side_read_only": self.server_side_read_only,
            "ok": False,
        }
        try:
            self.query("SELECT 1 AS ok")
            info["ok"] = True
        except Exception as exc:  # noqa: BLE001 - diagnostics must not raise
            info["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        return info

    def close(self) -> None:
        """Release connections. Safe to call more than once."""

    # -- shared helpers ----------------------------------------------------
    @staticmethod
    def assert_read_only(sql: str) -> None:
        assert_read_only(sql)


# --------------------------------------------------------------------------- #
# DuckDB (local, tests + bundled demo)
# --------------------------------------------------------------------------- #
class DuckDBSource(DataSource):
    """Local warehouse over the bundled synthetic credit dataset."""

    name = "duckdb"
    server_side_read_only = True

    def __init__(self, db_path: Optional[Path] = None, *, read_only: bool = True) -> None:
        try:
            import duckdb  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise DataAccessError("duckdb is required for the local warehouse backend") from exc
        cfg = get_env_config()
        self.duckdb = duckdb
        self.path = Path(db_path) if db_path else Path(cfg.data.duckdb_path or (get_data_dir() / "credit.duckdb"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.read_only = read_only
        self._conn = None

    @property
    def conn(self):  # lazy: opening a missing file read-only fails
        if self._conn is None:
            if self.read_only and self.path.exists():
                self._conn = self.duckdb.connect(str(self.path), read_only=True)
            else:
                self._conn = self.duckdb.connect(str(self.path))
        return self._conn

    def query(self, sql: str, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        assert_read_only(sql)
        cfg = get_env_config()
        cap = limit or cfg.data.max_scan_rows
        cur = self.conn.execute(sql)
        cols = [d[0] for d in cur.description]
        rows = cur.fetchmany(cap)
        return [dict(zip(cols, r)) for r in rows]

    def tables(self) -> List[str]:
        try:
            rows = self.query("SHOW TABLES")
        except Exception:
            return []
        return [list(r.values())[0] for r in rows]

    def schema(self, table: str) -> List[Dict[str, str]]:
        if not re.fullmatch(r"[A-Za-z0-9_\.]+", table):
            raise DataAccessError(f"invalid table name: {table!r}")
        rows = self.query(f"DESCRIBE {table}")
        return [{"column": r["column_name"], "type": r["column_type"]} for r in rows]

    def safe_target(self) -> str:
        return f"duckdb:{self.path.name}"

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


# --------------------------------------------------------------------------- #
# ODPS / MaxCompute
# --------------------------------------------------------------------------- #
class ODPSSource(DataSource):
    """MaxCompute / ODPS adapter (read-only by text audit only).

    Credentials come **only** from the environment - they are never hard-coded
    and never logged::

        CREDIT_AGENT_ODPS_ACCESS_ID / _ACCESS_KEY / _ENDPOINT / _PROJECT

    When ``tools/odps_conn_opmgt.py`` (or ``tools/odps_conn.py``) is importable
    it is reused, so credentials and project wiring stay in one place.
    """

    name = "odps"
    # MaxCompute has no read-only session flag; only the text audit applies.
    server_side_read_only = False

    # Order matters: opmgt_dev is the warehouse the credit topics query.
    _TOOL_MODULES = ("odps_conn_opmgt", "odps_conn")

    DEFAULT_ENDPOINT = "http://service.cn.maxcompute.aliyun.com/api"

    def __init__(self, *, project: Optional[str] = None) -> None:
        cfg = get_env_config()
        self.endpoint = cfg.data.odps_endpoint or self.DEFAULT_ENDPOINT
        self.access_id = cfg.data.odps_access_id
        self.access_key = cfg.data.odps_access_key
        self.project = project or cfg.data.odps_project
        if not all([self.endpoint, self.access_id, self.access_key, self.project]):
            raise DataAccessError(
                "ODPS credentials incomplete; set CREDIT_AGENT_ODPS_ENDPOINT/ACCESS_ID/ACCESS_KEY/PROJECT"
            )
        self._odps = self._build_client()

    _INSTALL_HINT = "ODPS backend requires: pip install pyodps (or INSTALL_EXTRAS=odps)"

    def _build_client(self):
        """Prefer the bundled ``tools/`` helper, else a direct ODPS client."""
        tool = self._load_tool()
        if tool is not None and getattr(tool, "DEFAULT_PROJECT", None) == self.project:
            try:
                return tool.get_odps()
            except ImportError as exc:
                raise DataAccessError(self._INSTALL_HINT) from exc
        try:
            from odps import ODPS  # type: ignore  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise DataAccessError(self._INSTALL_HINT) from exc
        return ODPS(self.access_id, self.access_key, project=self.project, endpoint=self.endpoint)

    @classmethod
    def _load_tool(cls):
        """Import the bundled ODPS helper if it is on ``sys.path`` or adjacent."""
        import importlib
        import sys

        for mod_name in cls._TOOL_MODULES:
            if mod_name in sys.modules:
                candidate = sys.modules[mod_name]
            else:
                root = Path(__file__).resolve().parents[3] / "tools"
                if str(root) not in sys.path:
                    sys.path.insert(0, str(root))
                try:
                    candidate = importlib.import_module(mod_name)
                except Exception:
                    continue
            try:
                candidate.get_credentials()
            except Exception:
                continue  # helper present but secrets unset -> fall back
            return candidate
        return None

    def query(self, sql: str, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        assert_read_only(sql)
        with self._odps.execute_sql(sql).open_reader() as reader:
            cols = [c.name for c in reader.schema.columns]
            rows = []
            cap = limit or get_env_config().data.max_scan_rows
            for i, record in enumerate(reader):
                if i >= cap:
                    break
                rows.append(dict(zip(cols, [record[c] for c in cols])))
        return rows

    def tables(self) -> List[str]:
        return [t.name for t in self._odps.list_tables()]

    def schema(self, table: str) -> List[Dict[str, str]]:
        if not re.fullmatch(r"[A-Za-z0-9_\.]+", table):
            raise DataAccessError(f"invalid table name: {table!r}")
        try:
            t = self._odps.get_table(table)
        except Exception:
            return []
        return [{"column": c.name, "type": str(c.type)} for c in t.table_schema.columns]

    def safe_target(self) -> str:
        host = re.sub(r"^https?://", "", self.endpoint).split("/")[0]
        return f"odps:{self.project}@{host}"


#: Backwards-compatible alias — earlier releases and tests import this name.
ODPSAdapter = ODPSSource


# --------------------------------------------------------------------------- #
# PostgreSQL / Hologres
# --------------------------------------------------------------------------- #
class PostgresSource(DataSource):
    """PostgreSQL reader, also used for Hologres (identical wire protocol).

    Read-only is enforced **by the server**: the session is opened with
    ``readonly=True`` and ``autocommit=True``, so a stray ``INSERT`` is rejected
    by PostgreSQL itself rather than by our regexes.
    """

    name = "postgres"
    server_side_read_only = True
    DEFAULT_PORT = 5432
    _INSTALL_HINT = "PostgreSQL/Hologres backend requires: pip install psycopg2-binary"

    def __init__(self, *, host: Optional[str] = None, port: Optional[int] = None,
                 database: Optional[str] = None, user: Optional[str] = None,
                 password: Optional[str] = None, sslmode: Optional[str] = None,
                 schema: Optional[str] = None, connect_timeout: Optional[int] = None,
                 dsn: Optional[str] = None) -> None:
        cfg = get_env_config().data
        self.host = host or cfg.pg_host
        self.port = int(port or cfg.pg_port or self.DEFAULT_PORT)
        self.database = database or cfg.pg_database
        self.user = user or cfg.pg_user
        self._password = password if password is not None else cfg.pg_password
        self.sslmode = sslmode if sslmode is not None else cfg.pg_sslmode
        self.schema_name = schema if schema is not None else cfg.pg_schema
        self.connect_timeout = int(connect_timeout or cfg.connect_timeout_seconds or 30)
        self.dsn = dsn
        self._conn = None
        if not self.dsn and not all([self.host, self.database, self.user]):
            raise DataAccessError(
                "PostgreSQL/Hologres connection incomplete; set CREDIT_AGENT_PG_HOST/"
                "DATABASE/USER/PASSWORD (or pass a DSN)"
            )

    def _connect(self):
        try:
            import psycopg2  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise DataAccessError(self._INSTALL_HINT) from exc
        kwargs: Dict[str, Any] = {
            "connect_timeout": self.connect_timeout,
            "application_name": "credit-risk-agent",
            "client_encoding": "utf8",
        }
        if self.sslmode:
            kwargs["sslmode"] = self.sslmode
        if self.schema_name:
            # Pins unqualified table names to one schema; saves analysts from
            # prefixing every table with `public.`
            kwargs["options"] = f"-c search_path={self.schema_name}"
        if self.dsn:
            conn = psycopg2.connect(self.dsn, **kwargs)
        else:
            conn = psycopg2.connect(host=self.host, port=self.port, dbname=self.database,
                                    user=self.user, password=self._password, **kwargs)
        # Server-enforced read-only. `autocommit=True` keeps a long extract from
        # holding an open write-capable transaction.
        conn.set_session(readonly=True, autocommit=True)
        return conn

    @property
    def conn(self):
        if self._conn is None or getattr(self._conn, "closed", 0):
            self._conn = self._connect()
        return self._conn

    def query(self, sql: str, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        assert_read_only(sql)
        cap = limit or get_env_config().data.max_scan_rows
        cur = self.conn.cursor()
        try:
            cur.execute(sql)
            if cur.description is None:  # pragma: no cover - guarded upstream
                return []
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchmany(cap)]
        finally:
            cur.close()

    def tables(self) -> List[str]:
        rows = self.query(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema NOT IN ('pg_catalog', 'information_schema') "
            "ORDER BY table_schema, table_name"
        )
        return [f"{r['table_schema']}.{r['table_name']}" for r in rows]

    def schema(self, table: str) -> List[Dict[str, str]]:
        if not re.fullmatch(r"[A-Za-z0-9_\.]+", table):
            raise DataAccessError(f"invalid table name: {table!r}")
        schema_name, _, table_name = table.rpartition(".")
        sql = (
            "SELECT column_name, data_type FROM information_schema.columns "
            f"WHERE table_name = '{table_name}'"
            + (f" AND table_schema = '{schema_name}'" if schema_name else "")
            + " ORDER BY ordinal_position"
        )
        return [{"column": r["column_name"], "type": r["data_type"]} for r in self.query(sql)]

    def safe_target(self) -> str:
        return f"{self.name}:{self.host}:{self.port}/{self.database}"

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


class HologresSource(PostgresSource):
    """Hologres reader — PostgreSQL protocol, port 80 by default.

    Hologres exposes the PostgreSQL wire protocol, so the only differences are
    the default port and the label. Everything else (including the server-side
    read-only session) is inherited.
    """

    name = "holo"
    DEFAULT_PORT = 80


# --------------------------------------------------------------------------- #
# MySQL / MariaDB
# --------------------------------------------------------------------------- #
class MySQLSource(DataSource):
    """MySQL / MariaDB reader with a server-enforced read-only session.

    ``SET SESSION TRANSACTION READ ONLY`` needs MySQL 5.7+ / MariaDB 10.0+. On
    an older server the statement fails; that is recorded in
    :attr:`readonly_enforced` instead of being silently ignored, so
    :meth:`test_connection` can report honestly that only the text audit applies.
    """

    name = "mysql"
    server_side_read_only = True
    DEFAULT_PORT = 3306
    _INSTALL_HINT = "MySQL backend requires: pip install pymysql"

    def __init__(self, *, host: Optional[str] = None, port: Optional[int] = None,
                 database: Optional[str] = None, user: Optional[str] = None,
                 password: Optional[str] = None, charset: Optional[str] = None,
                 connect_timeout: Optional[int] = None) -> None:
        cfg = get_env_config().data
        self.host = host or cfg.mysql_host
        self.port = int(port or cfg.mysql_port or self.DEFAULT_PORT)
        self.database = database or cfg.mysql_database
        self.user = user or cfg.mysql_user
        self._password = password if password is not None else cfg.mysql_password
        self.charset = charset or cfg.mysql_charset or "utf8mb4"
        self.connect_timeout = int(connect_timeout or cfg.connect_timeout_seconds or 30)
        self._conn = None
        self.readonly_enforced = False
        if not all([self.host, self.database, self.user]):
            raise DataAccessError(
                "MySQL connection incomplete; set CREDIT_AGENT_MYSQL_HOST/DATABASE/USER/PASSWORD"
            )

    def _connect(self):
        try:
            import pymysql  # noqa: PLC0415
            from pymysql.cursors import DictCursor  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover
            raise DataAccessError(self._INSTALL_HINT) from exc
        conn = pymysql.connect(
            host=self.host, port=self.port, user=self.user, password=self._password,
            database=self.database, charset=self.charset, cursorclass=DictCursor,
            connect_timeout=self.connect_timeout, autocommit=True,
        )
        try:
            with conn.cursor() as cur:
                cur.execute("SET SESSION TRANSACTION READ ONLY")
            self.readonly_enforced = True
        except Exception:
            # Old server or restricted account: fall back to the text audit, but
            # remember that the guarantee is weaker.
            self.readonly_enforced = False
        return conn

    @property
    def conn(self):
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    def query(self, sql: str, *, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        assert_read_only(sql)
        cap = limit or get_env_config().data.max_scan_rows
        with self.conn.cursor() as cur:
            cur.execute(sql)
            if cur.description is None:  # pragma: no cover - guarded upstream
                return []
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, [row.get(c) for c in cols])) for row in cur.fetchmany(cap)]

    def tables(self) -> List[str]:
        rows = self.query(
            "SELECT table_name FROM information_schema.tables "
            f"WHERE table_schema = '{self.database}' ORDER BY table_name"
        )
        return [r["table_name"] for r in rows]

    def schema(self, table: str) -> List[Dict[str, str]]:
        if not re.fullmatch(r"[A-Za-z0-9_\.]+", table):
            raise DataAccessError(f"invalid table name: {table!r}")
        schema_name, _, table_name = table.rpartition(".")
        owner = schema_name or self.database
        sql = (
            "SELECT column_name, data_type FROM information_schema.columns "
            f"WHERE table_name = '{table_name}' AND table_schema = '{owner}' "
            "ORDER BY ordinal_position"
        )
        return [{"column": r["column_name"], "type": r["data_type"]} for r in self.query(sql)]

    def safe_target(self) -> str:
        return f"mysql:{self.host}:{self.port}/{self.database}"

    def test_connection(self) -> Dict[str, Any]:
        info = super().test_connection()
        info["server_side_read_only"] = self.readonly_enforced
        if self._conn is not None and not self.readonly_enforced:
            info["note"] = "服务端不支持 SET SESSION TRANSACTION READ ONLY，仅保留 SQL 文本审计"
        return info

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #
_BUILDERS = {
    "duckdb": DuckDBSource,
    "odps": ODPSSource,
    "postgres": PostgresSource,
    "holo": HologresSource,
    "mysql": MySQLSource,
}

_source: Optional[DataSource] = None


def build_data_source(backend: Optional[str] = None, **kwargs: Any) -> DataSource:
    """Construct a backend by name without touching the process-wide singleton."""
    name = (backend or get_env_config().data.backend or "duckdb").strip().lower()
    builder = _BUILDERS.get(name)
    if builder is None:
        raise DataAccessError(
            f"unknown backend {name!r}; supported: {', '.join(SUPPORTED_BACKENDS)}"
        )
    return builder(**kwargs)


def get_data_source(reload: bool = False) -> DataSource:
    """Process-wide data source chosen by ``CREDIT_AGENT_DATA_BACKEND``."""
    global _source
    if _source is None or reload:
        _source = build_data_source()
    return _source


def reset_data_source() -> None:
    """Drop the cached source (tests, and after credentials change)."""
    global _source
    if _source is not None:
        _source.close()
    _source = None


#: backend -> (import name, pip package). Hologres reuses the PostgreSQL driver.
DRIVER_PACKAGES: Dict[str, tuple[str, str]] = {
    "duckdb": ("duckdb", "duckdb"),
    "odps": ("odps", "pyodps"),
    "postgres": ("psycopg2", "psycopg2-binary"),
    "holo": ("psycopg2", "psycopg2-binary"),
    "mysql": ("pymysql", "pymysql"),
}


def driver_available(backend: str) -> tuple[bool, str]:
    """``(installed, pip-hint)`` for one backend.

    Kept separate from :func:`describe_backends` because a missing driver and a
    wrong password are different failures that look identical from the outside —
    and conflating them is the classic hour-long detour ("the host is fine, the
    password is fine, so why..."). ``find_spec`` locates the package without
    importing it: importing ``pyodps`` costs ~300ms and emits deprecation noise,
    which is a poor trade for a yes/no answer.
    """
    import importlib.util  # noqa: PLC0415

    module, package = DRIVER_PACKAGES.get((backend or "").strip().lower(), ("", ""))
    if not module:
        return False, ""
    try:
        if importlib.util.find_spec(module) is None:
            return False, f"pip install {package}"
        return True, ""
    except (ImportError, ValueError):  # broken install / namespace shadowing
        return False, f"pip install {package}"


def describe_backends() -> List[Dict[str, Any]]:
    """Capability matrix + configuration status for every backend.

    Used by ``download_sample`` diagnostics and the installer's verification
    step; contains no credentials.
    """
    cfg = get_env_config()
    rows: List[Dict[str, Any]] = []
    for name in SUPPORTED_BACKENDS:
        builder = _BUILDERS[name]
        configured, missing = True, []
        if name == "odps":
            configured = bool(cfg.data.odps_access_id and cfg.data.odps_access_key and cfg.data.odps_project)
            missing = [] if configured else ["CREDIT_AGENT_ODPS_ACCESS_ID/KEY/PROJECT"]
        elif name in ("postgres", "holo"):
            configured = bool(cfg.data.pg_host and cfg.data.pg_database and cfg.data.pg_user)
            missing = [] if configured else ["CREDIT_AGENT_PG_HOST/DATABASE/USER/PASSWORD"]
        elif name == "mysql":
            configured = bool(cfg.data.mysql_host and cfg.data.mysql_database and cfg.data.mysql_user)
            missing = [] if configured else ["CREDIT_AGENT_MYSQL_HOST/DATABASE/USER/PASSWORD"]
        rows.append({
            "backend": name,
            "active": name == (cfg.data.backend or "duckdb").strip().lower(),
            "configured": configured,
            "missing": missing,
            "server_side_read_only": builder.server_side_read_only,
        })
    return rows


__all__ = [
    "DRIVER_PACKAGES",
    "SUPPORTED_BACKENDS",
    "DataAccessError",
    "DataSource",
    "DuckDBSource",
    "HologresSource",
    "MySQLSource",
    "ODPSAdapter",
    "ODPSSource",
    "PostgresSource",
    "assert_read_only",
    "assert_single_statement",
    "build_data_source",
    "count_statements",
    "describe_backends",
    "driver_available",
    "get_data_source",
    "reset_data_source",
    "split_statements",
]
