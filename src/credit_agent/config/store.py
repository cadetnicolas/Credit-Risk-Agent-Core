"""Structured configuration, stored either as JSON or inside an HTML page.

Why not just ``.env``
---------------------
``.env`` works for engineers and is hostile to everyone else: one misspelt
``CREDIT_AGENT_PG_PASSWORD`` produces "credentials incomplete" pointing at no
particular line, and a stray space after ``=`` is invisible. So there is a second
front end — a self-contained page (``setup.html``) with a real form, typed
fields, per-backend help and an inline completeness check.

The page holds its own data
---------------------------
Nothing is written to a side file and no local server is involved: the settings
live inside the page itself, in an inert
``<script id="credit-agent-config" type="application/json">`` block. The form
updates that block on every keystroke, the Save button writes the whole document
back (or downloads it), and the loader here reads the block straight out of the
HTML. One file to open, fill in and keep.

Resolution order, highest priority first
----------------------------------------
1. the **real** process environment: ``mcp.json`` ``env``, shell export, CI secret
2. a config file, whichever this finds first:

   a. ``$CREDIT_AGENT_CONFIG`` (explicit, and may itself be the ``.html`` page)
   b. ``~/.credit-agent/config.json``
   c. ``~/.credit-agent/setup.html``

3. ``.env`` — hand-edited
4. built-in defaults

"Real" means *already present before this package touched* ``os.environ``. A
value that ``.env`` injected is deliberately **not** protected: the saved page is
the more recent and more deliberate edit, so it wins. This is the opposite of
:mod:`credit_agent.config.dotenv`'s rule, and the split is intentional — ``.env``
must never clobber a host-injected credential, while a person who fills in a form
and presses Save expects the form to take effect.

One subtlety worth stating: ``postgres`` and ``holo`` share the
``CREDIT_AGENT_PG_*`` variables. Only the **active** backend is exported, because
exporting both would leave whichever ran last in charge — the classic "I changed
Hologres but it still connects to the old PostgreSQL" bug.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set

from .paths import get_runtime_root

#: Point this at a specific file when you keep several profiles side by side.
#: A ``.html`` value is read as a setup page; anything else as JSON.
CONFIG_FILE_VAR = "CREDIT_AGENT_CONFIG"

#: File name of the self-contained page inside the runtime root.
HTML_CONFIG_NAME = "setup.html"

SCHEMA_VERSION = 1

#: Backend field definitions. Single source of truth for the form, the
#: environment mapping, validation and the completeness check — adding a backend
#: here is enough to make it appear on the setup page.
FIELD_SPEC: Dict[str, Dict[str, Any]] = {
    "duckdb": {
        "label": "DuckDB",
        "summary": "本地 .duckdb 文件，无需凭证。演示数据和离线分析默认用它。",
        "readonly": "连接级只读（read_only=True）",
        "driver": "duckdb",
        "fields": [
            {
                "name": "path",
                "env": "CREDIT_AGENT_DUCKDB_PATH",
                "attr": "duckdb_path",
                "label": "数据库文件路径",
                "kind": "text",
                "placeholder": "留空 = ~/.credit-agent/data/credit.duckdb",
            },
        ],
        "required": [],
    },
    "holo": {
        "label": "Hologres",
        "summary": "阿里云 Hologres，走 PostgreSQL 协议，默认端口 80。",
        "readonly": "服务端会话只读",
        "driver": "psycopg2-binary",
        "fields": [
            {"name": "host", "env": "CREDIT_AGENT_PG_HOST", "attr": "pg_host", "label": "主机地址",
             "kind": "text", "placeholder": "hgpostcn-xxx.hologres.aliyuncs.com"},
            {"name": "port", "env": "CREDIT_AGENT_PG_PORT", "attr": "pg_port", "label": "端口",
             "kind": "int", "placeholder": "80"},
            {"name": "database", "env": "CREDIT_AGENT_PG_DATABASE", "attr": "pg_database", "label": "数据库",
             "kind": "text", "placeholder": "rtc_adhoc"},
            {"name": "user", "env": "CREDIT_AGENT_PG_USER", "attr": "pg_user", "label": "用户名",
             "kind": "text", "placeholder": ""},
            {"name": "password", "env": "CREDIT_AGENT_PG_PASSWORD", "attr": "pg_password", "label": "密码",
             "kind": "secret", "placeholder": ""},
            {"name": "schema", "env": "CREDIT_AGENT_PG_SCHEMA", "attr": "pg_schema", "label": "默认 schema",
             "kind": "text", "placeholder": "留空则不加 search_path"},
            {"name": "sslmode", "env": "CREDIT_AGENT_PG_SSLMODE", "attr": "pg_sslmode", "label": "SSL 模式",
             "kind": "select", "placeholder": "",
             "options": ["", "disable", "require", "verify-ca", "verify-full"]},
        ],
        "required": ["host", "database", "user", "password"],
        "shares_env_with": "postgres",
    },
    "postgres": {
        "label": "PostgreSQL",
        "summary": "标准 PostgreSQL，默认端口 5432。与 Hologres 共用同一组变量名但分别保存。",
        "readonly": "服务端会话只读",
        "driver": "psycopg2-binary",
        "fields": [
            {"name": "host", "env": "CREDIT_AGENT_PG_HOST", "attr": "pg_host", "label": "主机地址",
             "kind": "text", "placeholder": "pgm-xxx.pg.rds.aliyuncs.com"},
            {"name": "port", "env": "CREDIT_AGENT_PG_PORT", "attr": "pg_port", "label": "端口",
             "kind": "int", "placeholder": "5432"},
            {"name": "database", "env": "CREDIT_AGENT_PG_DATABASE", "attr": "pg_database", "label": "数据库",
             "kind": "text", "placeholder": ""},
            {"name": "user", "env": "CREDIT_AGENT_PG_USER", "attr": "pg_user", "label": "用户名",
             "kind": "text", "placeholder": ""},
            {"name": "password", "env": "CREDIT_AGENT_PG_PASSWORD", "attr": "pg_password", "label": "密码",
             "kind": "secret", "placeholder": ""},
            {"name": "schema", "env": "CREDIT_AGENT_PG_SCHEMA", "attr": "pg_schema", "label": "默认 schema",
             "kind": "text", "placeholder": "留空则不加 search_path"},
            {"name": "sslmode", "env": "CREDIT_AGENT_PG_SSLMODE", "attr": "pg_sslmode", "label": "SSL 模式",
             "kind": "select", "placeholder": "",
             "options": ["", "disable", "require", "verify-ca", "verify-full"]},
        ],
        "required": ["host", "database", "user", "password"],
    },
    "mysql": {
        "label": "MySQL",
        "summary": "MySQL / MariaDB，默认端口 3306。只读由服务端会话保证（需 5.7+）。",
        "readonly": "服务端会话只读",
        "driver": "pymysql",
        "fields": [
            {"name": "host", "env": "CREDIT_AGENT_MYSQL_HOST", "attr": "mysql_host", "label": "主机地址",
             "kind": "text", "placeholder": "rm-xxx.mysql.rds.aliyuncs.com"},
            {"name": "port", "env": "CREDIT_AGENT_MYSQL_PORT", "attr": "mysql_port", "label": "端口",
             "kind": "int", "placeholder": "3306"},
            {"name": "database", "env": "CREDIT_AGENT_MYSQL_DATABASE", "attr": "mysql_database", "label": "数据库",
             "kind": "text", "placeholder": ""},
            {"name": "user", "env": "CREDIT_AGENT_MYSQL_USER", "attr": "mysql_user", "label": "用户名",
             "kind": "text", "placeholder": ""},
            {"name": "password", "env": "CREDIT_AGENT_MYSQL_PASSWORD", "attr": "mysql_password", "label": "密码",
             "kind": "secret", "placeholder": ""},
            {"name": "charset", "env": "CREDIT_AGENT_MYSQL_CHARSET", "attr": "mysql_charset", "label": "字符集",
             "kind": "text", "placeholder": "utf8mb4"},
        ],
        "required": ["host", "database", "user", "password"],
    },
    "odps": {
        "label": "MaxCompute",
        "summary": "阿里云 MaxCompute / ODPS。无服务端只读开关，只由 SQL 文本审计保护，务必用只读子账号。",
        "readonly": "仅 SQL 文本审计",
        "driver": "pyodps",
        "fields": [
            {"name": "endpoint", "env": "CREDIT_AGENT_ODPS_ENDPOINT", "attr": "odps_endpoint", "label": "接入地址",
             "kind": "text", "placeholder": "留空用默认 endpoint"},
            {"name": "project", "env": "CREDIT_AGENT_ODPS_PROJECT", "attr": "odps_project", "label": "项目空间",
             "kind": "text", "placeholder": "例如 dwd_prod"},
            {"name": "access_id", "env": "CREDIT_AGENT_ODPS_ACCESS_ID", "attr": "odps_access_id", "label": "AccessKey ID",
             "kind": "text", "placeholder": ""},
            {"name": "access_key", "env": "CREDIT_AGENT_ODPS_ACCESS_KEY", "attr": "odps_access_key", "label": "AccessKey Secret",
             "kind": "secret", "placeholder": ""},
        ],
        "required": ["project", "access_id", "access_key"],
    },
}

#: Display order on the setup page.
BACKEND_ORDER = ("holo", "postgres", "mysql", "odps", "duckdb")

_LIMIT_SPEC = {
    "max_scan_rows": {"env": "CREDIT_AGENT_MAX_SCAN_ROWS", "label": "单次最多取多少行",
                      "default": 2_000_000, "low": 1, "high": 100_000_000},
    "connect_timeout_seconds": {"env": "CREDIT_AGENT_CONNECT_TIMEOUT", "label": "连接超时（秒）",
                                "default": 30, "low": 1, "high": 600},
}

#: Fields that must never leave the process.
_SECRET_FIELDS = frozenset(
    f"{backend}.{field['name']}"
    for backend, spec in FIELD_SPEC.items()
    for field in spec["fields"]
    if field["kind"] == "secret"
)


class ConfigError(ValueError):
    """The submitted settings cannot be stored as given."""


# --------------------------------------------------------------------------- #
# Path and file IO
# --------------------------------------------------------------------------- #
def _explicit_path() -> Optional[Path]:
    explicit = os.environ.get(CONFIG_FILE_VAR, "").strip()
    return Path(explicit).expanduser() if explicit else None


def config_file_path() -> Path:
    """Where the setup page is written when there is no config yet.

    The explicit override wins; otherwise the page lives beside the runtime root
    so ``~/.credit-agent`` stays the single directory to back up or delete.
    """
    explicit = _explicit_path()
    if explicit is not None:
        return explicit
    return get_runtime_root() / "setup.html"


def candidate_config_paths() -> List[Path]:
    """Search order. An explicit override is exact — no fallback.

    A typo in ``CREDIT_AGENT_CONFIG`` must be visible rather than silently
    ignored, which is how "I changed the password and nothing happened" starts.
    """
    explicit = _explicit_path()
    if explicit is not None:
        return [explicit]
    root = get_runtime_root()
    return [root / "config.json", root / HTML_CONFIG_NAME]


def default_config() -> Dict[str, Any]:
    return normalize({})


def _as_int(value: Any, low: int, high: int, *, where: str) -> Optional[int]:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{where} 需要是整数，收到 {value!r}") from exc
    if number < low or number > high:
        raise ConfigError(f"{where} 需要在 {low}–{high} 之间，收到 {number}")
    return number


def normalize(raw: Any, *, strict: bool = False) -> Dict[str, Any]:
    """Coerce arbitrary input into the canonical shape.

    ``strict=False`` is used when reading from disk: a hand-edited value that no
    longer parses falls back to the default instead of preventing the whole tool
    from starting. ``strict=True`` is used by the save endpoint so the person
    who typed the bad value is told which field it was.
    """
    issues: List[str] = []

    def fail(field: str, message: str) -> None:
        if strict:
            raise ConfigError(message)
        issues.append(f"{field}: {message}")

    source = raw if isinstance(raw, dict) else {}
    backend = str(source.get("active_backend") or "duckdb").strip().lower()
    if backend not in FIELD_SPEC:
        fail("active_backend", f"未知后端 {backend!r}")
        backend = "duckdb"

    limits_raw = source.get("limits") if isinstance(source.get("limits"), dict) else {}
    limits: Dict[str, Optional[int]] = {}
    for name, spec in _LIMIT_SPEC.items():
        try:
            limits[name] = _as_int(limits_raw.get(name), spec["low"], spec["high"],
                                   where=f"limits.{name}")
        except ConfigError as exc:
            fail(f"limits.{name}", str(exc))
            limits[name] = None

    backends_raw = source.get("backends") if isinstance(source.get("backends"), dict) else {}
    backends: Dict[str, Dict[str, Any]] = {}
    for name, spec in FIELD_SPEC.items():
        submitted = backends_raw.get(name)
        submitted = submitted if isinstance(submitted, dict) else {}
        values: Dict[str, Any] = {}
        for field in spec["fields"]:
            fname = field["name"]
            raw_value = submitted.get(fname)
            if field["kind"] == "int":
                try:
                    values[fname] = _as_int(raw_value, 1, 65535, where=f"{name}.{fname}")
                except ConfigError as exc:
                    fail(f"{name}.{fname}", str(exc))
                    values[fname] = None
                continue
            text = "" if raw_value is None else str(raw_value).strip()
            if field["kind"] == "select" and text not in field.get("options", [""]):
                text = ""
            values[fname] = text
        backends[name] = values

    return {
        "version": SCHEMA_VERSION,
        "active_backend": backend,
        "limits": limits,
        "backends": backends,
        "updated_at": str(source.get("updated_at") or ""),
        "issues": issues,
    }


#: The inert data block the setup page carries. ``[^>]*`` before ``>`` matters:
#: the template writes ``id="credit-agent-config" type="application/json"`` and
#: the attribute order is not something a browser guarantees on save.
_HTML_BLOCK_RE = re.compile(
    r'<script\b[^>]*\bid\s*=\s*["\']credit-agent-config["\'][^>]*>(?P<body>.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def json_for_html(data: Any) -> str:
    """Serialise so the result is safe to drop inside a ``<script>`` block.

    ``</script>`` inside a string — which a password may well contain — would
    end the block early and break the page. Escaping ``<`` as ``\\u003c`` is the
    standard fix: JSON.parse restores it, the HTML parser never sees a tag.
    """
    return json.dumps(data, ensure_ascii=False, indent=2).replace("<", "\\u003c")


def extract_config_from_html(text: str) -> Optional[Dict[str, Any]]:
    """Pull the embedded JSON out of a saved setup page.

    Returns ``None`` when the block is absent or unparseable, so the caller can
    fall back instead of dying on a half-copied file.
    """
    match = _HTML_BLOCK_RE.search(text)
    if match is None:
        return None
    body = match.group("body").strip()
    if not body:
        return None
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _read_config_source(path: Path) -> Dict[str, Any]:
    """Load one candidate path. Handles both ``.json`` and ``.html``."""
    kind = "html" if path.suffix.lower() in (".html", ".htm") else "json"
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {"exists": False, "kind": kind, "path": str(path), "raw": None,
                "error": "", "warnings": []}
    except OSError as exc:
        return {"exists": True, "kind": kind, "path": str(path), "raw": None,
                "error": "", "warnings": [f"无法读取 {path}: {type(exc).__name__}"]}

    if kind == "html":
        raw = extract_config_from_html(text)
        if raw is None:
            return {"exists": True, "kind": kind, "path": str(path), "raw": None,
                    "error": f"{path.name} 里没有找到配置块（可能不是本工具生成的页面）",
                    "warnings": []}
    else:
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            return {"exists": True, "kind": kind, "path": str(path), "raw": None,
                    "error": f"{path.name} 不是合法 JSON（第 {exc.lineno} 行）", "warnings": []}
        if not isinstance(raw, dict):
            return {"exists": True, "kind": kind, "path": str(path), "raw": None,
                    "error": f"{path.name} 的内容不是对象", "warnings": []}

    return {"exists": True, "kind": kind, "path": str(path), "raw": raw,
            "error": "", "warnings": []}


def load_config_file() -> Dict[str, Any]:
    """Find the first usable config source and normalise it.

    A corrupt file is reported but never fatal: a broken page must not stop the
    tool from starting with defaults. Earlier candidates that exist but fail to
    parse are remembered in ``skipped`` so the message can say *which* file was
    skipped rather than pretending nothing was there.
    """
    skipped: List[str] = []
    for path in candidate_config_paths():
        source = _read_config_source(path)
        if not source["exists"]:
            continue
        if source["raw"] is None:
            skipped.append(source["error"] or f"{path} 无法解析")
            continue
        data = normalize(source["raw"])
        warnings = list(source["warnings"]) + list(data.get("issues") or [])
        warning = _permission_warning(path)
        if warning:
            warnings.append(warning)
        return {"exists": True, "path": str(path), "kind": source["kind"],
                "data": data, "warnings": warnings, "error": "", "skipped": skipped}

    primary = config_file_path()
    return {"exists": False, "path": str(primary), "kind": "html",
            "data": default_config(), "warnings": [], "error": "", "skipped": skipped}


def atomic_write(path: Path, payload: str, *, mode: int) -> None:
    """Replace ``path`` in one step.

    Worth the six extra lines: the setup page has a Save button, people
    double-click it, and a half-written file would make the next tool call fail
    with a parse error at startup.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".config-", suffix=path.suffix)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def json_config_path() -> Path:
    """Where programmatic saves go.

    An explicit ``CREDIT_AGENT_CONFIG`` is honoured unless it points at a page —
    writing raw JSON into ``setup.html`` would produce a file the loader cannot
    read, so that case falls back to the JSON beside the runtime root.
    """
    explicit = _explicit_path()
    if explicit is not None and explicit.suffix.lower() not in (".html", ".htm"):
        return explicit
    return get_runtime_root() / "config.json"


