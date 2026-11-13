"""Drive a credit-risk MCP server over stdio and print a human-readable trace.

This is the "does it actually work" script. Two ways to point it at a server:

* ``--direct`` (default) boots ``python -m credit_agent.mcp_server`` from this
  checkout, so no client configuration is needed. Use this first.
* ``--config <file> [--server <name>]`` reuses an existing MCP client config
  (Claude Desktop, Cursor, Cline, or any other client) and boots exactly what
  that client would boot. Use this to debug a platform integration.

Either way it walks the analyst chain (list -> bind -> maturity -> metric ->
monitoring -> attribution) and prints each envelope's status and key numbers,
so a bad install is obvious at a glance.

Usage::

    python scripts/make_demo_workspace.py
    python scripts/demo_end_to_end.py --direct
    python scripts/demo_end_to_end.py --config ~/.cursor/mcp.json
    python scripts/demo_end_to_end.py --config ~/.cursor/mcp.json --server credit-risk-analysis
    python scripts/demo_end_to_end.py --direct --dataset other.csv

Requires demo data for the ``--direct`` path: run ``scripts/make_demo_workspace.py``
first.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

DEFAULT_WORKSPACE = Path.home() / "credit-agent-workspace"
DEFAULT_SERVER = "credit-risk-analysis"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _payload(result: Any) -> dict[str, Any]:
    payload = getattr(result, "structuredContent", None)
    if payload is None:
        payload = json.loads(result.content[0].text)
    if isinstance(payload, dict) and isinstance(payload.get("result"), dict):
        return payload["result"]
    return payload


def _show(label: str, env: dict[str, Any], extra: str = "") -> None:
    status = env.get("status", "?")
    code = env.get("block_code")
    parts = [f"[{status:>11}] {label}"]
    if code:
        parts.append(f"block_code={code}")
    if extra:
        parts.append(extra)
    print("  ".join(parts))
    for note in env.get("notes") or []:
        print(f"               note: {note}")


def _params_direct(python: str, workspace: Path):
    """Boot the server from this checkout, with the demo workspace as roots."""
    from mcp import StdioServerParameters

    roots = {
        "data": workspace / "datasets",
        "registry": workspace / "registries",
        "ledger": workspace / "ledger",
    }
    missing = [str(path) for path in roots.values() if not path.is_dir()]
    if missing:
        raise SystemExit(
            "FAIL: demo workspace incomplete, missing:\n  "
            + "\n  ".join(missing)
            + "\n       run: python scripts/make_demo_workspace.py"
        )

    env = dict(os.environ)
    src = str(PROJECT_ROOT / "src")
    # Keep an existing PYTHONPATH so a caller can layer their own paths.
    env["PYTHONPATH"] = src if not env.get("PYTHONPATH") else f"{src}{os.pathsep}{env['PYTHONPATH']}"
    return StdioServerParameters(
        command=python,
        args=[
            "-m", "credit_agent.mcp_server",
            "--data-root", str(roots["data"]),
            "--registry-root", str(roots["registry"]),
            "--ledger-root", str(roots["ledger"]),
        ],
        env=env,
        cwd=str(PROJECT_ROOT),
    )


def _params_from_config(config: Path, server_name: str):
    """Boot exactly what the target MCP client would boot."""
    from mcp import StdioServerParameters

    if not config.is_file():
        raise SystemExit(f"FAIL: {config} not found")
    try:
        document = json.loads(config.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(f"FAIL: {config} is not valid JSON: {exc}") from exc

    servers = document.get("mcpServers") or document.get("servers") or {}
    if server_name not in servers:
        available = ", ".join(sorted(servers)) or "(none)"
        raise SystemExit(
            f"FAIL: {server_name!r} not registered in {config}\n"
            f"       available servers: {available}\n"
            f"       pick one with --server <name>"
        )
    server = servers[server_name]

    command = server.get("command")
    if not command:
        raise SystemExit(f"FAIL: server {server_name!r} has no 'command'")
    # A missing interpreter is the single most common integration mistake, and
    # the raw SDK error for it is a bare FileNotFoundError traceback.
    resolved = command if Path(command).is_absolute() else None
    if resolved is not None and not Path(resolved).exists():
        raise SystemExit(f"FAIL: command does not exist: {resolved}")

    return StdioServerParameters(
        command=command,
        args=server.get("args") or [],
        env=server.get("env"),
        cwd=server.get("cwd"),
    )


async def run(params, dataset: str) -> int:
    from mcp import ClientSession
    from mcp.client.stdio import stdio_client

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            names = sorted(tool.name for tool in tools.tools)
            print(f"handshake ok, {len(names)} tools")
            if len(names) != 14:
                print(f"WARN: expected 14 tools, got {len(names)}: {names}", file=sys.stderr)

            listing = _payload(await session.call_tool("list_credit_datasets", {}))
            _show("list_credit_datasets", listing,
                  f"count={listing.get('summary', {}).get('count')}")
            if dataset not in (listing.get("summary", {}).get("datasets") or []):
                print(f"FAIL: {dataset} not visible under the data root", file=sys.stderr)
                print("       run: python scripts/make_demo_workspace.py", file=sys.stderr)
                return 3

            bound = _payload(await session.call_tool("inspect_and_bind_snapshot", {
                "dataset": dataset,
                "grain": "loan_month",
                "key_columns": ["loan_id", "period"],
                "decision_time_column": "decision_time",
                "observation_time_column": "obs_time",
            }))
            snapshot_id = bound.get("snapshot_id")
            _show("inspect_and_bind_snapshot", bound, f"snapshot={snapshot_id}")
            if bound.get("status") == "blocked":
                return 3

            maturity = _payload(await session.call_tool("check_label_maturity", {
                "snapshot_id": snapshot_id,
                "cohort_column": "cohort",
                "decision_time_column": "decision_time",
                "performance_window": {"kind": "mob", "mob": 3},
                "bad_flag_column": "bad",
                "observation_mode": "monthly_cohort",
            }))
            summary = maturity.get("summary", {})
            cohorts = summary.get("mature_cohorts") or []
            ready = sum(1 for item in cohorts if item.get("mature"))
            _show("check_label_maturity", maturity,
                  f"mature_cohorts={ready}/{len(cohorts)} rule={summary.get('maturity_rule')}")

            metric = _payload(await session.call_tool("resolve_metric_and_dimensions", {
                "snapshot_id": snapshot_id,
                "metric_id": "bad_rate",
                "metric_version": "v1",
                "registry_name": "registry_demo.json",
            }))
            dims = [item.get("column") for item in metric.get("summary", {}).get("dimensions") or []]
            _show("resolve_metric_and_dimensions", metric, f"dimensions={dims}")
            if metric.get("status") == "blocked":
                return 3

            periods = sorted({item["cohort"] for item in cohorts if item.get("mature")})
            if len(periods) < 2:
                print("FAIL: fewer than two mature periods; cannot compare", file=sys.stderr)
                return 3
            split = max(1, len(periods) // 2)
            monitoring = _payload(await session.call_tool("run_portfolio_monitoring", {
                "snapshot_id": snapshot_id,
                "metric_id": "bad_rate",
                "metric_version": "v1",
                "base_periods": periods[:split],
                "comp_periods": periods[split:],
                "dimensions": ["channel"],
            }))
            summary = monitoring.get("summary", {})
            base_rate = summary.get("base_rate")
            comp_rate = summary.get("comp_rate")
            detail = ""
            if isinstance(base_rate, float) and isinstance(comp_rate, float):
                detail = f"{base_rate:.4f} -> {comp_rate:.4f} (alarm={summary.get('alarm_level')})"
            _show("run_portfolio_monitoring", monitoring, detail)

            run_id = monitoring.get("run_id")
            if monitoring.get("status") == "blocked":
                return 3

            anomaly = _payload(await session.call_tool("decompose_and_locate_anomaly", {
                "monitoring_run_id": run_id,
            }))
            slices = anomaly.get("summary", {}).get("per_dimension") or []
            dominant = ", ".join(
                f"{item.get('dimension')}:{item.get('dominant')}" for item in slices
            )
            _show("decompose_and_locate_anomaly", anomaly,
                  f"run={anomaly.get('run_id')} [{dominant}]")
            for item in slices:
                closure = item.get("closure_error")
                print(f"               {item.get('dimension')}: mix={item.get('mix_effect')} "
                      f"rate={item.get('rate_effect')} closure={closure}")
                if isinstance(closure, float) and abs(closure) >= 1e-9:
                    print("               WARN: closure error above 1e-9, conclusions narrowed")

            print()
            print("allowed conclusions:", anomaly.get("allowed_conclusions"))
            print(f"next: simulate_policy_rules(snapshot_id={snapshot_id}, "
                  f"metric_id='bad_rate', metric_version='v1', observation_mode='mature_only')")
            return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--direct", action="store_true",
                      help="boot python -m credit_agent.mcp_server from this checkout (default)")
    mode.add_argument("--config", type=Path, default=None,
                      help="an MCP client config file; boots the server it declares")
    parser.add_argument("--server", default=DEFAULT_SERVER,
                        help=f"server name inside --config (default: {DEFAULT_SERVER})")
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE,
                        help="demo workspace for --direct (default: ~/credit-agent-workspace)")
    parser.add_argument("--python", default=sys.executable,
                        help="interpreter for --direct (default: the one running this script)")
    parser.add_argument("--dataset", default="demo_loan_panel.csv")
    options = parser.parse_args(argv)

    if options.config is not None:
        params = _params_from_config(options.config, options.server)
        print(f"mode: config {options.config} (server={options.server})")
    else:
        params = _params_direct(options.python, options.workspace)
        print(f"mode: direct {options.python} (workspace={options.workspace})")

    return asyncio.run(run(params, options.dataset))


if __name__ == "__main__":
    raise SystemExit(main())
