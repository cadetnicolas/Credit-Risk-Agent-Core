"""Release contract checks: names, codes, roots, envelopes and metadata."""

from __future__ import annotations

import inspect
import json
import re
import tomllib
from pathlib import Path

import pytest

from credit_agent import __version__
from credit_agent.mcp_server import _file, create_server
from credit_agent.tools import Ledger
from credit_agent.tools.codes import BLOCK_CODES, GATES, MCP_TOOL_NAMES, STATUSES

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"


@pytest.fixture
def roots(tmp_path):
    data = tmp_path / "authorized-data"
    registry = tmp_path / "signed-registries"
    ledger = tmp_path / "tool-ledger"
    data.mkdir()
    registry.mkdir()
    return data, registry, ledger


def _skill_frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---", text, flags=re.DOTALL)
    assert match, f"missing frontmatter: {path}"
    return {"raw": match.group(1), "body": text[match.end():]}


def test_status_and_code_enums_are_complete():
    assert set(STATUSES) == {"ok", "degraded", "needs_human", "blocked"}
    # 12 analysis tools + 2 acquisition tools (download_sample, import_sample_file)
    assert len(MCP_TOOL_NAMES) == 14
    assert {"download_sample", "import_sample_file"} <= set(MCP_TOOL_NAMES)
    assert "record_human_decision" not in MCP_TOOL_NAMES
    assert GATES == {
        "feature_list", "validation_fallback", "maturity_choice",
        "anomaly_disposition", "policy_trial", "model_signoff",
    }
    assert {
        "SNAPSHOT_INVALID", "METRIC_UNSIGNED", "LABEL_IMMATURE", "ADMISSION_REQUIRED",
        "TIME_POINT_VIOLATION", "DIMENSION_UNREGISTERED", "UNSUPPORTED_SCOPE",
        "RANDOM_SPLIT_FORBIDDEN", "VALIDATION_UNAVAILABLE", "VALIDATION_CHOICE_REQUIRED",
        "USER_CHOSE_STOP", "BASELINE_UNAVAILABLE", "FEATURE_REVIEW_REQUIRED",
        "REPORT_UNTRACED", "RESOURCE_UNAVAILABLE", "PERIOD_MISMATCH", "SIGNOFF_REQUIRED",
        # acquisition (v0.4)
        "ROW_CAP_EXCEEDED", "MULTI_STATEMENT_SQL", "SAMPLE_FILE_UNREADABLE",
        "BACKEND_UNAVAILABLE",
    } <= set(BLOCK_CODES)


def test_mcp_tools_match_single_source(roots):
    server = create_server(data_root=roots[0], registry_root=roots[1], ledger_root=roots[2])
    served = {tool.name for tool in server._tool_manager.list_tools()}
    assert served == set(MCP_TOOL_NAMES)
    assert "record_human_decision" not in served


def test_skill_declarations_match_runtime(roots):
    server = create_server(data_root=roots[0], registry_root=roots[1], ledger_root=roots[2])
    served = {tool.name for tool in server._tool_manager.list_tools()}
    skill_files = sorted(SKILLS.glob("*/SKILL.md"))
    assert len(skill_files) == 5
    for path in skill_files:
        fm = _skill_frontmatter(path)
        declared = set(re.findall(r"^  - ([a-z_]+)$", fm["raw"], flags=re.MULTILINE))
        assert declared and declared <= served, path
        assert "record_human_decision" not in declared
        assert 'version: "1.5.0"' in fm["raw"], path
        assert 'contract_version: "0.4"' in fm["raw"], path
        assert "gate-tools" not in fm["raw"], path
        assert "何时使用" in fm["body"] and "何时不使用" in fm["body"], path
        for target in re.findall(r"\]\((references/[^)]+)\)", fm["body"]):
            assert (path.parent / target).is_file(), (path, target)
        for code in re.findall(r"`([A-Z][A-Z_]{5,})`", fm["body"]):
            if code in {"JSON", "YAML", "PARQUET", "LIGHTGBM", "PDO", "WOE", "OOT", "GOOD"}:
                continue
            assert code in BLOCK_CODES, (path, code)


def test_roots_must_be_disjoint(roots, tmp_path):
    data, registry, ledger = roots
    with pytest.raises(ValueError, match="互不相交"):
        create_server(data_root=data, registry_root=data, ledger_root=ledger)
    with pytest.raises(ValueError, match="互不相交"):
        create_server(data_root=data, registry_root=registry, ledger_root=data / "ledger")
    with pytest.raises(ValueError, match="互不相交"):
        create_server(data_root=data, registry_root=registry, ledger_root=tmp_path)
    (tmp_path / "not-a-dir").write_text("x")
    with pytest.raises(ValueError):
        create_server(data_root=data, registry_root=registry, ledger_root=tmp_path / "not-a-dir")