def save_config_file(raw: Any, path: Optional[Path] = None) -> Dict[str, Any]:
    """Validate and write ``config.json`` (``0600``).

    The page saves itself in the browser; this exists for scripted setup, CI, and
    ``credit-risk-config --print`` round trips.
    """
    data = normalize(raw, strict=True)
    data.pop("issues", None)
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    target = path or json_config_path()
    atomic_write(target, json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                 mode=stat.S_IRUSR | stat.S_IWUSR)
    return data



def _permission_warning(path: Path) -> str:
    try:
        mode = path.stat().st_mode
    except OSError:
        return ""
    if os.name != "posix":
        return ""
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        return f"{path} 权限过宽（建议 chmod 600），其中可能包含数据库密码"
    return ""


# --------------------------------------------------------------------------- #
# Environment projection
# --------------------------------------------------------------------------- #
def config_to_env(data: Dict[str, Any]) -> Dict[str, str]:
    """Map the **active** backend onto the variables the library already reads.

    Two things are easy to get wrong here and both are silent:

    * ``CREDIT_AGENT_DATA_BACKEND`` itself must be exported. Without it the
      backend's own variables are set but nothing reads them — the symptom is a
      page that says "Hologres" while the tool keeps querying DuckDB.
    * only the active backend is projected. ``postgres`` and ``holo`` write the
      same ``CREDIT_AGENT_PG_*`` names, so projecting both would make the last
      one win regardless of which is selected.
    """
    backend = str(data.get("active_backend") or "duckdb")
    spec = FIELD_SPEC.get(backend)
    if spec is None:
        return {}
    env: Dict[str, str] = {"CREDIT_AGENT_DATA_BACKEND": backend}
    values = (data.get("backends") or {}).get(backend) or {}
    for field in spec["fields"]:
        value = values.get(field["name"])
        if value is None or value == "":
            continue
        env[field["env"]] = str(value)
    limits = data.get("limits") or {}
    for name, limit_spec in _LIMIT_SPEC.items():
        value = limits.get(name)
        if value is not None:
            env[limit_spec["env"]] = str(int(value))
    return env


