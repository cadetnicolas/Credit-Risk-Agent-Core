"""Resolve dataframe column roles without hard-coded business tables."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

from .config import ColumnBinding

# Hints only — never used alone when multiple columns match.
_LABEL_NAME_HINTS = frozenset(
    {"label", "target", "y", "ylabel", "bad_flag", "is_bad", "default_flag", "dpd30", "mob3_dpd30"}
)
_ID_NAME_HINTS = frozenset({"id", "pk", "primary_key", "loan_no", "cust_id", "user_id", "apply_id", "order_id"})


class SchemaResolutionError(ValueError):
    """Column roles ambiguous or missing — caller must set ``modeling.columns`` in plugin_params."""


@dataclass(frozen=True)
class ResolvedSchema:
    label: str
    feature_columns: List[str]
    id_columns: List[str]
    excluded_columns: List[str]
    label_positive_values: Sequence


def _column_name_looks_like_label(c: str) -> bool:
    cl = c.lower()
    return cl in _LABEL_NAME_HINTS or any(h in cl for h in ("label", "target", "bad", "default", "dpd"))


def _is_binary_label_series(s: pd.Series) -> bool:
    v = s.dropna()
    if len(v) < 2:
        return False
    uniq = set(v.unique().tolist())
    if uniq <= {0, 1}:
        return True
    if len(uniq) == 2:
        return True
    if len(v) < 10:
        return False
    return False


def _value_counts_payload(s: pd.Series, *, limit: int = 30) -> List[Dict[str, Any]]:
    vc = s.value_counts(dropna=False).head(limit)
    out: List[Dict[str, Any]] = []
    for val, cnt in vc.items():
        out.append({"value": val if pd.notna(val) else None, "count": int(cnt)})
    return out


def probe_label_columns(df: pd.DataFrame) -> List[Dict[str, Any]]:
    """Scan likely label columns and return value distributions for human review."""
    cands: List[str] = []
    for c in df.columns:
        if _column_name_looks_like_label(c):
            cands.append(c)
    if not cands:
        for c in df.columns:
            s = df[c]
            if pd.api.types.is_numeric_dtype(s) and 2 <= s.nunique(dropna=True) <= 20:
                cands.append(c)
    seen: set[str] = set()
    rows: List[Dict[str, Any]] = []
    for c in cands:
        if c in seen:
            continue
        seen.add(c)
        s = df[c]
        rows.append(
            {
                "column": c,
                "dtype": str(s.dtype),
                "nunique": int(s.nunique(dropna=True)),
                "is_binary_like": _is_binary_label_series(s),
                "value_counts": _value_counts_payload(s),
            }
        )
    return rows


def norm_label_value(v: Any) -> Any:
    """Normalise label values so ``"2"``, ``2`` and ``2.0`` compare equal."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, bool):
        return int(v)
    try:
        f = float(str(v).strip())
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return str(v).strip()


def _norm_set(values: Optional[Sequence[Any]]) -> set:
    return {norm_label_value(v) for v in (values or ()) if norm_label_value(v) is not None}


def _norm_series(s: pd.Series) -> pd.Series:
    return s.map(norm_label_value)


def unassigned_label_values(binding: ColumnBinding, df: pd.DataFrame) -> List[Any]:
    """Distinct label values without a role (bad / good / gray / invalid).

    Binary columns with no explicit ``negative_values`` treat the non-positive
    value as good; any column with more than two values requires every value
    to be assigned explicitly.
    """
    if not binding.label or binding.label not in df.columns:
        return []
    uniq = sorted(
        {v for v in _norm_series(df[binding.label]).dropna().unique().tolist()},
        key=lambda x: (str(type(x)), x),
    )
    pos = _norm_set(binding.positive_values)
    neg = _norm_set(binding.negative_values)
    assigned = pos | neg | _norm_set(binding.exclude_label_values) | _norm_set(binding.gray_values)
    if not neg and len(uniq) <= 2 and pos & set(uniq):
        return []
    return [v for v in uniq if v not in assigned]


