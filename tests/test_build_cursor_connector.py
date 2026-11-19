"""Unit tests for cursor/build_connector.py (no real MCP import required)."""

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
CURSOR = PROJECT / "cursor"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "credit_build_cursor_connector", CURSOR / "build_connector.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def mod(monkeypatch):
    module = _load_module()
    monkeypatch.setattr(module, "_precheck_python_env", lambda python, project: None)
    return module


@pytest.fixture
def out_base(tmp_path):
    resolved = tmp_path.resolve()
    if PROJECT == resolved or PROJECT in resolved.parents:
        base = Path(tempfile.mkdtemp(prefix="cra-cursor-", dir="/tmp")).resolve()
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
    output = out_base / "credit.risk.cursor"
    archive = _build(mod, roots, output)

    assert archive == out_base / "credit.risk.cursor.zip"
    assert archive.is_file()
    assert output.is_dir()
    assert list(out_base.glob(".cursor-plugin-build-*")) == []


def test_mcp_config_uses_cursor_fields(mod, roots, out_base):
    output = out_base / "plugin"
    _build(mod, roots, output)

    config = json.loads((output / "mcp.json").read_text(encoding="utf-8"))
    assert set(config) == {"mcpServers"}
    server = config["mcpServers"]["credit-risk-analysis"]
    assert "type" not in server
    assert "timeout" not in server
    assert server["command"] == str(Path(os.path.abspath(Path(sys.executable).expanduser())))
    assert server["cwd"] == str(PROJECT)
    assert server["env"] == {"PYTHONPATH": str(PROJECT / "src")}

    args = server["args"]
    assert args[0:2] == ["-m", "credit_agent.mcp_server"]
    for flag, key in (("--data-root", "data_root"),
                      ("--registry-root", "registry_root"),
                      ("--ledger-root", "ledger_root")):
        assert flag in args
        assert args[args.index(flag) + 1] == str(roots[key].resolve())


def test_zip_entry_manifest(mod, roots, out_base):
    output = out_base / "plugin"
    archive = _build(mod, roots, output)

    with ZipFile(archive) as bundle:
        names = set(bundle.namelist())
    root = "plugin/"
    required = [".cursor-plugin/plugin.json", "mcp.json", "assets/logo.svg", "README.md"]
    for skill in mod.SKILLS:
        required.append(f"skills/{skill}/SKILL.md")
    for rel in required:
        assert root + rel in names
    for skill in mod.SKILLS:
        prefix = f"{root}skills/{skill}/references/"
        assert any(name.startswith(prefix) for name in names)


def test_generated_manifest_version_and_relative_logo(mod, roots, out_base):
    output = out_base / "plugin"
    _build(mod, roots, output)

    meta = json.loads((output / ".cursor-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert meta["name"] == "credit-risk-analysis"
    assert meta["version"] == _pyproject_version() == "0.7.0"
    assert meta["logo"] == "assets/logo.svg"
    assert (output / meta["logo"]).is_file()
    assert "skills" not in meta
    assert "mcpServers" not in meta
    assert re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", meta["name"])


def test_version_is_injected_from_pyproject(mod, monkeypatch, roots, out_base):
    monkeypatch.setattr(mod, "_pyproject_version", lambda project: "9.9.9")
    output = out_base / "plugin"
    _build(mod, roots, output)

    meta = json.loads((output / ".cursor-plugin" / "plugin.json").read_text(encoding="utf-8"))
    assert meta["version"] == "9.9.9"


def test_existing_output_fails_without_artifacts(mod, roots, out_base):
    output = out_base / "plugin"
    output.mkdir()
    with pytest.raises(FileExistsError):
        _build(mod, roots, output)

    assert not (out_base / "plugin.zip").exists()
    assert list(output.iterdir()) == []
    assert list(out_base.glob(".cursor-plugin-build-*")) == []


def test_existing_archive_fails_without_artifacts(mod, roots, out_base):
    output = out_base / "plugin"
    archive = out_base / "plugin.zip"
    archive.write_text("sentinel", encoding="utf-8")
    with pytest.raises(FileExistsError):
        _build(mod, roots, output)

    assert not output.exists()
    assert archive.read_text(encoding="utf-8") == "sentinel"


def test_install_copies_plugin_and_refuses_to_replace(mod, roots, out_base):
    output = out_base / "plugin"
    _build(mod, roots, output)
    dest = out_base / "local" / "credit-risk-analysis"

    installed = mod.install_plugin(output, dest)
    assert (installed / ".cursor-plugin" / "plugin.json").is_file()
    assert (installed / "mcp.json").is_file()
    assert installed != output

    with pytest.raises(FileExistsError):
        mod.install_plugin(output, dest)
    replaced = mod.install_plugin(output, dest, replace=True)
    assert (replaced / "skills" / "credit-data-admission" / "SKILL.md").is_file()


def test_manifest_rejects_absolute_logo(mod):
    meta = json.loads((CURSOR / "plugin.json").read_text(encoding="utf-8"))
    meta["logo"] = "/tmp/logo.svg"
    with pytest.raises(ValueError, match="logo"):
        mod._validate_manifest(meta, meta["version"])


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
