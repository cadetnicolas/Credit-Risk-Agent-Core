"""Shared return shape for the credit tool functions.

Data frames stay in the ledger. Callers only see this envelope.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def envelope(
    *,
    tool: str,
    status: str,
    summary: Optional[Dict[str, Any]] = None,
    block_code: Optional[str] = None,
    snapshot_id: Optional[str] = None,
    run_id: Optional[str] = None,
    metric_id: Optional[str] = None,
    metric_version: Optional[str] = None,
    artifacts: Optional[List[Dict[str, Any]]] = None,
    allowed_conclusions: Optional[List[str]] = None,
    human_gate: Optional[Dict[str, Any]] = None,
    details: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build the tool envelope. ``blocked`` carries no citable risk numbers.

    ``details`` is the machine-readable half of a failure: which run, which
    citation path, which value was expected. It survives ``blocked`` because a
    block that only says "引用取不到数字" leaves the caller with nothing to fix.
    Blocked ``summary`` still drops every other field — no unverified figure is
    presented as a result. Diagnostics may quote a value from a run the caller
    itself passed in; that is not new information, and without it a mismatch
    cannot be corrected.
    """

    if status not in ("ok", "degraded", "needs_human", "blocked"):
        raise ValueError("status 必须是 ok、degraded、needs_human 或 blocked")
    if status == "blocked" and not block_code:
        raise ValueError("blocked 必须带 block_code")
    body = summary or {}
    if status == "blocked":
        body = {"message": body.get("message", "")}
    return {
        "ok": status != "blocked",
        "tool": tool,
        "status": status,
        "block_code": block_code,
        "snapshot_id": snapshot_id,
        "run_id": run_id,
        "metric_id": metric_id,
        "metric_version": metric_version,
        "summary": body,
        "artifacts": list(artifacts or []),
        "allowed_conclusions": list(allowed_conclusions or []),
        "human_gate": human_gate,
        "details": dict(details or {}),
    }
