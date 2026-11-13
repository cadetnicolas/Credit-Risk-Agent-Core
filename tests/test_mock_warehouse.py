"""Smoke test for the non-delivery-face mock warehouse (research capability)."""

from __future__ import annotations

from pathlib import Path

import pytest


def test_mock_warehouse_is_buildable(warehouse):
    duckdb = pytest.importorskip("duckdb")
    path = Path(warehouse)
    assert path.is_file()
    con = duckdb.connect(str(path), read_only=True)
    try:
        tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
        assert tables, "mock warehouse must contain at least one table"
        for name in tables:
            count = con.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            assert count > 0, name
    finally:
        con.close()
