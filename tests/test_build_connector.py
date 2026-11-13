"""Unit tests for workbuddy/build_connector.py (no real MCP import required)."""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import tomllib
from pathlib import Path
from zipfile import ZipFile

import pytest


PROJECT = Path(__file__).resolve().parents[1]
WORKBUDDY = PROJECT / "workbuddy"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "credit_build_connector", WORKBUDDY / "build_connector.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod(monkeypatch):
    """Load the builder with the interpreter precheck stubbed out.

    The real dependency check is exercised separately (and skipped when mcp is
    unavailable), so every build() test stays independent of package installs.
    """
    module = _load_module()
    monkeypatch.setattr(module, "_precheck_python_env", lambda python, project: None)
    return module


@pytest.fixture
def out_base(tmp_path):
    """An output parent guaranteed to be OUTSIDE the project.

    build() refuses an output inside the project, but this suite's conftest
    redirects pytest's tmp_path into .pytest-tmp/ inside the project. Fall back
    to the real system temp in that case (and clean it up afterwards).
    """
    resolved = tmp_path.resolve()
    if PROJECT == resolved or PROJECT in resolved.parents:
        base = Path(tempfile.mkdtemp(prefix="cra-connector-", dir="/tmp")).resolve()
        yield base
        shutil.rmtree(base, ignore_errors=True)
    else:
        yield resolved


@pytest.fixture
def roots(out_base):
    data = out_base / "data"
    registry = out_base / "registry"
    ledger = out_base / "ledger"
    data.mkdir(parents=True, exist_ok=True)
    registry.mkdir(parents=True, exist_ok=True)
    ledger.mkdir(parents=True, exist_ok=True)
    return {
        "python": Path(sys.executable),
        "data_root": data,
        "registry_root": registry,
        "ledger_root": ledger,
    }


def _build(mod, roots, output):
    return mod.build(
        python=roots["python"],
        data_root=roots["data_root"],
        registry_root=roots["registry_root"],
        ledger_root=roots["ledger_root"],
        output=output,
    )


def _pyproject_version():
    return tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]


def test_dotted_output_name_produces_matching_zip(mod, roots, out_base):
    output = out_base / "credit.risk.connector"
    archive = _build(mod, roots, output)

    assert archive == out_base / "credit.risk.connector.zip"
    assert archive.name == "credit.risk.connector.zip"
    assert archive.is_file()
    assert output.is_dir()
    # No build intermediates are left behind.
    assert list(out_base.glob(".connector-build-*")) == []


def test_mcp_config_fields(mod, roots, out_base):
    output = out_base / "connector"
    _build(mod, roots, output)

    server = json.loads((output / "mcp.json").read_text(encoding="utf-8"))["mcpServers"]["credit-risk-analysis"]
    assert server["type"] == "stdio"
    assert server["command"] == str(Path(os.path.abspath(Path(sys.executable).expanduser())))
    assert server["cwd"] == str(PROJECT)
    assert server["env"] == {"PYTHONPATH": str(PROJECT / "src")}
    assert server["timeout"] == 30000

    args = server["args"]
    assert args[0:2] == ["-m", "credit_agent.mcp_server"]
    for flag, key in (("--data-root", "data_root"),
                      ("--registry-root", "registry_root"),
                      ("--ledger-root", "ledger_root")):
        assert flag in args
        assert args[args.index(flag) + 1] == str(roots[key].resolve())


def test_zip_entry_manifest(mod, roots, out_base):
    output = out_base / "connector"
    archive = _build(mod, roots, output)

    with ZipFile(archive) as bundle:
        names = set(bundle.namelist())
    root = "connector/"
    required = ["connector-meta.json", "mcp.json", "icon.svg", "README.md"]
    for skill in mod.SKILLS:
        required.append(f"skills/{skill}/SKILL.md")
    for rel in required:
        assert root + rel in names
    for skill in mod.SKILLS:
        prefix = f"{root}skills/{skill}/references/"
        assert any(name.startswith(prefix) for name in names)


def test_generated_meta_self_check_and_version(mod, roots, out_base):
    output = out_base / "connector"
    _build(mod, roots, output)

    meta = json.loads((output / "connector-meta.json").read_text(encoding="utf-8"))
    assert meta["version"] == _pyproject_version() == "0.6.1"
    assert meta["minWorkbuddyVersion"] == "4.24.0"
    assert 20 <= len(meta["description_en"]) <= 100
    assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", meta["source"])
    for key in ("examples_zh", "examples_en"):
        assert 2 <= len(meta[key]) <= 5


def test_version_is_injected_from_pyproject(mod, monkeypatch, roots, out_base):
    monkeypatch.setattr(mod, "_pyproject_version", lambda project: "9.9.9")
    output = out_base / "connector"
    _build(mod, roots, output)

    meta = json.loads((output / "connector-meta.json").read_text(encoding="utf-8"))
    assert meta["version"] == "9.9.9"


def test_existing_output_fails_without_artifacts(mod, roots, out_base):
    output = out_base / "connector"
    output.mkdir()
    with pytest.raises(FileExistsError):
        _build(mod, roots, output)

    assert not (out_base / "connector.zip").exists()
    assert list(output.iterdir()) == []
    assert list(out_base.glob(".connector-build-*")) == []


def test_existing_archive_fails_without_artifacts(mod, roots, out_base):
    output = out_base / "connector"
    archive = out_base / "connector.zip"
    archive.write_text("sentinel", encoding="utf-8")
    with pytest.raises(FileExistsError):
        _build(mod, roots, output)

    assert not output.exists()
    assert archive.read_text(encoding="utf-8") == "sentinel"
    assert list(out_base.glob(".connector-build-*")) == []


def test_precheck_reports_missing_dependency(monkeypatch):
    module = _load_module()

    class Failed:
        returncode = 1
        stderr = "ModuleNotFoundError: No module named 'mcp'"

    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: Failed())
    with pytest.raises(ValueError, match="python dependency precheck failed"):
        module._precheck_python_env(Path(sys.executable), PROJECT)


def test_precheck_accepts_real_interpreter():
    pytest.importorskip("mcp")
    pytest.importorskip("yaml")
    module = _load_module()
    module._precheck_python_env(Path(sys.executable), PROJECT)
