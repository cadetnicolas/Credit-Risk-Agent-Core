"""SQLite ledger for snapshots, runs, and human decisions.

The frame for a snapshot stays on disk. Tool envelopes store ids, not rows.

Security notes (v0.3):
* Every id used to build a filesystem path is checked against ``_SAFE_ID`` and
  the resolved path must stay under the ledger root.
* Files/directories are created with restrictive permissions where the OS
  allows it (chmod failures are ignored so a read-only tmp_path still works).
* ``save_run`` writes the artifact atomically before touching the database.
* Human decisions are append-only and can be consumed exactly once per
  ``(gate, review_id)`` via ``consume_decision``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pandas as pd

_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _dump(payload: Dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


def _load(raw: str) -> Dict[str, Any]:
    return json.loads(raw)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Ledger:
    def __init__(self, root: Path, tenant: Optional[str] = None):
        self.root = Path(root)
        self.tenant = tenant
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "ledger.sqlite"
        self.frames = self.root / "frames"
        self.artifacts = self.root / "artifacts"
        # Some sandboxed interpreters raise on mkdir(exist_ok=True) when the
        # directory is already there. Guarding keeps a re-open idempotent.
        for directory in (self.frames, self.artifacts):
            if not directory.is_dir():
                directory.mkdir(parents=True, exist_ok=True)
        self._chmod(self.root, 0o700)
        self._chmod(self.frames, 0o700)
        self._chmod(self.artifacts, 0o700)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS snapshots(
                    id TEXT PRIMARY KEY, meta_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS maturity(
                    snapshot_id TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS metrics(
                    snapshot_id TEXT NOT NULL, metric_id TEXT NOT NULL,
                    version TEXT NOT NULL, binding_json TEXT NOT NULL,
                    PRIMARY KEY(snapshot_id, metric_id, version));
                CREATE TABLE IF NOT EXISTS admissions(
                    snapshot_id TEXT PRIMARY KEY, level TEXT NOT NULL, status TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs(
                    id TEXT PRIMARY KEY, tool TEXT NOT NULL, snapshot_id TEXT,
                    summary_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS decisions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    gate TEXT NOT NULL, review_id TEXT NOT NULL,
                    actor TEXT NOT NULL, channel TEXT NOT NULL, payload_json TEXT NOT NULL,
                    created_at TEXT, tenant TEXT);
                CREATE TABLE IF NOT EXISTS decision_consumptions(
                    gate TEXT NOT NULL, review_id TEXT NOT NULL, consumed_at TEXT,
                    PRIMARY KEY(gate, review_id));
                CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
                """
            )
            self._ensure_decision_schema(conn)
            if tenant is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO meta(key, value) VALUES('tenant', ?)",
                    (str(tenant),),
                )
        self._chmod(self.db_path, 0o600)

    # -- infrastructure -------------------------------------------------

    @staticmethod
    def _chmod(path: Path, mode: int) -> None:
        try:
            os.chmod(path, mode)
        except OSError:
            pass

    @staticmethod
    def _check_id(value: Any) -> None:
        if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
            raise ValueError("invalid_id")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _within(self, path: Path) -> Path:
        """Second confirmation that a constructed path stays under the root."""
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("invalid_id")
        return path

    def _ensure_decision_schema(self, conn: sqlite3.Connection) -> None:
        """Migrate a legacy decisions table to the append-only schema.

        The legacy table used PRIMARY KEY(gate, review_id), which forbids more
        than one row per key. An append-only ledger needs an autoincrement id,
        so that table is rebuilt. Partially migrated tables gain the missing
        columns through ALTER TABLE ADD COLUMN.
        """
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(decisions)")}
        if {"id", "created_at", "tenant"} <= cols:
            return
        if "id" not in cols:
            conn.executescript(
                """
                ALTER TABLE decisions RENAME TO decisions_legacy;
                CREATE TABLE decisions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    gate TEXT NOT NULL, review_id TEXT NOT NULL,
                    actor TEXT NOT NULL, channel TEXT NOT NULL, payload_json TEXT NOT NULL,
                    created_at TEXT, tenant TEXT);
                INSERT INTO decisions(gate, review_id, actor, channel, payload_json, created_at, tenant)
                    SELECT gate, review_id, actor, channel, payload_json, NULL, NULL
                    FROM decisions_legacy;
                DROP TABLE decisions_legacy;
                """
            )
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(decisions)")}
        if "created_at" not in cols:
            conn.execute("ALTER TABLE decisions ADD COLUMN created_at TEXT")
        if "tenant" not in cols:
            conn.execute("ALTER TABLE decisions ADD COLUMN tenant TEXT")

    # -- frames and masks -----------------------------------------------

    def save_frame(self, snapshot_id: str, frame: pd.DataFrame) -> Path:
        self._check_id(snapshot_id)
        path = self._within(self.frames / f"{snapshot_id}.parquet")
        # Object columns that look like long digit strings (for example a
        # 24-digit loan key) make pyarrow infer int64 and overflow. Persisting
        # them as Arrow strings keeps the values byte-identical and avoids the
        # OverflowError while leaving numeric columns untouched.
        out = frame.copy()
        for column in out.columns:
            if out[column].dtype == object:
                out[column] = out[column].astype("string")
        out.to_parquet(path, index=False)
        self._chmod(path, 0o600)
        return path

    def load_frame(self, snapshot_id: str) -> pd.DataFrame:
        self._check_id(snapshot_id)
        path = self._within(self.frames / f"{snapshot_id}.parquet")
        if not path.is_file():
            raise KeyError(snapshot_id)
        return pd.read_parquet(path)

    def save_mask(self, snapshot_id: str, mask: pd.Series) -> Path:
        self._check_id(snapshot_id)
        path = self._within(self.frames / f"{snapshot_id}.mask.parquet")
        pd.DataFrame({"mature": mask.astype(bool).to_numpy()}).to_parquet(path, index=False)
        self._chmod(path, 0o600)
        return path

    def load_mask(self, snapshot_id: str) -> pd.Series:
        self._check_id(snapshot_id)
        path = self._within(self.frames / f"{snapshot_id}.mask.parquet")
        if not path.is_file():
            raise KeyError(snapshot_id)
        return pd.read_parquet(path)["mature"]

    def save_latest_mask(self, snapshot_id: str, mask: pd.Series) -> Path:
        """Persist a separate 'latest observable' mask; never overwrite the mature mask."""
        self._check_id(snapshot_id)
        path = self._within(self.frames / f"{snapshot_id}.latest.parquet")
        pd.DataFrame({"mature": mask.astype(bool).to_numpy()}).to_parquet(path, index=False)
        self._chmod(path, 0o600)
        return path

    def load_latest_mask(self, snapshot_id: str) -> pd.Series:
        self._check_id(snapshot_id)
        path = self._within(self.frames / f"{snapshot_id}.latest.parquet")
        if not path.is_file():
            raise KeyError(snapshot_id)
        return pd.read_parquet(path)["mature"]

    # -- snapshot / metric / admission records --------------------------

    def save_snapshot(self, snapshot_id: str, meta: Dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO snapshots(id, meta_json) VALUES(?, ?)",
                (snapshot_id, _dump(meta)),
            )

    def get_snapshot(self, snapshot_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT meta_json FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
        return _load(row["meta_json"]) if row else None

    def save_maturity(self, snapshot_id: str, payload: Dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO maturity(snapshot_id, payload_json) VALUES(?, ?)",
                (snapshot_id, _dump(payload)),
            )

    def get_maturity(self, snapshot_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload_json FROM maturity WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
        return _load(row["payload_json"]) if row else None

    def save_metric(self, snapshot_id: str, metric_id: str, version: str, binding: Dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                """INSERT OR REPLACE INTO metrics(snapshot_id, metric_id, version, binding_json)
                   VALUES(?, ?, ?, ?)""",
                (snapshot_id, metric_id, version, _dump(binding)),
            )

    def get_metric(self, snapshot_id: str, metric_id: str, version: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                """SELECT binding_json FROM metrics
                   WHERE snapshot_id=? AND metric_id=? AND version=?""",
                (snapshot_id, metric_id, version),
            ).fetchone()
        return _load(row["binding_json"]) if row else None

    def save_admission(self, snapshot_id: str, level: str, status: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO admissions(snapshot_id, level, status) VALUES(?, ?, ?)",
                (snapshot_id, level, status),
            )

    def get_admission(self, snapshot_id: str) -> Optional[Dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT level, status FROM admissions WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
        if not row:
            return None
        return {"level": row["level"], "status": row["status"]}

    # -- runs ------------------------------------------------------------

    def save_run(self, run_id: str, tool: str, snapshot_id: Optional[str], summary: Dict[str, Any]) -> Dict[str, Any]:
        self._check_id(run_id)
        self._check_id(tool)
        raw = _dump(summary).encode("utf-8")
        path = self._within(self.artifacts / f"{run_id}.json")
        tmp = self._within(self.artifacts / f"{run_id}.json.tmp")
        tmp.write_bytes(raw)
        self._chmod(tmp, 0o600)
        os.replace(tmp, path)
        self._chmod(path, 0o600)
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO runs(id, tool, snapshot_id, summary_json) VALUES(?, ?, ?, ?)",
                    (run_id, tool, snapshot_id, raw.decode("utf-8")),
                )
        except Exception:
            try:
                path.unlink()
            except OSError:
                pass
            raise
        return {"kind": tool, "path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        self._check_id(run_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT tool, snapshot_id, summary_json FROM runs WHERE id=?", (run_id,)
            ).fetchone()
        if not row:
            return None
        return {
            "tool": row["tool"],
            "snapshot_id": row["snapshot_id"],
            "summary": _load(row["summary_json"]),
        }

    def save_json_artifact(self, run_id: str, kind: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        self._check_id(run_id)
        if not kind or not all(char.isascii() and (char.isalnum() or char in "_-") for char in kind):
            raise ValueError("invalid_artifact_kind")
        raw = _dump(payload).encode("utf-8")
        path = self._within(self.artifacts / f"{run_id}.{kind}.json")
        path.write_bytes(raw)
        self._chmod(path, 0o600)
        return {"kind": kind, "path": str(path), "sha256": hashlib.sha256(raw).hexdigest()}

    # -- human decisions (append-only, one-time consumption) ------------

    def save_decision(self, gate: str, review_id: str, actor: str, channel: str, payload: Dict[str, Any]) -> None:
        self._check_id(gate)
        self._check_id(review_id)
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO decisions(gate, review_id, actor, channel, payload_json, created_at, tenant)
                   VALUES(?, ?, ?, ?, ?, ?, ?)""",
                (gate, review_id, actor, channel, _dump(payload), _now(), self.tenant),
            )

    def get_decision(self, gate: str, review_id: str) -> Optional[Dict[str, Any]]:
        self._check_id(gate)
        self._check_id(review_id)
        with self._connect() as conn:
            row = conn.execute(
                """SELECT actor, channel, payload_json FROM decisions
                   WHERE gate=? AND review_id=? ORDER BY id DESC LIMIT 1""",
                (gate, review_id),
            ).fetchone()
        if not row:
            return None
        return {"actor": row["actor"], "channel": row["channel"], "payload": _load(row["payload_json"])}

    def consume_decision(self, gate: str, review_id: str) -> Optional[Dict[str, Any]]:
        """Return the latest decision once; subsequent calls return None.

        Consumption is keyed by ``(gate, review_id)`` and recorded in
        ``decision_consumptions``. ``get_decision`` never consumes, so the
        audit trail stays readable.
        """
        self._check_id(gate)
        self._check_id(review_id)
        with self._connect() as conn:
            consumed = conn.execute(
                "SELECT 1 FROM decision_consumptions WHERE gate=? AND review_id=?",
                (gate, review_id),
            ).fetchone()
            if consumed:
                return None
            row = conn.execute(
                """SELECT actor, channel, payload_json FROM decisions
                   WHERE gate=? AND review_id=? ORDER BY id DESC LIMIT 1""",
                (gate, review_id),
            ).fetchone()
            if not row:
                return None
            conn.execute(
                "INSERT INTO decision_consumptions(gate, review_id, consumed_at) VALUES(?, ?, ?)",
                (gate, review_id, _now()),
            )
        return {"actor": row["actor"], "channel": row["channel"], "payload": _load(row["payload_json"])}
