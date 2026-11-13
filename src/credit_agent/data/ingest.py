"""Data input layer.

A topic needs data before it can analyse anything. There are three ways to get
it, and all of them end up in the same place — a **snapshot** on local disk:

1. **User-supplied SQL**. The analyst already knows the table; they hand over a
   ``SELECT`` and the platform executes it read-only, snapshots the result, and
   registers it as a queryable local table.
2. **Uploaded files** — CSV/Excel/TSV/Parquet dropped in through the API or the CLI.
   The file is decoded, inspected, and snapshotted (see :func:`ingest_file`).
3. **Warehouse tables/views** — queried directly through :class:`DataSource`.

Why snapshot instead of querying live every time?

* **Reproducibility**: an audit must be able to re-derive every number in a
  report from a fixed input, even if the upstream table has since changed.
* **Cost**: MaxCompute scans are billed per byte; a snapshot means the
  drill-down / hypothesis / modelling steps do not re-scan.
* **Sandbox safety**: the sandbox can only reach local files, never production
  credentials, so the analysis code physically cannot read or write the warehouse.

The snapshot is written as Parquet when pyarrow is available (types preserved)
and falls back to CSV otherwise. Either way it is exposed to the sandbox as a
DuckDB view named after the dataset.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config.paths import get_datasets_dir
from .manifest import build_manifest, manifest_summary

# Conservative preview size: enough for the planner to understand the shape of
# the data without shipping megabytes into a prompt.
_MAX_SAMPLE_ROWS = 20

#: Row cap used when the caller does not set one. Overridable per call and via
#: ``CREDIT_AGENT_MAX_SCAN_ROWS``.
_DEFAULT_ROW_CAP = 2_000_000

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Extensions we can write. ``.pq`` is an alias for parquet.
_OUTPUT_FORMATS = {".parquet": "parquet", ".pq": "parquet", ".csv": "csv", ".json": "json"}

# A WHERE clause that touches a partition or time column — i.e. something that
# can actually prune the scan. Used only for an advisory warning: full scans are
# billed per byte on MaxCompute, so it is worth saying out loud, but never worth
# blocking an otherwise valid extract.
#
# `apply_month BETWEEN ...` counts: a cohort filter prunes just as well as a
# `dt=` partition filter, and warning on it would be a false positive on the most
# common credit query of all.
_PARTITION_PREDICATE = re.compile(
    r"(?i)\b(dt|pt|ds|bizdate|stat_?date|stat_?month|partition|part_?dt|p_date"
    r"|apply_?month|apply_?date|loan_?month|data_?date|month|date)\b\s*(=|>=|<=|>|<|between|in)\b"
)

# Keys ``build_manifest`` accepts; anything else in ``manifest_meta`` is ignored
# so a typo cannot silently inject a field into the published contract.
_MANIFEST_KEYS = frozenset({
    "source_kind", "file_name", "file_bytes", "as_of_date", "observation_end",
    "grain_key", "grain_duplicates", "time_column", "checks", "purpose", "operator", "extra",
})


# --- uploaded files ---------------------------------------------------------
# Suffix -> parser. Excel values enter the same reproducible snapshot pipeline.
_UPLOAD_SUFFIXES = {
    ".csv": "csv",
    ".tsv": "csv",
    ".txt": "csv",
    ".parquet": "parquet",
    ".pq": "parquet",
    ".xlsx": "excel",
    ".xls": "excel",
}

# 国内数仓导出的 CSV 大量仍是 GBK/GB18030，所以探测链里必须带它们。
_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "gbk", "big5", "latin-1")

_DELIMITERS = (",", ";", "\t", "|")

# CSV 公式注入：以这些字符开头的字符串被 Excel/表格软件当成公式执行。
# 上传文件来自不可信环境，落快照前统一加前导单引号中和掉。
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def upload_limits() -> Dict[str, int]:
    """Current upload caps (bytes / rows), so the API and CLI agree."""
    return {
        "max_bytes": _env_int("CREDIT_AGENT_UPLOAD_MAX_MB", 50) * 1024 * 1024,
        "max_rows": _env_int("CREDIT_AGENT_UPLOAD_MAX_ROWS", 2_000_000),
    }


class IngestError(RuntimeError):
    """Raised when a data input cannot be produced."""


def _safe_name(name: str) -> str:
    """Validate a dataset identifier, or derive a stable one from the SQL.

    The name becomes a SQL identifier (a DuckDB view), so it is strictly
    whitelisted - a query string must never reach the DDL.
    """
    cleaned = (name or "").strip()
    if not cleaned:
        raise IngestError("dataset name must not be empty")
    if not _IDENTIFIER_RE.match(cleaned):
        digest = hashlib.sha1(cleaned.encode("utf-8")).hexdigest()[:10]
        cleaned = f"ds_{digest}"
    return cleaned


def _normalise_value(value: Any) -> Any:
    """Coerce anything numpy/pyodps-flavoured into a JSON/Parquet-safe scalar.

    pyodps returns Decimal / datetime / Decimal-typed values that pandas 3 will
    happily store but DuckDB and json.dumps will not always accept.
    """
    if value is None:
        return None
    if isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return None if value != value else value  # drop NaN
    # Decimal and other numeric-ish types
    to_float = getattr(value, "__float__", None)
    if to_float is not None:
        try:
            num = float(value)
        except (TypeError, ValueError):
            pass
        else:
            return None if num != num else num
    return str(value)


def _normalise_frame(df) -> List[Dict[str, Any]]:
    """Return plain-Python rows so downstream JSON / DuckDB ingestion is safe."""
    columns = [str(c) for c in df.columns]
    rows: List[Dict[str, Any]] = []
    for record in df.itertuples(index=False, name=None):
        rows.append({col: _normalise_value(val) for col, val in zip(columns, record)})
    return rows


def _infer_columns(rows: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Column name + coarse dtype, for planning prompts and the UI."""
    if not rows:
        return []
    sample = rows[0]
    out: List[Dict[str, str]] = []
    for key, value in sample.items():
        if isinstance(value, bool):
            kind = "boolean"
        elif isinstance(value, int):
            kind = "integer"
        elif isinstance(value, float):
            kind = "number"
        elif value is None:
            kind = "unknown"
        else:
            kind = "string"
        out.append({"column": str(key), "type": kind})
    return out


