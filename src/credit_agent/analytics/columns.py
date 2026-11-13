"""Business field roles: which column is the loan id, the cohort month, MOB ...

Detection is a convenience only. Explicit bindings (task params, or the
human-confirmed ``context.analysis_columns``) always win, and a role that
cannot be resolved is reported instead of guessed.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

import pandas as pd

ROLE_LABELS: Dict[str, str] = {
    "loan_id": "借据号 / 客户号",
    "cohort": "放款月（cohort）",
    "mob": "账龄 MOB",
    "bad": "逾期 / 坏标记（0/1）",
    "dpd": "逾期天数",
    "bucket": "逾期阶段（M0/M1/...）",
    "period": "统计期（月份）",
    "balance": "在贷余额",
    "amount": "放款金额 / 敞口",
    "score": "风险评分",
    "approved": "审批结果（0/1）",
    "limit": "授信额度",
    "rate": "年化利率",
    "segment": "分析维度（渠道 / 区域 / 客群）",
}

_CANDIDATES: Dict[str, Tuple[str, ...]] = {
    "loan_id": ("loan_id", "loan_no", "loanid", "contract_no", "apply_id", "order_id", "cust_id", "user_id", "id"),
    "cohort": ("loan_month", "cohort", "disburse_month", "vintage", "issue_month", "open_month", "apply_month", "month"),
    "mob": ("mob", "mob_n", "months_on_book", "mob_month"),
    "bad": ("dpd30_flag", "bad_flag", "bad", "is_bad", "overdue_flag", "dpd30", "fpd30", "label", "target", "y"),
    "dpd": ("dpd", "dpd_days", "overdue_days", "days_past_due"),
    "bucket": ("bucket", "m_status", "overdue_stage", "dpd_bucket", "stage"),
    "period": ("stat_month", "period", "snapshot_month", "obs_month", "month", "loan_month", "apply_month"),
    "balance": ("balance", "principal_balance", "outstanding", "os_balance", "remain_principal"),
    "amount": ("amount", "loan_amount", "principal", "loan_amt", "credit_amount"),
    "score": ("score", "credit_score", "risk_score", "model_score", "x_score1"),
    "approved": ("approved", "is_approved", "approve_flag", "pass_flag"),
    "limit": ("limit", "credit_limit", "line", "credit_line"),
    "rate": ("apr", "rate", "interest_rate", "annual_rate"),
}

_SEGMENT_HINTS = ("channel", "region", "province", "city", "product", "segment", "source", "branch", "渠道", "区域", "产品")


def _lower_map(df: pd.DataFrame) -> Dict[str, str]:
    return {str(c).lower(): str(c) for c in df.columns}


def detect_role(df: pd.DataFrame, role: str, taken: Iterable[str] = ()) -> Optional[str]:
    lm = _lower_map(df)
    used = set(taken)
    for cand in _CANDIDATES.get(role, ()):
        col = lm.get(cand)
        if col and col not in used:
            return col
    return None


def detect_segments(df: pd.DataFrame, exclude: Iterable[str] = ()) -> List[str]:
    skip = set(exclude)
    out = []
    for c in df.columns:
        name = str(c)
        if name in skip:
            continue
        if any(h in name.lower() for h in _SEGMENT_HINTS):
            nun = df[c].nunique(dropna=True)
            if 1 < nun <= 50:
                out.append(name)
    return out


def resolve_columns(
    df: pd.DataFrame,
    required: Iterable[str],
    *,
    explicit: Optional[Dict[str, object]] = None,
    optional: Iterable[str] = (),
) -> Tuple[Dict[str, object], List[str]]:
    """Return ``(binding, missing_roles)``.

    ``explicit`` values naming a column that does not exist are treated as
    missing, never silently replaced by a detected column.
    """
    explicit = {k: v for k, v in (explicit or {}).items() if v not in (None, "", [])}
    binding: Dict[str, object] = {}
    missing: List[str] = []
    cols = {str(c) for c in df.columns}
    for role in list(required) + [r for r in optional if r not in required]:
        if role == "segment":
            continue
        val = explicit.get(role)
        if val is not None:
            if str(val) in cols:
                binding[role] = str(val)
            elif role in required:
                missing.append(role)
            continue
        found = detect_role(df, role, taken=[str(v) for v in binding.values() if isinstance(v, str)])
        if found:
            binding[role] = found
        elif role in required:
            missing.append(role)
    seg = explicit.get("segment") or explicit.get("dims")
    if isinstance(seg, str):
        seg = [s.strip() for s in seg.replace("，", ",").split(",") if s.strip()]
    if isinstance(seg, (list, tuple)):
        segs = [str(s) for s in seg if str(s) in cols]
        if len(segs) < len(seg) and "segment" in required:
            missing.append("segment")
        binding["segment"] = segs
    else:
        binding["segment"] = detect_segments(df, exclude=[v for v in binding.values() if isinstance(v, str)])
        if "segment" in required and not binding["segment"]:
            missing.append("segment")
    return binding, missing


def as_flag(series: pd.Series) -> pd.Series:
    """0/1 flag from numeric or common textual encodings; unknown -> NaN."""
    num = pd.to_numeric(series, errors="coerce")
    if num.notna().mean() > 0.95:
        return (num > 0).astype(float).where(num.notna())
    txt = series.astype(str).str.strip().str.lower()
    pos = {"1", "true", "yes", "y", "bad", "是", "逾期"}
    neg = {"0", "false", "no", "n", "good", "否", "正常"}
    return txt.map(lambda v: 1.0 if v in pos else (0.0 if v in neg else float("nan")))
