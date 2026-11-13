"""The generated WorkBuddy Connector must actually start and serve 11 tools."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import shutil
import sys
import uuid
from pathlib import Path

import pytest

pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402

from credit_agent.tools.codes import MCP_TOOL_NAMES  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("build_connector", ROOT / "workbuddy" / "build_connector.py")
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
build = _module.build


def test_generated_connector_starts_stdio_server():
    # build() deliberately refuses an output inside the project; conftest maps
    # pytest's tmp_path into the project, so use a scratch dir one level up.
    scratch = ROOT.parent / (".credit-connector-test-" + uuid.uuid4().hex[:8])
    scratch.mkdir()
    try:
        data = scratch / "data"
        registry = scratch / "registry"
        data.mkdir()
        registry.mkdir()
        output = scratch / "credit-risk-connector"
        archive = build(
            python=Path(sys.executable), data_root=data, registry_root=registry,
            ledger_root=scratch / "ledger", output=output,
        )
        assert archive.is_file()

        config = json.loads((output / "mcp.json").read_text(encoding="utf-8"))
        server = next(iter(config["mcpServers"].values()))
        assert server["type"] == "stdio"
        assert server["env"]["PYTHONPATH"] == str(ROOT / "src")
        assert "--data-root" in server["args"] and "--ledger-root" in server["args"]

        params = StdioServerParameters(
            command=server["command"], args=server["args"], env=server["env"], cwd=server["cwd"],
        )

        async def exercise():
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listing = await session.list_tools()
                    assert {tool.name for tool in listing.tools} == set(MCP_TOOL_NAMES)

        asyncio.run(exercise())
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