class DatasetRef(dict):
    """Metadata describing one ingested dataset.

    Stored in ``topic.context["dataset_ref"]`` and surfaced by the API/UI so an
    auditor can see exactly what the analysis ran on.
    """

    @property
    def name(self) -> str:
        return self["name"]

    @property
    def path(self) -> Optional[str]:
        return self.get("path")


def _format_from_path(path: Path) -> Optional[str]:
    return _OUTPUT_FORMATS.get(path.suffix.lower())


def _resolve_output(out_path: Optional[str], safe_name: str, wanted: str, *,
                    overwrite: bool, default_dir: Optional[str] = None) -> tuple[Path, str]:
    """Decide where the snapshot lands and in which format.

    Policy, in order of precedence:

    1. an explicit extension on ``out_path`` wins (``x.csv`` writes CSV);
    2. otherwise ``wanted`` (``parquet`` by default, CSV if parquet is missing);
    3. an ``out_path`` that is an existing directory gets the default filename
       appended, so callers can pass a folder instead of inventing a name.

    Never overwrites silently: the analyst's earlier snapshot may be the input to
    a report that has already been signed.
    """
    if not out_path:
        # ``default_dir`` exists so the MCP facade can point the default at the
        # host-authorized ``--data-root``. Writing to the library's own runtime
        # directory instead would make the file invisible to
        # ``list_credit_datasets`` and ``inspect_and_bind_snapshot``.
        base = Path(default_dir).expanduser() if default_dir else get_datasets_dir()
        target = base / f"{safe_name}.{wanted}"
        fmt = wanted
    else:
        candidate = Path(out_path).expanduser()
        treat_as_dir = candidate.is_dir() or str(out_path).endswith(("/", os.sep))
        if treat_as_dir:
            target = candidate / f"{safe_name}.{wanted}"
            fmt = wanted
        else:
            target = candidate
            fmt = _format_from_path(target) or ""
            if not fmt:
                if not target.suffix:
                    target = target.with_suffix(f".{wanted}")
                    fmt = wanted
                else:
                    raise IngestError(
                        f"输出文件后缀 {target.suffix!r} 不支持；"
                        f"可用：{', '.join(sorted(_OUTPUT_FORMATS))}（或去掉后缀按默认 {wanted} 写）"
                    )
    if not target.is_absolute():
        target = Path.cwd() / target
    if target.exists() and not overwrite:
        raise IngestError(
            f"输出文件已存在：{target}。如需覆盖请显式传 overwrite=true，"
            "或换一个 out_path（保留旧快照通常更安全，报告可能引用它）"
        )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise IngestError(f"无法创建输出目录 {target.parent}：{exc}") from exc
    return target, fmt


def _write_rows(rows: List[Dict[str, Any]], target: Path, fmt: str) -> tuple[Path, str]:
    """Write rows and return ``(path actually written, format actually used)``.

    The path can change: a parquet frame that will not round-trip (mixed object
    dtypes, missing pyarrow) is written as CSV next to the intended file rather
    than failing the whole acquisition.
    """
    try:
        import pandas as pd  # noqa: PLC0415
    except ImportError:  # pragma: no cover - pandas is a hard dependency
        target.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        return target, "json"

    frame = pd.DataFrame(rows)
    if fmt == "parquet":
        try:
            frame.to_parquet(target, index=False)
            return target, "parquet"
        except Exception:
            # pyarrow missing, or an all-object frame that will not round-trip.
            fallback = target.with_suffix(".csv")
            frame.to_csv(fallback, index=False, encoding="utf-8")
            return fallback, "csv"
    if fmt == "json":
        frame.to_json(target, orient="records", force_ascii=False)
        return target, "json"
    frame.to_csv(target, index=False, encoding="utf-8")
    return target, "csv"


