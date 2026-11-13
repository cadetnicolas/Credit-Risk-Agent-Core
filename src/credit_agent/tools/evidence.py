"""Human approval records and report-to-artifact citation checks.

``record_human_decision`` is called by the trusted host (approval card), not by
the model. ``verify_and_commit_report`` commits a report only when every figure
in the body is traceable to a run summary and every signoff claim is backed by
a human approval card.
"""

from __future__ import annotations

import math
import re
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Dict, List, Sequence

from credit_agent.tools import envelope, new_id
from credit_agent.tools.codes import GATES as _GATES, assert_code

_MISSING = object()

# Legal ``method`` values for the two fallback-style gates. The first group is
# used by validation_fallback, the second by maturity_choice; both are accepted
# here so the host can route a single approval card to either gate.
_FALLBACK_METHODS = frozenset({
    "random_holdout", "cv_only", "descriptive_only", "stop",
    "shorter_signed_window", "unlabeled_descriptive", "wait_for_maturity",
})

# Tool -> signoff gate. The approval card's review_id equals the tool's run_id.
_SIGNOFF_GATES = {
    "train_and_evaluate_candidate": "model_signoff",
    "simulate_policy_rules": "policy_trial",
    "decompose_and_locate_anomaly": "anomaly_disposition",
}
_APPROVAL_WORDS = ("已批准", "建议上线", "已立项", "已签收", "正式上线")

_NUMBER = re.compile(
    r"(?<![\w.])("
    r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?%"
    r"|-?\d+\.\d+%"
    r"|-?\d+%"
    r"|-?\d{1,3}(?:,\d{3})+(?:\.\d+)?\s*bp(?![A-Za-z])"
    r"|-?\d+\.\d+\s*bp(?![A-Za-z])"
    r"|-?\d+\s*bp(?![A-Za-z])"
    r"|-?\d{1,3}(?:,\d{3})+(?:\.\d+)?"
    r"|-?\d+\.\d+"
    r"|-?\d+"
    r")"
)

# ISO dates are removed before scanning so their digits are never mistaken for
# uncited numbers. Longest forms come first so YYYY-MM-DD is not truncated.
_ISO_DATE = re.compile(
    r"(?<!\d)\d{4}[-/]\d{2}[-/]\d{2}(?!\d)"
    r"|(?<!\d)\d{4}[-/]\d{2}(?!\d)"
)


def _blocked(message: str, code: str = "REPORT_UNTRACED") -> Dict[str, Any]:
    assert_code(code)
    return envelope(
        tool="verify_and_commit_report",
        status="blocked",
        block_code=code,
        summary={"message": message},
    )


def record_human_decision(
    *,
    ledger: Any,
    gate: str,
    review_id: str,
    actor: str,
    channel: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """Persist an approval-card decision. The actor cannot be the agent."""

    if channel != "approval_card":
        raise ValueError("channel 必须是 approval_card")
    if not isinstance(gate, str) or not gate.strip() or gate not in _GATES:
        raise ValueError("gate 不是已登记的审批环节")
    if not isinstance(review_id, str) or not review_id.strip():
        raise ValueError("review_id 不能为空")
    if not isinstance(actor, str) or not actor.strip() or actor.strip().lower() == "agent":
        raise ValueError("actor 必须是真人标识，不能是 agent")
    if not isinstance(payload, dict):
        raise ValueError("payload 必须是字典")
    if gate in ("validation_fallback", "maturity_choice") and "method" in payload:
        if payload["method"] not in _FALLBACK_METHODS:
            raise ValueError("method 不在合法枚举内")
    if gate == "feature_list" and "selected_features" in payload:
        selected = payload["selected_features"]
        if not isinstance(selected, list) or not selected or not all(isinstance(name, str) for name in selected):
            raise ValueError("selected_features 必须是非空字符串列表")
    ledger.save_decision(gate, review_id, actor, channel, payload)
    return envelope(
        tool="record_human_decision",
        status="ok",
        summary={"gate": gate, "review_id": review_id},
    )


def _dig(summary: Any, path: str):
    current = summary
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            index = int(part)
            if index < 0 or index >= len(current):
                return _MISSING
            current = current[index]
        else:
            return _MISSING
    return current


def _is_number(value: Any) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    return False


def _parse_rendered(rendered: str):
    text = "".join(str(rendered).split()).replace(",", "")
    scale = Decimal(1)
    if text.endswith("%"):
        scale = Decimal(100)
        text = text[:-1]
    elif text.endswith("bp"):
        scale = Decimal(10000)
        text = text[:-2]
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text):
        raise ValueError(rendered)
    decimals = len(text.partition(".")[2])
    shown = Decimal(text)
    return shown / scale, decimals, scale


