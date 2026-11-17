"""Trusted stdio MCP facade for credit analysis tools.

The host chooses three mutually disjoint roots for data, registries, and the
ledger. No model-supplied path can escape those roots, and no tool call leaks
an uncaught exception onto the MCP channel: every failure is returned as a
structured envelope with a block_code from ``credit_agent.tools.codes``.
Human decisions are intentionally host-only.
"""

from __future__ import annotations

import argparse
import functools
import json
from itertools import combinations
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from credit_agent.tools import (
    Ledger,
    check_label_maturity as check_maturity,
    compare_population_distribution as compare_distribution,
    decompose_and_locate_anomaly as locate_anomaly,
    describe_population as describe_distribution,
    envelope,
    inspect_and_bind_snapshot as bind_snapshot,
    prepare_modeling_sample as prepare_sample,
    profile_binning as profile_binning_fn,
    resolve_metric_and_dimensions as bind_metric,
    run_portfolio_monitoring as monitor_portfolio,
    screen_features as screen_model_features,
    simulate_policy_rules as simulate_policy,
    train_and_evaluate_candidate as train_candidate,
    verify_and_commit_report as commit_report,
)

MAX_DATA_BYTES = 100 * 1024 * 1024
MAX_REGISTRY_BYTES = 1024 * 1024
MAX_REPORT_CHARS = 200_000
MAX_CITATIONS = 500
MAX_REGISTRY_NODES = 10_000


def _file(root: Path, name: str, suffixes: set[str],
          max_bytes: int = MAX_DATA_BYTES) -> Path:
    if not name or Path(name).is_absolute():
        raise ValueError("invalid_resource_name")
    path = (root / name).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file() or path.suffix.lower() not in suffixes:
        raise ValueError("resource_unavailable")
    if path.is_symlink() or path.stat().st_size > max_bytes:
        raise ValueError("resource_unavailable")
    return path


def _readable_input(path: str, *, data_root: Path, ledger_root: Path) -> Path:
    """Resolve a caller-supplied input file for ``import_sample_file``.

    This is a personal analysis tool, so a file anywhere on disk is fair game —
    that is the entire point of the upload path. Two boundaries remain: the
    ledger is never an input, and symlinks are refused (a symlink can be
    repointed between validation and read).
    """
    if not path:
        raise ValueError("file_path_must_not_be_empty")
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = data_root / candidate
    candidate = candidate.resolve()
    if candidate.is_relative_to(ledger_root.resolve()):
        raise ValueError("resource_unavailable")
    if not candidate.is_file():
        raise FileNotFoundError(f"文件不存在或不是普通文件：{candidate}")
    if candidate.is_symlink():
        raise ValueError("resource_unavailable")
    if candidate.stat().st_size > MAX_DATA_BYTES:
        raise ValueError("too_large")
    return candidate


def _writable_output(out_path: str | None, *, data_root: Path,
                     ledger_root: Path, registry_root: Path) -> str | None:
    """Validate a caller-supplied output path for the acquisition tools.

    ``None`` keeps the default (``<data-root>/<name>.parquet``). A directory, a
    relative name, or an absolute filename are all accepted; the only hard rule
    is that a snapshot may never be written on top of the ledger or the
    registries, because those two roots are append-only evidence.
    """
    if out_path is None or not str(out_path).strip():
        return None
    candidate = Path(out_path).expanduser()
    if not candidate.is_absolute():
        candidate = data_root / candidate
    resolved = candidate.resolve() if candidate.exists() else candidate.absolute()
    for protected, label in ((ledger_root, "ledger"), (registry_root, "registry")):
        if resolved.is_relative_to(protected.resolve()):
            raise ValueError(
                f"resource_unavailable: 输出路径不能落在 {label} 目录内（{protected}），"
                "那两个根是只追加的证据目录"
            )
    return str(resolved)