def snapshot_rows(rows: List[Dict[str, Any]], name: str, *, sql: str = "",
                  fmt: Optional[str] = None, out_path: Optional[str] = None,
                  default_dir: Optional[str] = None,
                  overwrite: bool = True, manifest_meta: Optional[Dict[str, Any]] = None,
                  extra: Optional[Dict[str, Any]] = None) -> DatasetRef:
    """Persist ``rows`` to disk, describe the result, and publish a manifest.

    Parquet is preferred (it round-trips dtypes); CSV is the fallback because it
    needs no extra dependency and stays human-readable for auditors.

    ``out_path`` defaults to ``<datasets dir>/<name>.parquet`` and may be either
    a directory or a full filename. Whatever the caller chooses is recorded in
    ``manifest.output`` so a reader always knows where the bytes live.

    ``overwrite`` defaults to ``True`` at this level because re-running the same
    extract under the same name is the single most common operation. The
    user-facing entry points (:func:`download_sample`, :func:`import_sample_file`,
    and their MCP tools) default it to ``False`` instead, so an accidental
    replacement of a snapshot a signed report already cites needs a deliberate
    ``overwrite=true``.
    """
    safe = _safe_name(name)
    wanted = (fmt or "").lower() or "parquet"
    target, chosen = _resolve_output(out_path, safe, wanted, overwrite=overwrite,
                                     default_dir=default_dir)

    target, chosen = _write_rows(rows, target, chosen)

    columns = _infer_columns(rows)
    meta = {k: v for k, v in (manifest_meta or {}).items() if k in _MANIFEST_KEYS and v is not None}
    manifest = build_manifest(
        rows=rows,
        source_kind=meta.pop("source_kind", "sql_download"),
        dataset_name=safe,
        out_path=str(target),
        fmt=chosen,
        sql=sql,
        **meta,
    )
    ref = DatasetRef(
        {
            "name": safe,
            "sql": sql,
            "row_count": len(rows),
            "columns": columns,
            "format": chosen,
            "path": str(target),
            "ingested_at": time.time(),
            "sha1": manifest["fingerprint"]["rows_hash"].split(":")[-1][:16],
            "sample_id": manifest["sample_id"],
            "sensitive_columns": manifest["sensitive_columns"],
            "manifest": manifest,
        }
    )
    ref.update(extra or {})
    return ref


def _count_duplicate_keys(rows: List[Dict[str, Any]], key: str) -> int:
    """Rows whose ``key`` repeats — the silent grain error this contract exists for."""
    seen: set = set()
    duplicates = 0
    for row in rows:
        value = row.get(key)
        if value is None:
            continue
        if value in seen:
            duplicates += 1
        else:
            seen.add(value)
    return duplicates


