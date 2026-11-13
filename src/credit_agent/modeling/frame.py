"""Strict modeling read views for database numeric strings; source files stay intact."""
from __future__ import annotations

import re
import pandas as pd

NULL_MARKERS = {"", "\\N", "null", "NULL", "None", "nan", "NaN"}
ROLE_NAME = re.compile(r"(?:^|_)(?:id|key|date|time|month)$|primary_key|loan_no|cust_id|user_id|apply_id|日期|时间|编号", re.I)


def numeric_read_view(frame, *, protected=()):
    out = frame.copy()
    protected = set(protected) - {None, ""}
    previous = frame.attrs.get("modeling_normalization") or {}
    converted = {v["column"]: v for v in previous.get("converted", [])}
    unresolved = []
    for col in frame.columns:
        source = frame[col]
        if col in protected or ROLE_NAME.search(str(col)) or pd.api.types.is_numeric_dtype(source):
            continue
        text = source.astype("string").str.strip()
        markers = text.isin(NULL_MARKERS)
        clean = text.mask(markers)
        # Preserve codes and long identifiers instead of changing their identity.
        if clean.str.fullmatch(r"[+-]?0\d+", na=False).any() or clean.str.fullmatch(r"[+-]?\d{16,}", na=False).any():
            continue
        parsed = pd.to_numeric(clean, errors="coerce")
        meaningful = int(clean.notna().sum())
        bad = int((clean.notna() & parsed.isna()).sum())
        count = int(parsed.notna().sum())
        if bad == 0 and (count or int(markers.sum())):
            out[col] = pd.to_numeric(clean, errors="raise").astype(float)
            converted[col] = {"column": str(col), "from_dtype": str(source.dtype), "to_dtype": "float64", "null_marker_count": int(markers.sum())}
        elif count and meaningful and count / meaningful >= .95:
            unresolved.append({"column": str(col), "unparsed_count": bad,
                               "detail": "多数取值可解析为数值，但存在未知取值；未强制转型，研究默认排除此字段"})
    out.attrs["modeling_normalization"] = {"converted": list(converted.values()), "unresolved": unresolved,
                                           "source_modified": False, "reader": "numeric_read_view/v1", "rule": "All non-null values must parse; preserve leading-zero codes and identifiers"}
    return out