def label_definition_ready(binding: ColumnBinding, df: Optional[pd.DataFrame] = None) -> bool:
    """True when every label value has a role and at least one bad value exists."""
    if not binding.label or not binding.positive_values:
        return False
    groups = [_norm_set(v) for v in (binding.positive_values, binding.negative_values,
                                   binding.gray_values, binding.exclude_label_values)]
    if any(a & b for i, a in enumerate(groups) for b in groups[i + 1:]):
        return False
    if df is not None and binding.label not in df.columns:
        return False
    if df is not None and binding.label in df.columns:
        if unassigned_label_values(binding, df):
            return False
        present = set(_norm_series(df[binding.label]).dropna().unique().tolist())
        if not (_norm_set(binding.positive_values) & present):
            return False
    return True


def filter_modeling_rows(df: pd.DataFrame, binding: ColumnBinding) -> pd.DataFrame:
    """Drop NaN / invalid / gray(exclude) / unassigned rows."""
    label = binding.label
    if not label or label not in df.columns:
        return df
    lab = _norm_series(df[label])
    keep = lab.notna()
    excl = _norm_set(binding.exclude_label_values)
    if excl:
        keep &= ~lab.isin(excl)
    gray = _norm_set(binding.gray_values)
    if gray and binding.gray_policy == "exclude":
        keep &= ~lab.isin(gray)
    neg = _norm_set(binding.negative_values)
    if neg:
        allowed = _norm_set(binding.positive_values) | neg
        if binding.gray_policy != "exclude":
            allowed |= gray
        keep &= lab.isin(allowed)
    return df[keep]


def apply_gray_as_binary(series: pd.Series, binding: ColumnBinding) -> pd.Series:
    """When gray_policy is as_good/as_bad, map gray rows to a 0/1 marker."""
    gray = _norm_set(binding.gray_values)
    s = _norm_series(series)
    if not gray or binding.gray_policy == "exclude":
        return s
    pos = sorted(_norm_set(binding.positive_values), key=str)
    if binding.gray_policy == "as_bad" and pos:
        return s.map(lambda x: pos[0] if x in gray else x)
    if binding.gray_policy == "as_good":
        return s.map(lambda x: "__gray_good__" if x in gray else x)
    return s


def label_to_binary(series: pd.Series, positive_values: Sequence) -> pd.Series:
    """Map business label encoding to 0/1; NaN stays NaN (caller drops it)."""
    pos = _norm_set(positive_values)
    s = _norm_series(series)
    return s.map(lambda x: np.nan if x is None else (1.0 if x in pos else 0.0)).astype(float)


def binary_label_series(work: pd.DataFrame, binding: ColumnBinding) -> pd.Series:
    """Binary target on an already row-filtered frame."""
    label = binding.label
    if not label or label not in work.columns:
        raise SchemaResolutionError("未配置 label")
    s = apply_gray_as_binary(work[label], binding)
    return label_to_binary(s, binding.positive_values)


def prepare_label_series(df: pd.DataFrame, binding: ColumnBinding) -> pd.Series:
    """Filtered label column ready for ``label_to_binary``."""
    work = filter_modeling_rows(df, binding)
    return binary_label_series(work, binding)


def _label_candidates(df: pd.DataFrame) -> List[str]:
    out: List[str] = []
    for c in df.columns:
        if _column_name_looks_like_label(c) and _is_binary_label_series(df[c]):
            out.append(c)
    return out


def _multiclass_label_candidates(df: pd.DataFrame) -> List[str]:
    out: List[str] = []
    for c in df.columns:
        if _column_name_looks_like_label(c) and not _is_binary_label_series(df[c]):
            out.append(c)
    return out


def _id_candidates(df: pd.DataFrame) -> List[str]:
    return [c for c in df.columns if c.lower() in _ID_NAME_HINTS or c.lower().endswith("_id")]