def download_sample(sql: str, *, name: Optional[str] = None, out_path: Optional[str] = None,
                    as_of_date: Optional[str] = None, observation_end: Optional[str] = None,
                    row_cap: Optional[int] = None, grain_key: Optional[str] = None,
                    purpose: str = "", preview_only: bool = False, fmt: Optional[str] = None,
                    default_dir: Optional[str] = None,
                    overwrite: bool = False, source=None, operator: str = "",
                    user_sql: str = "", authorization: str = "") -> Dict[str, Any]:
    """Execute the analyst's SQL read-only and snapshot it under a manifest.

    The analyst owns the SQL: joins, CASE mappings, window functions and all the
    cleaning that a generated query could never anticipate. What this function
    owns is everything around it — one statement, no writes, a bounded row count,
    a recorded fingerprint, and an honest report of what it refused to do.

    Args:
        sql: one read-only statement.
        as_of_date: the date the extract was taken (``YYYY-MM-DD``). Required
            unless ``preview_only``; label maturity cannot be computed without it.
        observation_end: end of the performance window; defaults to ``as_of_date``.
        row_cap: refuse to write more rows than this. Defaults to
            ``CREDIT_AGENT_MAX_SCAN_ROWS`` (2,000,000).
        grain_key: optional column asserted to be unique. Duplicates are
            reported, not blocked — the analyst may be looking at a loan-level
            table on purpose.
        preview_only: run ``LIMIT 100`` and return columns + sample rows without
            writing anything. Use it to confirm column names, types and the
            partition filter before pulling a full extract.
        user_sql: the requester's SQL, verbatim. **Mandatory** — extraction is
            refused without it. ``sql`` must match it, otherwise this function
            raises instead of running: silently widening the scope (extra
            columns, an added WHERE, a different table) changes what every
            downstream number means, and annotating that afterwards does not
            undo it. Preview is gated the same way — a preview still reads data.
        authorization: the requester's own words approving a specific deviation
            (e.g. "同意加上产品维度和地区维度"). Only fill this after actually
            asking; it is recorded in the manifest as ``deviation_authorized_by``.
    """
    from .sql_prepare import SqlPrepareError, enrich_ingest_error, prepare_analyst_sql

    from ..config.env_schema import get_env_config

    backend = get_env_config().data.backend
    raw_text = (sql or "").strip()
    text = raw_text
    if not text:
        raise IngestError("SQL 为空")

    try:
        text = prepare_analyst_sql(text, backend=backend)
    except SqlPrepareError as exc:
        raise IngestError(str(exc)) from exc

    # One statement, read-only. Checked on prepared text so DataWorks-style
    # `SET ...` preambles do not count as a second statement.
    try:
        from .source import DataAccessError, DataSource, assert_read_only, assert_single_statement

        assert_single_statement(text)
        assert_read_only(text)
    except DataAccessError as exc:
        raise IngestError(str(exc)) from exc

    # ---- 取数范围门禁：不一致就拒绝执行，而不是执行完再补标注 -------------
    # 为什么不"先取再看、事后注明"：数据一旦落盘，下游所有占比、分组、结论都
    # 建立在被改写的口径上；标注撤不回这件事，只会在正确结论旁边多留一个错误
    # 结论。所以默认拒绝，把"要不要改口径"的决定权交回需求方。
    # 位置刻意放在建立数据源之前：拒绝时连一次数据库连接都不发生。
    from .sql_prepare import describe_sql_deviation

    requester_sql = (user_sql or "").strip()
    if not requester_sql:
        raise IngestError(
            "拒绝执行：未提供需求方的原始 SQL（user_sql）。\n"
            "取数范围只能由需求方定义，工具不替需求方决定拿哪些列、哪些行。\n"
            "如果你是想自己多取几列先看看，那正是本门禁要拦下的行为——"
            "请先向用户说明需要哪些字段、为什么需要，拿到用户给出的 SQL 后再执行。"
        )

    # 比对用的是调用方传入的原始文本，不是 prepare_analyst_sql 处理后的文本：
    # 预处理只做 SET/USE 脚本行的剥离与语法校验，不改变取数范围，不该被误判为偏离。
    deviation = describe_sql_deviation(requester_sql, raw_text)
    range_gate: Dict[str, Any] = {
        "sql_verbatim": deviation["verbatim"],
        "sql_deviation": deviation,
    }
    if deviation["verbatim"] is not True:
        approved = (authorization or "").strip()
        if not approved:
            raise IngestError(
                "拒绝执行：实际 SQL 与需求方原始 SQL 不一致，已拦下，未取数、未落盘。\n"
                f"偏离明细：{deviation['note']}\n"
                "只有两条路可走：\n"
                "  1) 改回原始 SQL，原样执行；\n"
                "  2) 先向用户说明要加什么列/加什么过滤、为什么必要，"
                "得到明确同意后，把用户的原话填进 authorization 再执行。\n"
                "不接受“先按自己的口径取数，再在结果里补一句说明”。"
            )
        range_gate["deviation_authorized_by"] = approved
        range_gate["sql_deviation_warning"] = (
            f"已按用户授权执行改写后的 SQL：{deviation['note']}。"
            f"授权原话：{approved}"
        )

    try:
        src = source or _default_source()
    except Exception as exc:  # credentials / driver missing
        raise IngestError(str(exc)) from exc

    checks: Dict[str, Any] = {
        "read_only": True,
        "single_statement": True,
        "backend": getattr(src, "name", backend),
        "server_side_read_only": bool(getattr(src, "server_side_read_only", False)),
        "partition_pruned": bool(_PARTITION_PREDICATE.search(text)),
    }
    if not checks["partition_pruned"]:
        checks["partition_warning"] = (
            "SQL 未见分区条件（dt/pt/bizdate 等）。MaxCompute 按扫描字节计费，"
            "确认是否真的需要全表扫描。"
        )
    if not checks["server_side_read_only"]:
        checks["readonly_note"] = (
            f"{checks['backend']} 无服务端只读会话标记，仅由 SQL 文本审计保证只读"
        )

    # ---- 取数范围门禁的结果在此并入 checks（门禁本身已在上文执行）--------
    checks.update(range_gate)

    if preview_only:
        wrapped = f"SELECT * FROM ({text}) AS _credit_preview LIMIT 100"
        try:
            rows = src.query(wrapped, limit=100)
        except Exception as exc:
            raise IngestError(enrich_ingest_error(text, backend, exc)) from exc
        if not rows:
            # The single most common preview outcome: the partition filter points
            # at a window with no data. Say so instead of returning a silent zero.
            checks["empty_result_warning"] = (
                "预览返回 0 行：分区/时间窗很可能写错了。请核对 dt / apply_month 的取值范围后再正式拉取。"
            )
        return {
            "preview": True,
            "row_count": len(rows),
            "columns": _infer_columns(rows),
            "sensitive_columns": build_manifest(rows=rows, source_kind="sql_preview",
                                                dataset_name="preview", sql=text)["sensitive_columns"],
            "rows": rows[:_MAX_SAMPLE_ROWS],
            "checks": checks,
            "backend": getattr(src, "name", backend),
            "target": getattr(src, "safe_target", lambda: backend)(),
            "sql_hash": build_manifest(rows=[], source_kind="sql_preview",
                                       dataset_name="preview", sql=text)["source_ref"]["sql_hash"],
        }

    if not as_of_date:
        raise IngestError(
            "缺少 as_of_date（取数当天的日期 YYYY-MM-DD）。标签成熟度 = 批次月末 + 表现期，"
            "而表现期终点只有取数那一刻知道，数据本身推不出来。"
            "只想先看列和类型的话，请传 preview_only=true。"
        )

    if not (purpose or "").strip():
        raise IngestError(
            "缺少 purpose。正式取数必须写明用途与授权来源，例如"
            "「需求方给定 SQL，原样执行」或「经需求方同意扩列后取数」。"
            "没有这一句，事后无法区分“照需求取数”与“自行扩大取数范围”。"
            "只想先看列名和类型请用 preview_only=true，不必落数据。"
        )

    cap = int(row_cap or get_env_config().data.max_scan_rows or _DEFAULT_ROW_CAP)
    if cap <= 0:
        raise IngestError("row_cap 必须大于 0")

    try:
        rows = src.query(text, limit=cap + 1)  # +1 to detect overflow
    except Exception as exc:
        raise IngestError(enrich_ingest_error(text, backend, exc)) from exc

    if not rows:
        raise IngestError("查询返回 0 行，请检查 SQL 与时间分区")
    if len(rows) > cap:
        raise IngestError(
            f"ROW_CAP_EXCEEDED: 结果超过 row_cap={cap} 行，已拒绝落盘（避免一次拉空生产表）。"
            "建议按 apply_month / 分区拆分多次下载，或收窄时间窗；确实需要全量请显式调大 row_cap。"
        )

    normalised = [{k: _normalise_value(v) for k, v in row.items()} for row in rows]
    checks["row_cap"] = {"limit": cap, "actual": len(normalised), "exceeded": False}

    grain_duplicates = None
    if grain_key:
        grain_duplicates = _count_duplicate_keys(normalised, grain_key)
        checks["grain"] = {"key": grain_key, "duplicates": grain_duplicates}
        if grain_duplicates:
            checks["grain_warning"] = (
                f"grain_key={grain_key} 有 {grain_duplicates} 行重复，粒度声明与实际不符；"
                "vintage / 迁徙率 / 坏账率会因此错位，请先确认一行代表什么。"
            )
    else:
        checks["grain"] = None

    safe = name or _sql_name(text)
    ref = snapshot_rows(
        normalised, safe, sql=text, fmt=fmt, out_path=out_path, overwrite=overwrite,
        default_dir=default_dir,
        manifest_meta={
            "source_kind": "sql_download",
            "as_of_date": as_of_date,
            "observation_end": observation_end,
            "grain_key": grain_key,
            "grain_duplicates": grain_duplicates,
            "checks": checks,
            "purpose": purpose,
            "operator": operator,
        },
    )
    return _result(ref, purpose=purpose)


