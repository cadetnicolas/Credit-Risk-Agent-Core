"""Diagnose the warehouse connection used by ``download_sample``.

Answers the only questions that matter after editing credentials:

1. which ``.env`` file was loaded, and which variables it actually set;
2. is the active backend complete, and if not, exactly which variables are blank;
3. does the server accept the connection, and is the session really read-only;
4. can the account see any tables at all (a login that cannot see the schema is
   configured but useless).

Usage::

    python scripts/check_db.py                 # current backend
    python scripts/check_db.py --backend mysql  # try another one
    python scripts/check_db.py --list-tables    # + visible tables (max 30)
    python scripts/check_db.py --json           # machine-readable

Never prints a password, secret or full DSN. Exits non-zero when the active
backend is not usable, so it can gate an install script.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from credit_agent.config import CONFIG_FILE_REPORT, ENV_FILE_REPORT, get_env_config  # noqa: E402
from credit_agent.config.paths import get_data_dir  # noqa: E402
from credit_agent.data.source import (  # noqa: E402
    DataAccessError,
    build_data_source,
    describe_backends,
    driver_available,
)

# Variables that must never be echoed, even truncated.
_SECRET_HINTS = ("password", "secret", "access_key", "api_key", "token")


def _driver_available(backend: str) -> tuple[bool, str]:
    """Kept as a thin alias so the diagnostics script and the setup page agree."""
    return driver_available(backend)


def _is_secret(name: str) -> bool:
    lowered = name.lower()
    return any(hint in lowered for hint in _SECRET_HINTS)


def _env_report() -> dict:
    report = dict(ENV_FILE_REPORT or {})
    applied = [k for k in report.get("applied", []) if not _is_secret(k)]
    secrets = [k for k in report.get("applied", []) if _is_secret(k)]
    return {
        "loaded_files": report.get("loaded_files", []),
        "applied": applied,
        # Names only — a secret's value never leaves os.environ.
        "applied_secret_keys": secrets,
        "skipped_already_set": [k for k in report.get("skipped", []) if not _is_secret(k)],
        # ``KEY=`` lines from the template; counted, not listed — they are the
        # normal state right after `cp .env.example .env`.
        "blank_count": len(report.get("blank", []) or []),
        "warnings": report.get("warnings", []),
        "disabled": report.get("disabled", False),
    }


def _config_report() -> dict:
    """What the setup page contributed, if anything.

    Kept separate from the ``.env`` section because the two have different
    precedence: the page wins over ``.env``, and only the real process
    environment beats both. When someone says "I changed it and it still fails",
    this is the line that answers why.
    """
    report = dict(CONFIG_FILE_REPORT or {})
    apply_info = report.get("apply") or {}
    return {
        "exists": report.get("exists", False),
        "path": report.get("path", ""),
        "kind": report.get("kind", ""),
        "active_backend": report.get("active_backend", ""),
        # Names only; values never leave os.environ.
        "applied": apply_info.get("applied", []),
        "cleared": apply_info.get("cleared", []),
        "blocked_by_process_env": apply_info.get("blocked_by_process_env", []),
        "skipped": report.get("skipped", []),
        "warnings": report.get("warnings", []),
    }


def _duckdb_hint() -> str:
    return (
        "运行 scripts/make_demo_workspace.py 生成演示数据，"
        "或把 CREDIT_AGENT_DATA_BACKEND 指向 odps/postgres/holo/mysql"
    )


def probe(backend: str | None, list_tables: bool, limit: int) -> dict:
    cfg = get_env_config()
    active = (backend or cfg.data.backend or "duckdb").strip().lower()

    result: dict = {
        "env_files": _env_report(),
        "config_file": _config_report(),
        "configured_backend": cfg.data.backend or "duckdb",
        "target_backend": active,
        "backends": describe_backends(),
        "connection": None,
        "tables": None,
    }

    # Annotate each backend with driver availability; the library-level
    # capability matrix deliberately says nothing about installed packages.
    for row in result["backends"]:
        installed, hint = _driver_available(row["backend"])
        row["driver_installed"] = installed
        row["driver_hint"] = hint

    driver_ok, driver_hint = _driver_available(active)
    if not driver_ok:
        result["connection"] = {
            "ok": False,
            "error": "driver not installed",
            "missing_driver": driver_hint,
            "hint": f"先装驱动：{driver_hint}，或 pip install -e '.[warehouse]' 一次装全",
        }
        return result

    # Backend-specific completeness, reported before any socket is opened so a
    # blank variable reads as "you forgot X", not as "connection refused".
    checker = {
        "duckdb": lambda: (True, []),
        "odps": cfg.data.backend_configured,
        "postgres": cfg.data.backend_configured,
        "holo": cfg.data.backend_configured,
        "mysql": cfg.data.backend_configured,
    }.get(active)
    if checker is None:
        result["connection"] = {
            "ok": False,
            "error": f"unknown backend {active!r}",
            "hint": "可选：duckdb / odps / postgres / holo / mysql",
        }
        return result

    if active == cfg.data.backend.strip().lower():
        complete, missing = cfg.data.backend_configured()
    else:
        # Probing a backend other than the configured one: validate its own
        # variables without pretending it is the active choice.
        complete, missing = _check_other(cfg.data, active)
    if not complete:
        result["connection"] = {
            "ok": False,
            "error": "credentials incomplete",
            "missing": missing,
            "hint": "在 .env 或 ~/.workbuddy/mcp.json 的 env 段补齐上述变量",
        }
        return result

    if active == "duckdb":
        db_path = Path(cfg.data.duckdb_path) if cfg.data.duckdb_path else (
            get_data_dir() / "credit.duckdb"
        )
        if not db_path.exists():
            result["connection"] = {"ok": False, "error": "database file not found",
                                    "path": str(db_path), "hint": _duckdb_hint()}
            return result

    try:
        source = build_data_source(active)
    except DataAccessError as exc:
        result["connection"] = {"ok": False, "error": str(exc)}
        return result
    except Exception as exc:  # noqa: BLE001 - diagnostics must never crash
        result["connection"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        return result

    try:
        info = source.test_connection()
        result["connection"] = info
        if list_tables and info.get("ok"):
            try:
                tables = source.tables()
                result["tables"] = {"count": len(tables), "sample": tables[:limit]}
            except Exception as exc:  # noqa: BLE001
                result["tables"] = {"error": f"{type(exc).__name__}: {exc}"}
    finally:
        source.close()
    return result


def _check_other(data, backend: str) -> tuple[bool, list[str]]:
    """Completeness check for a backend that is not the configured one."""
    if backend == "duckdb":
        return True, []
    if backend == "odps":
        pairs = [("CREDIT_AGENT_ODPS_ACCESS_ID", data.odps_access_id),
                 ("CREDIT_AGENT_ODPS_ACCESS_KEY", data.odps_access_key),
                 ("CREDIT_AGENT_ODPS_PROJECT", data.odps_project)]
    elif backend in ("postgres", "holo"):
        pairs = [("CREDIT_AGENT_PG_HOST", data.pg_host),
                 ("CREDIT_AGENT_PG_DATABASE", data.pg_database),
                 ("CREDIT_AGENT_PG_USER", data.pg_user),
                 ("CREDIT_AGENT_PG_PASSWORD", data.pg_password)]
    else:
        pairs = [("CREDIT_AGENT_MYSQL_HOST", data.mysql_host),
                 ("CREDIT_AGENT_MYSQL_DATABASE", data.mysql_database),
                 ("CREDIT_AGENT_MYSQL_USER", data.mysql_user),
                 ("CREDIT_AGENT_MYSQL_PASSWORD", data.mysql_password)]
    missing = [name for name, value in pairs if not str(value or "").strip()]
    return not missing, missing


def render(result: dict) -> str:
    lines: list[str] = []
    env = result["env_files"]
    page = result.get("config_file") or {}
    lines.append("[1] 配置来源")
    if page.get("exists"):
        label = "配置页" if page.get("kind") == "html" else "config.json"
        lines.append(f"    {label}：{page['path']}")
        if page.get("active_backend"):
            lines.append(f"      选中后端：{page['active_backend']}")
        if page.get("applied"):
            lines.append(f"      生效变量 {len(page['applied'])} 个：{', '.join(page['applied'][:8])}"
                         + (" …" if len(page["applied"]) > 8 else ""))
        if page.get("blocked_by_process_env"):
            lines.append(f"      ⚠ 被进程环境覆盖（页面里改了不生效）："
                         f"{', '.join(page['blocked_by_process_env'])}")
        for text in page.get("skipped", []):
            lines.append(f"      ⚠ 已跳过：{text}")
        for warning in page.get("warnings", []):
            lines.append(f"      ⚠ {warning}")
    else:
        lines.append("    未找到已保存的配置页（config.json / setup.html）")

    if env.get("disabled"):
        lines.append("    .env 加载已禁用（CREDIT_AGENT_SKIP_DOTENV=1）")
    elif env["loaded_files"]:
        for path in env["loaded_files"]:
            lines.append(f"    .env：{path}")
    else:
        lines.append("    .env：未找到（$CREDIT_AGENT_ENV_FILE → ./.env → ~/.credit-agent/.env）")
    if env["applied"]:
        lines.append(f"      生效变量 {len(env['applied'])} 个：{', '.join(env['applied'][:8])}"
                     + (" …" if len(env["applied"]) > 8 else ""))
    if env.get("blank_count"):
        lines.append(f"      另有 {env['blank_count']} 个变量为空值（按未配置处理，走默认）")
    if env["applied_secret_keys"]:
        lines.append(f"      生效的凭证类变量（值不显示）：{', '.join(env['applied_secret_keys'])}")
    if env["skipped_already_set"]:
        lines.append(f"      被进程环境覆盖（优先）：{', '.join(env['skipped_already_set'][:8])}")
    for warning in env["warnings"]:
        lines.append(f"      ⚠ {warning}")

    lines.append("")
    lines.append("[2] 后端可用性")
    for row in result["backends"]:
        if row["active"]:
            mark = "●"          # the backend the MCP server will actually use
        elif row["backend"] == result["target_backend"]:
            mark = "◐"          # only the one being probed by --backend
        else:
            mark = "○"
        state = "已配置" if row["configured"] else "未配置"
        ro = "服务端只读" if row["server_side_read_only"] else "仅 SQL 文本审计"
        driver = "" if row.get("driver_installed") else "   ✗ 驱动未装"
        lines.append(f"    {mark} {row['backend']:<9} {state:<5} {ro}{driver}")
        if row["missing"]:
            lines.append(f"      缺少：{', '.join(row['missing'])}")
        if not row.get("driver_installed") and row.get("driver_hint"):
            lines.append(f"      装驱动：{row['driver_hint']}")

    lines.append("")
    lines.append(f"[3] 连接测试（{result['target_backend']}）")
    conn = result["connection"] or {}
    if conn.get("ok"):
        lines.append(f"    ✓ 连接成功  target={conn.get('target')}")
        lines.append(f"      服务端只读：{'是' if conn.get('server_side_read_only') else '否'}")
        if conn.get("note"):
            lines.append(f"      说明：{conn['note']}")
    else:
        lines.append(f"    ✗ {conn.get('error', 'unknown error')}")
        if conn.get("missing"):
            lines.append(f"      缺少变量：{', '.join(conn['missing'])}")
        if conn.get("path"):
            lines.append(f"      路径：{conn['path']}")
        if conn.get("hint"):
            lines.append(f"      建议：{conn['hint']}")

    tables = result.get("tables")
    if tables:
        lines.append("")
        lines.append("[4] 可见表")
        if "error" in tables:
            lines.append(f"    读取失败：{tables['error']}")
        else:
            lines.append(f"    共 {tables['count']} 张，前 {len(tables['sample'])} 张：")
            for name in tables["sample"]:
                lines.append(f"      - {name}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="数据库连接与配置自检")
    parser.add_argument("--backend", default=None,
                        help="检查哪个后端（默认取 CREDIT_AGENT_DATA_BACKEND）")
    parser.add_argument("--list-tables", action="store_true", help="同时列出可见表")
    parser.add_argument("--limit", type=int, default=30, help="列出的表数量上限")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    result = probe(args.backend, args.list_tables, args.limit)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    else:
        print(render(result))

    conn = result["connection"] or {}
    active_row = next((r for r in result["backends"]
                       if r["backend"] == result["target_backend"]), None)
    usable = bool(conn.get("ok"))
    if not usable and active_row and active_row["active"]:
        return 1
    return 0 if usable or not (active_row and active_row["active"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
