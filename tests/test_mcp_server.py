"""Real stdio discovery and invocation of the credit MCP facade."""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from pathlib import Path

import pandas as pd
import pytest
import yaml

mcp = pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from credit_agent.mcp_server import _file, create_server


TOOLS = {
    "inspect_and_bind_snapshot", "check_label_maturity", "resolve_metric_and_dimensions",
    "run_portfolio_monitoring", "decompose_and_locate_anomaly", "prepare_modeling_sample",
    "screen_features", "train_and_evaluate_candidate", "simulate_policy_rules",
    "verify_and_commit_report",
}


def test_skill_tools_are_served_without_human_approval_tool(tmp_path):
    # The release contract requires three mutually disjoint roots.
    data_root = tmp_path / "data"
    registry_root = tmp_path / "registry"
    data_root.mkdir()
    registry_root.mkdir()
    # Overlapping roots are rejected by the release contract (negative case).
    with pytest.raises(ValueError, match="互不相交"):
        create_server(data_root=data_root, registry_root=data_root,
                      ledger_root=tmp_path / "ledger")
    server = create_server(data_root=data_root, registry_root=registry_root,
                           ledger_root=tmp_path / "ledger")
    served = {tool.name for tool in server._tool_manager.list_tools()}
    assert TOOLS <= served
    assert "record_human_decision" not in served
    skills = Path(__file__).parents[1] / "skills"
    for skill in skills.glob("*/SKILL.md"):
        body = skill.read_text(encoding="utf-8")
        declared = set(re.findall(r"^  - ([a-z_]+)$", body, flags=re.MULTILINE))
        assert declared <= served, skill
        for target in re.findall(r"\]\((references/[^)]+)\)", body):
            assert (skill.parent / target).is_file(), (skill, target)


def test_data_root_cannot_be_escaped(tmp_path):
    inside = tmp_path / "data"
    inside.mkdir()
    outside = tmp_path / "outside.csv"
    outside.write_text("x\n1\n")
    (inside / "link.csv").symlink_to(outside)
    with pytest.raises(ValueError):
        _file(inside, "../outside.csv", {".csv"})
    with pytest.raises(ValueError):
        _file(inside, "link.csv", {".csv"})


def test_real_stdio_discovery_and_gate(tmp_path):
    data = tmp_path / "data"
    registry = tmp_path / "registry"
    data.mkdir()
    registry.mkdir()
    pd.DataFrame({
        "id": [f"a{i}" for i in range(40)],
        "decision_at": ["2024-01-01"] * 40,
        "cohort": ["2024-01"] * 40,
        "observed_at": ["2024-01-02"] * 40,
        "bad": [0, 1] * 20,
    }).to_csv(data / "sample.csv", index=False)
    (registry / "metric.json").write_text(json.dumps({"metrics": [], "dimensions": []}))
    (registry / "metric.yaml").write_text(yaml.safe_dump({
        "metrics": [{"metric_id": "bad_rate", "version": "v1", "flag_column": "bad", "signed_by": "risk"}],
        "dimensions": [{"column": "cohort", "point_in_time": "decision"}],
    }))
    env = {**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")}
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "credit_agent.mcp_server", "--data-root", str(data),
              "--registry-root", str(registry), "--ledger-root", str(tmp_path / "ledger")],
        env=env,
    )

    async def exercise():
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listing = await session.list_tools()
                assert TOOLS <= {tool.name for tool in listing.tools}
                result = await session.call_tool("inspect_and_bind_snapshot", {
                    "dataset": "sample.csv", "grain": "loan",
                    "key_columns": ["id"], "decision_time_column": "decision_at",
                    "observation_time_column": "observed_at",
                })
                assert not result.isError
                payload = result.structuredContent or json.loads(result.content[0].text)
                if "result" in payload:
                    payload = payload["result"]
                assert payload["status"] == "ok"
                snapshot = payload["snapshot_id"]
                gate = await session.call_tool("check_label_maturity", {
                    "snapshot_id": snapshot, "cohort_column": "cohort",
                    "decision_time_column": "decision_at",
                    "performance_window": {"kind": "mob", "mob": 3},
                    "bad_flag_column": "bad",
                })
                assert not gate.isError
                blocked = gate.structuredContent or json.loads(gate.content[0].text)
                if "result" in blocked:
                    blocked = blocked["result"]
                assert blocked["block_code"] == "LABEL_IMMATURE"
                metric = await session.call_tool("resolve_metric_and_dimensions", {
                    "snapshot_id": snapshot, "metric_id": "bad_rate",
                    "metric_version": "v1", "registry_name": "metric.yaml",
                })
                assert not metric.isError
                bound = metric.structuredContent or json.loads(metric.content[0].text)
                if "result" in bound:
                    bound = bound["result"]
                assert bound["status"] == "ok"
                assert bound["summary"]["metric"]["signed_by"] == "risk"

    asyncio.run(exercise())
