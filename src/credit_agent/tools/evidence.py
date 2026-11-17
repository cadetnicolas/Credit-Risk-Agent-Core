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
from typing import Any, Dict, List, Sequence, Tuple

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
    # The lookbehind excludes ASCII identifiers only (``snap_abc123``,
    # ``v1.2.3``). It must NOT be ``\w``: Python's ``\w`` matches Han
    # characters, so "共400笔" would never be scanned at all — a Chinese report
    # could then carry any uncited figure just by gluing it to text.
    r"(?<![0-9A-Za-z_.])("
    r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?[ \t]*[%％]"
    r"|-?\d+\.\d+[ \t]*[%％]"
    r"|-?\d+[ \t]*[%％]"
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

# Labels, not measurements. A Chinese credit report names the month and the
# threshold in every paragraph ("9 月", "≥10 万"), and demanding a citation for
# those digits makes a *correct* report impossible to commit. These spans are
# masked before scanning; period correctness is still checked by the analysis
# run itself, which carries the period list it actually used.
_IGNORED_FRAGMENT = re.compile(
    r"(?<!\d)\d{4}\s*年(?:\s*\d{1,2}\s*月)?(?:\s*\d{1,2}\s*日)?"
    r"|(?<!\d)\d{1,2}\s*月(?:份)?(?:\s*\d{1,2}\s*日)?"
    r"|(?<!\d)\d{1,2}\s*日(?!\w)"
    r"|第\s*\d+\s*个?\s*(?:月|期|季度|批)"
    r"|\d+(?:\.\d+)?\s*个?\s*(?:月|期|季度|批)"
    r"|[Mm][Oo][Bb]\s*[-=]?\s*\d+"
    r"|[≥≤<>＞＜]\s*-?\d+(?:\.\d+)?"
)


def _masked_spans(text: str) -> List[tuple]:
    return [(match.start(), match.end()) for match in _IGNORED_FRAGMENT.finditer(text)]


def _overlaps(start: int, end: int, spans: Sequence[tuple]) -> bool:
    return any(start < right and left < end for left, right in spans)


def _blocked(
    message: str,
    code: str = "REPORT_UNTRACED",
    failures: Sequence[Dict[str, Any]] = (),
    checked: Any = None,
) -> Dict[str, Any]:
    assert_code(code)
    records = list(failures or [])
    details: Dict[str, Any] = {}
    if records:
        details["reason"] = records[0].get("reason")
        details["failure_count"] = len(records)
        details["failures"] = records[:_MAX_FAILURES]
    if checked:
        details["checked"] = checked
    return envelope(
        tool="verify_and_commit_report",
        status="blocked",
        block_code=code,
        summary={"message": message},
        details=details,
    )


# Why a report failed to verify, in the order the checks run. ``REPORT_UNTRACED``
# stays a single block_code on purpose: the code is what a host routes on, the
# reason is what the analyst reads. Splitting it into five codes would force
# every SKILL and approval card to learn five names for one outcome.
_REASON_HINT = {
    "no_runs_referenced": "先调用工具产出 run_id；本地脚本或对话里算出的数字不在账本里，没有可核验的对象。",
    "empty_report": "没有正文就没有可核验的东西；先写分析稿，再逐句挂引用。",
    "run_not_found": "核对 run_id 是否来自本次工具返回，以及它是否写在当前 ledger-root。",
    "citation_malformed": '每条引用必须是 {"path": "...", "rendered": "..."}。',
    "path_not_found": "path 相对该 run 的 summary；参照 available_paths 里的顶层字段重取。",
    "value_mismatch": "rendered 的精度必须与产物一致；expected 给出按当前精度渲染的正确写法。",
    "uncited_number": "正文只保留可引用的产物数字；阈值（如 ≥10 万）、月份（如 9 月）已自动豁免，其余数字要么引用，要么改成叙述。",
    "signoff_pending": "由受信任宿主写审批卡；Agent 不能自称已批准。去掉批准措辞，或先完成签收。",
}
_REASON_LABEL = {
    "no_runs_referenced": "未引用任何运行",
    "empty_report": "正文为空",
    "run_not_found": "引用的运行不存在",
    "citation_malformed": "引用格式无效",
    "path_not_found": "引用路径不存在",
    "value_mismatch": "引用值与产物不符",
    "uncited_number": "正文数字无引用",
    "signoff_pending": "人工签收未完成",
}
_MAX_FAILURES = 10
_MAX_AVAILABLE_PATHS = 24


def _failure(reason: str, **fields: Any) -> Dict[str, Any]:
    """One diagnosable failure record: reason + the exact thing that failed."""

    record = {"reason": reason, "hint": _REASON_HINT.get(reason, "")}
    record.update({key: value for key, value in fields.items() if value not in (None, [], "")})
    return record