def seed_from_environment(data: Dict[str, Any]) -> Dict[str, Any]:
    """Fill a brand-new page with whatever is already in force.

    Without this, the first generated page would claim "duckdb, no credentials"
    while ``.env`` was happily pointing at Hologres — and pressing Save would
    silently wipe that setup, because a blank field is a deliberate "clear".
    Seeding makes "open the page and save it unchanged" a genuine no-op.

    This does copy the current password into the file. That is the same secret
    the ``.env`` it came from already held, the file is written ``0600``, and the
    page warns about it — but it is a deliberate trade, not an oversight.
    """
    from .env_schema import get_env_config  # noqa: PLC0415

    cfg = get_env_config().data
    seeded = json.loads(json.dumps(data))
    backend = (cfg.backend or "duckdb").strip().lower()
    if backend not in FIELD_SPEC:
        backend = "duckdb"
    seeded["active_backend"] = backend

    target = seeded["backends"].setdefault(backend, {})
    for field in FIELD_SPEC[backend]["fields"]:
        current = getattr(cfg, field.get("attr", ""), None)
        if current in (None, ""):
            continue
        target[field["name"]] = current
    for name, limit_spec in _LIMIT_SPEC.items():
        current = getattr(cfg, name, None)
        if current is not None and current != limit_spec["default"]:
            seeded.setdefault("limits", {})[name] = current
    return seeded


