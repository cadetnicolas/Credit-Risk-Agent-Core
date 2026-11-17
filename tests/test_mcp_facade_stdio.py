"""End-to-end stdio coverage: all 11 tools must speak the envelope contract."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

from credit_agent.tools.codes import BLOCK_CODES, MCP_TOOL_NAMES, STATUSES

pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


def _payload(result):
    payload = result.structuredContent or json.loads(result.content[0].text)
    if "result" in payload and isinstance(payload["result"], dict):
        return payload["result"]
    return payload


def test_all_tools_are_callable_over_stdio(tmp_path):
    data = tmp_path / "data"
    registry = tmp_path / "registry"
    ledger = tmp_path / "ledger"
    data.mkdir()
    registry.mkdir()
    pd.DataFrame({
        "id": [f"a{i}" for i in range(40)],
        "decision_at": ["2024-01-01"] * 40,
        "cohort": ["2024-01"] * 40,
        "observed_at": ["2024-01-02"] * 40,
        "bad": [0, 1] * 20,
    }).to_csv(data / "sample.csv", index=False)
    (registry / "metric.json").write_text(json.dumps({
        "metrics": [{"metric_id": "bad_rate", "version": "v1", "flag_column": "bad", "signed_by": "risk"}],
        "dimensions": [{"column": "cohort", "point_in_time": "decision"}],
    }), encoding="utf-8")
    (registry / "aliases.yaml").write_text("metrics: &m []\ndimensions: *m\n", encoding="utf-8")

    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "credit_agent.mcp_server", "--data-root", str(data),
              "--registry-root", str(registry), "--ledger-root", str(ledger)],
        env=env,
    )

    async def exercise():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listing = await session.list_tools()
                assert {tool.name for tool in listing.tools} == set(MCP_TOOL_NAMES)

                calls = {
                    "list_credit_datasets": {},
                    "inspect_and_bind_snapshot": {
                        "dataset": "sample.csv", "grain": "loan", "key_columns": ["id"],
                        "decision_time_column": "decision_at", "observation_time_column": "observed_at",
                    },
                }
                results = {}
                for name, arguments in calls.items():
                    results[name] = _payload(await session.call_tool(name, arguments))
                assert results["list_credit_datasets"]["summary"]["count"] == 1
                snapshot = results["inspect_and_bind_snapshot"]["snapshot_id"]
                assert snapshot

                rest = {
                    "check_label_maturity": {
                        "snapshot_id": snapshot, "cohort_column": "cohort",
                        "decision_time_column": "decision_at",
                        "performance_window": {"kind": "mob", "mob": 3}, "bad_flag_column": "bad",
                    },
                    "resolve_metric_and_dimensions": {
                        "snapshot_id": snapshot, "metric_id": "bad_rate", "metric_version": "v1",
                        "registry_name": "metric.json",
                    },
                    "run_portfolio_monitoring": {
                        "snapshot_id": snapshot, "metric_id": "bad_rate", "metric_version": "v1",
                        "base_periods": ["2024-01"], "comp_periods": ["2024-01"],
                    },
                    "decompose_and_locate_anomaly": {"monitoring_run_id": "run_missing"},
                    "prepare_modeling_sample": {"snapshot_id": "snap_missing", "label": "bad"},
                    "screen_features": {"prepare_run_id": "run_missing", "features": ["bad"]},
                    "train_and_evaluate_candidate": {"screen_run_id": "run_missing"},
                    "simulate_policy_rules": {"snapshot_id": snapshot, "mode": "unknown", "bad": "bad"},
                    "describe_population": {
                        "snapshot_id": snapshot, "spec": {}, "period": "2024-01", "column": "bad",
                    },
                    "compare_population_distribution": {
                        "base_run_id": "run_missing", "comp_run_id": "run_missing",
                    },
                }
                for name, arguments in rest.items():
                    raw = await session.call_tool(name, arguments)
                    assert not raw.isError, (name, raw.content)
                    results[name] = _payload(raw)

                # 空 run_ids 不再被当作「无需核验」而放行；诊断要能跨协议传回来。
                report = await session.call_tool("verify_and_commit_report", {
                    "run_ids": ["run_missing"],
                    "report_markdown": "",
                    "citations": [],
                })
                assert not report.isError
                results["verify_and_commit_report"] = _payload(report)

                alias = await session.call_tool("resolve_metric_and_dimensions", {
                    "snapshot_id": snapshot, "metric_id": "bad_rate", "metric_version": "v1",
                    "registry_name": "aliases.yaml",
                })
                assert not alias.isError
                results["alias_registry"] = _payload(alias)

                for name, payload in results.items():
                    assert isinstance(payload, dict), name
                    assert payload.get("status") in STATUSES, (name, payload)
                    if payload.get("status") == "blocked":
                        assert payload.get("block_code") in BLOCK_CODES, (name, payload)
                assert results["simulate_policy_rules"]["status"] == "blocked"
                assert results["alias_registry"]["status"] == "blocked"
                assert results["alias_registry"]["block_code"] == "RESOURCE_UNAVAILABLE"

                # A TypeError from a mismatched facade->tools signature would be
                # swallowed as UNSUPPORTED_SCOPE. These assertions catch that.
                assert results["inspect_and_bind_snapshot"]["status"] in ("ok", "degraded")
                assert results["check_label_maturity"]["block_code"] == "LABEL_IMMATURE", results["check_label_maturity"]
                assert results["resolve_metric_and_dimensions"]["status"] != "blocked", results["resolve_metric_and_dimensions"]
                assert results["verify_and_commit_report"]["block_code"] == "REPORT_UNTRACED"
                assert results["verify_and_commit_report"]["details"]["reason"] == "run_not_found"
                business_codes = {
                    "SNAPSHOT_INVALID", "ADMISSION_REQUIRED", "LABEL_IMMATURE",
                    "FEATURE_REVIEW_REQUIRED", "VALIDATION_UNAVAILABLE", "BASELINE_UNAVAILABLE",
                    "REPORT_UNTRACED", "METRIC_UNSIGNED", "DIMENSION_UNREGISTERED",
                    "TIME_POINT_VIOLATION", "PERIOD_MISMATCH", "RESOURCE_UNAVAILABLE",
                }
                for name in ("run_portfolio_monitoring", "decompose_and_locate_anomaly",
                             "prepare_modeling_sample", "screen_features",
                             "train_and_evaluate_candidate"):
                    assert results[name]["block_code"] in business_codes, (name, results[name])

    asyncio.run(exercise())
