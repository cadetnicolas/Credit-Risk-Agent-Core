"""Single source of truth for the v0.3 tool contract.

Every ``status``, ``block_code``, human-approval ``gate`` and registered MCP
tool name is declared here. SKILL.md files, the MCP facade, the ledger and the
release-contract tests all align to these constants.
"""

from __future__ import annotations

STATUSES = ("ok", "degraded", "needs_human", "blocked")

BLOCK_CODES = frozenset({
    "SNAPSHOT_INVALID",
    "METRIC_UNSIGNED",
    "LABEL_IMMATURE",
    "ADMISSION_REQUIRED",
    "TIME_POINT_VIOLATION",
    "DIMENSION_UNREGISTERED",
    "UNSUPPORTED_SCOPE",
    "RANDOM_SPLIT_FORBIDDEN",
    "VALIDATION_UNAVAILABLE",
    "VALIDATION_CHOICE_REQUIRED",
    "USER_CHOSE_STOP",
    "BASELINE_UNAVAILABLE",
    "FEATURE_REVIEW_REQUIRED",
    "REPORT_UNTRACED",
    "RESOURCE_UNAVAILABLE",
    "PERIOD_MISMATCH",
    "SIGNOFF_REQUIRED",
    # --- acquisition (v0.4) ---
    # Deliberately operational rather than compliance-oriented: the warehouse and
    # the field-permission system own data egress. These four guard the analyst
    # against their own accidents — a paste containing two statements, a query
    # that would pull the whole table, a missing extract date, a dead connection.
    "ROW_CAP_EXCEEDED",
    "MULTI_STATEMENT_SQL",
    "SAMPLE_FILE_UNREADABLE",
    "BACKEND_UNAVAILABLE",
    # --- acquisition scope (v0.5) ---
    # The executed SQL is not the requester's SQL. This one blocks *before*
    # reading, so no snapshot exists to annotate: widening the columns or the
    # WHERE silently redefines the denominator, and a note on top of wrong data
    # does not make the data right.
    "SQL_SCOPE_NOT_AUTHORIZED",
    # --- description (v0.7) ---
    # Two ways a label-free analysis can look right and mean the wrong thing:
    # the stated population disagrees with the denominator it is computed on,
    # or the two periods were described under different calibers.
    "POPULATION_MISMATCH",
    "SPEC_MISMATCH",
})

GATES = frozenset({
    "feature_list",
    "validation_fallback",
    "maturity_choice",
    "anomaly_disposition",
    "policy_trial",
    "model_signoff",
})

MCP_TOOL_NAMES = (
    # --- acquisition ---
    "download_sample",
    "import_sample_file",
    # --- analysis ---
    "list_credit_datasets",
    "inspect_and_bind_snapshot",
    "check_label_maturity",
    "resolve_metric_and_dimensions",
    "describe_population",
    "compare_population_distribution",
    "run_portfolio_monitoring",
    "decompose_and_locate_anomaly",
    "prepare_modeling_sample",
    "screen_features",
    "profile_binning",
    "train_and_evaluate_candidate",
    "simulate_policy_rules",
    "verify_and_commit_report",
)


def is_valid_code(code: str) -> bool:
    return isinstance(code, str) and code in BLOCK_CODES


def assert_code(code: str) -> None:
    if not is_valid_code(code):
        raise ValueError(f"unknown block_code: {code}")


__all__ = [
    "STATUSES",
    "BLOCK_CODES",
    "GATES",
    "MCP_TOOL_NAMES",
    "is_valid_code",
    "assert_code",
]