def _matches(artifact: Any, rendered: str) -> bool:
    try:
        target, decimals, scale = _parse_rendered(rendered)
    except (ValueError, ArithmeticError):
        return False
    try:
        art = Decimal(str(artifact))
        display = (art * scale).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    except (ArithmeticError, ValueError):
        return False
    back = display / scale
    diff = abs(back - target)
    magnitude = max(abs(back), abs(target))
    if magnitude == 0:
        return diff == 0
    return diff <= Decimal("1e-9") * magnitude


def _compact(text: Any) -> str:
    """Remove all whitespace for an exact string comparison."""
    return "".join(str(text).split())


def _norm_number(text: Any) -> str:
    """Remove whitespace and thousands separators for exact number matching."""
    return _compact(text).replace(",", "")


def _scan_numbers(report_markdown: str) -> List[str]:
    text = _ISO_DATE.sub(" ", str(report_markdown or ""))
    return [match.group(1) for match in _NUMBER.finditer(text)]


def _signoff_status(ledger: Any, run_tools: Sequence) -> Dict[str, str]:
    status: Dict[str, str] = {}
    for run_id, tool in run_tools:
        gate = _SIGNOFF_GATES.get(tool)
        if gate is None:
            continue
        decision = ledger.get_decision(gate, run_id)
        signed = decision is not None and decision.get("channel") == "approval_card"
        # When several runs map to the same gate, a single pending one wins.
        if status.get(gate) == "pending":
            continue
        status[gate] = "signed" if signed else "pending"
    return status


def verify_and_commit_report(
    *,
    ledger: Any,
    run_ids: Sequence[str],
    report_markdown: str,
    citations: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """Commit a report only when every cited figure matches a run summary."""

    summaries = []
    snapshot_id = None
    run_tools: List[Any] = []
    for run_id in run_ids:
        run = ledger.get_run(run_id)
        if not run:
            return _blocked("引用的运行不存在")
        if snapshot_id is None and run.get("snapshot_id"):
            snapshot_id = run["snapshot_id"]
        summaries.append(run["summary"])
        run_tools.append((run_id, run.get("tool")))
    if isinstance(citations, (str, bytes)) or not isinstance(citations, Sequence):
        return _blocked("引用格式无效")
    for cite in citations:
        if not isinstance(cite, dict) or "path" not in cite or "rendered" not in cite:
            return _blocked("引用缺少 path 或 rendered")
        rendered = str(cite["rendered"])
        values = []
        text_values = []
        seen = False
        for summary in summaries:
            found = _dig(summary, str(cite["path"]))
            if found is _MISSING:
                continue
            seen = True
            if _is_number(found):
                values.append(found)
            elif isinstance(found, str):
                text_values.append(found)
        if not seen:
            return _blocked("引用取不到数字")
        numeric_ok = any(_matches(value, rendered) for value in values)
        text_ok = any(_compact(value) == _compact(rendered) for value in text_values)
        if not (numeric_ok or text_ok):
            return _blocked("引用与产物数字对不上")
    rendered_norms = {_norm_number(cite.get("rendered", "")) for cite in citations}
    for fragment in _scan_numbers(str(report_markdown)):
        if _norm_number(fragment) not in rendered_norms:
            return _blocked("正文数字没有对应引用")

    signoff_status = _signoff_status(ledger, run_tools)
    report_text = str(report_markdown or "")
    claims_approval = any(word in report_text for word in _APPROVAL_WORDS)
    if claims_approval and (any(state == "pending" for state in signoff_status.values()) or not signoff_status):
        return _blocked("报告包含上线/批准措辞但缺少人工签收", code="SIGNOFF_REQUIRED")

    summary = {
        "citation_count": len(citations),
        "signoff_status": signoff_status,
        "signoff_required": True,
    }
    run_id = new_id("run")
    artifact = ledger.save_run(run_id, "verify_and_commit_report", snapshot_id, summary)
    return envelope(
        tool="verify_and_commit_report",
        status="ok",
        summary=summary,
        snapshot_id=snapshot_id,
        run_id=run_id,
        artifacts=[artifact],
    )
