"""Build a Cursor plugin bundle from the single source-of-truth project.

The generated directory is a Cursor plugin: ``.cursor-plugin/plugin.json``,
``mcp.json``, ``skills/`` and ``assets/logo.svg``. Machine-specific paths are
written at build time. Cursor loads a copy placed in
``~/.cursor/plugins/local/<name>``; this script can do that copy with
``--install``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


PLUGIN_NAME = "credit-risk-analysis"
SKILLS = (
    "credit-data-admission",
    "portfolio-anomaly-attribution",
    "policy-cutoff-simulation",
    "scorecard-modeling-pipeline",
    "deliverable-html-report",
    "population-distribution-analysis",
)
_NAME_RE = re.compile(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?")


def _pyproject_version(project: Path) -> str:
    data = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["version"]


def _read_init_version(project: Path) -> str:
    text = (project / "src" / "credit_agent" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', text, re.MULTILINE)
    return match.group(1) if match else "unknown"


def _precheck_python_env(python: Path, project: Path) -> None:
    """Verify the chosen interpreter can import the runtime deps with src visible."""
    src = project / "src"
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(src) + (os.pathsep + existing if existing else "")
    result = subprocess.run(
        [str(python), "-c", "import credit_agent, mcp, yaml"],
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(
            f"python dependency precheck failed for {python}: "
            "'import credit_agent, mcp, yaml' did not succeed. "
            "Install the project (pip install -e '.[mcp]') or expose the src directory via PYTHONPATH. "
            f"stderr: {result.stderr.strip()}"
        )


def _path_is_relative_inside_plugin(value: str) -> bool:
    if not value or value.startswith(("/", "\\")) or ":" in value[:3]:
        return False
    parts = Path(value).parts
    return ".." not in parts and not Path(value).is_absolute()


def _validate_manifest(meta: dict, expected_version: str) -> None:
    problems = []
    name = meta.get("name")
    if not isinstance(name, str) or _NAME_RE.fullmatch(name) is None:
        problems.append(f"name {name!r} is not a Cursor plugin identifier")
    version = meta.get("version")
    if version != expected_version or not re.fullmatch(r"\d+\.\d+\.\d+", str(version)):
        problems.append(f"version {version!r} is invalid or != pyproject version {expected_version!r}")
    if not str(meta.get("description", "")).strip():
        problems.append("missing description")
    logo = meta.get("logo", "")
    if not isinstance(logo, str) or not _path_is_relative_inside_plugin(logo):
        problems.append(f"logo {logo!r} must be a relative path inside the plugin")
    for key in ("skills", "rules", "agents", "commands", "mcpServers"):
        if key in meta:
            problems.append(f"{key} must stay unset so Cursor discovers the default folders")
    if problems:
        raise ValueError("plugin.json self-check failed: " + "; ".join(problems))


def _assert_bundle(zip_path: Path, output_name: str) -> None:
    with ZipFile(zip_path) as bundle:
        names = set(bundle.namelist())
    root = f"{output_name}/"

    def has(rel: str) -> bool:
        return root + rel in names

    required = [
        ".cursor-plugin/plugin.json",
        "mcp.json",
        "assets/logo.svg",
        "README.md",
    ]
    for name in SKILLS:
        required.append(f"skills/{name}/SKILL.md")
    missing = [rel for rel in required if not has(rel)]
    for name in SKILLS:
        prefix = f"{root}skills/{name}/references/"
        if not any(entry.startswith(prefix) for entry in names):
            missing.append(f"skills/{name}/references/*")
    if missing:
        raise ValueError(f"generated bundle is missing entries: {missing}")


def _mcp_config(*, python: Path, project: Path, data_root: Path,
                registry_root: Path, ledger_root: Path) -> dict:
    # Cursor infers stdio from ``command``. ``timeout`` and ``type`` are WorkBuddy
    # connector fields; leaving them out keeps this file on the Cursor plugin schema.
    return {
        "mcpServers": {
            PLUGIN_NAME: {
                "command": str(python),
                "args": [
                    "-m", "credit_agent.mcp_server",
                    "--data-root", str(data_root),
                    "--registry-root", str(registry_root),
                    "--ledger-root", str(ledger_root),
                ],
                "cwd": str(project),
                "env": {"PYTHONPATH": str(project / "src")},
            }
        }
    }


def build(*, python: Path, data_root: Path, registry_root: Path,
          ledger_root: Path, output: Path) -> Path:
    project = Path(__file__).resolve().parents[1]
    # Do not resolve the interpreter: a venv launcher is a symlink, and resolving
    # it would drop the venv's site-packages (mcp) from the interpreter's path.
    python = Path(os.path.abspath(python.expanduser()))
    data_root = data_root.expanduser().resolve()
    registry_root = registry_root.expanduser().resolve()
    ledger_root = ledger_root.expanduser().resolve()
    output = output.expanduser().resolve()

    if not python.is_file():
        raise ValueError(f"python executable does not exist: {python}")
    if not (project / "src").is_dir():
        raise ValueError(f"project src directory does not exist: {project / 'src'}")
    for name, path in (("data-root", data_root), ("registry-root", registry_root)):
        if not path.is_dir():
            raise ValueError(f"{name} must be an existing directory")
    if output == project or output in project.parents or project in output.parents:
        raise ValueError("output must be outside the credit project")

    archive = output.parent / (output.name + ".zip")
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")
    if archive.exists():
        raise FileExistsError(f"archive already exists: {archive}")

    _precheck_python_env(python, project)

    version = _pyproject_version(project)
    meta = json.loads((project / "cursor" / "plugin.json").read_text(encoding="utf-8"))
    template_version = meta.get("version")
    meta["version"] = version
    _validate_manifest(meta, version)
    logo = project / "cursor" / meta["logo"]
    if not logo.is_file():
        raise ValueError(f"logo file does not exist: {logo}")

    tmp_dir = Path(tempfile.mkdtemp(prefix=".cursor-plugin-build-", dir=output.parent))
    tmp_zip = output.parent / (tmp_dir.name + ".zip")
    created_output = False
    created_archive = False
    try:
        (tmp_dir / ".cursor-plugin").mkdir()
        (tmp_dir / "skills").mkdir()
        (tmp_dir / "assets").mkdir()
        (tmp_dir / ".cursor-plugin" / "plugin.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        shutil.copy2(logo, tmp_dir / meta["logo"])
        shutil.copy2(project / "cursor" / "README.md", tmp_dir / "README.md")
        (tmp_dir / "mcp.json").write_text(
            json.dumps(
                _mcp_config(
                    python=python,
                    project=project,
                    data_root=data_root,
                    registry_root=registry_root,
                    ledger_root=ledger_root,
                ),
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )

        for name in SKILLS:
            source = project / "skills" / name
            if not (source / "SKILL.md").is_file():
                raise ValueError(f"missing SKILL.md: {name}")
            shutil.copytree(source, tmp_dir / "skills" / name)

        with ZipFile(tmp_zip, "w", ZIP_DEFLATED) as bundle:
            for path in sorted(tmp_dir.rglob("*")):
                if path.is_file():
                    rel = Path(output.name) / path.relative_to(tmp_dir)
                    bundle.write(path, rel.as_posix())

        _assert_bundle(tmp_zip, output.name)

        os.replace(tmp_dir, output)
        created_output = True
        os.replace(tmp_zip, archive)
        created_archive = True
    except BaseException:
        if created_output:
            shutil.rmtree(output, ignore_errors=True)
        if created_archive and archive.exists():
            archive.unlink()
        shutil.rmtree(tmp_dir, ignore_errors=True)
        if tmp_zip.exists():
            tmp_zip.unlink()
        raise

    print("version manifest: " + " ".join([
        f"pyproject={version}",
        f"__init__={_read_init_version(project)}",
        f"plugin.json(template)={template_version}",
        f"plugin.json(generated)={meta['version']}",
        f"archive={archive.name}",
    ]))
    return archive


def default_install_dir() -> Path:
    return Path.home() / ".cursor" / "plugins" / "local" / PLUGIN_NAME


def install_plugin(output: Path, dest: Path, *, replace: bool = False) -> Path:
    """Copy a built plugin into Cursor's local plugin directory.

    Cursor only follows a symlink whose target stays inside
    ``~/.cursor/plugins/local``, so this copies the directory instead of linking
    back to the build output.
    """

    output = output.expanduser().resolve()
    dest = dest.expanduser().resolve()
    if not (output / ".cursor-plugin" / "plugin.json").is_file():
        raise ValueError(f"output is not a built Cursor plugin: {output}")
    if not (output / "mcp.json").is_file():
        raise ValueError(f"built plugin is missing mcp.json: {output}")
    if dest == output or dest in output.parents or output in dest.parents:
        raise ValueError("install destination must be separate from the build output")
    if dest.exists():
        if not replace:
            raise FileExistsError(f"install destination already exists: {dest}")
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(output, dest)
    return dest


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a local Cursor credit-analysis plugin")
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--registry-root", type=Path, required=True)
    parser.add_argument("--ledger-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--install",
        action="store_true",
        help="also copy the built plugin into the Cursor local plugin directory",
    )
    parser.add_argument(
        "--install-dir",
        type=Path,
        default=None,
        help="override the local plugin directory (default: ~/.cursor/plugins/local/credit-risk-analysis)",
    )
    parser.add_argument(
        "--replace",
        action="store_true",
        help="replace an existing install directory; required when that directory already exists",
    )
    args = parser.parse_args()
    archive = build(
        python=args.python,
        data_root=args.data_root,
        registry_root=args.registry_root,
        ledger_root=args.ledger_root,
        output=args.output,
    )
    print(archive)
    if args.install:
        dest = args.install_dir or default_install_dir()
        print(install_plugin(args.output, dest, replace=args.replace))


if __name__ == "__main__":
    main()