def _active_env_names(data: Dict[str, Any]) -> Set[str]:
    spec = FIELD_SPEC.get(str(data.get("active_backend") or "duckdb")) or {}
    return {field["env"] for field in spec.get("fields", [])}


#: Which variables this module wrote, and whose value is currently in force.
_injected: Dict[str, str] = {}
#: Variables that ``.env`` wrote during startup — replaceable, unlike real ones.
_dotenv_applied: Set[str] = set()


def _protected_names() -> Set[str]:
    """Environment variables that came from outside this package's files."""
    return {
        name for name in os.environ
        if name not in _dotenv_applied and name not in _injected
    }


def apply_config(data: Dict[str, Any], *, env_report: Optional[Dict[str, Any]] = None,
                 force: bool = False) -> Dict[str, Any]:
    """Project ``data`` into ``os.environ`` and report what happened.

    ``force=True`` ignores the process-environment precedence. Only the
    connection test uses it, because there the person is explicitly asking
    "would *these* values work?".
    """
    global _dotenv_applied
    if env_report is not None:
        _dotenv_applied = set(env_report.get("applied") or [])

    protected = set() if force else _protected_names()
    wanted = config_to_env(data)
    active_names = _active_env_names(data)

    report: Dict[str, Any] = {
        "applied": [], "cleared": [], "blocked_by_process_env": [],
        "active_backend": data.get("active_backend") or "duckdb",
    }

    for name, value in wanted.items():
        if name in protected:
            report["blocked_by_process_env"].append(name)
            continue
        os.environ[name] = value
        _injected[name] = "config_file"
        report["applied"].append(name)

    # A field left blank in the form must not silently inherit the value that
    # `.env` happened to define for the same variable name.
    for name in active_names:
        if name in wanted or name in protected:
            continue
        if name in _dotenv_applied or name in _injected:
            os.environ.pop(name, None)
            _injected.pop(name, None)
            report["cleared"].append(name)

    # Switching away from PostgreSQL leaves CREDIT_AGENT_PG_* behind; drop only
    # the ones this module set itself.
    for name in list(_injected):
        if name in wanted or name in active_names:
            continue
        if _injected.get(name) == "config_file":
            os.environ.pop(name, None)
            _injected.pop(name, None)
            report["cleared"].append(name)

    report["applied"].sort()
    report["cleared"].sort()
    report["blocked_by_process_env"].sort()
    return report


