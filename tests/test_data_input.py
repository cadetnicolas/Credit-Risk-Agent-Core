"""Tests for the data input layer.

These guard the properties that make a topic's numbers trustworthy: the input is
snapshotted (so the report is reproducible), write statements are refused before
they reach a driver, and the snapshot is what downstream analysis actually reads.
"""

from __future__ import annotations

import os

import pytest

from credit_agent.data.ingest import (
    DatasetRef,
    IngestError,
    _safe_name,
    ingest_bytes,
    ingest_file,
    ingest_sql,
    list_datasets,
    read_preview,
    snapshot_rows,
)
from credit_agent.data.source import DataAccessError, DataSource

SQL = "select loan_no, debt_trend, org_num from opmgt_dev.t_zy_early_risk_mob where mob = 6;"


class _FakeSource(DataSource):
    """Minimal stand-in: returns fixed rows, records what it was asked."""

    def __init__(self, rows=None):
        self.rows = rows if rows is not None else [
            {"loan_no": "L1", "debt_trend": "明显上升", "org_num": 5},
            {"loan_no": "L2", "debt_trend": "基本持平", "org_num": 1},
        ]
        self.calls: list = []

    def query(self, sql, *, limit=None):
        self.calls.append((sql, limit))
        if limit:
            return self.rows[:limit]
        return self.rows

    def tables(self):
        return ["t_zy_early_risk_mob"]


# ---------------------------------------------------------------------------
def test_safe_name_rejects_injection_shaped_input():
    assert _safe_name("early_risk_mob6") == "early_risk_mob6"
    # anything that is not a bare identifier is replaced by a stable hash
    assert _safe_name("opmgt_dev.t_zy_early_risk_mob").startswith("ds_")
    assert _safe_name("x; DROP TABLE t --").startswith("ds_")
    with pytest.raises(IngestError):
        _safe_name("")


def test_ingest_sql_snapshots_the_result():
    src = _FakeSource()
    ref = ingest_sql(SQL, "early_risk_mob6", source=src)

    assert isinstance(ref, DatasetRef)
    assert ref["name"] == "early_risk_mob6"
    assert ref["row_count"] == 2
    assert ref["sql"] == SQL
    assert ref["sha1"]
    assert ref["path"]
    import os

    assert os.path.exists(ref["path"]), "快照文件必须真实落盘"
    # the statement must have been passed through untouched
    assert src.calls[0][0] == SQL


def test_ingest_sql_derives_name_from_sql():
    ref = ingest_sql(SQL, None, source=_FakeSource())
    assert ref["name"] == "t_zy_early_risk_mob"


def test_ingest_sql_respects_limit():
    ref = ingest_sql(SQL, "limited", limit=1, source=_FakeSource())
    assert ref["row_count"] == 1


def test_write_statements_are_refused_before_execution():
    """A write must never reach a driver - it is rejected at the source layer."""
    for bad in (
        "insert into t_zy_early_risk_mob values (1,2,3)",
        "drop table t_zy_early_risk_mob",
        "update t_zy_early_risk_mob set org_num = 0",
        "create table x as select 1",
    ):
        with pytest.raises((IngestError, DataAccessError)):
            ingest_sql(bad, "bad", source=_FakeSource())


def test_empty_result_is_an_error_not_an_empty_dataset():
    with pytest.raises(IngestError):
        ingest_sql(SQL, "empty", source=_FakeSource(rows=[]))


def test_snapshot_round_trips_values():
    rows = [{"a": 1, "b": "x", "c": None}, {"a": 2.5, "b": "y", "c": 3}]
    ref = snapshot_rows(rows, "roundtrip")
    assert ref["row_count"] == 2
    assert [c["column"] for c in ref["columns"]] == ["a", "b", "c"]

    import pandas as pd

    path = ref["path"]
    back = pd.read_parquet(path) if path.endswith(".parquet") else pd.read_csv(path)
    assert len(back) == 2
    assert list(back.columns) == ["a", "b", "c"]
    assert back.iloc[0]["b"] == "x"


def test_query_df_default_implementation_wraps_query():
    src = _FakeSource()
    frame = src.query_df(SQL, limit=1)
    assert len(frame) == 1


# ---------------------------------------------------------------------------
# 上传文件（CSV / TSV / Parquet）
# ---------------------------------------------------------------------------
CSV = "loan_no,debt_trend,org_num\nL1,明显上升,5\nL2,基本持平,1\nL3,明显下降,9\n"


