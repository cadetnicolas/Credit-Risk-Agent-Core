"""The saved-configuration layer: schema, precedence, and the HTML round trip.

The tests that matter most here are the ones guarding silent failure modes:

* the backend name is missing from the projection (page says Hologres, tool
  keeps querying DuckDB);
* ``postgres`` and ``holo`` both get exported, so whichever ran last wins;
* a blank field silently inherits a stale value from ``.env``;
* a password containing ``</script>`` truncates the page.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from credit_agent.config import store


def _holo(**over):
    values = {"host": "hg.internal", "port": 80, "database": "db",
              "user": "u", "password": "p", "schema": "", "sslmode": ""}
    values.update(over)
    return values


def _config(backend="holo", **backends):
    data = store.normalize({"active_backend": backend,
                            "backends": {backend: backends or _holo()}})
    return data


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    monkeypatch.setenv("CREDIT_AGENT_HOME", str(tmp_path))
    monkeypatch.delenv("CREDIT_AGENT_CONFIG", raising=False)
    store.reset_state()
    return tmp_path


# --------------------------------------------------------------------------- #
# Schema
# --------------------------------------------------------------------------- #
def test_default_config_changes_nothing_but_the_backend_name():
    env = store.config_to_env(store.default_config())
    assert env == {"CREDIT_AGENT_DATA_BACKEND": "duckdb"}


def test_every_field_has_an_environment_name_and_an_attribute():
    for backend, spec in store.FIELD_SPEC.items():
        assert spec["fields"], backend
        for field in spec["fields"]:
            assert field["env"].startswith("CREDIT_AGENT_"), (backend, field["name"])
            assert field.get("attr"), (backend, field["name"])
            assert field["kind"] in {"text", "int", "secret", "select"}


def test_required_fields_all_exist_in_the_field_list():
    for backend, spec in store.FIELD_SPEC.items():
        names = {field["name"] for field in spec["fields"]}
        assert set(spec["required"]) <= names, backend


def test_strict_mode_rejects_a_non_numeric_port():
    with pytest.raises(store.ConfigError) as excinfo:
        store.normalize({"backends": {"mysql": {"port": "abc"}}}, strict=True)
    assert "端口" in str(excinfo.value) or "mysql.port" in str(excinfo.value)


def test_lenient_mode_falls_back_and_records_the_issue():
    data = store.normalize({"backends": {"mysql": {"port": "abc"}}})
    assert data["backends"]["mysql"]["port"] is None
    assert any("mysql.port" in issue for issue in data["issues"])


def test_unknown_backend_falls_back_to_duckdb():
    data = store.normalize({"active_backend": "oracle"})
    assert data["active_backend"] == "duckdb"
    assert any("oracle" in issue for issue in data["issues"])


# --------------------------------------------------------------------------- #
# Environment projection
# --------------------------------------------------------------------------- #
def test_the_backend_name_is_exported():
    """Without this the page can say Hologres while the tool queries DuckDB."""
    assert store.config_to_env(_config("holo"))["CREDIT_AGENT_DATA_BACKEND"] == "holo"


def test_only_the_active_pg_backend_is_projected():
    """Hologres and PostgreSQL share CREDIT_AGENT_PG_*; both must not be written."""
    data = store.normalize({
        "active_backend": "postgres",
        "backends": {"postgres": {"host": "pg-only", "database": "d", "user": "u", "password": "p"},
                     "holo": {"host": "holo-only", "database": "d", "user": "u", "password": "p"}},
    })
    assert store.config_to_env(data)["CREDIT_AGENT_PG_HOST"] == "pg-only"

    data["active_backend"] = "holo"
    assert store.config_to_env(data)["CREDIT_AGENT_PG_HOST"] == "holo-only"


def test_blank_optional_fields_are_not_exported():
    env = store.config_to_env(_config(schema="", sslmode=""))
    assert "CREDIT_AGENT_PG_SCHEMA" not in env
    assert "CREDIT_AGENT_PG_SSLMODE" not in env


def test_limits_are_exported_only_when_set():
    data = _config()
    assert "CREDIT_AGENT_MAX_SCAN_ROWS" not in store.config_to_env(data)
    data["limits"]["max_scan_rows"] = 5000
    assert store.config_to_env(data)["CREDIT_AGENT_MAX_SCAN_ROWS"] == "5000"


# --------------------------------------------------------------------------- #
# Precedence
# --------------------------------------------------------------------------- #
def test_real_process_environment_wins(monkeypatch):
    monkeypatch.setenv("CREDIT_AGENT_PG_HOST", "from-process")
    report = store.apply_config(_config(), env_report={"applied": []})
    assert "CREDIT_AGENT_PG_HOST" in report["blocked_by_process_env"]
    assert os.environ["CREDIT_AGENT_PG_HOST"] == "from-process"


def test_dotenv_injected_value_is_replaced(monkeypatch):
    """A value `.env` set is not protected: the saved page is the newer edit."""
    monkeypatch.setenv("CREDIT_AGENT_PG_HOST", "from-dotenv")
    report = store.apply_config(_config(), env_report={"applied": ["CREDIT_AGENT_PG_HOST"]})
    assert "CREDIT_AGENT_PG_HOST" in report["applied"]
    assert os.environ["CREDIT_AGENT_PG_HOST"] == "hg.internal"


def test_a_blank_field_clears_a_dotenv_value(monkeypatch):
    """Otherwise clearing a field in the form would silently keep the old value."""
    monkeypatch.setenv("CREDIT_AGENT_PG_SCHEMA", "stale")
    report = store.apply_config(_config(schema=""), env_report={"applied": ["CREDIT_AGENT_PG_SCHEMA"]})
    assert "CREDIT_AGENT_PG_SCHEMA" in report["cleared"]
    assert "CREDIT_AGENT_PG_SCHEMA" not in os.environ


def test_switching_backend_drops_the_previous_backend_variables(monkeypatch):
    store.apply_config(_config("mysql", host="my", database="d", user="u",
                               password="p", charset="utf8mb4"), env_report={"applied": []})
    assert "CREDIT_AGENT_MYSQL_HOST" in os.environ
    store.apply_config(_config("holo"), env_report={"applied": []})
    assert "CREDIT_AGENT_MYSQL_HOST" not in os.environ
    assert os.environ["CREDIT_AGENT_PG_HOST"] == "hg.internal"


def test_force_ignores_the_process_environment(monkeypatch):
    monkeypatch.setenv("CREDIT_AGENT_PG_HOST", "from-process")
    store.apply_config(_config(), env_report={"applied": []}, force=True)
    assert os.environ["CREDIT_AGENT_PG_HOST"] == "hg.internal"


# --------------------------------------------------------------------------- #
# Page round trip
# --------------------------------------------------------------------------- #
def test_a_password_containing_a_script_tag_survives():
    secret = 'a</script><b>&"x'
    payload = {"backends": {"holo": {"password": secret}}}
    block = store.json_for_html(payload)
    assert "</script>" not in block

    page = f'<script id="credit-agent-config" type="application/json">{block}</script>'
    assert store.extract_config_from_html(page)["backends"]["holo"]["password"] == secret


def test_extract_ignores_a_page_without_the_block():
    assert store.extract_config_from_html("<html><body>hello</body></html>") is None


def test_extract_ignores_a_corrupt_block():
    page = '<script id="credit-agent-config" type="application/json">{not json}</script>'
    assert store.extract_config_from_html(page) is None


def test_extract_tolerates_attribute_order():
    page = '<script type="application/json" id="credit-agent-config">{"a": 1}</script>'
    assert store.extract_config_from_html(page) == {"a": 1}


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #
def test_save_writes_owner_only_and_reloads(sandbox):
    store.save_config_file(_config())
    path = sandbox / "config.json"
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    loaded = store.load_config_file()
    assert loaded["exists"] and loaded["kind"] == "json"
    assert loaded["data"]["backends"]["holo"]["password"] == "p"


def test_loading_prefers_json_over_html(sandbox):
    store.save_config_file(_config("mysql", host="from-json", database="d",
                                   user="u", password="p", charset="utf8mb4"))
    (sandbox / "setup.html").write_text(
        '<script id="credit-agent-config" type="application/json">'
        '{"active_backend": "holo", "backends": {"holo": {"host": "from-html"}}}'
        "</script>",
        encoding="utf-8",
    )
    loaded = store.load_config_file()
    assert loaded["kind"] == "json"
    assert loaded["data"]["backends"]["mysql"]["host"] == "from-json"


def test_a_corrupt_json_file_falls_through_to_the_page(sandbox):
    (sandbox / "config.json").write_text("{ this is not json", encoding="utf-8")
    (sandbox / "setup.html").write_text(
        '<script id="credit-agent-config" type="application/json">'
        '{"active_backend": "holo", "backends": {"holo": {"host": "from-html"}}}'
        "</script>",
        encoding="utf-8",
    )
    loaded = store.load_config_file()
    assert loaded["kind"] == "html"
    assert loaded["data"]["backends"]["holo"]["host"] == "from-html"
    assert loaded["skipped"], "the unreadable file should be reported, not silently dropped"


def test_explicit_path_is_exact_with_no_fallback(sandbox, monkeypatch, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("elsewhere") / "custom.json"
    monkeypatch.setenv("CREDIT_AGENT_CONFIG", str(elsewhere))
    assert store.candidate_config_paths() == [elsewhere]
    store.save_config_file(_config())
    assert elsewhere.is_file()


def test_an_explicit_page_path_never_receives_raw_json(sandbox, monkeypatch):
    """Writing JSON into the .html would produce a page the loader cannot read."""
    page = sandbox / "setup.html"
    monkeypatch.setenv("CREDIT_AGENT_CONFIG", str(page))
    store.save_config_file(_config())
    assert not page.exists()
    assert (sandbox / "config.json").is_file()


def test_missing_file_is_not_an_error(sandbox):
    loaded = store.load_config_file()
    assert loaded["exists"] is False
    assert loaded["data"]["active_backend"] == "duckdb"


def test_world_readable_file_is_flagged(sandbox):
    store.save_config_file(_config())
    path = sandbox / "config.json"
    path.chmod(0o644)
    assert "chmod 600" in store.load_config_file()["warnings"][0]


# --------------------------------------------------------------------------- #
# Seeding and redaction
# --------------------------------------------------------------------------- #
def test_seeding_mirrors_what_is_already_in_force(monkeypatch):
    from credit_agent.config.env_schema import reset_env_config

    monkeypatch.setenv("CREDIT_AGENT_DATA_BACKEND", "mysql")
    monkeypatch.setenv("CREDIT_AGENT_MYSQL_HOST", "my.rds")
    monkeypatch.setenv("CREDIT_AGENT_MYSQL_DATABASE", "credit")
    monkeypatch.setenv("CREDIT_AGENT_MYSQL_USER", "reader")
    monkeypatch.setenv("CREDIT_AGENT_MYSQL_PASSWORD", "secret-value")
    reset_env_config()

    seeded = store.seed_from_environment(store.default_config())
    assert seeded["active_backend"] == "mysql"
    assert seeded["backends"]["mysql"]["host"] == "my.rds"
    assert seeded["backends"]["mysql"]["password"] == "secret-value"
    reset_env_config()


def test_redaction_hides_every_secret():
    rendered = store.redacted_config(_config())
    assert rendered["backends"]["holo"]["password"] == "已设置"
    assert '"p"' not in json.dumps(rendered)


def test_missing_required_uses_labels_not_variable_names():
    data = store.normalize({"active_backend": "holo",
                            "backends": {"holo": {"host": "h"}}})
    missing = store.missing_required(data)
    assert "密码" in missing and "主机地址" not in missing


def test_origins_mark_a_value_that_the_process_environment_supplies(monkeypatch):
    monkeypatch.setenv("CREDIT_AGENT_PG_USER", "from-process")
    data = store.normalize({"active_backend": "postgres",
                            "backends": {"postgres": {"host": "h"}}})
    store.apply_config(data, env_report={"applied": []})
    origins = store.origins_for(data)
    assert origins["postgres.user"] == "process_env"
    assert origins["postgres.host"] == "config_file"
