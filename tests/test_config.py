"""Tests for environment configuration loading.

Regression guard: nested config sections must be built from the real process
environment. Validating the outer model with ``{}`` leaves every section at its
default, which silently disables the LLM, the ODPS backend and any other
environment-driven switch.
"""

from __future__ import annotations

import os

import pytest

from credit_agent.config.env_schema import (
    AgentConfig,
    APIConfig,
    DataConfig,
    EmbeddingConfig,
    EnvConfig,
    LLMConfig,
    SandboxConfig,
    get_env_config,
    reset_env_config,
)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    """Keep env mutations from leaking between tests."""
    reset_env_config()
    yield
    reset_env_config()


def test_nested_sections_read_environment(monkeypatch):
    monkeypatch.setenv("CREDIT_AGENT_LLM_API_KEY", "sk-unit-test")
    monkeypatch.setenv("CREDIT_AGENT_LLM_MODEL", "unit-model")
    monkeypatch.setenv("CREDIT_AGENT_DATA_BACKEND", "odps")
    monkeypatch.setenv("CREDIT_AGENT_ODPS_PROJECT", "opmgt_dev")
    monkeypatch.setenv("CREDIT_AGENT_ODPS_ACCESS_ID", "unit-id")
    monkeypatch.setenv("CREDIT_AGENT_ODPS_ACCESS_KEY", "unit-key")
    monkeypatch.setenv("CREDIT_AGENT_SANDBOX_TIMEOUT_SECONDS", "42")
    monkeypatch.setenv("CREDIT_AGENT_MAX_GRAPH_STEPS", "77")
    monkeypatch.setenv("CREDIT_AGENT_EMBED_DIM", "128")
    monkeypatch.setenv("API_AUTH_KEY", "unit-auth")
    monkeypatch.setenv("CREDIT_AGENT_EXPERIENCE_RECALL_LIMIT", "5")

    cfg = get_env_config()

    assert cfg.llm.configured is True
    assert cfg.llm.model == "unit-model"
    assert cfg.data.backend == "odps"
    assert cfg.data.odps_project == "opmgt_dev"
    assert cfg.data.odps_access_id == "unit-id"
    assert cfg.data.odps_access_key == "unit-key"
    assert cfg.sandbox.timeout_seconds == 42
    assert cfg.agent.max_graph_steps == 77
    assert cfg.embedding.dim == 128
    assert cfg.api.api_auth_key == "unit-auth"
    assert cfg.agent.experience_recall_limit == 5


def test_planning_mode_follows_llm_availability(monkeypatch):
    monkeypatch.delenv("CREDIT_AGENT_LLM_API_KEY", raising=False)
    assert get_env_config().planning_mode == "baseline"

    reset_env_config()
    monkeypatch.setenv("CREDIT_AGENT_LLM_API_KEY", "sk-x")
    assert get_env_config().planning_mode == "llm"

    reset_env_config()
    monkeypatch.setenv("CREDIT_AGENT_DETERMINISTIC_PLANNING", "1")
    assert get_env_config().planning_mode == "baseline"


def test_missing_odps_credentials_are_reported_clearly(monkeypatch):
    from credit_agent.data.source import ODPSAdapter

    for var in ("CREDIT_AGENT_ODPS_ACCESS_ID", "CREDIT_AGENT_ODPS_ACCESS_KEY",
                 "CREDIT_AGENT_ODPS_ENDPOINT", "CREDIT_AGENT_ODPS_PROJECT"):
        monkeypatch.delenv(var, raising=False)

    with pytest.raises(Exception) as exc:
        ODPSAdapter()
    assert "CREDIT_AGENT_ODPS" in str(exc.value)


def test_boolean_parsing_accepts_common_spellings(monkeypatch):
    for raw, expected in (("1", True), ("true", True), ("YES", True),
                          ("on", True), ("0", False), ("false", False),
                          ("", False)):
        monkeypatch.setenv("CREDIT_AGENT_DETERMINISTIC_PLANNING", raw)
        reset_env_config()
        assert get_env_config().agent.deterministic_planning is expected
