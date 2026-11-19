"""Build a local Codex marketplace containing the project's credit analysis plugin."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
from itertools import combinations
from pathlib import Path


PLUGIN = "credit-risk-analysis"
MARKETPLACE = "credit-risk-local"
PROJECT = Path(__file__).resolve().parents[1]


def _write_json(path: Path, content: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build(*, python: Path, data_root: Path, registry_root: Path,
          ledger_root: Path, output: Path) -> Path:
    """Generate a machine-local marketplace, ready for `codex plugin marketplace add`."""
    # Resolving a venv interpreter symlink would bypass its site-packages.
    python = Path(os.path.abspath(python.expanduser()))
    if not python.is_file() or not os.access(python, os.X_OK):
        raise ValueError(f"Python interpreter is not executable: {python}")
    roots = {
        "data-root": data_root.expanduser().resolve(),
        "registry-root": registry_root.expanduser().resolve(),
        "ledger-root": ledger_root.expanduser().resolve(),
    }
    for name in ("data-root", "registry-root"):
        if not roots[name].is_dir():
            raise ValueError(f"{name} must be an existing directory: {roots[name]}")
    ledger = roots["ledger-root"]
    if (ledger.exists() and not ledger.is_dir()) or not ledger.parent.is_dir():
        raise ValueError(f"ledger-root must be a directory with an existing parent: {ledger}")
    for (left_name, left), (right_name, right) in combinations(roots.items(), 2):
        if left.is_relative_to(right) or right.is_relative_to(left):
            raise ValueError(f"{left_name} and {right_name} must be disjoint")

    output = output.expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"Output already exists: {output}")
    for name, root in {"project": PROJECT, **roots}.items():
        if output.is_relative_to(root) or root.is_relative_to(output):
            raise ValueError(f"Output must be separate from {name}: {root}")
    if not output.parent.is_dir():
        raise ValueError(f"Output parent must exist: {output.parent}")
    project = tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    skills = sorted(path.parent for path in (PROJECT / "skills").glob("*/SKILL.md"))
    if not skills:
        raise ValueError("No source skills found. Build from a repository checkout.")

    env = {"PYTHONPATH": str(PROJECT / "src")}
    probe = subprocess.run(
        [str(python), "-c", "import mcp.server.fastmcp, yaml, credit_agent.mcp_server"],
        cwd=PROJECT, env={**os.environ, **env}, capture_output=True, text=True,
    )
    if probe.returncode:
        raise ValueError(f"Install the project with [mcp] in {python}. Import check failed:\n{probe.stderr.strip()}")

    args = ["-m", "credit_agent.mcp_server"]
    for name, root in roots.items():
        args.extend([f"--{name}", str(root)])
    with tempfile.TemporaryDirectory(prefix=".codex-build-", dir=output.parent) as temporary:
        bundle = Path(temporary) / "marketplace"
        plugin = bundle / "plugins" / PLUGIN
        _write_json(bundle / ".agents/plugins/marketplace.json", {
            "name": MARKETPLACE,
            "interface": {"displayName": "Credit Risk Local"},
            "plugins": [{
                "name": PLUGIN,
                "source": {"source": "local", "path": f"./plugins/{PLUGIN}"},
                "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
            }],
        })
        _write_json(plugin / ".codex-plugin/plugin.json", {
            "name": PLUGIN,
            "version": project["version"],
            "description": project["description"],
            "skills": "./skills/",
            "mcpServers": "./.mcp.json",
            "interface": {"displayName": "信贷分析助手"},
        })
        _write_json(plugin / ".mcp.json", {"mcpServers": {PLUGIN: {
            "command": str(python), "args": args, "cwd": str(PROJECT),
            "env": env, "startup_timeout_sec": 30,
        }}})
        for skill in skills:
            shutil.copytree(skill, plugin / "skills" / skill.name)
        shutil.copy2(PROJECT / "LICENSE", plugin / "LICENSE")
        shutil.copy2(PROJECT / "codex/README.md", bundle / "README.md")
        if output.exists():
            raise FileExistsError(f"Output already exists: {output}")
        bundle.rename(output)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("python", "data-root", "registry-root", "ledger-root", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    try:
        print(build(**vars(parser.parse_args())))
    except (OSError, ValueError) as exc:
        parser.exit(1, f"{exc}\n")


if __name__ == "__main__":
    main()