def _available_paths(summaries: Sequence[Dict[str, Any]]) -> List[str]:
    """Top-level paths the caller could actually cite from these runs."""

    seen: List[str] = []
    for summary in summaries:
        if not isinstance(summary, dict):
            continue
        for key in summary:
            name = str(key)
            if name not in seen:
                seen.append(name)
    return seen[:_MAX_AVAILABLE_PATHS]


def _expected_renderings(values: Sequence[Any], texts: Sequence[Any], rendered: str) -> List[str]:
    """How the artifact would have to be written to match at this precision."""

    candidates: List[str] = []
    stripped = str(rendered).strip()
    try:
        _, decimals, scale = _parse_rendered(rendered)
    except (ValueError, ArithmeticError):
        decimals, scale = 4, Decimal(1)
    if stripped.endswith(("%", "％")):
        suffix = "%"
    elif stripped.endswith("bp"):
        suffix = "bp"
    else:
        suffix = ""
    grouped = "," in stripped
    for value in values:
        try:
            shown = (Decimal(str(value)) * scale).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
        except (ArithmeticError, ValueError):
            continue
        # Keep the caller's own thousands style so the suggestion is copy-pasteable.
        body = f"{shown:,}" if grouped else f"{shown}"
        candidates.append(f"{body}{suffix}")
    candidates.extend(str(text) for text in texts)
    return candidates[:4]


def _context(text: str, start: int, end: int, width: int = 12) -> str:
    """The fragment plus enough surroundings to find it in the draft."""

    left = max(0, start - width)
    right = min(len(text), end + width)
    return ("…" if left else "") + text[left:right].replace("\n", " ") + ("…" if right < len(text) else "")


def _failure_summary(failures: Sequence[Dict[str, Any]]) -> str:
    """One sentence naming every kind of failure and the first concrete example."""

    counts: Dict[str, List[Dict[str, Any]]] = {}
    for item in failures:
        counts.setdefault(item["reason"], []).append(item)
    parts = [f"{_REASON_LABEL.get(reason, reason)} {len(items)} 项" for reason, items in counts.items()]
    first = failures[0]
    example = first.get("fragment") or first.get("path") or first.get("run_id")
    text = "未通过核验：" + "、".join(parts)
    if example:
        text += f"（首个问题：{example}）"
    return text


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
    text = "".join(str(rendered).split()).replace(",", "").replace("％", "%")
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


def _brief(value: Any, limit: int = 120) -> str:
    """A non-numeric artifact value, short enough to sit in an error record."""

    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _norm_number(text: Any) -> str:
    """Normalise a written number: no whitespace, no thousands separators, one percent sign."""

    return _compact(text).replace(",", "").replace("％", "%").replace("\u00a0", "")


