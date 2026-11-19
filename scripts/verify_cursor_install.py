"""Self-check a locally installed Cursor plugin for the credit SKILL + MCP server.

Checks four layers and exits non-zero on the first failing layer:

1. ``~/.cursor/plugins/local/credit-risk-analysis/.cursor-plugin/plugin.json``.
2. ``skills/``: the six declared skills and their reference docs.
3. ``mcp.json``: server entry, interpreter, three mutually disjoint roots.
4. A real stdio handshake: boot the configured command and expect 16 tools.

Usage::

    PYTHONPATH=src python scripts/verify_cursor_install.py
    PYTHONPATH=src python scripts/verify_cursor_install.py --skip-handshake
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

PLUGIN_NAME = "credit-risk-analysis"
SERVER_NAME = PLUGIN_NAME
# 16 tools = 2 data-access + 14 analysis. Keep this equal to
# credit_agent.tools.codes.MCP_TOOL_NAMES — the release-contract test asserts it.
EXPECTED_TOOL_COUNT = 16
REQUIRED_TOOLS = (
    "download_sample",
    "import_sample_file",
    "inspect_and_bind_snapshot",
    "check_label_maturity",
    "resolve_metric_and_dimensions",
    "describe_population",
    "compare_population_distribution",
    "run_portfolio_monitoring",
    "decompose_and_locate_anomaly",
    "simulate_policy_rules",
    "prepare_modeling_sample",
    "screen_features",
    "profile_binning",
    "train_and_evaluate_candidate",
    "verify_and_commit_report",
    "list_credit_datasets",
)
FORBIDDEN_TOOLS = ("record_human_decision",)
SKILLS = {
    "credit-data-admission": "references/data-contract-and-windows.md",
    "portfolio-anomaly-attribution": "references/attribution-rules-and-formulas.md",
    "policy-cutoff-simulation": "references/cutoff-and-swapset-methodology.md",
    "scorecard-modeling-pipeline": "references/scorecard-math-and-thresholds.md",
    "population-distribution-analysis": "references/population-spec-and-tests.md",
    "deliverable-html-report": "references/layout-and-components.md",
}


def _fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def check_manifest(plugin_root: Path) -> int:
    path = plugin_root / ".cursor-plugin" / "plugin.json"
    if not path.is_file():
        return _fail(f"缺少插件清单：{path}")
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return _fail(f"插件清单不是合法 JSON：{exc}")
    if meta.get("name") != PLUGIN_NAME:
        return _fail(f"插件 name 应为 {PLUGIN_NAME}，实际 {meta.get('name')!r}")
    logo = meta.get("logo")
    if not isinstance(logo, str) or not logo or logo.startswith(("/", "\\")) or ".." in Path(logo).parts:
        return _fail(f"logo 必须是插件内的相对路径，实际 {logo!r}")
    if not (plugin_root / logo).is_file():
        return _fail(f"logo 文件不存在：{plugin_root / logo}")
    print(f"OK: plugin manifest {path}")
    return 0


def check_skills(skills_root: Path) -> int:
    missing = []
    for name, reference in SKILLS.items():
        directory = skills_root / name
        if not (directory / "SKILL.md").is_file():
            missing.append(f"{name}/SKILL.md")
        if not (directory / reference).is_file():
            missing.append(f"{name}/{reference}")
    if missing:
        return _fail("缺少 skill 文件：" + ", ".join(missing))
    print(f"OK: {len(SKILLS)} skills installed under {skills_root}")
    return 0


def load_server(config: Path) -> tuple[dict, int]:
    if not config.is_file():
        return {}, _fail(f"配置文件不存在：{config}")
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return {}, _fail(f"配置文件不是合法 JSON：{exc}")
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or SERVER_NAME not in servers:
        return {}, _fail(f"{config} 的 mcpServers 中没有 {SERVER_NAME}")
    server = servers[SERVER_NAME]
    if not isinstance(server, dict):
        return {}, _fail(f"{SERVER_NAME} 配置不是对象")
    if "command" not in server or "args" not in server:
        return {}, _fail(f"{SERVER_NAME} 缺少 command 或 args")
    if not Path(server["command"]).exists():
        return {}, _fail(f"解释器不存在：{server['command']}")
    print(f"OK: {SERVER_NAME} registered in {config}")
    return server, 0


def check_roots(server: dict) -> int:
    args = list(server["args"])
    roots = {}
    for flag in ("--data-root", "--registry-root", "--ledger-root"):
        if flag not in args:
            return _fail(f"args 缺少 {flag}")
        index = args.index(flag) + 1
        if index >= len(args):
            return _fail(f"{flag} 没有取值")
        roots[flag] = Path(args[index]).expanduser().resolve()
    for flag, root in roots.items():
        if not root.is_dir():
            return _fail(f"{flag} 目录不存在：{root}")
    names = list(roots)
    for left in range(len(names)):
        for right in range(left + 1, len(names)):
            a, b = roots[names[left]], roots[names[right]]
            if a == b or a.is_relative_to(b) or b.is_relative_to(a):
                return _fail(f"{names[left]} 与 {names[right]} 必须互不相交：{a} / {b}")
    print("OK: 三个根目录存在且两两不相交")
    return 0


async def check_handshake(server: dict) -> int:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError:
        print("SKIP: 未安装 mcp 客户端，跳过握手检查（pip install 'mcp>=1.30,<2'）")
        return 0

    params = StdioServerParameters(
        command=server["command"],
        args=list(server["args"]),
        env={**os.environ, **server.get("env", {})},
        cwd=server.get("cwd"),
    )
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listing = await session.list_tools()
    except Exception as exc:  # noqa: BLE001 - surface the real startup error
        return _fail(f"stdio 握手失败：{type(exc).__name__}: {exc}")

    names = sorted(tool.name for tool in listing.tools)
    missing = [name for name in REQUIRED_TOOLS if name not in names]
    leaked = [name for name in FORBIDDEN_TOOLS if name in names]
    if missing:
        return _fail(f"缺少工具：{missing}")
    if leaked:
        return _fail(f"不应暴露给 Agent 的工具出现了：{leaked}")
    if len(names) != EXPECTED_TOOL_COUNT:
        return _fail(f"工具数应为 {EXPECTED_TOOL_COUNT}，实际 {len(names)}：{names}")
    print(f"OK: stdio 握手成功，发现 {len(names)} 个工具")
    for name in names:
        print(f"    - {name}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--plugin-root",
        type=Path,
        default=Path.home() / ".cursor" / "plugins" / "local" / PLUGIN_NAME,
        help="已安装的 Cursor 插件目录",
    )
    parser.add_argument("--skip-handshake", action="store_true", help="只做静态检查")
    options = parser.parse_args()
    plugin_root = options.plugin_root.expanduser().resolve()

    print(
        "NOTE: Cursor 在启动时加载 ~/.cursor/plugins/local。"
        "插件刚写入或刚替换后，执行 Developer: Reload Window 再看 MCP 列表。"
    )
    for step in (
        lambda: check_manifest(plugin_root),
        lambda: check_skills(plugin_root / "skills"),
    ):
        code = step()
        if code:
            return code

    server, code = load_server(plugin_root / "mcp.json")
    if code:
        return code
    code = check_roots(server)
    if code:
        return code
    if options.skip_handshake:
        print("SKIP: 按要求跳过 stdio 握手")
        return 0
    return asyncio.run(check_handshake(server))


if __name__ == "__main__":
    sys.exit(main())