def _bounded_yaml(content: str, yaml_module: Any) -> Any:
    """Reject alias expansion bombs and oversized registry documents."""
    if "&" in content or "*" in content:
        raise ValueError("registry_aliases_forbidden")
    data = yaml_module.safe_load(content)

    def nodes(value: Any, depth: int = 0) -> int:
        if depth > 50:
            raise ValueError("registry_too_deep")
        if isinstance(value, dict):
            return 1 + sum(nodes(item, depth + 1) for item in value.values())
        if isinstance(value, list):
            return 1 + sum(nodes(item, depth + 1) for item in value)
        return 1

    if nodes(data) > MAX_REGISTRY_NODES:
        raise ValueError("registry_too_complex")
    return data


def _exception_code(exc: BaseException) -> str:
    text = str(exc)
    # Acquisition failures carry their own meaning; match on the marker strings
    # the data layer raises so the host sees a specific code, not UNSUPPORTED_SCOPE.
    # 取数范围越界排在第一位：它是最该被单独看见、也最不该被当成"其它错误"的一类。
    if "拒绝执行" in text:
        return "SQL_SCOPE_NOT_AUTHORIZED"
    if "ROW_CAP_EXCEEDED" in text:
        return "ROW_CAP_EXCEEDED"
    if "多条语句" in text or "MULTI_STATEMENT" in text:
        return "MULTI_STATEMENT_SQL"
    if "PERIOD_MISMATCH" in text:
        return "PERIOD_MISMATCH"
    if any(token in text for token in ("无法解码", "解析失败", "不是文件", "文件不存在", "文件没有数据行")):
        return "SAMPLE_FILE_UNREADABLE"
    if any(token in text for token in ("credentials", "凭证", "requires: pip install",
                                       "connection incomplete", "连接")):
        return "BACKEND_UNAVAILABLE"
    if isinstance(exc, KeyError):
        return "SNAPSHOT_INVALID"
    if isinstance(exc, (OSError, pd.errors.ParserError, pd.errors.EmptyDataError)):
        return "RESOURCE_UNAVAILABLE"
    if isinstance(exc, ValueError):
        text = str(exc)
        if "PERIOD_MISMATCH" in text:
            return "PERIOD_MISMATCH"
        if any(token in text for token in ("resource_", "registry_", "invalid_", "too_large", "too_many")):
            return "RESOURCE_UNAVAILABLE"
        return "UNSUPPORTED_SCOPE"
    return "UNSUPPORTED_SCOPE"


