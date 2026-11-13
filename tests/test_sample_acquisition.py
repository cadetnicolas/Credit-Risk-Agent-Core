"""Acquisition contract: multi-backend sources, output paths, manifests, MCP doors.

These tests pin the decisions in ``docs/sample-acquisition-design.md``:

* the analyst owns the SQL — the tool owns one-statement / read-only / bounded;
* ``as_of_date`` is mandatory because the data cannot supply it;
* the output path is a caller choice with a sensible default;
* both doors (warehouse extract, local file) publish the *same* manifest.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from credit_agent.config.env_schema import get_env_config, reset_env_config
from credit_agent.config.paths import get_datasets_dir
from credit_agent.data.ingest import (
    IngestError,
    download_sample,
    import_sample_file,
    snapshot_rows,
)
from credit_agent.data.manifest import (
    build_manifest,
    detect_label_columns,
    detect_sensitive_columns,
    sql_preview,
)
from credit_agent.data.source import (
    DataAccessError,
    HologresSource,
    MySQLSource,
    PostgresSource,
    assert_read_only,
    assert_single_statement,
    count_statements,
    describe_backends,
    reset_data_source,
)

SQL = "SELECT apply_month, product, channel, region, approved FROM fct_application"


# --------------------------------------------------------------------------- #
# SQL audit
# --------------------------------------------------------------------------- #
def test_trailing_semicolon_is_one_statement():
    assert count_statements("select 1 from t;") == 1


def test_semicolon_inside_literal_does_not_split():
    assert count_statements("select ';' as s from t") == 1
    assert count_statements("select 'a;b' as s, 'c' as c2 from t") == 1


def test_semicolon_inside_comment_does_not_split():
    assert count_statements("-- drop table t;\nselect 1 from t") == 1
    assert count_statements("/* drop; */ select 1 from t") == 1


def test_multiple_statements_are_rejected():
    with pytest.raises(DataAccessError, match="多条语句"):
        assert_single_statement("select 1 from t; select 2 from t")


def test_write_smuggled_after_a_select_is_rejected():
    with pytest.raises(DataAccessError):
        assert_read_only("select 1 from t; delete from t")


@pytest.mark.parametrize("sql", [
    "drop table fct_application",
    "update fct_application set a = 1",
    "create table x as select 1",
    "truncate table t",
    "grant select on t to public",
])
def test_write_statements_are_rejected(sql):
    with pytest.raises(DataAccessError):
        assert_read_only(sql)


def test_dataworks_set_preamble_is_tolerated():
    """Analysts paste DataWorks scripts; the SET line must not read as a 2nd statement."""
    assert_read_only("set odps.sql.allow.fullscan=true;\nselect 1 from t")


# --------------------------------------------------------------------------- #
# Backend configuration
# --------------------------------------------------------------------------- #
def test_blank_port_falls_back_to_backend_default(monkeypatch):
    monkeypatch.setenv("CREDIT_AGENT_PG_PORT", "")
    reset_env_config()
    assert get_env_config().data.pg_port is None

    pg = PostgresSource(host="h", database="d", user="u", password="p")
    assert pg.port == 5432
    holo = HologresSource(host="h", database="d", user="u", password="p")
    assert holo.port == 80
    mysql = MySQLSource(host="h", database="d", user="u", password="p")
    assert mysql.port == 3306


def test_missing_credentials_name_the_variables():
    with pytest.raises(DataAccessError, match="CREDIT_AGENT_PG_HOST"):
        PostgresSource(host="", database="", user="", password="")
    with pytest.raises(DataAccessError, match="CREDIT_AGENT_MYSQL_HOST"):
        MySQLSource(host="", database="", user="", password="")


def test_safe_target_never_contains_a_password():
    pg = PostgresSource(host="db.internal", database="miner", user="u", password="S3cret!")
    label = pg.safe_target()
    assert "S3cret" not in label
    assert "miner" in label


def test_describe_backends_reports_readonly_strength(monkeypatch):
    monkeypatch.setenv("CREDIT_AGENT_DATA_BACKEND", "postgres")
    reset_env_config()
    rows = {row["backend"]: row for row in describe_backends()}
    assert rows["postgres"]["active"] is True
    assert rows["postgres"]["configured"] is False
    assert rows["postgres"]["missing"]
    # The distinction that matters: PG/MySQL/DuckDB enforce read-only server side,
    # MaxCompute has no such flag and leans on the text audit alone.
    assert rows["duckdb"]["server_side_read_only"] is True
    assert rows["postgres"]["server_side_read_only"] is True
    assert rows["holo"]["server_side_read_only"] is True
    assert rows["mysql"]["server_side_read_only"] is True
    assert rows["odps"]["server_side_read_only"] is False


# --------------------------------------------------------------------------- #
# Output path
# --------------------------------------------------------------------------- #
def test_default_output_lands_in_datasets_dir():
    ref = snapshot_rows([{"a": 1}], "default_out")
    assert Path(ref["path"]).parent == get_datasets_dir()
    assert ref["format"] == "parquet"


def test_out_path_accepts_a_directory(tmp_path):
    ref = snapshot_rows([{"a": 1}], "dir_out", out_path=str(tmp_path))
    assert Path(ref["path"]) == tmp_path / "dir_out.parquet"


def test_out_path_extension_wins_over_default_format(tmp_path):
    ref = snapshot_rows([{"a": 1}], "named", out_path=str(tmp_path / "handover.csv"))
    assert ref["format"] == "csv"
    assert Path(ref["path"]).name == "handover.csv"


def test_out_path_rejects_unknown_extension(tmp_path):
    with pytest.raises(IngestError, match="后缀"):
        snapshot_rows([{"a": 1}], "bad", out_path=str(tmp_path / "x.txt"))


def test_existing_output_needs_explicit_overwrite(tmp_path):
    target = str(tmp_path / "dup.parquet")
    snapshot_rows([{"a": 1}], "dup", out_path=target)
    with pytest.raises(IngestError, match="已存在"):
        snapshot_rows([{"a": 2}], "dup", out_path=target, overwrite=False)
    again = snapshot_rows([{"a": 2}], "dup", out_path=target, overwrite=True)
    assert again["row_count"] == 1


# --------------------------------------------------------------------------- #
# download_sample against the bundled warehouse
# --------------------------------------------------------------------------- #
@pytest.fixture
def duck(warehouse):
    reset_data_source()
    yield
    reset_data_source()


def test_download_requires_as_of_date(duck, tmp_path):
    with pytest.raises(IngestError, match="as_of_date"):
        download_sample(SQL, name="needs_date", out_path=str(tmp_path), user_sql=SQL)


def test_preview_only_writes_nothing(duck, tmp_path):
    body = download_sample(SQL, preview_only=True, user_sql=SQL)
    assert body["preview"] is True
    assert 0 < body["row_count"] <= 100
    assert body["columns"]
    assert body["checks"]["read_only"] is True
    assert body["checks"]["single_statement"] is True
    assert not list(tmp_path.iterdir())


def test_extract_refused_without_requester_sql(duck, tmp_path):
    """取数范围由需求方定义。没有他的原句就不执行，而不是"先取完再标注"。"""
    with pytest.raises(IngestError, match="拒绝执行"):
        download_sample(SQL, as_of_date="2026-10-31", purpose="测试用取数",
                        name="no_user_sql", out_path=str(tmp_path))
    assert not list(tmp_path.iterdir())


def test_preview_is_gated_too(duck, tmp_path):
    """预览也会读到数据，所以同样要凭需求方的 SQL 才能跑。"""
    with pytest.raises(IngestError, match="拒绝执行"):
        download_sample(SQL, preview_only=True)
    assert not list(tmp_path.iterdir())


def test_widened_select_is_refused_before_reading_data(duck, tmp_path):
    widened = SQL.replace(" FROM ", ", loan_amt, cust_id FROM ")
    with pytest.raises(IngestError, match="拒绝执行"):
        download_sample(widened, as_of_date="2026-10-31", purpose="测试用取数",
                        name="widened", out_path=str(tmp_path), user_sql=SQL)
    assert not list(tmp_path.glob("*.parquet"))


def test_added_where_is_refused(duck, tmp_path):
    with pytest.raises(IngestError, match="WHERE"):
        download_sample(f"{SQL} WHERE region = 'A'", as_of_date="2026-10-31",
                        purpose="测试用取数", name="filtered",
                        out_path=str(tmp_path), user_sql=SQL)
    assert not list(tmp_path.glob("*.parquet"))


def test_changed_source_table_is_refused(duck, tmp_path):
    with pytest.raises(IngestError, match="拒绝执行"):
        download_sample(SQL.replace("fct_application", "fct_loan"),
                        as_of_date="2026-10-31", purpose="测试用取数",
                        name="other_table", out_path=str(tmp_path), user_sql=SQL)


def test_deviation_runs_only_with_the_requesters_words(duck, tmp_path):
    """授权通道:改写必须带上需求方的原话,并落到 manifest 里可追溯。"""
    widened = f"{SQL} WHERE region IS NOT NULL"
    body = download_sample(widened, as_of_date="2026-10-31", purpose="测试用取数",
                           name="authorized", out_path=str(tmp_path), user_sql=SQL,
                           authorization="同意只看有地区的样本")
    checks = body["checks"]
    assert checks["sql_verbatim"] is False
    assert checks["deviation_authorized_by"] == "同意只看有地区的样本"
    assert body["manifest"]["checks"]["deviation_authorized_by"] == "同意只看有地区的样本"


def test_row_cap_refuses_to_write(duck, tmp_path):
    with pytest.raises(IngestError, match="ROW_CAP_EXCEEDED"):
        download_sample(SQL, as_of_date="2026-10-31", purpose="测试用取数", row_cap=2,
                        name="capped", out_path=str(tmp_path), user_sql=SQL)
    assert not list(tmp_path.glob("*.parquet"))


def test_two_statements_blocked_before_any_driver_call(duck, tmp_path):
    with pytest.raises(IngestError, match="多条语句"):
        download_sample("select 1 from fct_application; select 2",
                        as_of_date="2026-10-31", purpose="测试用取数", name="multi", out_path=str(tmp_path),
                        user_sql="select 1 from fct_application; select 2")


def test_manifest_contract_is_complete(duck, tmp_path):
    body = download_sample(
        f"{SQL} WHERE apply_month = '2023-07'",
        as_of_date="2026-10-31", observation_end="2026-10-31",
        name="manifest_ok", out_path=str(tmp_path),
        grain_key="apply_month", purpose="vintage 排查",
        user_sql=f"{SQL} WHERE apply_month = '2023-07'",
    )
    manifest = body["manifest"]
    for key in ("sample_id", "manifest_version", "source_kind", "source_ref", "row_count",
                "columns", "sensitive_columns", "time_span", "observation", "grain",
                "checks", "fingerprint", "output", "purpose", "acquired_at"):
        assert key in manifest, key

    assert manifest["source_kind"] == "sql_download"
    assert manifest["observation"]["as_of_date"] == "2026-10-31"
    assert manifest["output"]["path"].endswith(".parquet")
    assert manifest["purpose"] == "vintage 排查"
    assert manifest["row_count"] > 0

    # The SQL text is fingerprinted, and the preview masks literals: a filter
    # value can itself be a customer id, so it must not reach the manifest.
    assert manifest["source_ref"]["sql_hash"].startswith("sha256:")
    assert "'?'" in manifest["source_ref"]["sql_preview"]
    assert "2023-07" not in manifest["source_ref"]["sql_preview"]
    assert manifest["fingerprint"]["rows_hash"].startswith("sha256:")


def test_partition_absence_is_a_warning_not_a_block(duck, tmp_path):
    body = download_sample(SQL, as_of_date="2026-10-31", purpose="测试用取数", name="no_partition",
                           out_path=str(tmp_path), user_sql=SQL)
    assert body["checks"]["partition_pruned"] is False
    assert "partition_warning" in body["checks"]


def test_grain_duplicates_are_reported(duck, tmp_path):
    body = download_sample(f"{SQL} WHERE apply_month = '2023-07'",
                           as_of_date="2026-10-31", purpose="测试用取数", name="grain",
                           out_path=str(tmp_path), grain_key="apply_month",
                           user_sql=f"{SQL} WHERE apply_month = '2023-07'")
    assert body["checks"]["grain"]["key"] == "apply_month"
    assert body["checks"]["grain"]["duplicates"] > 0
    assert "grain_warning" in body["checks"]


def test_same_extract_has_a_stable_sql_hash(duck, tmp_path):
    first = download_sample(SQL, as_of_date="2026-10-31", purpose="测试用取数", name="h1", out_path=str(tmp_path),
                         user_sql=SQL)
    second = download_sample(SQL, as_of_date="2026-10-31", purpose="测试用取数", name="h2", out_path=str(tmp_path),
                         user_sql=SQL)
    assert first["manifest"]["source_ref"]["sql_hash"] == second["manifest"]["source_ref"]["sql_hash"]
    assert first["manifest"]["fingerprint"]["rows_hash"] == second["manifest"]["fingerprint"]["rows_hash"]


# --------------------------------------------------------------------------- #
# import_sample_file
# --------------------------------------------------------------------------- #
def test_gbk_handover_file_with_chinese_headers(tmp_path):
    raw = (
        "客户号,申请月份,逾期天数,身份证号\n"
        "C1,2026-01,0,110101199001011234\n"
        "C2,2026-01,35,110101199001011235\n"
    ).encode("gb18030")
    source = tmp_path / "合作方样本.csv"
    source.write_bytes(raw)

    body = import_sample_file(str(source), name="gbk_handover", out_path=str(tmp_path / "out"))
    assert body["row_count"] == 2
    assert "客户号" in [c["name"] for c in body["manifest"]["columns"]]

    report = body["quality_report"]
    assert report["rows"] == 2
    assert "身份证号" in report["sensitive_columns"]
    assert report["label_candidates"], "逾期天数 should be recognised as a label"
    assert report["time_column"], "申请月份 should be recognised as a time column"
    # Sensitive columns are a hint, never a blocker.
    assert body["manifest"]["sensitive_columns"]


def test_import_produces_the_same_manifest_shape(tmp_path):
    source = tmp_path / "plain.csv"
    source.write_text("apply_id,apply_month,bad_flag\n1,2026-01,0\n2,2026-01,1\n", encoding="utf-8")
    body = import_sample_file(str(source), name="plain", out_path=str(tmp_path / "out"),
                              as_of_date="2026-10-31", purpose="测试用取数", grain_key="apply_id")
    manifest = body["manifest"]
    assert manifest["source_kind"] == "file_upload"
    assert manifest["source_ref"]["file_name"] == "plain.csv"
    assert manifest["source_ref"]["file_bytes"] > 0
    assert manifest["observation"]["as_of_date"] == "2026-10-31"
    assert manifest["grain"] == {"key": "apply_id", "duplicates": 0}
    # Same keys the warehouse door publishes, so downstream cannot tell them apart.
    for key in ("sample_id", "columns", "sensitive_columns", "checks", "fingerprint", "output"):
        assert key in manifest, key


def test_duplicate_grain_key_is_surfaced(tmp_path):
    source = tmp_path / "dup_keys.csv"
    source.write_text("apply_id,bad_flag\n1,0\n1,1\n", encoding="utf-8")
    body = import_sample_file(str(source), name="dupkeys", out_path=str(tmp_path / "out"),
                              grain_key="apply_id")
    assert body["manifest"]["grain"]["duplicates"] == 1
    assert any("重复" in finding for finding in body["quality_report"]["findings"])


def test_missing_file_is_actionable(tmp_path):
    with pytest.raises(IngestError, match="文件不存在"):
        import_sample_file(str(tmp_path / "nope.csv"))


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def test_sql_preview_masks_every_literal():
    preview = sql_preview("select * from t where cust_id = '1234567890' and dt in ('20260101')")
    assert "1234567890" not in preview
    assert "20260101" not in preview
    assert preview.count("'?'") == 2


def test_sensitive_and_label_detection_cover_english_and_chinese():
    assert set(detect_sensitive_columns(["id_card_no", "mobile", "loan_amount"])) == {
        "id_card_no", "mobile",
    }
    assert "身份证号" in detect_sensitive_columns(["客户号", "身份证号"])
    assert "逾期天数" in detect_label_columns(["逾期天数", "放款金额"])
    assert "bad_flag" in detect_label_columns(["bad_flag", "score"])


def test_manifest_never_records_the_raw_sql():
    manifest = build_manifest(
        rows=[{"a": 1}], source_kind="sql_download", dataset_name="t",
        sql="select * from dwd_apply where cust_id = 'secret-value'",
    )
    blob = str(manifest)
    assert "secret-value" not in blob
    assert "dwd_apply" in blob          # structure kept, values masked


# --------------------------------------------------------------------------- #
# MCP doors
# --------------------------------------------------------------------------- #
@pytest.fixture
def mcp(tmp_path):
    from credit_agent.mcp_server import create_server

    data = tmp_path / "authorized-data"
    registry = tmp_path / "signed-registries"
    ledger = tmp_path / "tool-ledger"
    for path in (data, registry):
        path.mkdir()
    server = create_server(data_root=data, registry_root=registry, ledger_root=ledger)
    tools = server._tool_manager._tools
    return {"download": tools["download_sample"].fn,
            "import": tools["import_sample_file"].fn,
            "roots": (data, registry, ledger)}


def test_mcp_download_blocks_multiple_statements(mcp, duck):
    out = mcp["download"]("select 1 from fct_application; select 2", as_of_date="2026-10-31",
                          purpose="测试用取数", user_sql="select 1 from fct_application; select 2")
    assert out["status"] == "blocked"
    assert out["block_code"] == "MULTI_STATEMENT_SQL"


def test_mcp_download_blocks_oversized_extract(mcp, duck):
    out = mcp["download"](SQL, as_of_date="2026-10-31", purpose="测试用取数", row_cap=2, user_sql=SQL)
    assert out["status"] == "blocked"
    assert out["block_code"] == "ROW_CAP_EXCEEDED"
    assert "row_cap" in out["summary"]["message"]


def test_mcp_download_requires_extract_date(mcp, duck):
    out = mcp["download"](SQL, user_sql=SQL)
    assert out["status"] == "blocked"
    assert "as_of_date" in out["summary"]["message"]


def test_mcp_output_path_may_not_target_the_ledger(mcp, duck):
    ledger = mcp["roots"][2]
    out = mcp["download"](SQL, as_of_date="2026-10-31", purpose="测试用取数", out_path=str(ledger / "x.parquet"),
                          user_sql=SQL)
    assert out["status"] == "blocked"
    assert out["block_code"] == "RESOURCE_UNAVAILABLE"
    assert not ledger.exists() or not list(ledger.rglob("*.parquet"))


def test_mcp_download_writes_under_data_root_by_default(mcp, duck):
    data_root = mcp["roots"][0]
    out = mcp["download"](SQL, as_of_date="2026-10-31", purpose="测试用取数", dataset_name="from_tool",
                           user_sql=SQL)
    assert out["status"] in ("ok", "degraded")
    written = Path(out["summary"]["path"])
    assert written.is_relative_to(data_root)
    assert out["summary"]["as_of_date"] == "2026-10-31"
    assert out["artifacts"][0]["kind"] == "sample_manifest"


def test_mcp_preview_needs_no_extract_date(mcp, duck):
    out = mcp["download"](SQL, preview_only=True, user_sql=SQL)
    # No WHERE at all here, so the partition hint fires and the call is degraded —
    # that is the point of the hint. What matters is that it is not blocked.
    assert out["status"] in ("ok", "degraded")
    assert out["summary"]["preview"] is True
    assert out["summary"]["checks"]["partition_pruned"] is False


def test_mcp_preview_flags_an_empty_window(mcp, duck):
    """A zero-row preview is the 'wrong partition' case — it must say so."""
    empty = "SELECT * FROM fct_application WHERE apply_month = '1999-01'"
    out = mcp["download"](empty, preview_only=True, user_sql=empty)
    assert out["status"] == "degraded"
    assert "empty_result_warning" in out["summary"]["checks"]
    assert out["summary"]["row_count"] == 0


def test_mcp_refuses_rewritten_sql_instead_of_annotating_it(mcp, duck):
    """宿主侧也要看到明确的拒绝码，而不是一条"已标注"的降级记录。"""
    widened = SQL.replace(" FROM ", ", loan_amt FROM ")
    out = mcp["download"](widened, as_of_date="2026-10-31",
                          purpose="测试用取数", user_sql=SQL)
    assert out["status"] == "blocked"
    assert out["block_code"] == "SQL_SCOPE_NOT_AUTHORIZED"
    assert "loan_amt" in out["summary"]["message"]


def test_mcp_import_reports_missing_file(mcp):
    out = mcp["import"](str(mcp["roots"][0] / "nope.csv"))
    assert out["status"] == "blocked"
    assert out["block_code"] == "SAMPLE_FILE_UNREADABLE"


def test_mcp_import_accepts_a_file_outside_the_data_root(mcp, tmp_path):
    """A personal tool: the handover file lives wherever the analyst keeps it."""
    source = tmp_path / "elsewhere.csv"
    source.write_text("apply_id,bad_flag,apply_month\n1,0,2026-01\n", encoding="utf-8")
    out = mcp["import"](str(source), dataset_name="outside_root")
    assert out["status"] in ("ok", "degraded")
    assert Path(out["summary"]["path"]).is_relative_to(mcp["roots"][0])
    assert out["summary"]["source_kind"] == "file_upload"