def _scan_matches(report_markdown: str) -> List[re.Match]:
    """Numeric fragments that need a citation, with offsets so failures can be located."""

    text = _ISO_DATE.sub(" ", str(report_markdown or ""))
    spans = _masked_spans(text)
    return [match for match in _NUMBER.finditer(text) if not _overlaps(match.start(), match.end(), spans)]


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
    """Commit a report only when every cited figure matches a run summary.

    Every failure names what failed: which run, which citation path, which value
    was expected, which fragment of the body has no citation. ``details.reason``
    carries the first one so a host can branch without parsing prose, while
    ``details.failures`` lists them all.
    """

    provided = [str(item) for item in (run_ids or []) if str(item).strip()]
    body = str(report_markdown or "")
    cleaned = _ISO_DATE.sub(" ", body)
    matches = _scan_matches(body)
    citation_count = (
        0
        if isinstance(citations, (str, bytes)) or not isinstance(citations, Sequence)
        else len(citations)
    )
    # Same shape on every branch: a host must be able to read ``checked``
    # without guessing which keys exist for which failure. ``citations`` counts
    # what was passed in; ``runs_resolved`` counts what the ledger could load.
    checked: Dict[str, int] = {
        "runs": len(provided),
        "runs_resolved": 0,
        "citations": citation_count,
        "numbers": len(matches),
    }

    if not provided:
        return _blocked(
            "没有可核验的运行：run_ids 为空，正文数字没有追溯对象",
            failures=[_failure("no_runs_referenced")],
            checked=checked,
        )

    pairs: List[Tuple[str, Dict[str, Any]]] = []
    snapshot_id = None
    run_tools: List[Any] = []
    missing: List[str] = []
    for run_id in provided:
        try:
            run = ledger.get_run(run_id)
        except (ValueError, KeyError, TypeError):
            run = None
        if not run:
            missing.append(run_id)
            continue
        if snapshot_id is None and run.get("snapshot_id"):
            snapshot_id = run["snapshot_id"]
        stored = run.get("summary")
        pairs.append((run_id, stored if isinstance(stored, dict) else {}))
        run_tools.append((run_id, run.get("tool")))
    checked["runs_resolved"] = len(pairs)
    if missing:
        return _blocked(
            "引用的运行不存在：" + "、".join(missing),
            failures=[
                _failure(
                    "run_not_found",
                    run_id=missing[0],
                    missing_run_ids=missing,
                    known_run_ids=[rid for rid in provided if rid not in missing],
                )
            ],
            checked=checked,
        )
    if not body.strip():
        return _blocked(
            "正文为空：没有任何内容可以核验",
            failures=[_failure("empty_report")],
            checked=checked,
        )

    if isinstance(citations, (str, bytes)) or not isinstance(citations, Sequence):
        return _blocked(
            "引用格式无效：citations 必须是对象列表",
            failures=[_failure("citation_malformed", citation_index=0)],
            checked=checked,
        )
    checked["citations"] = citation_count

    failures: List[Dict[str, Any]] = []
    available = _available_paths([summary for _, summary in pairs])
    for index, cite in enumerate(citations):
        if not isinstance(cite, dict) or "path" not in cite or "rendered" not in cite:
            failures.append(_failure("citation_malformed", citation_index=index))
            continue
        path = str(cite["path"])
        rendered = str(cite["rendered"])
        only = cite.get("run_id")
        scoped = [(rid, summary) for rid, summary in pairs if only is None or rid == str(only)]
        if not scoped:
            failures.append(
                _failure("run_not_found", run_id=_brief(only) or "<empty>", citation_index=index)
            )
            continue
        values: List[Any] = []
        texts: List[str] = []
        raw: List[Any] = []
        seen = False
        for _, summary in scoped:
            found = _dig(summary, path)
            if found is _MISSING:
                continue
            seen = True
            if _is_number(found):
                values.append(found)
            elif isinstance(found, str):
                texts.append(found)
            else:
                raw.append(found)
        if not seen:
            failures.append(
                _failure("path_not_found", path=path, citation_index=index, available_paths=available)
            )
            continue
        if not (
            any(_matches(value, rendered) for value in values)
            or any(_compact(value) == _compact(rendered) for value in texts)
        ):
            failures.append(
                _failure(
                    "value_mismatch",
                    path=path,
                    rendered=rendered,
                    citation_index=index,
                    artifact_values=values[:3] or None,
                    artifact_text=texts[:3] or None,
                    artifact_value=_brief(raw[0]) if raw else None,
                    expected=_expected_renderings(values, texts, rendered),
                )
            )

    cited = {_norm_number(cite.get("rendered", "")) for cite in citations if isinstance(cite, dict)}
    for match in matches:
        fragment = match.group(1)
        if _norm_number(fragment) in cited:
            continue
        failures.append(
            _failure(
                "uncited_number",
                fragment=fragment,
                context=_context(cleaned, match.start(), match.end()),
            )
        )
        if len(failures) >= _MAX_FAILURES:
            break
    if failures:
        return _blocked(_failure_summary(failures), failures=failures, checked=checked)

    signoff_status = _signoff_status(ledger, run_tools)
    claims_approval = any(word in body for word in _APPROVAL_WORDS)
    pending_gates = [gate for gate, state in signoff_status.items() if state != "signed"]
    if claims_approval and (pending_gates or not signoff_status):
        return _blocked(
            "报告包含上线/批准措辞但缺少人工签收",
            code="SIGNOFF_REQUIRED",
            failures=[
                _failure(
                    "signoff_pending",
                    gates=pending_gates,
                    signed_gates=[gate for gate, state in signoff_status.items() if state == "signed"],
                )
            ],
            checked=checked,
        )

    states = set(signoff_status.values())
    if not signoff_status:
        business_signoff = "not_required"
    elif "pending" in states:
        business_signoff = "pending"
    else:
        business_signoff = "signed"
    summary = {
        # Three separate states, never folded into one "passed": the numbers can
        # be reproducible while the business definition is still unsigned.
        "computation_reproducible": True,
        "citations_verified": True,
        "business_signoff": business_signoff,
        "citation_count": len(citations),
        "number_count": len(matches),
        # A report whose every figure is a label (a date, a threshold) carries no
        # verification work; say so instead of looking like a fully checked one.
        "verification_scope": "figures" if matches else "no_figures",
        "signoff_status": signoff_status,
        "signoff_required": True,
        "checked": checked,
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
