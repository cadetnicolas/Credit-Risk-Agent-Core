"""SQL preflight for DataWorks / MaxCompute statements."""

from __future__ import annotations

import pytest

from credit_agent.data.ingest import IngestError, ingest_sql
from credit_agent.data.sql_prepare import SqlPrepareError, prepare_analyst_sql
from credit_agent.data.source import DataSource


class _FakeSource(DataSource):
    def query(self, sql, *, limit=None):
        return [{"a": 1}]

    def tables(self):
        return []


def test_rejects_unbound_question_mark_on_duckdb_only():
    with pytest.raises(SqlPrepareError, match="\\?"):
        prepare_analyst_sql("select * from t where dt = ?", backend="duckdb")


def test_odps_allows_question_mark_in_backticks_and_comments():
    sql = "-- 是否?标记\nselect `y标签`, `pred?score` from opmgt_dev.t where a|b > 1"
    out = prepare_analyst_sql(sql, backend="odps")
    assert "`y标签`" in out


def test_question_mark_inside_backtick_not_a_placeholder():
    assert not __import__(
        "credit_agent.data.sql_prepare", fromlist=["_find_unbound_question_mark"]
    )._find_unbound_question_mark("select `col?x` from t")


def test_rejects_pipe_separated_select_list_on_duckdb():
    sql = "select area_flag|婚姻状况|is_loan from opmgt_dev.some_table"
    with pytest.raises(SqlPrepareError, match="\\|"):
        prepare_analyst_sql(sql, backend="duckdb")


def test_strips_set_lines_for_duckdb():
    sql = "set odps.sql.hive.compatible=true;\nselect 1 as x"
    out = prepare_analyst_sql(sql, backend="duckdb")
    assert out.lower().startswith("select")


def test_ingest_wraps_prepare_errors(monkeypatch):
    monkeypatch.setenv("CREDIT_AGENT_DATA_BACKEND", "duckdb")
    from credit_agent.config.env_schema import reset_env_config

    reset_env_config()
    with pytest.raises(IngestError, match="占位符"):
        ingest_sql("select ? from t", "x", source=_FakeSource())
    reset_env_config()