def _default_source():
    from .source import get_data_source

    return get_data_source()


def _result(ref: DatasetRef, *, purpose: str = "") -> Dict[str, Any]:
    """Envelope-friendly summary: manifest head + the quality flags worth reading."""
    manifest = ref["manifest"]
    body = manifest_summary(manifest)
    body["dataset_name"] = ref["name"]
    body["checks"] = manifest.get("checks", {})
    body["manifest"] = manifest
    if purpose:
        body["purpose"] = purpose
    return body



def ingest_sql(sql: str, name: Optional[str] = None, *, limit: Optional[int] = None,
               source=None, out_path: Optional[str] = None, fmt: Optional[str] = None,
               overwrite: bool = True, as_of_date: Optional[str] = None,
               purpose: str = "") -> DatasetRef:
    """Execute a read-only SQL statement and snapshot the result.

    Lower-level sibling of :func:`download_sample`: no row-cap probe, no checks
    report, no required ``as_of_date``. Prefer ``download_sample`` for anything
    a human will read — this one exists for internal callers and old code.

    Args:
        sql: the statement to run. It is validated by
             :func:`assert_read_only` before it reaches any driver, so write/DDL
             statements are rejected rather than executed.
        name: snapshot identifier. Defaults to a stable hash of the SQL.
        limit: hard cap on rows pulled from the warehouse.
        source: optional :class:`DataSource`; defaults to the configured backend.
        out_path: directory or filename overriding the default snapshot location.

    Returns:
        A :class:`DatasetRef` with row count, inferred columns, path, manifest.

    Raises:
        IngestError: the statement was rejected, the backend is unavailable, or
            the result set was empty.
    """
    from .source import DataAccessError, DataSource, get_data_source
    from .sql_prepare import SqlPrepareError, enrich_ingest_error, prepare_analyst_sql

    from ..config.env_schema import get_env_config

    backend = get_env_config().data.backend

    # Building the client can itself fail (missing credentials / missing
    # driver), and that failure must also surface as an IngestError.
    try:
        src = source or get_data_source()
    except DataAccessError as exc:
        raise IngestError(str(exc)) from exc

    try:
        sql = prepare_analyst_sql(sql, backend=backend)
    except SqlPrepareError as exc:
        raise IngestError(str(exc)) from exc

    # Reject anything that is not a plain read before a driver ever sees it.
    if isinstance(src, DataSource):
        src.assert_read_only(sql)

    cap = limit
    try:
        rows = src.query(sql, limit=cap)
    except DataAccessError as exc:
        raise IngestError(str(exc)) from exc
    except Exception as exc:  # driver/transport failure
        raise IngestError(enrich_ingest_error(sql, backend, exc)) from exc

    if not rows:
        raise IngestError("查询返回 0 行，请检查 SQL 与时间分区")

    # `rows` may arrive as dicts (DuckDB) or as a DataFrame-backed list.
    normalised = [{k: _normalise_value(v) for k, v in row.items()} for row in rows]
    return snapshot_rows(
        normalised, name or _sql_name(sql), sql=sql, fmt=fmt, out_path=out_path,
        overwrite=overwrite,
        manifest_meta={"source_kind": "sql_download", "as_of_date": as_of_date,
                       "purpose": purpose},
    )


# --- uploaded files ---------------------------------------------------------
def _file_name(filename: str) -> str:
    """Derive a dataset identifier from an uploaded filename.

    Chinese filenames are common here and are perfectly fine as a source label,
    but the *identifier* has to stay whitelisted because it becomes a DuckDB
    view name - so anything unusual degrades to a stable hash.
    """
    stem = Path(filename or "").name
    stem = re.sub(r"\.(csv|tsv|txt|parquet|pq)$", "", stem, flags=re.IGNORECASE)
    stem = re.sub(r"[^A-Za-z0-9_]", "_", stem).strip("_")
    if not stem or not _IDENTIFIER_RE.match(stem):
        return "ds_" + hashlib.sha1((filename or "").encode("utf-8")).hexdigest()[:10]
    return stem[:48]


def _decode(raw: bytes, encoding: Optional[str]) -> str:
    """Decode uploaded bytes, probing the encodings Chinese exports actually use."""
    candidates = (encoding,) if encoding else _ENCODINGS
    for enc in candidates:
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    raise IngestError(
        f"无法解码文件（已尝试 {', '.join(candidates)}）；"
        "请用 UTF-8 或 GBK 重新导出，或在请求里显式指定 encoding"
    )