def _guarded(name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Convert every uncaught tool failure into a structured blocked envelope."""

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - boundary of the MCP channel
                message = str(exc)[:400] or type(exc).__name__
                return envelope(
                    tool=name,
                    status="blocked",
                    block_code=_exception_code(exc),
                    summary={"message": message, "exception_type": type(exc).__name__},
                )

        return wrapper

    return decorate


def _assert_disjoint_roots(data_root: Path, registry_root: Path, ledger_root: Path) -> None:
    roots = (("data-root", data_root), ("registry-root", registry_root), ("ledger-root", ledger_root))
    for (left_name, left), (right_name, right) in combinations(roots, 2):
        if left == right or left.is_relative_to(right) or right.is_relative_to(left):
            raise ValueError(f"{left_name} 与 {right_name} 必须是互不相交的独立目录")


def _require_mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict) or not value:
        raise ValueError(f"{field}_must_be_a_non_empty_object")
    return value


def create_server(*, data_root: Path, registry_root: Path, ledger_root: Path):
    try:
        from mcp.server.fastmcp import FastMCP
        import yaml
    except ImportError as exc:
        raise RuntimeError("Install credit-risk-agent-core[mcp] to run the MCP server") from exc

    data_root = Path(data_root).resolve(strict=True)
    registry_root = Path(registry_root).resolve(strict=True)
    ledger_root = Path(ledger_root).expanduser().resolve()
    if not data_root.is_dir() or not registry_root.is_dir():
        raise ValueError("data_root and registry_root must be directories")
    if ledger_root.exists() and not ledger_root.is_dir():
        raise ValueError("ledger_root must be a directory")
    if not ledger_root.parent.is_dir():
        raise ValueError("ledger_root parent must be an existing directory")
    _assert_disjoint_roots(data_root, registry_root, ledger_root)

    ledger = Ledger(ledger_root)
    server = FastMCP("credit-risk-agent")

    def _registry(registry_name: str) -> dict[str, Any]:
        """Load a host-maintained registry file from inside ``registry_root``."""

        path = _file(registry_root, registry_name, {".json", ".yaml", ".yml"}, MAX_REGISTRY_BYTES)
        content = path.read_text(encoding="utf-8")
        registry = json.loads(content) if path.suffix.lower() == ".json" else _bounded_yaml(content, yaml)
        if not isinstance(registry, dict):
            raise ValueError("registry_must_be_an_object")
        return registry

    @server.tool(name="download_sample")
    @_guarded("download_sample")
    def download_sample(sql: str, as_of_date: str | None = None,
                        dataset_name: str | None = None, out_path: str | None = None,
                        observation_end: str | None = None, row_cap: int | None = None,
                        grain_key: str | None = None, purpose: str = "",
                        preview_only: bool = False, fmt: str | None = None,
                        overwrite: bool = False, user_sql: str = "",
                        authorization: str = "") -> dict[str, Any]:
        """Run the analyst's own SQL read-only and snapshot it under a manifest.

        The analyst writes the SQL (joins, CASE mappings, window functions,
        whatever cleaning the real table needs). This tool owns everything
        around it: one statement only, no writes, a bounded row count, a
        recorded fingerprint, and an honest report of what it did not check.

        **Extraction outside the requester's SQL is refused, not annotated.**
        ``user_sql`` is mandatory and ``sql`` must match it; if it does not, the
        call fails before any data is read. Silent widening — extra columns, an
        added WHERE, another table — redefines the denominator, and every
        downstream number then means something else while still looking
        authoritative. Writing a note on top of wrong data does not make the
        data right, so the tool stops instead of proceeding.

        If more columns are genuinely needed, ask the requester first. Only after
        they agree, re-call with both the new ``sql`` and their reply in
        ``authorization``; the manifest then records ``deviation_authorized_by``.

        ``purpose`` is required for a full extract (not for previews): state why
        the data is being pulled and who authorized it.

        ``preview_only=true`` runs ``LIMIT 100`` and writes nothing — use it to
        confirm column names, types and the partition filter before pulling a
        full extract. Previews are gated on ``user_sql`` too: a preview still
        reads data.

        ``as_of_date`` (YYYY-MM-DD) is the date the extract was taken and is
        required unless previewing: label maturity needs it, and the data itself
        cannot supply it. ``out_path`` defaults to ``<data-root>/<name>.parquet``;
        pass a directory or a filename to redirect it.
        """
        from credit_agent.data.ingest import download_sample as run_download

        target = _writable_output(out_path, data_root=data_root,
                                  ledger_root=ledger_root, registry_root=registry_root)
        body = run_download(
            sql, name=dataset_name, out_path=target, as_of_date=as_of_date,
            observation_end=observation_end, row_cap=row_cap, grain_key=grain_key,
            purpose=purpose, preview_only=preview_only, fmt=fmt, overwrite=overwrite,
            user_sql=user_sql, authorization=authorization,
            # Default the snapshot into the host-authorized data root, so that
            # list_credit_datasets / inspect_and_bind_snapshot can see it.
            default_dir=str(data_root),
        )
        notes: list[str] = []
        checks = body.get("checks") or {}
        for key in ("partition_warning", "readonly_note", "grain_warning",
                    "empty_result_warning", "sql_deviation_warning"):
            if checks.get(key):
                notes.append(str(checks[key]))
        if preview_only:
            return envelope(tool="download_sample",
                            status="degraded" if notes else "ok",
                            summary=body, artifacts=[{"kind": "sql_preview"}])
        status = "degraded" if notes else "ok"
        return envelope(tool="download_sample", status=status, summary=body,
                        artifacts=[{"kind": "sample_manifest", "sample_id": body.get("sample_id"),
                                    "path": body.get("path")}])

    @server.tool(name="import_sample_file")
    @_guarded("import_sample_file")
    def import_sample_file(file_path: str, dataset_name: str | None = None,
                           out_path: str | None = None, encoding: str = "",
                           delimiter: str = "", sheet: str = "",
                           column_map: dict[str, str] | None = None,
                           grain_key: str | None = None, as_of_date: str | None = None,
                           purpose: str = "", overwrite: bool = False,
                           out_fmt: str | None = None) -> dict[str, Any]:
        """Import a local CSV/TSV/Excel/Parquet file as a sample.

        This is the non-warehouse door: a partner's Excel handover, an offline
        score file, a CSV from a system nobody can query. It produces the *same*
        manifest as ``download_sample``, so downstream tools never learn which
        door the data came in by.

        Encoding (GBK/GB18030/UTF-8), delimiter and sheet are probed
        automatically and reported back. ``column_map`` maps delivered headers
        onto standard roles (``key``/``label``/``cohort``/``time``/``feature``);
        cleaning is not done here — clean the file locally and import it again,
        so the chain keeps both the raw and the cleaned snapshot.
        """
        from credit_agent.data.ingest import import_sample_file as run_import

        source = _readable_input(file_path, data_root=data_root, ledger_root=ledger_root)
        target = _writable_output(out_path, data_root=data_root,
                                  ledger_root=ledger_root, registry_root=registry_root)
        body = run_import(
            str(source), name=dataset_name, out_path=target, encoding=encoding,
            delimiter=delimiter, sheet=sheet, column_map=column_map,
            grain_key=grain_key, as_of_date=as_of_date, purpose=purpose,
            overwrite=overwrite, out_fmt=out_fmt, default_dir=str(data_root),
        )
        report = body.get("quality_report") or {}
        notes = list(report.get("findings") or [])
        return envelope(
            tool="import_sample_file",
            status="degraded" if notes else "ok",
            summary=body,
            artifacts=[{"kind": "sample_manifest", "sample_id": body.get("sample_id"),
                        "path": body.get("path")}],
        )

    @server.tool(name="list_credit_datasets")
    @_guarded("list_credit_datasets")
    def list_credit_datasets() -> dict[str, Any]:
        """List CSV/Parquet names under the host-authorized data root."""
        names = sorted(str(path.relative_to(data_root)) for path in data_root.rglob("*")
                       if path.is_file() and path.suffix.lower() in {".csv", ".parquet"}
                       and path.resolve().is_relative_to(data_root))
        return envelope(tool="list_credit_datasets", status="ok",
                        summary={"datasets": names, "count": len(names)})

    @server.tool(name="inspect_and_bind_snapshot")
    @_guarded("inspect_and_bind_snapshot")
    def inspect_and_bind_snapshot(dataset: str, grain: str, key_columns: list[str],
                                  decision_time_column: str, observation_time_column: str | None = None,
                                  weight_column: str | None = None,
                                  column_roles: dict[str, str] | None = None,
                                  as_of_date: str | None = None) -> dict[str, Any]:
        """Bind one authorized CSV/Parquet file and return its quality summary.

        ``as_of_date`` is the date the extract was taken (YYYY-MM-DD). Ask the
        user for it: the performance window runs from the observation time to
        the extract date, so a file of old rows pulled today is still mature.
        Omit it only when the user cannot supply one; the cutoff then falls back
        to the largest date inside the file.
        """
        path = _file(data_root, dataset, {".csv", ".parquet"})
        frame = pd.read_csv(path) if path.suffix.lower() == ".csv" else pd.read_parquet(path)
        return bind_snapshot(frame, ledger=ledger, grain=grain, key_columns=key_columns,
                             decision_time_column=decision_time_column,
                             observation_time_column=observation_time_column,
                             weight_column=weight_column, column_roles=column_roles,
                             as_of_date=as_of_date)

    @server.tool(name="check_label_maturity")
    @_guarded("check_label_maturity")
    def check_label_maturity(snapshot_id: str, cohort_column: str, decision_time_column: str,
                             performance_window: dict[str, Any],
                             bad_flag_column: str | None = None,
                             observation_mode: str = "monthly_cohort",
                             maturity_choice_review_id: str | None = None) -> dict[str, Any]:
        """Check cohort maturity. modeling uses monthly MOB; strategy can use latest observed samples."""
        _require_mapping(performance_window, "performance_window")
        if observation_mode not in ("monthly_cohort", "latest_observed"):
            raise ValueError("observation_mode_must_be_monthly_cohort_or_latest_observed")
        return check_maturity(snapshot_id, ledger=ledger, cohort_column=cohort_column,
                              decision_time_column=decision_time_column,
                              performance_window=performance_window, bad_flag_column=bad_flag_column,
                              observation_mode=observation_mode,
                              maturity_choice_review_id=maturity_choice_review_id)

    @server.tool(name="resolve_metric_and_dimensions")
    @_guarded("resolve_metric_and_dimensions")
    def resolve_metric_and_dimensions(snapshot_id: str, metric_id: str,
                                      metric_version: str, registry_name: str) -> dict[str, Any]:
        """Bind a metric from a host-maintained JSON/YAML registry, not model-authored text."""
        return bind_metric(snapshot_id, ledger=ledger, metric_id=metric_id,
                           metric_version=metric_version, registry=_registry(registry_name))

    @server.tool(name="describe_population")
    @_guarded("describe_population")
    def describe_population(snapshot_id: str, spec: dict[str, Any], period: str, column: str,
                            dimensions: list[str] | None = None, bins: int = 10,
                            registry_name: str | None = None) -> dict[str, Any]:
        """Describe one column of one period without reading any target label.

        `spec` is the caliber, and it must be filled in *before* the numbers are
        looked at: population label, time column, denominator rule, and what a
        blank or a zero means for every field used. A used field whose blanks or
        zeros are unexplained returns `needs_human` instead of being silently
        dropped or recoded to zero.

        No label is read, so no maturity gate applies — and equally, no bad rate
        can come out of this tool. Columns whose snapshot role is an outcome are
        refused as the described column, as a dimension, as the denominator
        filter, and as the period column. A 0/1 column that nobody has declared
        as a `feature` returns `needs_human`, because its mean is a rate; the
        check reads the values, not the dtype, so text `"0"/"1"` and booleans
        are treated the same as `int64`. Values are normalised to tokens before
        any numeric coercion, and the rule depends on which profile the column
        will get: a coercible column is judged numerically (a 0..9 field is a
        distribution, not a flag), while a non-coercible one falls back to a
        categorical profile that prints every category's share — so there, zero
        and one appearing together is enough. The same goes for outcomes written
        as words (`good`/`bad`, `正常`/`逾期`). Literal missing codes (`N/A`, `-`,
        `未知`, blank) count as missing rather than as a category, so `n_missing`
        is honest and a column that is "50 ones plus 350 N/As" is seen as the
        one-valued column it is. The check runs on the whole table,
        on the period, and again on the denominator subset actually computed, and
        a dimension cell that is binary inside an otherwise mixed column is
        suppressed. Defaults to the full distribution:
        thresholds passed via `spec.observation_points` are reported as extra
        rows, never as the whole view.
        """
        registry = _registry(registry_name) if registry_name else None
        return describe_distribution(snapshot_id, ledger=ledger, spec=spec, period=period,
                                     column=column, dimensions=dimensions, bins=bins,
                                     registry=registry)

    @server.tool(name="compare_population_distribution")
    @_guarded("compare_population_distribution")
    def compare_population_distribution(base_run_id: str, comp_run_id: str,
                                        alpha: float = 0.05) -> dict[str, Any]:
        """Compare two described periods that share one caliber fingerprint.

        Inputs are `describe_population` run ids, not raw periods: describing
        first is what keeps a threshold slice from becoming the whole view.
        Every test comes back with what it tests, when it applies, and what it
        may conclude; `role` marks the pre-declared primary test.
        """
        return compare_distribution(base_run_id, comp_run_id, ledger=ledger, alpha=alpha)

    @server.tool(name="run_portfolio_monitoring")
    @_guarded("run_portfolio_monitoring")
    def run_portfolio_monitoring(snapshot_id: str, metric_id: str, metric_version: str,
                                 base_periods: list[str], comp_periods: list[str],
                                 dimensions: list[str] | None = None) -> dict[str, Any]:
        """Calculate risk trend, vintage, and migration for a signed metric and explicit periods."""
        if not base_periods or not comp_periods:
            raise ValueError("PERIOD_MISMATCH: base_periods 与 comp_periods 都是必填的非空期次列表")
        return monitor_portfolio(snapshot_id, ledger=ledger, metric_id=metric_id,
                                 metric_version=metric_version, base_periods=base_periods,
                                 comp_periods=comp_periods, dimensions=dimensions)

    @server.tool(name="decompose_and_locate_anomaly")
    @_guarded("decompose_and_locate_anomaly")
    def decompose_and_locate_anomaly(monitoring_run_id: str, min_n: int | None = None,
                                     min_events: float | None = None,
                                     contribution_floor: float | None = None,
                                     alpha: float | None = None,
                                     min_bad_increase: int | None = None) -> dict[str, Any]:
        """Separate mix and within-segment changes and locate supported slices."""
        return locate_anomaly(monitoring_run_id, ledger=ledger, min_n=min_n, min_events=min_events,
                              contribution_floor=contribution_floor, alpha=alpha,
                              min_bad_increase=min_bad_increase)

    @server.tool(name="prepare_modeling_sample")
    @_guarded("prepare_modeling_sample")
    def prepare_modeling_sample(snapshot_id: str, label: str, time_column: str | None = None,
                                positive_values: list[Any] | None = None,
                                gray_values: list[Any] | None = None,
                                gray_policy: str = "exclude", id_columns: list[str] | None = None,
                                exclude_columns: list[str] | None = None,
                                test_size: float = 0.3,
                                fallback_review_id: str | None = None,
                                random_state: int = 0) -> dict[str, Any]:
        """Prefer OOT; offer weaker validation for trusted human selection."""
        return prepare_sample(snapshot_id, ledger=ledger, label=label, time_column=time_column,
                              positive_values=positive_values if positive_values is not None else [1],
                              gray_values=gray_values or [], gray_policy=gray_policy,
                              id_columns=id_columns, exclude_columns=exclude_columns, test_size=test_size,
                              fallback_review_id=fallback_review_id, random_state=random_state)

    @server.tool(name="screen_features")
    @_guarded("screen_features")
    def screen_features(prepare_run_id: str, features: list[str], min_iv: float | None = None,
                        max_feature_psi: float | None = None,
                        max_features: int | None = None,
                        woe_bins: int | None = None,
                        binning_method: str | None = None,
                        monotonic_direction: str | None = None,
                        min_bin_fraction: float | None = None,
                        max_prebins: int | None = None) -> dict[str, Any]:
        """Screen candidate features and stop for trusted human review.

        分箱口径需与后续训练一致：评分卡建议 binning_method="chi2"（ChiMerge）、
        woe_bins=5~6、min_bin_fraction=0.05（分母为该特征有值样本数）。
        """
        if not features:
            raise ValueError("features_must_be_a_non_empty_list")
        return screen_model_features(prepare_run_id, ledger=ledger, features=features,
                                     min_iv=min_iv, max_feature_psi=max_feature_psi,
                                     max_features=max_features, woe_bins=woe_bins,
                                     binning_method=binning_method,
                                     monotonic_direction=monotonic_direction,
                                     min_bin_fraction=min_bin_fraction,
                                     max_prebins=max_prebins)

    @server.tool(name="profile_binning")
    @_guarded("profile_binning")
    def profile_binning(prepare_run_id: str, features: list[str], woe_bins: int = 6,
                        binning_method: str = "chi2",
                        monotonic_direction: str = "increasing",
                        min_bin_fraction: float = 0.05, max_prebins: int = 20,
                        include_merge_log: bool = True) -> dict[str, Any]:
        """ChiMerge 单特征分箱档：逐箱样本/坏率/WOE/IV、合并过程、KS、PSI。

        与筛选、训练共用同一套分箱内核，交付报告里的箱边界就是训练用的箱边界。
        """
        if not features:
            raise ValueError("features_must_be_a_non_empty_list")
        return profile_binning_fn(prepare_run_id, ledger=ledger, features=features,
                                  woe_bins=woe_bins, binning_method=binning_method,
                                  monotonic_direction=monotonic_direction,
                                  min_bin_fraction=min_bin_fraction, max_prebins=max_prebins,
                                  include_merge_log=include_merge_log)

    @server.tool(name="train_and_evaluate_candidate")
    @_guarded("train_and_evaluate_candidate")
    def train_and_evaluate_candidate(screen_run_id: str, woe_bins: int = 10,
                                     binning_method: str = "quantile",
                                     monotonic_direction: str = "increasing",
                                     min_bin_fraction: float = 0.05,
                                     max_prebins: int = 20) -> dict[str, Any]:
        """Train only when the trusted host stored a feature approval card.

        分箱口径需与人工审查时一致，否则训练结果无法复现审查结论：
        woe_bins 建议 5~6、binning_method 用 chi2 以对齐 ChiMerge 审查口径。
        """
        return train_candidate(screen_run_id, ledger=ledger, woe_bins=woe_bins,
                               binning_method=binning_method,
                               monotonic_direction=monotonic_direction,
                               min_bin_fraction=min_bin_fraction,
                               max_prebins=max_prebins)

    @server.tool(name="simulate_policy_rules")
    @_guarded("simulate_policy_rules")
    def simulate_policy_rules(snapshot_id: str, mode: str, bad: str,
                              time_column: str | None = None, features: list[str] | None = None,
                              score: str | None = None, depth: int = 1,
                              min_lift: float = 2.0, min_coverage: float = 0.01,
                              edges: list[float] | None = None,
                              current_threshold: float | None = None,
                              candidate_threshold: float | None = None,
                              higher_is_safer: bool = True,
                              request_loosen_benefit: bool = False,
                              fallback_review_id: str | None = None,
                              economics: dict[str, Any] | None = None,
                              observation_mode: str = "mature_only",
                              latest_review_id: str | None = None) -> dict[str, Any]:
        """Mine one-condition rules or simulate cutoffs on mature or latest observed samples."""
        if observation_mode not in ("mature_only", "latest_observed"):
            raise ValueError("observation_mode_must_be_mature_only_or_latest_observed")
        if economics is not None:
            _require_mapping(economics, "economics")
        return simulate_policy(snapshot_id, ledger=ledger, mode=mode, time_column=time_column,
                               bad=bad, features=features, score=score, depth=depth,
                               min_lift=min_lift, min_coverage=min_coverage, edges=edges,
                               current_threshold=current_threshold,
                               candidate_threshold=candidate_threshold,
                               higher_is_safer=higher_is_safer,
                               request_loosen_benefit=request_loosen_benefit,
                               fallback_review_id=fallback_review_id,
                               economics=economics, observation_mode=observation_mode,
                               latest_review_id=latest_review_id)

    @server.tool(name="verify_and_commit_report")
    @_guarded("verify_and_commit_report")
    def verify_and_commit_report(run_ids: list[str], report_markdown: str,
                                 citations: list[dict[str, Any]]) -> dict[str, Any]:
        """Persist a report only if its cited numbers match prior tool summaries."""
        if len(report_markdown or "") > MAX_REPORT_CHARS or len(citations or []) > MAX_CITATIONS:
            raise ValueError("report_too_large")
        return commit_report(ledger=ledger, run_ids=run_ids,
                             report_markdown=report_markdown, citations=citations)

    try:
        from credit_agent.tools.codes import MCP_TOOL_NAMES

        served = {tool.name for tool in server._tool_manager.list_tools()}
        if served != set(MCP_TOOL_NAMES):
            raise RuntimeError("registered MCP tools do not match the release contract")
    except ImportError:
        pass
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Credit tools MCP stdio server")
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--registry-root", type=Path, required=True)
    parser.add_argument("--ledger-root", type=Path, required=True)
    args = parser.parse_args()
    create_server(data_root=args.data_root, registry_root=args.registry_root,
                  ledger_root=args.ledger_root).run(transport="stdio")


if __name__ == "__main__":
    main()