def ensure_config_loaded(env_report: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Startup hook: find the saved configuration and project it into the env.

    Called from :mod:`credit_agent.config` right after ``.env``. With no saved
    file this returns ``exists=False`` and changes nothing, so behaviour for
    everyone who configures through ``.env`` or ``mcp.json`` is exactly what it
    was before this module existed.
    """
    loaded = load_config_file()
    result: Dict[str, Any] = {
        "exists": loaded["exists"],
        "path": loaded["path"],
        "kind": loaded.get("kind", ""),
        "error": loaded["error"],
        "skipped": list(loaded.get("skipped") or []),
        "warnings": list(loaded["warnings"]),
        "applied": [],
    }
    if not loaded["exists"]:
        return result
    result["apply"] = apply_config(loaded["data"], env_report=env_report)
    result["applied"] = result["apply"]["applied"]
    result["active_backend"] = loaded["data"]["active_backend"]
    return result


# --------------------------------------------------------------------------- #
# Views for the setup page
# --------------------------------------------------------------------------- #
def _origin_of(env_name: str, *, backend: str, active: str, configured: bool) -> str:
    """Where the value currently in force came from.

    ``stored`` means "saved for a backend that is not selected" — a real state
    that confuses people ("I entered the password, why is it asking again?"), so
    it gets its own label instead of being folded into "configured".
    """
    if backend != active:
        return "stored" if configured else "unset"
    if _injected.get(env_name) == "config_file":
        return "config_file"
    if env_name in _dotenv_applied:
        return "dotenv"
    if os.environ.get(env_name, "").strip():
        return "process_env"
    return "unset" if not configured else "config_file"


def origins_for(data: Dict[str, Any]) -> Dict[str, str]:
    """``"holo.host" -> "config_file" | "process_env" | "dotenv" | "unset" | "stored"``.

    The page shows a warning only for ``process_env``, because that is the one
    case where editing the form and pressing Save changes nothing — the value
    comes from ``mcp.json``. Saying so up front is cheaper than debugging
    "I filled it in twice and it still fails".
    """
    active = str(data.get("active_backend") or "duckdb")
    origins: Dict[str, str] = {}
    for name, spec in FIELD_SPEC.items():
        values = data["backends"].get(name) or {}
        for field in spec["fields"]:
            configured = bool(values.get(field["name"]) not in (None, ""))
            origins[f"{name}.{field['name']}"] = _origin_of(
                field["env"], backend=name, active=active, configured=configured
            )
    return origins


def page_facts(data: Dict[str, Any], loaded: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Non-secret context the page renders: specs, driver state, effective limits.

    Deliberately contains no field values — the values travel in the config block
    that the page itself owns. This is only the "how to draw it" half.
    """
    from .env_schema import get_env_config  # noqa: PLC0415 - avoids an import cycle

    cfg = get_env_config().data
    drivers: Dict[str, Dict[str, Any]] = {}
    try:
        from ..data.source import driver_available  # noqa: PLC0415
    except Exception:  # pragma: no cover - duckdb-only install still renders
        driver_available = None
    for name in FIELD_SPEC:
        if driver_available is None:
            drivers[name] = {"installed": None, "hint": ""}
            continue
        installed, hint = driver_available(name)
        drivers[name] = {"installed": installed, "hint": hint}

    return {
        "source_path": (loaded or {}).get("path", ""),
        "exists": bool((loaded or {}).get("exists")),
        "skipped": list((loaded or {}).get("skipped") or []),
        "warnings": list((loaded or {}).get("warnings") or []),
        "origins": origins_for(data),
        "drivers": drivers,
        "order": list(BACKEND_ORDER),
        "spec": {
            name: {
                "label": spec["label"],
                "summary": spec["summary"],
                "readonly": spec["readonly"],
                "driver": spec["driver"],
                "required": spec["required"],
                "shares_env_with": spec.get("shares_env_with", ""),
                "fields": [{k: v for k, v in f.items() if k != "env"} for f in spec["fields"]],
            }
            for name, spec in FIELD_SPEC.items()
        },
        "limits": {
            name: {
                "label": spec["label"],
                "effective": getattr(cfg, name, spec["default"]),
                "default": spec["default"],
                "low": spec["low"],
                "high": spec["high"],
            }
            for name, spec in _LIMIT_SPEC.items()
        },
    }


def redacted_config(data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The config with every secret replaced by a presence flag.

    Used by ``--print`` and by the installer's verification step. Built from
    ``_SECRET_FIELDS`` rather than by matching on the key name, so a new field
    marked ``secret`` is covered automatically and cannot be forgotten.
    """
    data = data or load_config_file()["data"]
    out = json.loads(json.dumps(data))
    for backend, values in (out.get("backends") or {}).items():
        for fname in list(values):
            if f"{backend}.{fname}" in _SECRET_FIELDS:
                values[fname] = "已设置" if values[fname] else ""
    out.pop("issues", None)
    return out


def missing_required(data: Dict[str, Any], backend: Optional[str] = None) -> List[str]:
    """Human-readable list of blank required fields for one backend."""
    name = backend or str(data.get("active_backend") or "duckdb")
    spec = FIELD_SPEC.get(name)
    if spec is None:
        return []
    values = (data.get("backends") or {}).get(name) or {}
    labels = {field["name"]: field["label"] for field in spec["fields"]}
    return [labels[fname] for fname in spec["required"] if not str(values.get(fname) or "").strip()]


# --------------------------------------------------------------------------- #
# Connection test
# --------------------------------------------------------------------------- #
@contextlib.contextmanager
def temporarily_applied(data: Dict[str, Any], *,
                        env_report: Optional[Dict[str, Any]] = None) -> Iterator[None]:
    """Expose ``data`` as if saved, then restore ``os.environ`` exactly.

    Deliberately goes through the same projection the real tools use. Testing a
    connection by passing keyword arguments straight to the driver would still
    pass with a misspelt variable name, which is the single most common reason a
    page says "connected" and `download_sample` then refuses.

    Not thread-safe by design: it is used by the one-shot setup command, which
    runs a single check per process.
    """
    from .env_schema import reset_env_config  # noqa: PLC0415
    from ..data.source import reset_data_source  # noqa: PLC0415

    saved_env = dict(os.environ)
    saved_injected = dict(_injected)
    try:
        apply_config(data, env_report=env_report, force=True)
        reset_env_config()
        reset_data_source()
        yield
    finally:
        os.environ.clear()
        os.environ.update(saved_env)
        _injected.clear()
        _injected.update(saved_injected)
        reset_env_config()
        reset_data_source()


def test_connection(data: Dict[str, Any], *, env_report: Optional[Dict[str, Any]] = None,
                    list_tables: bool = False, limit: int = 30) -> Dict[str, Any]:
    """Open a real connection with the supplied settings and report the truth."""
    backend = str(data.get("active_backend") or "duckdb").strip().lower()

    missing = missing_required(data, backend)
    if missing:
        return {"ok": False, "error": "字段尚未填全", "missing": missing,
                "hint": "补齐后再测；也可以先保存，稍后回来填"}

    try:
        from ..data.source import DataAccessError, build_data_source, driver_available  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover
        return {"ok": False, "error": f"无法加载数据源模块：{exc}"}

    installed, hint = driver_available(backend)
    if not installed:
        return {"ok": False, "error": "驱动未安装", "missing_driver": hint,
                "hint": f"先装驱动：{hint}，或 pip install -e '.[warehouse]' 一次装全"}

    with temporarily_applied(data, env_report=env_report):
        source = None
        try:
            source = build_data_source(backend)
            info = dict(source.test_connection() or {})
            info.setdefault("ok", False)
            if list_tables and info.get("ok"):
                try:
                    tables = source.tables()
                    info["tables"] = {"count": len(tables), "sample": tables[:limit]}
                except Exception as exc:  # noqa: BLE001 - diagnostics must not crash
                    info["tables"] = {"error": f"{type(exc).__name__}: {exc}"}
            return info
        except DataAccessError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        finally:
            if source is not None:
                with contextlib.suppress(Exception):
                    source.close()


def reset_state() -> None:
    """Test hook: forget the injection bookkeeping."""
    global _dotenv_applied
    _injected.clear()
    _dotenv_applied = set()


__all__ = [
    "BACKEND_ORDER",
    "CONFIG_FILE_VAR",
    "ConfigError",
    "FIELD_SPEC",
    "HTML_CONFIG_NAME",
    "SCHEMA_VERSION",
    "apply_config",
    "atomic_write",
    "candidate_config_paths",
    "config_file_path",
    "config_to_env",
    "default_config",
    "ensure_config_loaded",
    "extract_config_from_html",
    "json_config_path",
    "json_for_html",
    "load_config_file",
    "missing_required",
    "normalize",
    "origins_for",
    "page_facts",
    "redacted_config",
    "reset_state",
    "save_config_file",
    "seed_from_environment",
    "temporarily_applied",
    "test_connection",
]