def test_ingest_csv_snapshots_and_records_provenance():
    ref = ingest_bytes(CSV.encode("utf-8"), "early_risk.csv")
    assert ref["name"] == "early_risk"
    assert ref["row_count"] == 3
    assert ref["origin"] == "upload"
    assert ref["source_file"] == "early_risk.csv"
    assert ref["columns"][0]["column"] == "loan_no"
    assert os.path.exists(ref["path"])


def test_csv_encoding_is_probed_including_gbk(monkeypatch, tmp_path):
    """GBK 导出是国内数仓常态，必须能自动解码（而不是报错或乱码）。"""
    gbk = ingest_bytes(CSV.encode("gb18030"), "风险名单.csv")
    assert gbk["row_count"] == 3
    assert gbk["columns"][1]["column"] == "debt_trend"

    utf8_bom = ingest_bytes("﻿" .encode("utf-8") + CSV.encode("utf-8"), "bom.csv")
    assert utf8_bom["columns"][0]["column"] == "loan_no"

    # 显式指定编码时不走探测
    explicit = ingest_bytes(CSV.encode("gb18030"), "x.csv", encoding="gb18030")
    assert explicit["row_count"] == 3


def test_delimiter_is_sniffed(monkeypatch):
    tsv = ingest_bytes("a\tb\n1\t2\n".encode(), "x.tsv")
    assert tsv["delimiter"] == "\t"
    semi = ingest_bytes("a;b\n1;2\n".encode(), "x.csv")
    assert semi["delimiter"] == ";"
    forced = ingest_bytes("a|b\n1|2\n".encode(), "x.csv", delimiter="|")
    assert forced["delimiter"] == "|"


def test_single_column_file_is_rejected_with_actionable_error():
    """只解析出 1 列通常意味着分隔符猜错了，必须明确提示而不是静默产出垃圾。"""
    with pytest.raises(IngestError, match="分隔符"):
        ingest_bytes("a;b\n1;2\n".encode(), "x.tsv", delimiter=",")


def test_headers_are_cleaned_and_deduplicated():
    ref = ingest_bytes("loan_no,,loan_no\nL1,a,L2\n".encode(), "dup.csv")
    cols = [c["column"] for c in ref["columns"]]
    assert cols[1] == "col_2", "空表头要有兜底名"
    assert cols[2] == "loan_no_1", "重复列名必须消歧，否则 DuckDB 里互相覆盖"


def test_formula_injection_is_neutralised():
    """上传文件不可信：=cmd|'/c calc'!A1 这类单元格必须被中和。"""
    ref = ingest_bytes("name,note\n张三,=1+1\n".encode(), "evil.csv")
    rows = read_preview(ref)
    assert rows[0]["note"].startswith("'"), rows[0]


def test_empty_and_unsupported_files_are_errors():
    with pytest.raises(IngestError, match="空"):
        ingest_bytes(b"", "empty.csv")
    with pytest.raises(IngestError, match="没有数据行"):
        ingest_bytes(b"a,b\n", "header_only.csv")
    with pytest.raises(IngestError, match="不支持的文件类型"):
        ingest_bytes(b"x", "risk.xlsb")


def test_upload_caps_are_enforced(monkeypatch):
    monkeypatch.setenv("CREDIT_AGENT_UPLOAD_MAX_ROWS", "2")
    with pytest.raises(IngestError, match="行上限"):
        ingest_bytes(CSV.encode("utf-8"), "big.csv")

    monkeypatch.setenv("CREDIT_AGENT_UPLOAD_MAX_MB", "1")
    with pytest.raises(IngestError, match="上限"):
        ingest_bytes(b"x" * (2 * 1024 * 1024), "big.csv")


def test_ingest_file_reads_from_disk(tmp_path):
    path = tmp_path / "名单.csv"
    path.write_bytes(CSV.encode("gb18030"))
    ref = ingest_file(path)
    assert ref["row_count"] == 3
    assert ref["source_file"] == "名单.csv"

    with pytest.raises(IngestError, match="文件不存在"):
        ingest_file(tmp_path / "nope.csv")


def test_preview_and_listing(tmp_path):
    ref = ingest_bytes(CSV.encode("utf-8"), "preview.csv")
    rows = read_preview(ref, limit=2)
    assert len(rows) == 2
    names = {d["name"] for d in list_datasets()}
    assert ref["name"] in names