def _sniff_delimiter(sample: str) -> str:
    """Pick the delimiter with the most hits on the header line."""
    header = next((line for line in sample.splitlines() if line.strip()), "")
    counts = {d: header.count(d) for d in _DELIMITERS}
    best = max(counts, key=lambda d: counts[d])
    return best if counts[best] > 0 else ","


def _clean_columns(columns) -> List[str]:
    """Make header names safe and unique.

    Blank / pandas ``Unnamed: n`` headers are common in exports; duplicate names
    would silently shadow each other in DuckDB, so they get a numeric suffix.
    """
    seen: Dict[str, int] = {}
    out: List[str] = []
    for idx, col in enumerate(columns):
        name = str(col).replace("\ufeff", "").strip()
        if not name or name.lower().startswith("unnamed"):
            name = f"col_{idx + 1}"
        # pandas 自己把重复表头改成 "x.1"，点号不是合法 SQL 标识符字符，
        # 空格同理；统一折成下划线，避免下游 DuckDB 里到处加引号。
        name = re.sub(r"[\s\.]+", "_", name).strip("_") or f"col_{idx + 1}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        else:
            seen[name] = 0
        out.append(name)
    return out


def _guard_formula(value: Any) -> Any:
    """Neutralise spreadsheet formula injection in uploaded string cells."""
    if isinstance(value, str) and value[:1] in _FORMULA_PREFIXES:
        # Negative label enums and numeric text are values, not executable formulas.
        if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value):
            return value
        return "'" + value
    return value


def _rows_from_frame(df) -> List[Dict[str, Any]]:
    """Clean headers, guard formulas, and normalise to plain-Python rows."""
    df = df.copy()
    df.columns = _clean_columns(df.columns)
    columns = list(df.columns)
    rows: List[Dict[str, Any]] = []
    for record in df.itertuples(index=False, name=None):
        row = {col: _normalise_value(val) for col, val in zip(columns, record)}
        rows.append({col: _guard_formula(val) for col, val in row.items()})
    return rows


def parse_file(raw: bytes, filename: str, *, delimiter: str = "", encoding: str = "",
               fmt: Optional[str] = None, sheet_name: str = ""):
    """Decode and parse uploaded bytes into (rows, meta).

    Kept separate from :func:`ingest_file` so the API can validate a file before
    deciding to snapshot it.
    """
    suffix = Path(filename or "").suffix.lower()
    kind = (fmt or "").lower() or _UPLOAD_SUFFIXES.get(suffix)
    if kind not in ("csv", "parquet", "excel"):
        raise IngestError(
            f"不支持的文件类型 {suffix or '(无扩展名)'}；支持 {', '.join(sorted(set(_UPLOAD_SUFFIXES)))}"
        )

    try:
        import pandas as pd  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise IngestError("缺少 pandas，无法解析上传文件") from exc

    meta: Dict[str, Any] = {"format": kind, "filename": filename or ""}

    if kind == "excel":
        from .excel import read_excel_frame
        frame, meta["sheet_name"] = read_excel_frame(raw, filename, sheet_name)
        return _rows_from_frame(frame), meta

    if kind == "parquet":
        try:
            frame = pd.read_parquet(io.BytesIO(raw))
        except Exception as exc:
            raise IngestError(f"Parquet 解析失败：{exc}") from exc
        return _rows_from_frame(frame), meta

    text = _decode(raw, encoding or None)
    delim = delimiter or _sniff_delimiter(text[:4096])
    meta["delimiter"] = delim
    meta["encoding"] = encoding or "auto"
    try:
        frame = pd.read_csv(io.StringIO(text), sep=delim)
    except Exception as exc:
        raise IngestError(f"CSV 解析失败（分隔符 {delim!r}）：{exc}") from exc
    if frame.shape[1] <= 1:
        raise IngestError(
            f"只解析出 {frame.shape[1]} 列，分隔符可能不对；"
            "请在上传时显式指定 delimiter（, ; \\t |）"
        )
    return _rows_from_frame(frame), meta


