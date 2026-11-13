"""Minimal ``.env`` loader (no third-party dependency).

Why this exists
---------------
:mod:`credit_agent.config.env_schema` reads ``os.environ`` only. That is the
right contract for a library — but it means a ``.env`` file sitting next to the
project does nothing on its own, which is a silent trap: you fill in
``.env.example``, run the tool, and the tool still believes no credentials
exist.

This module closes that gap without adding ``python-dotenv`` to the dependency
list. It is deliberately small and predictable:

* ``KEY=VALUE`` per line, optional ``export`` prefix, ``#`` comments;
* values may be single- or double-quoted (double quotes understand ``\\n``,
  ``\\t``, ``\\"``, ``\\\\``); unquoted values are comment-stripped on `` #``;
* **existing process environment always wins** (``override=False`` by default),
  so a host that injects credentials through ``mcp.json`` ``env`` is never
  clobbered by a stale file on disk;
* no ``$VAR`` expansion — an explicit non-feature, so that a literal password
  containing ``$`` is not silently mangled.

Nothing here logs or returns a secret value: :func:`load_env_files` reports
file paths and key *names* only.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path
from typing import Dict, List, Optional, Sequence

#: Explicit override, highest priority.
ENV_FILE_VAR = "CREDIT_AGENT_ENV_FILE"

_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Keys whose presence in a world-readable file is worth a warning. Matches on
#: substring so ``CREDIT_AGENT_PG_PASSWORD`` and a bare ``DB_PASSWORD`` both hit.
_SECRET_KEY_HINT = re.compile(r"(?i)(password|passwd|pwd|secret|token|access_key|api_key|credential)")

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\", "'": "'"}


def default_env_files() -> List[Path]:
    """Search order, highest priority first.

    ``$CREDIT_AGENT_ENV_FILE`` is exact (no fallback if it is missing — a typo
    there should be visible, not silently ignored). Otherwise the working
    directory of the MCP process, then the runtime home.
    """
    paths: List[Path] = []
    explicit = os.environ.get(ENV_FILE_VAR, "").strip()
    if explicit:
        paths.append(Path(explicit).expanduser())
    paths.append(Path.cwd() / ".env")
    paths.append(Path.home() / ".credit-agent" / ".env")

    seen, unique = set(), []
    for path in paths:
        try:
            key = str(path.resolve())
        except OSError:  # pragma: no cover - unreadable cwd
            key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def _unquote(raw: str) -> str:
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        inner = text[1:-1]
        if text[0] == "'":
            return inner  # single quotes are literal, by convention
        out, i = [], 0
        while i < len(inner):
            ch = inner[i]
            if ch == "\\" and i + 1 < len(inner):
                out.append(_ESCAPES.get(inner[i + 1], inner[i + 1]))
                i += 2
                continue
            out.append(ch)
            i += 1
        return "".join(out)
    # Unquoted: a ` #` sequence starts a trailing comment.
    cut = text.find(" #")
    if cut == -1 and text.startswith("#"):
        return ""
    return (text[:cut] if cut != -1 else text).strip()


def parse_env_text(text: str) -> Dict[str, str]:
    """Parse ``.env`` content into a mapping. Pure function, no side effects."""
    values: Dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.lower().startswith("export "):
            stripped = stripped[7:].lstrip()
        key, sep, raw = stripped.partition("=")
        key = key.strip()
        if not sep or not _KEY_RE.match(key):
            continue
        values[key] = _unquote(raw)
    return values


def _permission_warning(path: Path) -> Optional[str]:
    """Warn when a file holding secrets is readable by group/other."""
    try:
        mode = path.stat().st_mode
    except OSError:  # pragma: no cover
        return None
    if os.name != "posix":
        return None
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        return f"{path} 权限过宽（建议 chmod 600），其中可能包含数据库密码"
    return None


def load_env_files(paths: Optional[Sequence[Path]] = None,
                   override: bool = False) -> Dict[str, object]:
    """Load the first readable ``.env`` and fill in missing variables.

    Returns a report with ``loaded_files``, ``applied`` (key names only),
    ``skipped`` (already set in the process environment) and ``warnings``.
    Never returns or logs a value.
    """
    explicit = os.environ.get(ENV_FILE_VAR, "").strip()
    if explicit and paths is None:
        # An explicit path is authoritative: no fallback. Silently loading a
        # different file because of a typo is how "I changed the password and
        # nothing happened" incidents start.
        candidates = [Path(explicit).expanduser()]
    else:
        candidates = list(paths) if paths is not None else default_env_files()
    report: Dict[str, object] = {
        "loaded_files": [],
        "applied": [],
        "skipped": [],
        # ``KEY=`` means "not configured", which is what the shipped template
        # looks like before you edit it. Writing an empty string into the
        # environment would fail the int fields at validation time
        # (``CREDIT_AGENT_MAX_SCAN_ROWS=`` -> "unable to parse string as an
        # integer"), i.e. `cp .env.example .env` alone would break startup.
        "blank": [],
        "warnings": [],
    }
    warnings: List[str] = report["warnings"]  # type: ignore[assignment]
    applied: List[str] = report["applied"]  # type: ignore[assignment]
    skipped: List[str] = report["skipped"]  # type: ignore[assignment]
    blank: List[str] = report["blank"]  # type: ignore[assignment]

    found = False
    for path in candidates:
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8-sig")
        except OSError as exc:
            warnings.append(f"无法读取 {path}: {type(exc).__name__}")
            continue

        found = True
        report["loaded_files"].append(str(path))  # type: ignore[union-attr]
        warning = _permission_warning(path)
        if warning:
            warnings.append(warning)

        for key, value in parse_env_text(text).items():
            if not override and os.environ.get(key, "") != "":
                skipped.append(key)
                continue
            if value.strip() == "":
                # ``KEY=`` is "not configured". With override=True an explicit
                # blank also means "unset", so drop any inherited value rather
                # than planting an empty string that fails int validation.
                if override:
                    os.environ.pop(key, None)
                blank.append(key)
                continue
            os.environ[key] = value
            applied.append(key)

    if explicit and not found:
        warnings.append(f"{ENV_FILE_VAR}={explicit} 指向的文件不存在，已忽略")
    return report


#: Set to ``1`` to disable file lookup entirely (test suite).
SKIP_VAR = "CREDIT_AGENT_SKIP_DOTENV"

_loaded: Optional[Dict[str, object]] = None


def ensure_env_loaded() -> Dict[str, object]:
    """Idempotent: load ``.env`` once per process."""
    global _loaded
    if _loaded is None:
        if os.environ.get(SKIP_VAR, "").strip() in ("1", "true", "yes"):
            _loaded = {"loaded_files": [], "applied": [], "skipped": [],
                       "warnings": [], "disabled": True}
        else:
            _loaded = load_env_files()
    return _loaded


def reset_env_loaded() -> None:
    """Test hook: forget that loading already happened."""
    global _loaded
    _loaded = None


__all__ = [
    "ENV_FILE_VAR",
    "SKIP_VAR",
    "default_env_files",
    "ensure_env_loaded",
    "load_env_files",
    "parse_env_text",
    "reset_env_loaded",
]
