"""``.env`` loading and the connection self-check.

The point of these tests is the failure mode that made them necessary: a
correctly filled ``.env`` used to be ignored entirely, so the tool reported
"credentials incomplete" while the credentials sat right there on disk.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from credit_agent.config.dotenv import (
    default_env_files,
    load_env_files,
    parse_env_text,
    reset_env_loaded,
)
from credit_agent.config.env_schema import get_env_config, reset_env_config

ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #
def test_parse_handles_comments_quotes_and_export():
    text = """
# a comment
CREDIT_AGENT_DATA_BACKEND=postgres
export CREDIT_AGENT_PG_HOST = db.internal
CREDIT_AGENT_PG_PASSWORD="p@ss #word"
CREDIT_AGENT_PG_USER='single quoted'
CREDIT_AGENT_PG_DATABASE=credit  # trailing comment
NOT_A_PAIR
1BAD=x
"""
    values = parse_env_text(text)
    assert values["CREDIT_AGENT_DATA_BACKEND"] == "postgres"
    assert values["CREDIT_AGENT_PG_HOST"] == "db.internal"
    # Quoted values keep '#' verbatim; only unquoted trailing comments strip.
    assert values["CREDIT_AGENT_PG_PASSWORD"] == "p@ss #word"
    assert values["CREDIT_AGENT_PG_USER"] == "single quoted"
    assert values["CREDIT_AGENT_PG_DATABASE"] == "credit"
    assert "NOT_A_PAIR" not in values
    assert "1BAD" not in values


def test_double_quotes_understand_escapes_single_quotes_do_not():
    values = parse_env_text('A="line1\\nline2"\\nB=\'lit\\n\'\n'.replace("\\nB", "\nB"))
    assert values["A"] == "line1\nline2"
    assert values["B"] == "lit\\n"  # single-quoted: backslash is literal


def test_dollar_signs_are_not_expanded():
    """A password containing ``$`` must survive untouched."""
    values = parse_env_text("PW=ab$cd${HOME}")
    assert values["PW"] == "ab$cd${HOME}"


# --------------------------------------------------------------------------- #
# Loading semantics
# --------------------------------------------------------------------------- #
def test_process_environment_wins_over_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("CREDIT_AGENT_PG_HOST=from-file\nCREDIT_AGENT_PG_USER=from-file\n")
    monkeypatch.setenv("CREDIT_AGENT_PG_HOST", "from-shell")
    monkeypatch.delenv("CREDIT_AGENT_PG_USER", raising=False)

    report = load_env_files([env_file])

    assert os.environ["CREDIT_AGENT_PG_HOST"] == "from-shell"
    assert os.environ["CREDIT_AGENT_PG_USER"] == "from-file"
    assert "CREDIT_AGENT_PG_HOST" in report["skipped"]
    assert "CREDIT_AGENT_PG_USER" in report["applied"]


def test_override_true_replaces_the_environment(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("CREDIT_AGENT_PG_HOST=from-file\n")
    monkeypatch.setenv("CREDIT_AGENT_PG_HOST", "from-shell")

    load_env_files([env_file], override=True)
    assert os.environ["CREDIT_AGENT_PG_HOST"] == "from-file"


def test_missing_explicit_file_warns_instead_of_failing(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDIT_AGENT_ENV_FILE", str(tmp_path / "nope.env"))
    report = load_env_files()
    assert report["loaded_files"] == []
    assert any("不存在" in w for w in report["warnings"])


def test_secret_values_are_never_in_the_report(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("CREDIT_AGENT_PG_PASSWORD=sup3rs3cret\n")
    report = load_env_files([env_file])
    assert "sup3rs3cret" not in repr(report)
    assert "CREDIT_AGENT_PG_PASSWORD" in report["applied"]


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits only")
def test_world_readable_file_raises_a_warning(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("CREDIT_AGENT_PG_PASSWORD=x\n")
    env_file.chmod(0o644)
    report = load_env_files([env_file])
    assert any("chmod 600" in w for w in report["warnings"])


def test_default_search_order_starts_with_explicit_then_cwd(tmp_path, monkeypatch):
    explicit = tmp_path / "custom.env"
    monkeypatch.setenv("CREDIT_AGENT_ENV_FILE", str(explicit))
    found = default_env_files()
    assert found[0] == explicit
    assert Path.cwd() / ".env" in found


# --------------------------------------------------------------------------- #
# End to end: the file actually changes the resolved config
# --------------------------------------------------------------------------- #
def test_dotenv_feeds_the_typed_config(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "CREDIT_AGENT_DATA_BACKEND=mysql\n"
        "CREDIT_AGENT_MYSQL_HOST=127.0.0.1\n"
        "CREDIT_AGENT_MYSQL_PORT=3307\n"
        "CREDIT_AGENT_MYSQL_DATABASE=credit_ops\n"
        "CREDIT_AGENT_MYSQL_USER=reader\n"
        "CREDIT_AGENT_MYSQL_PASSWORD=pw\n"
    )
    monkeypatch.delenv("CREDIT_AGENT_DATA_BACKEND", raising=False)
    monkeypatch.delenv("CREDIT_AGENT_MYSQL_HOST", raising=False)
    monkeypatch.delenv("CREDIT_AGENT_MYSQL_PORT", raising=False)
    monkeypatch.delenv("CREDIT_AGENT_MYSQL_DATABASE", raising=False)
    monkeypatch.delenv("CREDIT_AGENT_MYSQL_USER", raising=False)
    monkeypatch.delenv("CREDIT_AGENT_MYSQL_PASSWORD", raising=False)

    load_env_files([env_file], override=True)
    reset_env_config()
    cfg = get_env_config().data
    assert cfg.backend == "mysql"
    assert cfg.mysql_port == 3307
    assert cfg.backend_configured() == (True, [])
    reset_env_config()
    reset_env_loaded()


def test_blank_value_becomes_none_not_a_validation_error(tmp_path, monkeypatch):
    """``KEY=`` in a template must mean "unset", not "invalid int"."""
    env_file = tmp_path / ".env"
    env_file.write_text("CREDIT_AGENT_PG_PORT=\n")
    monkeypatch.delenv("CREDIT_AGENT_PG_PORT", raising=False)
    load_env_files([env_file], override=True)
    reset_env_config()
    assert get_env_config().data.pg_port is None
    reset_env_config()


def test_shipped_template_alone_does_not_break_startup(tmp_path, monkeypatch):
    """`cp .env.example .env` must leave a working configuration.

    Every value in the template is blank. Writing those blanks into the
    environment used to make ``DataConfig`` fail validation on the int fields
    (``CREDIT_AGENT_MAX_SCAN_ROWS=``), i.e. the documented first step of the
    setup guide broke the tool before any credential was entered.
    """
    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    env_file = tmp_path / ".env"
    env_file.write_text(template, encoding="utf-8")

    for key in ("CREDIT_AGENT_DATA_BACKEND", "CREDIT_AGENT_MAX_SCAN_ROWS",
                "CREDIT_AGENT_CONNECT_TIMEOUT", "CREDIT_AGENT_UPLOAD_MAX_MB",
                "CREDIT_AGENT_UPLOAD_MAX_ROWS"):
        monkeypatch.delenv(key, raising=False)

    report = load_env_files([env_file], override=True)
    assert report["applied"] == []          # template holds no real values
    assert "CREDIT_AGENT_MAX_SCAN_ROWS" in report["blank"]

    reset_env_config()
    cfg = get_env_config()
    assert cfg.data.backend == "" or cfg.data.backend == "duckdb"
    assert cfg.data.max_scan_rows == 2_000_000
    assert cfg.data.connect_timeout_seconds == 30
    reset_env_config()


def test_override_with_blank_clears_an_inherited_value(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("CREDIT_AGENT_DATA_BACKEND=\n")
    monkeypatch.setenv("CREDIT_AGENT_DATA_BACKEND", "mysql")

    load_env_files([env_file], override=True)
    assert os.environ.get("CREDIT_AGENT_DATA_BACKEND") in (None, "")
    reset_env_config()


# --------------------------------------------------------------------------- #
# scripts/check_db.py
# --------------------------------------------------------------------------- #
def _load_check_db():
    spec = importlib.util.spec_from_file_location("check_db", ROOT / "scripts" / "check_db.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_check_db_reports_missing_variables_without_touching_the_network(monkeypatch):
    module = _load_check_db()
    # This test is about variable-completeness reporting, not about what happens
    # to be installed on the machine running it. On a host without `pymysql`
    # (CI installs only `.[mcp,dev]`) `probe` stops at "driver not installed"
    # and never reaches the missing-variable branch, so stub the driver lookup.
    monkeypatch.setattr(module, "_driver_available", lambda backend: (True, ""))
    for name in ("CREDIT_AGENT_MYSQL_HOST", "CREDIT_AGENT_MYSQL_DATABASE",
                 "CREDIT_AGENT_MYSQL_USER", "CREDIT_AGENT_MYSQL_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CREDIT_AGENT_DATA_BACKEND", "mysql")
    reset_env_config()

    result = module.probe(None, list_tables=False, limit=5)
    assert result["target_backend"] == "mysql"
    assert result["connection"]["ok"] is False
    assert "CREDIT_AGENT_MYSQL_HOST" in result["connection"]["missing"]
    reset_env_config()


def test_check_db_never_echoes_a_password(monkeypatch):
    module = _load_check_db()
    monkeypatch.setattr(module, "_driver_available", lambda backend: (True, ""))
    monkeypatch.setenv("CREDIT_AGENT_DATA_BACKEND", "postgres")
    monkeypatch.setenv("CREDIT_AGENT_PG_HOST", "db.internal")
    monkeypatch.setenv("CREDIT_AGENT_PG_DATABASE", "credit")
    monkeypatch.setenv("CREDIT_AGENT_PG_USER", "reader")
    monkeypatch.setenv("CREDIT_AGENT_PG_PASSWORD", "top-secret-value")
    reset_env_config()

    rendered = module.render(module.probe(None, list_tables=False, limit=5))
    assert "top-secret-value" not in rendered
    reset_env_config()


def test_check_db_blames_the_driver_not_the_password():
    """A missing driver must not be reported as a connection failure."""
    module = _load_check_db()
    import importlib.util

    real = importlib.util.find_spec

    def fake(name, *args, **kwargs):
        if name == "pymysql":
            return None
        return real(name, *args, **kwargs)

    importlib.util.find_spec = fake
    try:
        available, hint = module._driver_available("mysql")
        assert available is False
        assert hint == "pip install pymysql"
    finally:
        importlib.util.find_spec = real
