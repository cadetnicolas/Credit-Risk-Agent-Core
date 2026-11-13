"""Trusted-host approval-card adapter (NOT an MCP tool).

Run this only on the host / admin side, after a real human has confirmed the
decision in an approval control. It writes a human decision into the ledger so
that the modelling and report tools can consume it. It is deliberately kept
out of the MCP tool surface: an Agent must never be able to approve its own
work.

Convention (v0.3)
-----------------
* ``--review-id`` MUST equal the ``run_id`` / ``review_id`` returned by the
  tool whose output is being approved. For example:

    - feature_list            -> the ``run_id`` from ``screen_features``
    - model_signoff           -> the ``run_id`` from ``train_and_evaluate_candidate``
    - policy_trial            -> the ``run_id`` from ``simulate_policy_rules``
    - anomaly_disposition     -> the ``run_id`` from ``decompose_and_locate_anomaly``
    - validation_fallback     -> the ``review_id`` from the validation gate
    - maturity_choice         -> the ``review_id`` from the maturity gate

  The report signoff check looks up ``get_decision(gate, run_id)`` using exactly
  that id; a mismatched review-id will never satisfy the signoff gate.

* ``--actor`` MUST be a real human identity (a reviewer / risk owner id). The
  value "agent" is rejected.

* A decision is one-time: ``Ledger.consume_decision`` marks it consumed, and a
  replayed approval card is refused.

Usage
-----
    python scripts/host_approval.py \
        --ledger-root /path/to/ledger \
        --gate model_signoff \
        --review-id run_0123456789ab \
        --actor risk_owner_42 \
        [--method wait_for_maturity] [--features f1,f2]

On success the last stdout line is a JSON envelope with ``status: "ok"``.
Invalid arguments or rejected decisions exit with a non-zero status.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

# Allow running from a checkout without a pre-set PYTHONPATH.
_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))

from credit_agent.tools import Ledger, record_human_decision  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Write a trusted-host approval-card decision into the credit ledger."
    )
    parser.add_argument("--ledger-root", required=True, help="Ledger directory shared with the MCP server.")
    parser.add_argument("--gate", required=True,
                        help="Approval gate, e.g. feature_list / model_signoff / policy_trial.")
    parser.add_argument("--review-id", required=True,
                        help="Must equal the run_id / review_id of the tool output being approved.")
    parser.add_argument("--actor", required=True, help="Real human identity; 'agent' is rejected.")
    parser.add_argument("--method", default=None,
                        help="Optional fallback / maturity method (e.g. stop, wait_for_maturity).")
    parser.add_argument("--features", default=None,
                        help="Comma-separated selected_features for the feature_list gate.")
    return parser


def _payload(method: Optional[str], features: Optional[str]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    if method is not None:
        payload["method"] = method
    if features is not None:
        payload["selected_features"] = [name.strip() for name in features.split(",") if name.strip()]
    return payload


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        ledger = Ledger(Path(args.ledger_root))
        result = record_human_decision(
            ledger=ledger,
            gate=args.gate,
            review_id=args.review_id,
            actor=args.actor,
            channel="approval_card",
            payload=_payload(args.method, args.features),
        )
    except (ValueError, OSError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
