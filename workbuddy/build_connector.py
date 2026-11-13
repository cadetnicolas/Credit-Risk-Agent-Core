"""Build a WorkBuddy Connector bundle from the single source-of-truth project."""

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


SKILLS = (
    "credit-data-admission",
    "portfolio-anomaly-attribution",
    "policy-cutoff-simulation",
    "scorecard-modeling-pipeline",
    "deliverable-html-report",
)


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


def _validate_meta(meta: dict, expected_version: str) -> None:
    problems = []
    version = meta.get("version")
    if version != expected_version or not re.fullmatch(r"\d+\.\d+\.\d+", str(version)):
        problems.append(f"version {version!r} is invalid or != pyproject version {expected_version!r}")
    if not meta.get("minWorkbuddyVersion"):
        problems.append("missing minWorkbuddyVersion")
    desc_en = meta.get("description_en", "")
    if not (20 <= len(desc_en) <= 100):
        problems.append(f"description_en length {len(desc_en)} is outside 20-100")
    source = meta.get("source", "")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", source):
        problems.append(f"source {source!r} is not kebab-case")
    for key in ("examples_zh", "examples_en"):
        count = len(meta.get(key, []))
        if not (2 <= count <= 5):
            problems.append(f"{key} has {count} examples (expected 2-5)")
    if problems:
        raise ValueError("connector-meta.json self-check failed: " + "; ".join(problems))


def _assert_bundle(zip_path: Path, output_name: str) -> None:
    with ZipFile(zip_path) as bundle:
        names = set(bundle.namelist())
    root = f"{output_name}/"

    def has(rel: str) -> bool:
        return root + rel in names

    required = ["connector-meta.json", "mcp.json", "icon.svg", "README.md"]
    for name in SKILLS:
        required.append(f"skills/{name}/SKILL.md")
    missing = [rel for rel in required if not has(rel)]
    for name in SKILLS:
        prefix = f"{root}skills/{name}/references/"
        if not any(entry.startswith(prefix) for entry in names):
            missing.append(f"skills/{name}/references/*")
    if missing:
        raise ValueError(f"generated bundle is missing entries: {missing}")


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
    # Both targets are checked before any write, so a collision leaves no artifacts.
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")
    if archive.exists():
        raise FileExistsError(f"archive already exists: {archive}")

    _precheck_python_env(python, project)

    version = _pyproject_version(project)
    meta = json.loads((project / "workbuddy" / "connector-meta.json").read_text(encoding="utf-8"))
    template_version = meta.get("version")
    meta["version"] = version
    _validate_meta(meta, version)

    mcp_config = {
        "mcpServers": {
            "credit-risk-analysis": {
                "type": "stdio",
                "command": str(python),
                "args": [
                    "-m", "credit_agent.mcp_server",
                    "--data-root", str(data_root),
                    "--registry-root", str(registry_root),
                    "--ledger-root", str(ledger_root),
                ],
                "cwd": str(project),
                "env": {"PYTHONPATH": str(project / "src")},
                "timeout": 30000,
            }
        }
    }

    # Build into a temp directory next to the final target, then rename atomically.
    tmp_dir = Path(tempfile.mkdtemp(prefix=".connector-build-", dir=output.parent))
    tmp_zip = output.parent / (tmp_dir.name + ".zip")
    created_output = False
    created_archive = False
    try:
        (tmp_dir / "skills").mkdir()
        (tmp_dir / "connector-meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        shutil.copy2(project / "workbuddy" / "icon.svg", tmp_dir / "icon.svg")
        shutil.copy2(project / "workbuddy" / "README.md", tmp_dir / "README.md")
        (tmp_dir / "mcp.json").write_text(
            json.dumps(mcp_config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        for name in SKILLS:
            source = project / "skills" / name
            if not (source / "SKILL.md").is_file():
                raise ValueError(f"missing SKILL.md: {name}")
            shutil.copytree(source, tmp_dir / "skills" / name)

        with ZipFile(tmp_zip, "w", ZIP_DEFLATED) as bundle:
            for path in sorted(tmp_dir.rglob("*")):
                if path.is_file():
                    bundle.write(path, str(Path(output.name) / path.relative_to(tmp_dir)))

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
        f"connector-meta(template)={template_version}",
        f"connector-meta(generated)={meta['version']}",
        f"archive={archive.name}",
    ]))
    return archive


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a local WorkBuddy credit connector package")
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--registry-root", type=Path, required=True)
    parser.add_argument("--ledger-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(build(python=args.python, data_root=args.data_root, registry_root=args.registry_root,
                ledger_root=args.ledger_root, output=args.output))


if __name__ == "__main__":
    main()