def test_tool_failures_return_structured_envelope(roots):
    server = create_server(data_root=roots[0], registry_root=roots[1], ledger_root=roots[2])
    tool = server._tool_manager.get_tool("inspect_and_bind_snapshot").fn
    result = tool(dataset="missing.csv", grain="loan", key_columns=["id"],
                  decision_time_column="apply_date")
    assert isinstance(result, dict)
    assert result["status"] == "blocked" and result["ok"] is False
    assert result["block_code"] in BLOCK_CODES
    assert result["tool"] == "inspect_and_bind_snapshot"
    assert result["summary"]["message"]

    bad_mode = server._tool_manager.get_tool("simulate_policy_rules").fn
    result = bad_mode(snapshot_id="snap_x", mode="unknown", bad="bad")
    assert result["status"] == "blocked"
    assert result["block_code"] in BLOCK_CODES

    bad_run = server._tool_manager.get_tool("screen_features").fn
    result = bad_run(prepare_run_id="run_missing", features=["a"])
    assert result["status"] == "blocked"
    assert result["block_code"] in BLOCK_CODES


def test_list_datasets_uses_envelope_shape(roots):
    data, registry, ledger = roots
    (data / "sample.csv").write_text("id,bad\n1,0\n", encoding="utf-8")
    (data / "nested").mkdir()
    (data / "nested" / "other.parquet").write_bytes(b"not-a-real-parquet")
    server = create_server(data_root=data, registry_root=registry, ledger_root=ledger)
    result = server._tool_manager.get_tool("list_credit_datasets").fn()
    assert result["status"] == "ok"
    assert "sample.csv" in result["summary"]["datasets"]
    assert "nested/other.parquet" in result["summary"]["datasets"]
    assert result["summary"]["count"] == len(result["summary"]["datasets"])


def test_file_guard_rejects_escape_symlink_and_oversize(roots, tmp_path):
    data = roots[0]
    (data / "small.csv").write_text("id\n1\n", encoding="utf-8")
    (tmp_path / "outside.csv").write_text("id\n1\n", encoding="utf-8")
    (data / "link.csv").symlink_to(tmp_path / "outside.csv")
    with pytest.raises(ValueError):
        _file(data, "../outside.csv", {".csv"})
    with pytest.raises(ValueError):
        _file(data, "link.csv", {".csv"})
    with pytest.raises(ValueError):
        _file(data, str(tmp_path / "outside.csv"), {".csv"})
    with pytest.raises(ValueError):
        _file(data, "small.csv", {".csv"}, max_bytes=1)
    assert _file(data, "small.csv", {".csv"}).name == "small.csv"


def test_new_contract_parameters_are_exposed(roots):
    server = create_server(data_root=roots[0], registry_root=roots[1], ledger_root=roots[2])
    def params(name: str) -> inspect.Parameters:
        fn = server._tool_manager.get_tool(name).fn
        return inspect.signature(fn).parameters

    assert {"observation_mode", "maturity_choice_review_id"} <= set(params("check_label_maturity"))
    assert {"economics", "observation_mode", "latest_review_id"} <= set(params("simulate_policy_rules"))
    assert {"base_periods", "comp_periods", "dimensions"} <= set(params("run_portfolio_monitoring"))
    assert params("run_portfolio_monitoring")["base_periods"].default is inspect.Parameter.empty
    assert params("run_portfolio_monitoring")["comp_periods"].default is inspect.Parameter.empty
    assert {"min_n", "min_events", "contribution_floor", "alpha", "min_bad_increase"} <= set(
        params("decompose_and_locate_anomaly"))
    assert {"min_iv", "max_feature_psi", "max_features", "woe_bins", "binning_method",
            "monotonic_direction", "min_bin_fraction", "max_prebins"} <= set(params("screen_features"))
    assert {"woe_bins", "binning_method", "min_bin_fraction", "max_prebins", "include_merge_log"} <= set(
        params("profile_binning"))
    assert "max_prebins" in params("train_and_evaluate_candidate")
    assert "random_state" in params("prepare_modeling_sample")


def test_ledger_has_latest_mask_and_one_time_decision_apis():
    assert hasattr(Ledger, "save_latest_mask") and hasattr(Ledger, "load_latest_mask")
    assert hasattr(Ledger, "consume_decision")


def test_business_red_lines_are_enforced_in_source():
    policy = (ROOT / "src/credit_agent/tools/policy.py").read_text(encoding="utf-8")
    rules = (ROOT / "src/credit_agent/analytics/rules.py").read_text(encoding="utf-8")
    assert "features or df.columns" not in rules, "rule mining must not scan the whole table"
    assert re.search(r"swap_in_bad_rate.{0,40}None", policy), "swap-in bad rate must be unknown"
    assert "min_hits" in rules


def test_connector_metadata_and_version_are_release_ready():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    version = config["project"]["version"]
    assert version == __version__ == "0.6.1"
    meta = json.loads((ROOT / "workbuddy/connector-meta.json").read_text(encoding="utf-8"))
    assert meta["version"] == version
    assert meta.get("minWorkbuddyVersion") == "4.24.0"
    assert 20 <= len(meta["description_en"]) <= 100
    assert re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", meta["source"])
    assert 2 <= len(meta["examples_zh"]) <= 5 and 2 <= len(meta["examples_en"]) <= 5
    assert "mcp" in config["project"]["optional-dependencies"]
    assert "PyYAML" in " ".join(config["project"]["dependencies"])
    assert any("pytest" in item for item in config["project"]["optional-dependencies"]["dev"])
    assert (ROOT / "LICENSE").is_file()
    assert "tools" in (ROOT / "MANIFEST.in").read_text(encoding="utf-8")
    assert (ROOT / "scripts/host_approval.py").is_file()