def resolve_schema(df: pd.DataFrame, binding: ColumnBinding, *, numeric_only: bool = True) -> ResolvedSchema:
    excluded = set(binding.exclude_columns)
    if binding.time_column:
        excluded.add(binding.time_column)
    id_cols = list(binding.id_columns)
    if binding.allow_id_inference and not id_cols:
        id_cols = _id_candidates(df)
    excluded.update(id_cols)

    label = binding.label
    if label:
        if label not in df.columns:
            raise SchemaResolutionError(f"指定标签列不存在: {label}")
        if not label_definition_ready(binding, df):
            missing = unassigned_label_values(binding, df)
            hint = f"未分配角色的取值: {missing}。" if missing else "positive_values 在数据中不存在。"
            raise SchemaResolutionError(
                f"标签列 {label} 的取值角色不完整，{hint}请为每个取值指定 坏(positive_values) / "
                "好(negative_values) / 灰(gray_values) / 无效(exclude_label_values)。"
            )
    elif binding.allow_label_inference:
        cands = _label_candidates(df)
        if len(cands) == 1:
            label = cands[0]
        elif len(cands) > 1:
            raise SchemaResolutionError(
                f"无法自动唯一确定标签列，候选: {cands}。"
                "请在 plugin_params.modeling.columns.label 中显式指定。"
            )
        else:
            multi = _multiclass_label_candidates(df)
            if len(multi) == 1:
                raise SchemaResolutionError(
                    f"发现多分类标签列 {multi[0]}，无法自动二值化。"
                    "请通过「标签定义」评审步骤配置 positive_values 等映射。"
                )
            if len(multi) > 1:
                raise SchemaResolutionError(
                    f"发现多个多分类标签候选 {multi}，请显式指定 modeling.columns.label。"
                )
            raise SchemaResolutionError(
                "未找到可用的二分类标签列。请显式配置 modeling.columns.label，"
                "或在 DAG 中完成「标签定义」人工确认。"
            )
    else:
        raise SchemaResolutionError("未配置 label 且已禁用 allow_label_inference")

    # Inference resolves a column name, never its business encoding.
    from dataclasses import replace
    if not label_definition_ready(replace(binding, label=label), df):
        raise SchemaResolutionError("请明确标签取值的好/坏/灰/无效角色后再执行建模。")
    excluded.add(label)

    if binding.feature_allowlist:
        feats = [c for c in binding.feature_allowlist if c in df.columns and c not in excluded]
    else:
        feats = []
        for c in df.columns:
            if c in excluded:
                continue
            if pd.api.types.is_numeric_dtype(df[c]):
                feats.append(c)
            elif not numeric_only:
                s = df[c]
                n = s.nunique(dropna=True)
                if n <= 50 and n >= 2:
                    feats.append(c)

    if not feats:
        raise SchemaResolutionError(
            "没有可用的特征列，请配置 feature_allowlist、关闭 numeric_only 或检查 exclude_columns"
        )

    pos = tuple(binding.positive_values)

    return ResolvedSchema(
        label=label,
        feature_columns=feats,
        id_columns=id_cols,
        excluded_columns=sorted(excluded),
        label_positive_values=tuple(pos),
    )


def label_probe_payload(df: pd.DataFrame, binding: ColumnBinding) -> Dict[str, Any]:
    """Artifact body for ``label_probe.json`` / human review."""
    candidates = probe_label_columns(df)
    message = ""
    ready = False
    resolved_label: Optional[str] = None
    try:
        resolved_label = resolve_schema(df, binding, numeric_only=False).label
        ready = True
    except SchemaResolutionError as exc:
        message = str(exc)
    if ready and binding.label:
        ready = label_definition_ready(binding, df)

    unassigned: List[Any] = []
    if binding.label and binding.label in df.columns:
        unassigned = unassigned_label_values(binding, df)

    return {
        "status": "ready" if ready else "needs_label_definition",
        "row_count": len(df),
        "resolved_label": resolved_label,
        "label_candidates": candidates,
        "unassigned_values": unassigned,
        "configured_columns": {
            "label": binding.label,
            "positive_values": list(binding.positive_values),
            "negative_values": list(binding.negative_values),
            "exclude_label_values": list(binding.exclude_label_values),
            "gray_values": list(binding.gray_values),
            "gray_policy": binding.gray_policy,
            "time_column": binding.time_column,
        },
        "suggested_defaults": {},
        "message": message,
        "human_input_kind": "modeling_label",
    }
