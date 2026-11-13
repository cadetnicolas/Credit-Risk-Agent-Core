"""The ``sample_manifest`` contract shared by both acquisition entries.

A sample can arrive two ways — a SQL extract from the warehouse, or a file the
analyst already has — and everything downstream must be blind to the
difference. That is the whole point of this module: one structure, produced at
acquisition time, carried by the ledger and quoted by the evidence pack.

Two fields earn their keep and are worth calling out:

``observation.as_of_date``
    Label maturity is ``cohort month + performance window``. The performance
    window runs to *the date the extract was taken*, which is known only at
    acquisition time and cannot be recovered from the data itself. Recording it
    here turns maturity from a guess into arithmetic.

``grain`` / ``checks.grain``
    ``one_row_per_application`` and ``one_row_per_loan`` produce different
    vintage, roll-rate and bad-rate numbers from the *same* file, and nothing in
    the file reveals which one it is. Recording the declared key and the
    duplicate count makes the mistake visible instead of silent.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence

MANIFEST_VERSION = "1.0"

# Column names that *look* like they carry personal data. This is a hint for the
# report writer and for evidence-pack exclusion — it never blocks a download.
#
# Chinese headers are matched too: a handover file from a partner is far more
# likely to say 身份证号 / 手机号 / 客户姓名 than id_card_no, so an English-only
# pattern list would silently miss the most common real case.
_SENSITIVE_PATTERNS = (
    re.compile(r"(id_?card|cert(ificate)?_?no|id_?no$|^id_?number|身份证|证件号)", re.I),
    re.compile(r"(mobile|phone|tel(_?no)?$|contact|手机|电话|联系方式)", re.I),
    re.compile(r"(bank_?card|card_?no|acct_?no|account_?no|银行卡|卡号|账号)", re.I),
    re.compile(r"(e?mail|邮箱)", re.I),
    re.compile(r"(name$|_name$|cust_?nm|customer_?name|姓名|客户名)", re.I),
    re.compile(r"(addr|address|street|postcode|zip|地址|住址|邮编)", re.I),
    re.compile(r"(device_?id|idfa|imei|oaid|mac_?addr|设备号)", re.I),
    re.compile(r"(passport|license_?no|plate_?no|护照|车牌)", re.I),
)

# Columns that plausibly carry the cohort / observation time. Chinese credit
# extracts label these 申请月份 / 放款日期 / 账龄 rather than apply_month.
_TIME_HINT = re.compile(
    r"(month|_dt$|^dt$|date|time|period|cohort|apply_?d|月份|日期|账龄|期间|批次)",
    re.I,
)

# Label columns. 逾期天数 / 坏账标志 are the two names that actually appear.
_LABEL_HINT = re.compile(
    r"(^dpd|^bad|^y_|label|flag|overdue|delinq|逾期|坏账|违约|是否通过|风险标识)",
    re.I,
)


def _to_jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return None if value != value else value
    if hasattr(value, "isoformat"):
        try:
            return value.isoformat()
        except Exception:
            return str(value)
    try:
        num = float(value)
    except Exception:
        return str(value)
    return None if num != num else num


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


def sql_preview(sql: str, *, limit: int = 280) -> str:
    """A shape-preserving preview of the SQL, with literals masked.

    Filter values often *are* the sensitive part (``where cust_id = '...'``), so
    the preview keeps the statement structure and replaces every string literal.
    This is what goes into the manifest and the evidence pack; the full text
    never leaves the acquisition call.
    """
    masked = re.sub(r"'(?:[^']|'')*'", "'?'", sql or "")
    masked = re.sub(r"\s+", " ", masked).strip()
    return masked if len(masked) <= limit else masked[: limit - 1] + "…"


def detect_sensitive_columns(columns: Iterable[str]) -> List[str]:
    """Columns whose *name* suggests personal data. Advisory only."""
    hits: List[str] = []
    for column in columns:
        name = str(column)
        if any(pattern.search(name) for pattern in _SENSITIVE_PATTERNS):
            hits.append(name)
    return hits


def guess_time_column(columns: Sequence[str]) -> Optional[str]:
    for column in columns:
        if _TIME_HINT.search(str(column)):
            return str(column)
    return None


def _column_stats(rows: Sequence[Dict[str, Any]], *, scope: int = 50_000) -> List[Dict[str, Any]]:
    """Null rate and coarse dtype per column, computed on a bounded sample."""
    if not rows:
        return []
    sample = rows if len(rows) <= scope else list(rows[:scope])
    n = len(sample)
    keys = list(sample[0].keys())
    out: List[Dict[str, Any]] = []
    for key in keys:
        nulls = 0
        kinds: set[str] = set()
        for row in sample:
            value = row.get(key)
            if value is None or (isinstance(value, float) and value != value):
                nulls += 1
                continue
            if isinstance(value, bool):
                kinds.add("boolean")
            elif isinstance(value, int):
                kinds.add("integer")
            elif isinstance(value, float):
                kinds.add("number")
            else:
                kinds.add("string")
        if not kinds:
            dtype = "unknown"
        elif len(kinds) == 1:
            dtype = next(iter(kinds))
        else:
            dtype = "string"   # mixed -> treated as text downstream
        out.append({"name": str(key), "dtype": dtype, "null_rate": round(nulls / n, 4)})
    return out


def _rows_hash(rows: Sequence[Dict[str, Any]]) -> str:
    """Cheap content fingerprint: column order + count + head/tail rows."""
    head = [_to_jsonable_value(r) for r in rows[:100]]
    tail = [_to_jsonable_value(r) for r in rows[-100:]] if len(rows) > 100 else []
    payload = json.dumps({"n": len(rows), "head": head, "tail": tail},
                         sort_keys=True, ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _to_jsonable_value(row: Dict[str, Any]) -> Dict[str, Any]:
    return {str(k): _to_jsonable(v) for k, v in row.items()}


def _time_span(rows: Sequence[Dict[str, Any]], column: Optional[str]) -> Optional[Dict[str, str]]:
    if not column or not rows:
        return None
    values = [row.get(column) for row in rows]
    values = [v for v in values if v is not None and str(v).strip()]
    if not values:
        return None
    try:
        lo, hi = min(values), max(values)  # type: ignore[type-var]
    except TypeError:
        as_text = sorted(str(v) for v in values)
        lo, hi = as_text[0], as_text[-1]
    return {"column": column, "min": str(lo), "max": str(hi)}


def make_sample_id(*, source_kind: str, fingerprint: str, at: Optional[float] = None) -> str:
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(at or time.time()))
    digest = fingerprint.split(":")[-1][:6] if fingerprint else "000000"
    return f"smp_{source_kind}_{stamp}_{digest}"


def build_manifest(
    *,
    rows: Sequence[Dict[str, Any]],
    source_kind: str,
    dataset_name: str,
    out_path: Optional[str] = None,
    fmt: Optional[str] = None,
    sql: str = "",
    file_name: Optional[str] = None,
    file_bytes: Optional[int] = None,
    as_of_date: Optional[str] = None,
    observation_end: Optional[str] = None,
    grain_key: Optional[str] = None,
    grain_duplicates: Optional[int] = None,
    time_column: Optional[str] = None,
    checks: Optional[Dict[str, Any]] = None,
    purpose: str = "",
    operator: str = "",
    generator: str = "credit-agent",
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Assemble the manifest described in ``docs/sample-acquisition-design.md`` §5."""
    stats = _column_stats(rows)
    columns = [c["name"] for c in stats]
    sensitive = detect_sensitive_columns(columns)
    rows_digest = _rows_hash(rows)
    schema_digest = sha256_text(json.dumps([[c["name"], c["dtype"]] for c in stats]))

    source_ref: Dict[str, Any] = {
        "sql_hash": sha256_text(sql) if sql else None,
        "sql_preview": sql_preview(sql) if sql else None,
        "file_name": file_name,
        "file_bytes": file_bytes,
    }
    manifest: Dict[str, Any] = {
        "manifest_version": MANIFEST_VERSION,
        "source_kind": source_kind,          # sql_download | file_upload
        "dataset_name": dataset_name,
        "source_ref": source_ref,
        "row_count": len(rows),
        "column_count": len(columns),
        "columns": [{**c, "is_sensitive": c["name"] in sensitive} for c in stats],
        "sensitive_columns": sensitive,
        "time_span": _time_span(rows, time_column or guess_time_column(columns)),
        "observation": {
            "as_of_date": as_of_date,
            "observation_end": observation_end or as_of_date,
        },
        "grain": {"key": grain_key, "duplicates": grain_duplicates},
        "checks": checks or {},
        "fingerprint": {
            "schema_hash": schema_digest,
            "rows_hash": rows_digest,
            "rows_hash_scope": "columns+count+head100+tail100",
            "generator": generator,
        },
        "output": {"path": out_path, "format": fmt},
        "purpose": purpose,
        "operator": operator,
        "acquired_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    # sample_id derives from the content hash, so re-running the same extract at
    # a different time yields a different id but an identical rows_hash — which
    # is exactly what "same sample, different day" should look like.
    manifest["sample_id"] = make_sample_id(source_kind=source_kind, fingerprint=rows_digest)
    if extra:
        manifest.update(extra)
    return manifest


def manifest_summary(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """The compact view for a tool envelope — everything an analyst needs first."""
    return {
        "sample_id": manifest.get("sample_id"),
        "source_kind": manifest.get("source_kind"),
        "dataset_name": manifest.get("dataset_name"),
        "row_count": manifest.get("row_count"),
        "column_count": manifest.get("column_count"),
        "sensitive_columns": manifest.get("sensitive_columns", []),
        "time_span": manifest.get("time_span"),
        "as_of_date": (manifest.get("observation") or {}).get("as_of_date"),
        "path": (manifest.get("output") or {}).get("path"),
        "format": (manifest.get("output") or {}).get("format"),
        "rows_hash": (manifest.get("fingerprint") or {}).get("rows_hash"),
    }


__all__ = [
    "MANIFEST_VERSION",
    "build_manifest",
    "detect_label_columns",
    "detect_sensitive_columns",
    "guess_time_column",
    "make_sample_id",
    "manifest_summary",
    "sha256_text",
    "sql_preview",
]


def detect_label_columns(columns: Iterable[str]) -> List[str]:
    """Columns that plausibly carry the good/bad outcome."""
    return [str(c) for c in columns if _LABEL_HINT.search(str(c))]