def _data_quality_report(rows: List[Dict[str, Any]], meta: Dict[str, Any], *,
                         grain_key: Optional[str] = None,
                         grain_duplicates: Optional[int] = None,
                         column_map: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """The "导入即体检" report — attached to every upload, readable as a table."""
    from .manifest import detect_label_columns, detect_sensitive_columns, guess_time_column

    columns = list(rows[0].keys()) if rows else []
    nulls: Dict[str, int] = {c: 0 for c in columns}
    for row in rows:
        for column in columns:
            value = row.get(column)
            if value is None or (isinstance(value, float) and value != value):
                nulls[column] += 1
    n = len(rows) or 1
    null_rate = {c: round(count / n, 4) for c, count in nulls.items()}

    label_hits = detect_label_columns(columns)
    time_column = guess_time_column(columns)
    sensitive = detect_sensitive_columns(columns)
    high_null = [c for c, rate in null_rate.items() if rate > 0.6]

    findings: List[str] = []
    if not label_hits:
        findings.append("未识别到标签列（dpd*/bad*/y_*/flag*）：只能做描述性分析，无法建模或算坏账率")
    if not time_column:
        findings.append("未识别到时间/批次列：无法做 vintage、OOT，也无法判断标签成熟度")
    if grain_key and grain_duplicates:
        findings.append(f"grain_key={grain_key} 有 {grain_duplicates} 行重复：粒度声明与实际不符")
    if high_null:
        findings.append(f"高缺失列（>60%）：{', '.join(high_null[:5])}")
    if sensitive:
        findings.append(f"敏感列名提示：{', '.join(sensitive)}（不会阻断，但证据包会自动剔除这些列）")

    return {
        "rows": len(rows),
        "columns": len(columns),
        "encoding": meta.get("encoding") or "auto",
        "delimiter": meta.get("delimiter"),
        "sheet_name": meta.get("sheet_name"),
        "label_candidates": label_hits,
        "time_column": time_column,
        "sensitive_columns": sensitive,
        "high_null_columns": high_null,
        "null_rate_top10": dict(sorted(null_rate.items(), key=lambda kv: -kv[1])[:10]),
        "grain": {"key": grain_key, "duplicates": grain_duplicates},
        "column_map": column_map or {},
        "findings": findings,
    }


def ingest_bytes(raw: bytes, filename: str, name: Optional[str] = None, *,
                 delimiter: str = "", encoding: str = "",
                 fmt: Optional[str] = None, sheet_name: str = "",
                 out_path: Optional[str] = None, out_fmt: Optional[str] = None,
                 default_dir: Optional[str] = None,
                 overwrite: bool = True, as_of_date: Optional[str] = None,
                 grain_key: Optional[str] = None, purpose: str = "",
                 column_map: Optional[Dict[str, str]] = None) -> DatasetRef:
    """Snapshot an uploaded file's bytes as a dataset."""
    limits = upload_limits()
    if not raw:
        raise IngestError("上传文件为空")
    if len(raw) > limits["max_bytes"]:
        raise IngestError(
            f"文件 {len(raw) / 1024 / 1024:.1f} MB 超过 "
            f"{limits['max_bytes'] // 1024 // 1024} MB 上限（CREDIT_AGENT_UPLOAD_MAX_MB）"
        )

    rows, meta = parse_file(raw, filename, delimiter=delimiter, encoding=encoding, fmt=fmt, sheet_name=sheet_name)
    if not rows:
        raise IngestError("文件没有数据行")
    if len(rows) > limits["max_rows"]:
        raise IngestError(
            f"文件 {len(rows)} 行超过 {limits['max_rows']} 行上限（CREDIT_AGENT_UPLOAD_MAX_ROWS）"
        )

    safe = _safe_name(name or _file_name(filename))
    if meta["format"] == "excel" and not name:
        safe += "_" + hashlib.sha256(raw + meta["sheet_name"].encode()).hexdigest()[:10]

    duplicates = _count_duplicate_keys(rows, grain_key) if grain_key else None
    report = _data_quality_report(rows, meta, grain_key=grain_key,
                                 grain_duplicates=duplicates, column_map=column_map)
    ref = snapshot_rows(
        rows,
        safe,
        fmt=out_fmt,
        out_path=out_path,
        default_dir=default_dir,
        overwrite=overwrite,
        manifest_meta={
            "source_kind": "file_upload",
            "file_name": filename or "",
            "file_bytes": len(raw),
            "as_of_date": as_of_date,
            "grain_key": grain_key,
            "grain_duplicates": duplicates,
            "time_column": report.get("time_column"),
            "checks": {"file_upload": True, "report": report},
            "purpose": purpose,
        },
        extra={
            "origin": "upload",
            "source_file": filename or "",
            "delimiter": meta.get("delimiter", ""),
            "encoding": meta.get("encoding", ""),
            "source_format": meta["format"],
            "sheet_name": meta.get("sheet_name", ""),
            "upload_sha256": hashlib.sha256(raw).hexdigest(),
            "quality_report": report,
        },
    )
    return ref


def ingest_file(path, name: Optional[str] = None, *, delimiter: str = "",
                encoding: str = "", fmt: Optional[str] = None, sheet_name: str = "",
                out_path: Optional[str] = None, out_fmt: Optional[str] = None,
                default_dir: Optional[str] = None,
                overwrite: bool = True, as_of_date: Optional[str] = None,
                grain_key: Optional[str] = None, purpose: str = "",
                column_map: Optional[Dict[str, str]] = None,
                max_bytes: Optional[int] = None) -> DatasetRef:
    """Snapshot a local CSV/Excel/TSV/Parquet file (CLI ``--file`` / ``--csv``)."""
    target = Path(path).expanduser()
    if not target.exists():
        raise IngestError(f"文件不存在：{target}")
    if not target.is_file():
        raise IngestError(f"不是文件：{target}")
    if max_bytes is not None and target.stat().st_size > max_bytes:
        raise IngestError(
            f"文件 {target.stat().st_size / 1024 / 1024:.1f} MB 超过 {max_bytes // 1024 // 1024} MB 上限"
        )
    return ingest_bytes(
        target.read_bytes(), target.name, name, delimiter=delimiter, encoding=encoding,
        fmt=fmt, sheet_name=sheet_name, out_path=out_path, out_fmt=out_fmt,
        default_dir=default_dir,
        overwrite=overwrite, as_of_date=as_of_date, grain_key=grain_key,
        purpose=purpose, column_map=column_map,
    )


def import_sample_file(file_path: str, *, name: Optional[str] = None,
                       out_path: Optional[str] = None, encoding: str = "",
                       delimiter: str = "", sheet: str = "",
                       column_map: Optional[Dict[str, str]] = None,
                       grain_key: Optional[str] = None, as_of_date: Optional[str] = None,
                       purpose: str = "", overwrite: bool = False,
                       out_fmt: Optional[str] = None, default_dir: Optional[str] = None,
                       max_bytes: Optional[int] = None) -> Dict[str, Any]:
    """Bring a local file in as a sample and report what it actually contains.

    This is the non-warehouse path: an Excel handover from a partner, an offline
    score file, a CSV exported from a system nobody can query. It produces the
    *same manifest* as :func:`download_sample`, so everything downstream —
    maturity, vintage, modeling — never learns which door the data came in by.

    ``column_map`` maps delivered header names onto standard roles
    (``key``/``label``/``cohort``/``time``/``feature``) so that a file whose
    columns are called "放款金额(元)" and "MOB1逾期天数" is still usable without
    renaming it on disk.

    Cleaning is deliberately *not* implemented here: clean locally, import the
    cleaned file as a second sample, and the chain keeps both snapshots — the
    raw one and the cleaned one — so the transformation is visible afterwards.
    """
    limits = upload_limits()
    cap = max_bytes if max_bytes is not None else limits["max_bytes"]
    ref = ingest_file(
        file_path, name, delimiter=delimiter, encoding=encoding, sheet_name=sheet,
        out_path=out_path, out_fmt=out_fmt, overwrite=overwrite,
        default_dir=default_dir,
        as_of_date=as_of_date, grain_key=grain_key, purpose=purpose,
        column_map=column_map, max_bytes=cap,
    )
    body = _result(ref, purpose=purpose)
    body["quality_report"] = ref.get("quality_report", {})
    body["source_file"] = ref.get("source_file", "")
    return body



def load_dataset_frame(dataset: DatasetRef, *, max_rows: Optional[int] = None):
    """Load a full snapshot into a pandas DataFrame (in-process modeling / validation)."""
    path = dataset.get("path")
    if not path or not Path(path).exists():
        raise IngestError(f"数据集快照不存在: {path}")
    try:
        import pandas as pd  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover
        raise IngestError("需要 pandas 才能加载数据集") from exc
    p = Path(path)
    fmt = (dataset.get("format") or p.suffix.lstrip(".")).lower()
    try:
        if fmt == "parquet" or p.suffix.lower() in (".parquet", ".pq"):
            frame = pd.read_parquet(p)
        else:
            frame = pd.read_csv(p)
    except Exception as exc:
        raise IngestError(f"读取快照失败: {exc}") from exc
    if max_rows is not None and max_rows > 0 and len(frame) > max_rows:
        frame = frame.head(int(max_rows))
    return frame


def read_preview(dataset: DatasetRef, limit: int = _MAX_SAMPLE_ROWS) -> List[Dict[str, Any]]:
    """Return the first rows of a snapshot, for UI preview."""
    path = dataset.get("path")
    if not path or not Path(path).exists():
        return []
    try:
        import pandas as pd  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        return []
    try:
        if dataset.get("format") == "parquet":
            frame = pd.read_parquet(path)
        else:
            frame = pd.read_csv(path)
    except Exception:
        return []
    return _normalise_frame(frame.head(limit))


def list_datasets() -> List[Dict[str, Any]]:
    """Describe every snapshot currently on disk (for reuse across topics)."""
    directory = get_datasets_dir()
    if not directory.exists():
        return []
    out: List[Dict[str, Any]] = []
    for path in sorted(directory.iterdir()):
        if path.suffix.lower() not in (".parquet", ".csv", ".json"):
            continue
        stat = path.stat()
        out.append(
            {
                "name": path.stem,
                "format": path.suffix.lstrip("."),
                "path": str(path),
                "size_bytes": stat.st_size,
                "updated_at": stat.st_mtime,
            }
        )
    return out


def _sql_name(sql: str) -> str:
    """Derive a readable, stable dataset name from the SQL text."""
    match = re.search(r"from\s+([A-Za-z0-9_\.]+)", sql, flags=re.IGNORECASE)
    if match:
        table = match.group(1).split(".")[-1]
        table = re.sub(r"[^A-Za-z0-9_]", "_", table)
        if _IDENTIFIER_RE.match(table):
            return table[:48]
    return "ds_" + hashlib.sha1(sql.encode("utf-8")).hexdigest()[:10]


def register_dataset_view(dataset: DatasetRef) -> Optional[str]:
    """Expose a snapshot to the sandbox as a DuckDB view.

    Returns the view name, or ``None`` when the snapshot is unusable. Only the
    validated identifier reaches the DDL; the SQL text never does.
    """
    try:
        import duckdb  # noqa: PLC0415
    except ImportError:
        return None

    path = dataset.get("path")
    if not path or not Path(path).exists():
        return None

    view = _safe_name(dataset["name"])
    con = duckdb.connect()
    try:
        if dataset.get("format") == "parquet":
            con.execute(f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM read_parquet('{path}')")
        elif dataset.get("format") == "csv":
            con.execute(
                f"CREATE OR REPLACE VIEW {view} AS SELECT * FROM read_csv_auto('{path}')"
            )
        else:
            return None
    finally:
        con.close()
    return view


def describe_dataset(dataset: DatasetRef) -> str:
    """One-line human summary, used in reports and the workbench UI."""
    cols = ", ".join(c["column"] for c in (dataset.get("columns") or [])[:8])
    return f"{dataset['name']}（{dataset.get('row_count', 0)} 行；列：{cols}）"
