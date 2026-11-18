"""Label-free description and comparison: caliber gate, full distribution, closure."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from credit_agent.tools import Ledger
from credit_agent.tools.descriptive import compare_population_distribution, describe_population
from credit_agent.tools.evidence import verify_and_commit_report


def _frame(n: int = 800) -> pd.DataFrame:
    """Deterministic two-period application panel with 10% blanks and some zeros."""

    rows = []
    for period, offset in (("2024-09", 0), ("2024-10", 2)):
        for i in range(n):
            value = float((i * 7) % 11 + offset)
            org = None if i % 10 == 0 else (0.0 if i % 17 == 0 else value)
            rows.append(
                {
                    "app_id": f"{period}-{i}",
                    "period": period,
                    "org_num": org,
                    "amount": float(1000 + (i * 13) % 9000),
                    "channel": "线上" if i % 3 else "线下",
                }
            )
    return pd.DataFrame(rows)


def _ledger(tmp_path, frame: pd.DataFrame) -> tuple[Ledger, str]:
    ledger = Ledger(tmp_path / "ledger")
    snapshot_id = "snap-apps"
    ledger.save_snapshot(
        snapshot_id,
        {"grain": "application", "columns": list(frame.columns), "decision_time_column": "period"},
    )
    ledger.save_frame(snapshot_id, frame)
    return ledger, snapshot_id


def _spec(**overrides) -> dict:
    spec = {
        "spec_id": "population_monthly",
        "spec_version": "v1",
        "population_label": "全部申请记录",
        "time_column": "period",
        "denominator_rule": "all_records",
        "field_semantics": {
            "org_num": {"missing": "no_record", "zero": "no_record"},
            "amount": {"missing": "unknown", "zero": "true_zero"},
        },
        "observation_points": [
            {"name": "≥10 家机构", "threshold": 10.0, "kind": "observation_point"},
            {"name": "策略阈值 ≥12", "threshold": 12.0, "kind": "applied_policy_threshold"},
        ],
    }
    spec.update(overrides)
    return spec


def _describe(ledger: Ledger, snapshot_id: str, period: str, **overrides):
    params = {
        "ledger": ledger,
        "spec": _spec(),
        "period": period,
        "column": "org_num",
        "dimensions": ["channel"],
    }
    params.update(overrides)
    return describe_population(snapshot_id, **params)


def test_full_distribution_comes_before_the_threshold(tmp_path):
    ledger, snapshot_id = _ledger(tmp_path, _frame())
    out = _describe(ledger, snapshot_id, "2024-10")
    assert out["status"] == "ok", out
    summary = out["summary"]
    # 全量分布先输出：分箱、分位数、均值都在，阈值只是附注。
    assert summary["column"]["bins"] and len(summary["column"]["bins"]) >= 3
    assert summary["column"]["quantiles"]["p50"] is not None
    assert summary["column"]["mean"] is not None
    points = {item["name"]: item for item in summary["observation_points"]}
    assert points["≥10 家机构"]["kind"] == "observation_point"
    assert points["策略阈值 ≥12"]["kind"] == "applied_policy_threshold"
    assert points["≥10 家机构"]["count"] > 0
    # 观察阈值不替代全量：分箱的总人数等于分母。
    assert sum(row["count"] for row in summary["column"]["bins"]) == summary["column"]["n_effective"]


def test_missing_and_zero_are_counted_separately(tmp_path):
    ledger, snapshot_id = _ledger(tmp_path, _frame())
    out = _describe(ledger, snapshot_id, "2024-10")
    profile = out["summary"]["column"]
    assert profile["n_missing"] == 80
    assert profile["n_zero"] > 0
    assert profile["n_missing"] + profile["n_zero"] == profile["n_null_or_zero"]
    assert profile["missing_share"] == pytest.approx(0.1)
    # 描述工具永远给不出坏账率：allowed 里没有，forbidden 里点名禁止。
    assert "bad_rate_change" in out["summary"]["forbidden_conclusions"]
    assert not any("bad" in item for item in out["allowed_conclusions"])


def test_undeclared_missing_semantics_stops_before_any_number(tmp_path):
    ledger, snapshot_id = _ledger(tmp_path, _frame())
    out = _describe(ledger, snapshot_id, "2024-10", spec=_spec(field_semantics={}))
    assert out["status"] == "needs_human"
    assert out["run_id"] is None, "没定口径就不该落 run"
    undecided = out["summary"]["undecided"]
    assert [item["column"] for item in undecided] == ["org_num"]
    assert undecided[0]["n_missing"] == 80 and undecided[0]["n_zero"] > 0
    assert out["human_gate"]["kind"] == "field_semantics"

    # 只声明缺失、不声明零值含义，仍然拦下：零值也会改分母。
    partial = _describe(
        ledger, snapshot_id, "2024-10",
        spec=_spec(field_semantics={"org_num": {"missing": "no_record"}}),
    )
    assert partial["status"] == "needs_human"


def test_population_label_must_match_the_denominator(tmp_path):
    ledger, snapshot_id = _ledger(tmp_path, _frame())
    out = _describe(ledger, snapshot_id, "2024-10", spec=_spec(population_label="已放款客群"))
    assert out["block_code"] == "POPULATION_MISMATCH"
    assert "分母规则" in out["summary"]["message"]

    disbursed_without_filter = _describe(
        ledger, snapshot_id, "2024-10",
        spec=_spec(population_label="已放款客群", denominator_rule="disbursed"),
    )
    assert disbursed_without_filter["block_code"] == "UNSUPPORTED_SCOPE"

    typo_filter = _describe(
        ledger, snapshot_id, "2024-10",
        spec=_spec(
            population_label="已放款客群",
            denominator_rule="disbursed",
            denominator_filter={"column": "channel", "values": ["放款"]},
        ),
    )
    assert typo_filter["block_code"] == "UNSUPPORTED_SCOPE", typo_filter
    assert "不存在" in typo_filter["summary"]["message"]


def test_grain_and_time_column_are_checked_against_the_snapshot(tmp_path):
    ledger, snapshot_id = _ledger(tmp_path, _frame())
    wrong_grain = _describe(ledger, snapshot_id, "2024-10", spec=_spec(grain="loan"))
    assert wrong_grain["block_code"] == "POPULATION_MISMATCH"

    ledger.save_snapshot(
        snapshot_id,
        {
            "grain": "application",
            "columns": list(_frame().columns),
            "decision_time_column": "period",
            "observation_time_column": "amount",
        },
    )
    violation = _describe(ledger, snapshot_id, "2024-10", spec=_spec(time_column="amount"))
    assert violation["block_code"] == "TIME_POINT_VIOLATION"


def test_time_column_must_be_the_registered_decision_time(tmp_path):
    """拿别的字段切期，两期分母就变成了「好客户数 / 坏客户数」。"""

    frame = _frame().assign(bad_flag=[i % 20 == 0 for i in range(1600)])
    ledger, snapshot_id = _ledger(tmp_path, frame)
    ledger.save_snapshot(
        snapshot_id,
        {
            "grain": "application",
            "columns": list(frame.columns),
            "decision_time_column": "period",
            "column_roles": {"bad_flag": "observation_outcome"},
            "observation_outcomes": ["bad_flag"],
        },
    )
    # 标签列切期：直接拒。
    as_period = _describe(ledger, snapshot_id, "2024-10", spec=_spec(time_column="bad_flag"))
    assert as_period["status"] == "blocked"
    assert as_period["block_code"] == "UNSUPPORTED_SCOPE"
    assert as_period["run_id"] is None

    # 非决策时点列切期：也拒，否则会造出快照里不存在的「期次」。
    wrong = _describe(ledger, snapshot_id, "2024-10", spec=_spec(time_column="app_id"))
    assert wrong["block_code"] == "TIME_POINT_VIOLATION"
    assert "决策时点列 period" in wrong["summary"]["message"]

    # 快照没登记决策时点列：拒绝而不是放行。
    ledger.save_snapshot(
        snapshot_id,
        {"grain": "application", "columns": list(frame.columns), "decision_time_column": ""},
    )
    undeclared = _describe(ledger, snapshot_id, "2024-10")
    assert undeclared["block_code"] == "SNAPSHOT_INVALID"


def test_binary_column_must_be_classified_before_it_is_described(tmp_path):
    """0/1 列的均值就是取值为 1 的占比；没声明它是什么，就不算。"""

    frame = _frame().assign(gb_flag=[i % 10 for i in range(1600)])
    frame["gb_flag"] = (frame["gb_flag"] == 0).astype(int)
    frame["是否坏账"] = [1 if i % 8 == 0 else 0 for i in range(1600)]
    ledger, snapshot_id = _ledger(tmp_path, frame)

    gate = _describe(ledger, snapshot_id, "2024-10", column="gb_flag")
    assert gate["status"] == "needs_human"
    assert gate["run_id"] is None
    assert gate["human_gate"]["kind"] == "binary_column_role"
    assert [item["column"] for item in gate["summary"]["undecided"]] == ["gb_flag"]

    # 中文列名里的表现结果词直接拒，不用等到人工确认。
    chinese = _describe(ledger, snapshot_id, "2024-10", column="是否坏账")
    assert chinese["status"] == "blocked"
    assert chinese["block_code"] == "UNSUPPORTED_SCOPE"
    assert "坏账" in chinese["summary"]["message"]

    # 作为维度也要先声明：否则就是「按结果分组再看分布」。
    as_dim = _describe(ledger, snapshot_id, "2024-10", dimensions=["gb_flag"])
    assert as_dim["status"] == "needs_human"

    # 声明为决策时点特征后放行，并给出 share_one 与读法提示。
    ledger.save_snapshot(
        snapshot_id,
        {
            "grain": "application",
            "columns": list(frame.columns),
            "decision_time_column": "period",
            "column_roles": {"gb_flag": "feature"},
        },
    )
    declared = dict(_spec()["field_semantics"])
    declared["gb_flag"] = {"missing": "unknown", "zero": "true_zero"}
    ok = _describe(ledger, snapshot_id, "2024-10", column="gb_flag", spec=_spec(field_semantics=declared))
    assert ok["status"] == "ok", ok["summary"]
    binary = ok["summary"]["column"]["binary"]
    assert binary["n_one"] + binary["n_zero"] == ok["summary"]["column"]["n_effective"]
    assert binary["share_one"] == pytest.approx(ok["summary"]["column"]["mean"])
    assert "不是风险水平" in ok["summary"]["column"]["reading"]


def test_categorical_column_cannot_declare_zero_equivalent(tmp_path):
    """分类列没有 0 这一档可并入，接受这个口径又不生效等于口径是文案。"""

    ledger, snapshot_id = _ledger(tmp_path, _frame())
    out = _describe(
        ledger, snapshot_id, "2024-10",
        column="channel",
        spec=_spec(field_semantics={"channel": {"missing": "zero_equivalent"}}),
    )
    assert out["status"] == "blocked"
    assert out["block_code"] == "UNSUPPORTED_SCOPE"
    assert "zero_equivalent" in out["summary"]["message"]


def test_discrete_field_keeps_one_bin_per_value(tmp_path):
    """等频分位会把 0~15 的整数字段压成两个箱，等于看不出分布。"""

    frame = pd.DataFrame(
        {
            "app_id": [f"a-{i}" for i in range(640)],
            "period": ["2024-10"] * 640,
            "org_num": [float(i % 16) for i in range(640)],
            "amount": [float(100 + i % 50) for i in range(640)],
            "channel": ["线上"] * 640,
        }
    )
    ledger, snapshot_id = _ledger(tmp_path, frame)
    out = _describe(ledger, snapshot_id, "2024-10")
    profile = out["summary"]["column"]
    assert profile["bin_mode"] == "discrete"
    kinds = [row["kind"] for row in profile["bins"]]
    # 16 个观测值给出 15 个箱，两端固定带 underflow / overflow 行。
    assert kinds == ["underflow"] + ["bin"] * 15 + ["overflow"]
    assert profile["bins"][0]["count"] == 0 and profile["bins"][-1]["count"] == 0
    assert sum(row["count"] for row in profile["bins"]) == profile["n_effective"]
    assert sum(row["share"] for row in profile["bins"]) == pytest.approx(1.0)


def test_out_of_range_values_are_reported_not_dropped(tmp_path):
    """np.histogram 会丢掉边界外的值；再用箱内人数做分母，占比就被放大了。"""

    frame = pd.DataFrame(
        {
            "app_id": [f"a-{i}" for i in range(400)],
            "period": ["2024-09"] * 200 + ["2024-10"] * 200,
            "org_num": [float(i % 10) for i in range(200)] + [float(i % 10 + 40) for i in range(200)],
            "amount": [float(100 + i % 50) for i in range(400)],
            "channel": ["线上"] * 400,
        }
    )
    ledger, snapshot_id = _ledger(tmp_path, frame)
    spec = _spec()
    base = _describe(ledger, snapshot_id, "2024-09", spec=spec)
    comp = _describe(ledger, snapshot_id, "2024-10", spec=spec)
    out = compare_population_distribution(base["run_id"], comp["run_id"], ledger=ledger)
    assert out["status"] == "ok", out
    overflow = [row for row in out["summary"]["bins"] if row["kind"] == "overflow"]
    assert overflow, "边界外的值必须有 overflow 行"
    # 对比期整列都在基期边界之外：占比 100% 落在 overflow，而不是变成空分布。
    assert overflow[0]["count_comp"] == 200
    assert overflow[0]["share_comp"] == pytest.approx(1.0)
    assert sum(row["share_base"] for row in out["summary"]["bins"]) == pytest.approx(1.0)


def test_label_columns_cannot_be_described_or_used_as_dimensions(tmp_path):
    """描述工具的唯一硬承诺：拿不到坏账率。0/1 标记列的 mean 就是坏账率。"""

    frame = _frame().assign(bad_flag=[i % 20 == 0 for i in range(1600)])
    frame["fpd30"] = [i % 25 == 0 for i in range(1600)]
    ledger, snapshot_id = _ledger(tmp_path, frame)
    ledger.save_snapshot(
        snapshot_id,
        {
            "grain": "application",
            "columns": list(frame.columns),
            "decision_time_column": "period",
            "column_roles": {"bad_flag": "observation_outcome"},
            "observation_outcomes": ["bad_flag"],
        },
    )

    as_column = _describe(ledger, snapshot_id, "2024-10", column="bad_flag")
    assert as_column["status"] == "blocked"
    assert as_column["block_code"] == "UNSUPPORTED_SCOPE"
    assert "observation_outcome" in as_column["summary"]["message"]
    assert as_column["run_id"] is None

    as_dimension = _describe(ledger, snapshot_id, "2024-10", dimensions=["bad_flag"])
    assert as_dimension["block_code"] == "UNSUPPORTED_SCOPE"

    # 角色没声明、名字明显是表现结果的，也要拦住。
    by_name = _describe(ledger, snapshot_id, "2024-10", column="fpd30")
    assert by_name["block_code"] == "UNSUPPORTED_SCOPE"
    assert "fpd" in by_name["summary"]["message"]

    as_filter = _describe(
        ledger, snapshot_id, "2024-10", column="amount", dimensions=[],
        spec=_spec(
            denominator_rule="disbursed",
            population_label="已放款客群",
            denominator_filter={"column": "bad_flag", "values": ["True"]},
        ),
    )
    assert as_filter["block_code"] == "UNSUPPORTED_SCOPE"


def test_all_records_refuses_a_denominator_filter(tmp_path):
    """过滤条件与「全部记录」矛盾时以前是静默忽略过滤、按全量算。"""

    ledger, snapshot_id = _ledger(tmp_path, _frame())
    out = _describe(
        ledger, snapshot_id, "2024-10",
        spec=_spec(denominator_filter={"column": "channel", "values": ["线上"]}),
    )
    assert out["status"] == "blocked"
    assert out["block_code"] == "UNSUPPORTED_SCOPE"
    assert "不接受 denominator_filter" in out["summary"]["message"]


def test_zero_equivalent_semantics_actually_changes_the_numbers(tmp_path):
    """口径是计算输入：声明「缺失等价于 0」之后，分母与中位数必须跟着变。"""

    ledger, snapshot_id = _ledger(tmp_path, _frame())
    as_unknown = _describe(
        ledger, snapshot_id, "2024-10",
        spec=_spec(field_semantics={"org_num": {"missing": "unknown", "zero": "no_record"}}),
    )
    as_zero = _describe(
        ledger, snapshot_id, "2024-10",
        spec=_spec(field_semantics={"org_num": {"missing": "zero_equivalent", "zero": "no_record"}}),
    )
    plain = as_unknown["summary"]["column"]
    folded = as_zero["summary"]["column"]
    assert plain["n_effective"] == 720 and folded["n_effective"] == 800
    assert folded["n_imputed_zero"] == 80
    assert plain["n_zero"] == folded["n_zero"] - 80
    assert folded["mean"] < plain["mean"]
    # 补入的缺失已属于零值档：null-or-zero 的记录数就等于零值数。
    assert folded["n_null_or_zero"] == folded["n_zero"]
    assert plain["n_null_or_zero"] == plain["n_missing"] + plain["n_zero"]
    # 含零与不含零的位置统计都摆出来，不替读者选。
    assert plain["without_zero"]["p50"] == pytest.approx(folded["without_zero"]["p50"])


def test_zero_only_field_does_not_demand_missing_semantics(tmp_path):
    """只有零值、没有缺失时，不该要求先声明缺失含义。"""

    frame = _frame()
    frame["org_num"] = frame["org_num"].fillna(0.0)
    ledger, snapshot_id = _ledger(tmp_path, frame)
    out = _describe(
        ledger, snapshot_id, "2024-10",
        spec=_spec(field_semantics={"org_num": {"zero": "true_zero"}}),
    )
    assert out["status"] == "ok", out["summary"]
    assert out["summary"]["column"]["n_imputed_zero"] == 0


def test_numeric_filter_value_matches_its_text_form(tmp_path):
    """数据里是 1、口径里写 "1.0"，不该被判成「过滤值不存在」。"""

    frame = _frame().assign(loan_flag=[1 if i % 4 else 0 for i in range(1600)])
    ledger, snapshot_id = _ledger(tmp_path, frame)
    spec = _spec(
        denominator_rule="disbursed",
        population_label="已放款客群",
        denominator_filter={"column": "loan_flag", "values": ["1.0"]},
    )
    # 过滤列也是 0/1：没声明角色时先拦下来，否则两点分母就是放款/未放款数。
    gated = _describe(ledger, snapshot_id, "2024-10", spec=spec)
    assert gated["status"] == "needs_human"
    assert [item["column"] for item in gated["summary"]["undecided"]] == ["loan_flag"]

    ledger.save_snapshot(
        snapshot_id,
        {
            "grain": "application",
            "columns": list(frame.columns),
            "decision_time_column": "period",
            "column_roles": {"loan_flag": "feature"},
        },
    )
    out = _describe(ledger, snapshot_id, "2024-10", spec=spec)
    assert out["status"] == "ok", out["summary"]
    assert out["summary"]["population"]["denominator"] == 600


def test_primary_test_typo_is_rejected_instead_of_going_all_exploratory(tmp_path):
    ledger, snapshot_id = _ledger(tmp_path, _frame())
    out = _describe(ledger, snapshot_id, "2024-10", spec=_spec(primary_test="mann_whitny"))
    assert out["block_code"] == "UNSUPPORTED_SCOPE"
    assert "primary_test" in out["summary"]["message"]


def test_compare_reuses_the_published_bin_edges(tmp_path):
    """描述用 5 个箱、比较用 10 个箱，引用就对不上报告里的任何一行。"""

    ledger, snapshot_id = _ledger(tmp_path, _frame())
    base = _describe(ledger, snapshot_id, "2024-09", bins=5)
    comp = _describe(ledger, snapshot_id, "2024-10", bins=5)
    published = base["summary"]["column"]["bin_edges"]
    out = compare_population_distribution(base["run_id"], comp["run_id"], ledger=ledger)
    assert out["status"] == "ok", out
    assert out["summary"]["base"]["bin_edges"] == published
    assert out["summary"]["comp"]["bin_edges"] == published
    assert len(out["summary"]["bins"]) == len(published) + 1


def test_categorical_compare_keeps_a_category_that_disappeared(tmp_path):
    """一个渠道在一期消失，本身就是构成变化最大的信号，不能被交集吃掉。"""

    rows = []
    for period, channels in (("2024-09", ["线上", "线下", "渠道A"]), ("2024-10", ["线上", "线下"])):
        for i in range(300):
            rows.append(
                {
                    "app_id": f"{period}-{i}",
                    "period": period,
                    "org_num": float(i % 11),
                    "amount": float(1000 + i),
                    "channel": channels[i % len(channels)],
                }
            )
    ledger, snapshot_id = _ledger(tmp_path, pd.DataFrame(rows))
    base = _describe(ledger, snapshot_id, "2024-09", column="channel", spec=_spec(field_semantics={}))
    comp = _describe(ledger, snapshot_id, "2024-10", column="channel", spec=_spec(field_semantics={}))
    assert base["status"] == comp["status"] == "ok", (base, comp)
    out = compare_population_distribution(base["run_id"], comp["run_id"], ledger=ledger)
    values = {row["value"]: row for row in out["summary"]["category_deltas"]}
    assert "渠道A" in values
    assert values["渠道A"]["count_comp"] == 0
    assert values["渠道A"]["share_delta_pp"] == pytest.approx(-33.3333, abs=1e-3)


def test_compare_gates_a_column_whose_declaration_was_withdrawn(tmp_path):
    """口径改过之后 compare 仍要过同一道闸门，且回同一个信封（needs_human，不是 blocked）。"""

    frame = _frame().assign(gb_flag=[1 if i % 20 == 0 else 0 for i in range(1600)])
    ledger, snapshot_id = _ledger(tmp_path, frame)
    declared = dict(_spec()["field_semantics"])
    declared["gb_flag"] = {"missing": "unknown", "zero": "true_zero"}
    ledger.save_snapshot(
        snapshot_id,
        {
            "grain": "application",
            "columns": list(frame.columns),
            "decision_time_column": "period",
            "column_roles": {"period": "decision_time", "gb_flag": "feature"},
            "explicit_roles": {"gb_flag": "feature"},
        },
    )
    spec = _spec(field_semantics=declared)
    base = _describe(ledger, snapshot_id, "2024-09", column="gb_flag", spec=spec)
    comp = _describe(ledger, snapshot_id, "2024-10", column="gb_flag", spec=spec)
    assert base["status"] == comp["status"] == "ok"

    # 事后撤回声明（例如发现这列其实是表现结果）：已落地的两个 run 不能再用来比占比。
    ledger.save_snapshot(
        snapshot_id,
        {
            "grain": "application",
            "columns": list(frame.columns),
            "decision_time_column": "period",
            "column_roles": {"period": "decision_time", "gb_flag": "unclassified"},
            "explicit_roles": {},
        },
    )
    out = compare_population_distribution(base["run_id"], comp["run_id"], ledger=ledger)
    assert out["status"] == "needs_human", out["summary"]
    assert out["human_gate"]["kind"] == "binary_column_role"
    assert out["human_gate"]["columns"] == ["gb_flag"]
    assert "坏账率" in out["summary"]["message"]
    assert "gb_flag" in out["summary"]["message"]
    # 信封里仍然不能出现计数。
    assert set(out["summary"]["undecided"][0]) == {"column", "role", "observed_values"}


def test_signoff_needs_both_periods(tmp_path):
    """基期签收、对比期没签收，结果不能整篇说「已签收口径」。"""

    ledger, snapshot_id = _ledger(tmp_path, _frame())
    plain_spec = _spec(observation_points=[])
    registry = {
        "populations": [
            {
                "spec_id": "population_monthly",
                "version": "v1",
                "population_label": "全部申请记录",
                "time_column": "period",
                "denominator_rule": "all_records",
                "field_semantics": plain_spec["field_semantics"],
            }
        ]
    }
    base = _describe(ledger, snapshot_id, "2024-09", spec=plain_spec, registry=registry)
    comp = _describe(ledger, snapshot_id, "2024-10", spec=plain_spec)
    assert base["summary"]["spec"]["signed"] is True
    assert comp["summary"]["spec"]["signed"] is False
    out = compare_population_distribution(base["run_id"], comp["run_id"], ledger=ledger)
    assert out["summary"]["spec"]["signed"] is False
    assert out["summary"]["signoff"] == {"base": True, "comp": False}
    assert any("签收状态不一致" in note for note in out["summary"]["limitations"])


def test_caliber_fingerprint_is_not_mistaken_for_a_figure(tmp_path):
    """指纹是标识符不是度量值：引用得上去，也不能把正文里的真数字漏掉。"""

    ledger, snapshot_id = _ledger(tmp_path, _frame())
    out = _describe(ledger, snapshot_id, "2024-10")
    summary = out["summary"]
    fingerprint = summary["spec"]["fingerprint"]
    assert fingerprint.startswith("fp_")
    denominator = summary["population"]["denominator"]
    report = f"按口径 {fingerprint} 统计，10 月共 {denominator} 笔申请；口径哈希 a1b2c3d4e5f60718。"
    committed = verify_and_commit_report(
        ledger=ledger,
        run_ids=[out["run_id"]],
        report_markdown=report,
        citations=[
            {"path": "spec.fingerprint", "rendered": fingerprint},
            {"path": "population.denominator", "rendered": str(denominator)},
        ],
    )
    assert committed["status"] == "ok", committed["details"]

    # 同一段正文里出现一个没引用的真数字，仍然要被点名。
    noisy = verify_and_commit_report(
        ledger=ledger,
        run_ids=[out["run_id"]],
        report_markdown=report + "另有 4321 笔待补。",
        citations=[
            {"path": "spec.fingerprint", "rendered": fingerprint},
            {"path": "population.denominator", "rendered": str(denominator)},
        ],
    )
    assert noisy["status"] == "blocked"
    assert noisy["details"]["reason"] == "uncited_number"
    assert noisy["details"]["failures"][0]["fragment"] == "4321"


def test_published_edges_are_imposed_on_both_periods(tmp_path):
    """只给对比期套边界、基期自己重算，两期分箱就是错位的两套区间。"""

    ledger, snapshot_id = _ledger(tmp_path, _frame())
    base = _describe(ledger, snapshot_id, "2024-09", column="amount", bins=5)
    comp = _describe(ledger, snapshot_id, "2024-10", column="amount", bins=5)
    published = base["summary"]["column"]["bin_edges"]
    assert base["summary"]["column"]["bin_mode"] == "quantile"
    assert len(published) == 6

    out = compare_population_distribution(base["run_id"], comp["run_id"], ledger=ledger)
    assert out["status"] == "ok", out
    assert out["summary"]["base"]["bin_edges"] == published
    assert out["summary"]["comp"]["bin_edges"] == published
    assert out["summary"]["binning_note"] is None
    rows = out["summary"]["bins"]
    assert len(rows) == len(published) + 1
    # 每行的两期计数落在同一区间：合计等于各自分母。
    assert sum(row["count_base"] for row in rows) == out["summary"]["denominator"]["base"]
    assert sum(row["count_comp"] for row in rows) == out["summary"]["denominator"]["comp"]
    for row in rows:
        if row["kind"] == "bin":
            assert row["lower"] == row["lower"] and row["upper"] is not None


def test_tests_follow_the_declared_missing_caliber(tmp_path):
    """描述层把缺失补成 0、检验层却用原始非缺失样本，一份报告就引了两个总体。"""

    from scipy import stats as scipy_stats

    rows = []
    for period, shift in (("2024-09", 0.0), ("2024-10", 5.0)):
        for i in range(200):
            rows.append(
                {
                    "app_id": f"{period}-{i}",
                    "period": period,
                    "org_num": None if i % 4 == 0 else float(i % 20) + shift,
                    "amount": float(1000 + i),
                    "channel": "线上" if i % 2 else "线下",
                }
            )
    frame = pd.DataFrame(rows)
    ledger, snapshot_id = _ledger(tmp_path, frame)
    spec = _spec(field_semantics={"org_num": {"missing": "zero_equivalent", "zero": "true_zero"}})
    base = _describe(ledger, snapshot_id, "2024-09", spec=spec)
    comp = _describe(ledger, snapshot_id, "2024-10", spec=spec)
    out = compare_population_distribution(base["run_id"], comp["run_id"], ledger=ledger)
    assert out["status"] == "ok", out
    # 描述层把缺失补成 0，样本量是全部记录。
    assert out["summary"]["base"]["n_effective"] == 200
    assert out["summary"]["base"]["n_imputed_zero"] == 50

    imputed = frame.assign(v=frame["org_num"].astype(float).fillna(0.0))
    expected = scipy_stats.ks_2samp(
        imputed.loc[imputed["period"] == "2024-09", "v"].to_numpy(),
        imputed.loc[imputed["period"] == "2024-10", "v"].to_numpy(),
    )
    ks = next(item for item in out["summary"]["tests"] if item["test"] == "ks_2samp")
    assert ks["statistic"] == pytest.approx(float(expected.statistic))
    assert ks["p_value"] == pytest.approx(float(expected.pvalue))

    # 与「按非缺失样本检验」的结果不同，才说明口径真的贯穿到检验层。
    raw = scipy_stats.ks_2samp(
        frame.loc[frame["period"] == "2024-09", "org_num"].dropna().to_numpy(dtype=float),
        frame.loc[frame["period"] == "2024-10", "org_num"].dropna().to_numpy(dtype=float),
    )
    assert float(raw.statistic) != pytest.approx(ks["statistic"])


def test_categorical_missing_counts_as_a_category(tmp_path):
    """分类列的占比分母要一致：描述用全部记录，比较也必须是全部记录。"""

    rows = []
    for period, values in (
        ("2024-09", ["线上", "线下", None]),
        ("2024-10", ["线上", "线上", "线下"]),
    ):
        for i in range(300):
            rows.append(
                {
                    "app_id": f"{period}-{i}",
                    "period": period,
                    "org_num": float(i % 11),
                    "amount": float(1000 + i),
                    "channel": values[i % len(values)],
                }
            )
    ledger, snapshot_id = _ledger(tmp_path, pd.DataFrame(rows))
    spec = _spec(field_semantics={"channel": {"missing": "unknown"}})
    base = _describe(ledger, snapshot_id, "2024-09", column="channel", spec=spec)
    comp = _describe(ledger, snapshot_id, "2024-10", column="channel", spec=spec)
    describe_shares = {row["value"]: row["share"] for row in base["summary"]["column"]["top_values"]}
    assert describe_shares["线上"] == pytest.approx(1 / 3)
    assert base["summary"]["column"]["missing_share"] == pytest.approx(1 / 3)

    out = compare_population_distribution(base["run_id"], comp["run_id"], ledger=ledger)
    assert out["status"] == "ok", out
    deltas = {row["value"]: row for row in out["summary"]["category_deltas"]}
    assert deltas["线上"]["share_base"] == pytest.approx(describe_shares["线上"])
    assert deltas["<缺失>"]["is_missing"] is True
    assert deltas["<缺失>"]["share_base"] == pytest.approx(1 / 3)
    assert deltas["<缺失>"]["share_comp"] == pytest.approx(0.0)
    assert sum(row["share_base"] for row in deltas.values()) == pytest.approx(1.0)


def test_all_missing_period_gives_a_distance_of_one_not_zero(tmp_path):
    """基期全缺失时 TVD 必须是 1（构成完全不同），不能是「没变化」的 0。"""

    rows = []
    for period, values in (
        ("2024-09", [None]),
        ("2024-10", ["线上", "线下"]),
    ):
        for i in range(200):
            rows.append(
                {
                    "app_id": f"{period}-{i}",
                    "period": period,
                    "org_num": float(i % 11),
                    "amount": float(1000 + i),
                    "channel": values[i % len(values)],
                }
            )
    ledger, snapshot_id = _ledger(tmp_path, pd.DataFrame(rows))
    spec = _spec(field_semantics={"channel": {"missing": "unknown"}})
    base = _describe(ledger, snapshot_id, "2024-09", column="channel", spec=spec)
    comp = _describe(ledger, snapshot_id, "2024-10", column="channel", spec=spec)
    out = compare_population_distribution(base["run_id"], comp["run_id"], ledger=ledger)
    assert out["status"] == "ok", out
    assert out["summary"]["total_variation_distance"] == pytest.approx(1.0)
    assert out["summary"]["missing_share_delta"] == pytest.approx(-1.0)


def test_categorical_primary_test_is_honoured(tmp_path):
    """声明了 chi2_categories 主检验，就必须真的有一项成为 primary。"""

    ledger, snapshot_id = _ledger(tmp_path, _frame())
    spec = _spec(primary_test="chi2_categories")
    base = _describe(ledger, snapshot_id, "2024-09", column="channel", spec=spec)
    comp = _describe(ledger, snapshot_id, "2024-10", column="channel", spec=spec)
    out = compare_population_distribution(base["run_id"], comp["run_id"], ledger=ledger)
    assert out["status"] == "ok", out
    roles = {item["test"]: item["role"] for item in out["summary"]["tests"]}
    assert roles.get("chi2_categories") == "primary", roles


def test_compare_needs_one_caliber_and_two_periods(tmp_path):
    ledger, snapshot_id = _ledger(tmp_path, _frame())
    base = _describe(ledger, snapshot_id, "2024-09")
    comp = _describe(ledger, snapshot_id, "2024-10")
    assert base["status"] == comp["status"] == "ok"

    same_period = compare_population_distribution(base["run_id"], base["run_id"], ledger=ledger)
    assert same_period["block_code"] == "PERIOD_MISMATCH"

    revised = _describe(ledger, snapshot_id, "2024-10", spec=_spec(spec_version="v2"))
    mismatch = compare_population_distribution(base["run_id"], revised["run_id"], ledger=ledger)
    assert mismatch["block_code"] == "SPEC_MISMATCH"
    assert "修订" in mismatch["summary"]["message"]


def test_each_test_declares_what_it_answers(tmp_path):
    ledger, snapshot_id = _ledger(tmp_path, _frame())
    base = _describe(ledger, snapshot_id, "2024-09")
    comp = _describe(ledger, snapshot_id, "2024-10")
    out = compare_population_distribution(base["run_id"], comp["run_id"], ledger=ledger)
    assert out["status"] == "ok", out
    tests = out["summary"]["tests"]
    assert {item["test"] for item in tests} == {"chi2_bins", "ks_2samp", "mann_whitney", "welch_t"}
    for item in tests:
        assert item["tests_what"] and item["applicability"] and item["allowed_conclusions"]
        assert item["role"] == "exploratory"
    # 未预声明主检验时，明确写明「多项同看会放大假阳性」。
    assert any("主检验" in note for note in out["summary"]["limitations"])
    assert "bad_rate_change" in out["summary"]["forbidden_conclusions"]


def test_primary_test_is_carried_when_declared(tmp_path):
    ledger, snapshot_id = _ledger(tmp_path, _frame())
    spec = _spec(primary_test="mann_whitney")
    base = _describe(ledger, snapshot_id, "2024-09", spec=spec)
    comp = _describe(ledger, snapshot_id, "2024-10", spec=spec)
    out = compare_population_distribution(base["run_id"], comp["run_id"], ledger=ledger)
    roles = {item["test"]: item["role"] for item in out["summary"]["tests"]}
    assert roles["mann_whitney"] == "primary"
    assert all(role == "exploratory" for name, role in roles.items() if name != "mann_whitney")


def test_described_numbers_can_be_cited_in_a_report(tmp_path):
    """本次返工的最后一环：描述统计自己产 run，报告才核得过。"""

    ledger, snapshot_id = _ledger(tmp_path, _frame())
    out = _describe(ledger, snapshot_id, "2024-10")
    summary = out["summary"]
    report = (
        f"10 月全部申请记录 {summary['population']['denominator']} 笔，"
        f"机构数中位数 {summary['column']['quantiles']['p50']:.1f} 家。"
    )
    committed = verify_and_commit_report(
        ledger=ledger,
        run_ids=[out["run_id"]],
        report_markdown=report,
        citations=[
            {"path": "population.denominator", "rendered": str(summary["population"]["denominator"])},
            {"path": "column.quantiles.p50", "rendered": f"{summary['column']['quantiles']['p50']:.1f}"},
        ],
    )
    assert committed["status"] == "ok", committed["details"]
    assert committed["summary"]["computation_reproducible"] is True

def test_binary_column_cannot_be_bound_as_the_decision_time(tmp_path):
    """0/1 当决策时点，两期分母就是好客户数与坏客户数。"""

    frame = _frame().assign(dec=[1 if i < 100 else 0 for i in range(1600)])
    ledger, snapshot_id = _ledger(tmp_path, frame)
    ledger.save_snapshot(
        snapshot_id,
        {"grain": "application", "columns": list(frame.columns), "decision_time_column": "dec"},
    )
    out = _describe(ledger, snapshot_id, "1", spec=_spec(time_column="dec"))
    assert out["status"] == "blocked"
    assert out["block_code"] == "TIME_POINT_VIOLATION"
    assert "0/1" in out["summary"]["message"]


def test_another_periods_unknown_code_does_not_disable_the_gate(tmp_path):
    """别的期混进一个 2，本期的 0/1 列不能因此蒙混过关。"""

    rows = []
    for period, value in (("2024-09", 0.0), ("2024-10", 2.0)):
        for i in range(300):
            rows.append(
                {
                    "app_id": f"{period}-{i}",
                    "period": period,
                    "org_num": float(i % 11),
                    "amount": float(1000 + i),
                    "channel": ["线上", "线下"][i % 2],
                    "gb_flag": 1.0 if i % 12 == 0 else 0.0,
                }
            )
    frame = pd.DataFrame(rows)
    frame.loc[frame["period"] == "2024-10", "gb_flag"] = [
        2.0 if i < 3 else (1.0 if i % 12 == 0 else 0.0) for i in range(300)
    ]
    ledger, snapshot_id = _ledger(tmp_path, frame)
    declared = dict(_spec()["field_semantics"])
    declared["gb_flag"] = {"missing": "unknown", "zero": "true_zero"}
    # 整表已不是 0/1（有 2.0），但本期是：按时段判断仍然拦。
    out = _describe(ledger, snapshot_id, "2024-09", column="gb_flag", spec=_spec(field_semantics=declared))
    assert out["status"] == "needs_human", out["summary"]
    assert [item["column"] for item in out["summary"]["undecided"]] == ["gb_flag"]
    # 本期自己也混了脏码（3 个 2.0）：仍是标记列，不能因此变成「普通数值字段」。
    dirty = _describe(ledger, snapshot_id, "2024-10", column="gb_flag", spec=_spec(field_semantics=declared))
    assert dirty["status"] == "needs_human", dirty["summary"]


def test_needs_human_does_not_hand_over_the_rate(tmp_path):
    """待确认的 0/1 列不能把分子分母写进信封——那本身就是坏账率。"""

    frame = _frame().assign(gb_flag=[1 if i % 20 == 0 else 0 for i in range(1600)])
    ledger, snapshot_id = _ledger(tmp_path, frame)
    out = _describe(ledger, snapshot_id, "2024-10", column="gb_flag")
    assert out["status"] == "needs_human"
    assert out["run_id"] is None
    item = out["summary"]["undecided"][0]
    assert set(item) == {"column", "role", "observed_values"}
    assert item["observed_values"] == ["0", "1"]
    assert "n_one" not in str(out["summary"])


def test_run_records_who_declared_the_binary_column(tmp_path):
    """声明 feature 之后要留痕：否则事后无法审计是谁担保的。"""

    frame = _frame().assign(gb_flag=[1 if i % 20 == 0 else 0 for i in range(1600)])
    ledger, snapshot_id = _ledger(tmp_path, frame)
    declared = dict(_spec()["field_semantics"])
    declared["gb_flag"] = {"missing": "unknown", "zero": "true_zero"}
    ledger.save_snapshot(
        snapshot_id,
        {
            "grain": "application",
            "columns": list(frame.columns),
            "decision_time_column": "period",
            "column_roles": {"gb_flag": "feature"},
        },
    )
    out = _describe(
        ledger, snapshot_id, "2024-10", column="gb_flag", spec=_spec(field_semantics=declared)
    )
    assert out["status"] == "ok", out["summary"]
    trace = out["summary"]["described_column"]
    assert trace == {"name": "gb_flag", "role": "feature", "role_source": "explicit", "label_gate": "passed"}
    stored = ledger.get_run(out["run_id"])["summary"]["described_column"]
    assert stored["role"] == "feature"
    assert any("由分析者担保" in note for note in out["summary"]["limitations"])

    # 未声明角色的列则显示 role_source=default。
    plain = _describe(ledger, snapshot_id, "2024-10", column="org_num")
    assert plain["summary"]["described_column"]["role_source"] == "default"


def test_primary_test_that_does_not_apply_is_not_reported_as_declared(tmp_path):
    """数值列声明 chi2_categories：没有检验匹配，就必须说清全是探索性。"""

    ledger, snapshot_id = _ledger(tmp_path, _frame())
    spec = _spec(primary_test="chi2_categories")
    base = _describe(ledger, snapshot_id, "2024-09", spec=spec)
    comp = _describe(ledger, snapshot_id, "2024-10", spec=spec)
    out = compare_population_distribution(base["run_id"], comp["run_id"], ledger=ledger)
    assert out["status"] == "ok", out
    assert out["summary"]["primary_test"] is None
    assert out["summary"]["declared_primary_test"] == "chi2_categories"
    assert all(item["role"] == "exploratory" for item in out["summary"]["tests"])
    assert any("不适用于本列" in note for note in out["summary"]["limitations"])


def test_comparison_edges_hold_when_the_comp_period_moves(tmp_path):
    """对比期整体平移后，若重新分箱就会得到另一组边界。"""

    rows = []
    for period, shift in (("2024-09", 0.0), ("2024-10", 5000.0)):
        for i in range(400):
            rows.append(
                {
                    "app_id": f"{period}-{i}",
                    "period": period,
                    "org_num": float(i % 11),
                    "amount": float(1000 + i * 7 % 4000 + shift),
                    "channel": ["线上", "线下"][i % 2],
                }
            )
    ledger, snapshot_id = _ledger(tmp_path, pd.DataFrame(rows))
    base = _describe(ledger, snapshot_id, "2024-09", column="amount", bins=5)
    comp = _describe(ledger, snapshot_id, "2024-10", column="amount", bins=5)
    published = base["summary"]["column"]["bin_edges"]
    assert comp["summary"]["column"]["bin_edges"] != published

    out = compare_population_distribution(base["run_id"], comp["run_id"], ledger=ledger)
    assert out["status"] == "ok", out
    assert len(published) == 6
    assert out["summary"]["comp"]["bin_edges"] == published
    rows_out = out["summary"]["bins"]
    assert len(rows_out) == len(published) + 1
    # 对比期整体上移：全部落在 overflow，而不是被悄悄丢掉。
    assert rows_out[-1]["kind"] == "overflow"
    assert rows_out[-1]["count_comp"] == 400
    assert rows_out[-1]["count_base"] == 0


def test_binary_columns_are_gated_whatever_dtype_they_arrived_in(tmp_path):
    """闸门和计算必须看同一种数据：文本 "0"/"1" 一样会被 as_numeric 算成均值。"""

    frame = _frame()
    frame["gb_str"] = pd.Series(["1" if i % 20 == 0 else "0" for i in range(1600)], dtype="string")
    frame["gb_bool"] = [i % 20 == 0 for i in range(1600)]
    frame["gb_text"] = ["true" if i % 20 == 0 else "false" for i in range(1600)]
    ledger, snapshot_id = _ledger(tmp_path, frame)
    stored = ledger.load_frame(snapshot_id)
    # parquet 会把 object/文本列读成 StringDtype：这正是实际落地形态。
    assert str(stored["gb_str"].dtype).startswith("string")

    for name in ("gb_str", "gb_bool", "gb_text"):
        out = _describe(ledger, snapshot_id, "2024-10", column=name)
        assert out["status"] == "needs_human", (name, out.get("summary"))
        assert [item["column"] for item in out["summary"]["undecided"]] == [name]

    as_dim = _describe(ledger, snapshot_id, "2024-10", dimensions=["gb_str"])
    assert as_dim["status"] == "needs_human"

    as_filter = _describe(
        ledger, snapshot_id, "2024-10", column="amount", dimensions=[],
        spec=_spec(
            denominator_rule="disbursed",
            population_label="已放款客群",
            denominator_filter={"column": "gb_str", "values": ["1"]},
        ),
    )
    assert as_filter["status"] == "needs_human"
    assert [item["column"] for item in as_filter["summary"]["undecided"]] == ["gb_str"]


def test_a_dirty_code_does_not_turn_a_marker_into_a_plain_numeric_field(tmp_path):
    """0/1 覆盖 94%（890 个 0、50 个 1、60 个 2）仍是标记列，不能因此放出 top_values。"""

    values = [0.0] * 890 + [1.0] * 50 + [2.0] * 60
    frame = pd.DataFrame(
        {
            "app_id": [f"a{i}" for i in range(1000)],
            "period": ["2024-10"] * 1000,
            "gb": values,
            "amount": [float(1000 + i) for i in range(1000)],
            "channel": ["线上"] * 1000,
        }
    )
    ledger, snapshot_id = _ledger(tmp_path, frame)
    out = _describe(
        ledger, snapshot_id, "2024-10",
        column="gb",
        spec=_spec(field_semantics={"gb": {"missing": "unknown", "zero": "true_zero"}}),
    )
    assert out["status"] == "needs_human", out["summary"]
    assert out["run_id"] is None


def test_text_zero_one_with_dirt_is_still_gated(tmp_path):
    """文本 "0"/"1" 掺 6% 非数值脏码时无法转成数值，但 top_values 的占比就是坏账率。

    闸门若只在「能转成数值」之后判断，这种列会退回分类画像，把 890/50/60
    原样输出。所以 token 归一化必须发生在任何强制转换之前。
    """

    values = ["0"] * 890 + ["1"] * 50 + ["N/A"] * 60
    frame = pd.DataFrame(
        {
            "app_id": [f"a{i}" for i in range(1000)],
            "period": ["2024-10"] * 1000,
            "gb_text": pd.Series(values, dtype="string"),
            "amount": [float(1000 + i) for i in range(1000)],
            "channel": ["线上"] * 1000,
        }
    )
    ledger, snapshot_id = _ledger(tmp_path, frame)
    stored = ledger.load_frame(snapshot_id)
    # 落地后仍是字符串列：这正是走不到数值分支的形态。
    assert not pd.api.types.is_numeric_dtype(stored["gb_text"])

    out = _describe(
        ledger, snapshot_id, "2024-10",
        column="gb_text",
        spec=_spec(field_semantics={"gb_text": {"missing": "unknown", "zero": "true_zero"}}),
    )
    assert out["status"] == "needs_human", out["summary"]
    assert out["run_id"] is None
    item = out["summary"]["undecided"][0]
    assert set(item) == {"column", "role", "observed_values"}
    assert "890" not in str(out["summary"])

    # 中文口径的「是/否/未知」同理。
    chinese = _frame().assign(gb_cn=["是"] * 1424 + ["否"] * 80 + ["未知"] * 96)
    cn_ledger, cn_snapshot = _ledger(tmp_path / "cn", chinese)
    cn = _describe(
        cn_ledger, cn_snapshot, "2024-10",
        column="gb_cn",
        spec=_spec(field_semantics={"gb_cn": {"missing": "unknown", "zero": "true_zero"}}),
    )
    assert cn["status"] == "needs_human", cn["summary"]


def test_outcome_written_as_words_is_still_gated(tmp_path):
    """好坏写成 good/bad、正常/逾期，不需要列名语义就能认出：top_values 里 bad 的占比就是坏账率。"""

    for values, declared in (
        (["good"] * 1520 + ["bad"] * 80, {"x_field": {"missing": "unknown", "zero": "true_zero"}}),
        (["正常"] * 1520 + ["逾期"] * 80, {"x_field": {"missing": "unknown", "zero": "true_zero"}}),
        (["good"] * 1400 + ["bad"] * 80 + ["pending"] * 120, {}),
    ):
        frame = _frame().assign(x_field=values)
        ledger, snapshot_id = _ledger(tmp_path / f"case{len(values)}", frame)
        out = _describe(ledger, snapshot_id, "2024-10", column="x_field",
                        spec=_spec(field_semantics=declared))
        assert out["status"] == "needs_human", (values[0], out.get("summary"))
        assert out["human_gate"]["kind"] == "binary_column_role"
        assert out["run_id"] is None

    # 普通两值分类字段不能被误判成标记列。
    plain = _frame().assign(x_field=["线上"] * 800 + ["线下"] * 800)
    ledger, snapshot_id = _ledger(tmp_path / "plain", plain)
    ok = _describe(ledger, snapshot_id, "2024-10", column="x_field",
                   spec=_spec(field_semantics={"x_field": {"missing": "unknown", "zero": "true_zero"}}))
    assert ok["status"] == "ok", ok["summary"]


def test_more_than_one_dirty_code_does_not_promote_a_flag_to_a_category(tmp_path):
    """两个脏码就退回分类画像？那 890/50/30/30 里的 50/1000 就是坏账率。"""

    values = ["0"] * 890 + ["1"] * 50 + ["N/A"] * 30 + ["unknown"] * 30
    frame = pd.DataFrame(
        {
            "app_id": [f"a{i}" for i in range(1000)],
            "period": ["2024-10"] * 1000,
            "x_field": pd.Series(values, dtype="string"),
            "amount": [float(1000 + i) for i in range(1000)],
            "channel": ["线上"] * 1000,
        }
    )
    ledger, snapshot_id = _ledger(tmp_path, frame)
    spec = _spec(field_semantics={"x_field": {"missing": "unknown", "zero": "true_zero"}})

    as_column = _describe(
        ledger, snapshot_id, "2024-10", column="x_field", dimensions=[], spec=spec
    )
    assert as_column["status"] == "needs_human", as_column["summary"]
    assert "890" not in str(as_column["summary"])

    as_dimension = _describe(
        ledger, snapshot_id, "2024-10", column="amount", dimensions=["x_field"], spec=spec
    )
    assert as_dimension["status"] == "needs_human"

    as_filter = _describe(
        ledger, snapshot_id, "2024-10", column="amount", dimensions=[],
        spec=_spec(
            denominator_rule="disbursed",
            population_label="已放款客群",
            denominator_filter={"column": "x_field", "values": ["1"]},
            field_semantics={"x_field": {"missing": "unknown", "zero": "true_zero"}},
        ),
    )
    assert as_filter["status"] == "needs_human"

    # 数字的写法也算同一个值：「00」「0.00」不是第四个取值。
    padded = _frame().assign(x_field=["00"] * 1400 + ["1.00"] * 200)
    padded_ledger, padded_snapshot = _ledger(tmp_path / "padded", padded)
    out = _describe(
        padded_ledger, padded_snapshot, "2024-10", column="x_field", dimensions=[],
        spec=_spec(field_semantics={"x_field": {"missing": "unknown", "zero": "true_zero"}}),
    )
    assert out["status"] == "needs_human", out["summary"]


def test_the_gate_stands_on_the_denominator_subset(tmp_path):
    """放款决策那一列在申请期有 3/4 等拒绝码，切到已放款之后正好只剩 1/0。"""

    rows = []
    for i in range(900):
        rows.append(
            {"app_id": f"a{i}", "period": "2024-10", "status": "已放款", "x_field": 1.0 if i < 50 else 0.0}
        )
    for i in range(100):
        rows.append(
            {"app_id": f"r{i}", "period": "2024-10", "status": "拒绝",
             "x_field": float(3 + i % 5)}   # 3..7：拒绝码，让整列不止 0/1
        )
    frame = pd.DataFrame(rows)
    ledger, snapshot_id = _ledger(tmp_path, frame)
    semantics = {
        "x_field": {"missing": "unknown", "zero": "true_zero"},
        "status": {"missing": "unknown", "zero": "true_zero"},
    }
    spec = _spec(
        denominator_rule="disbursed",
        population_label="已放款客群",
        denominator_filter={"column": "status", "values": ["已放款"]},
        field_semantics=semantics,
    )
    out = _describe(ledger, snapshot_id, "2024-10", column="x_field", dimensions=[], spec=spec)
    assert out["status"] == "needs_human", out["summary"]
    assert "share_one" not in str(out["summary"])
    # 同一列在「全部申请」的分母下是 1/0/3/4，可以正常描述。
    everything = _describe(ledger, snapshot_id, "2024-10", column="x_field", dimensions=[],
                           spec=_spec(field_semantics=semantics))
    assert everything["status"] == "ok", everything["summary"]


def test_a_cell_that_is_binary_inside_a_mixed_column_is_suppressed(tmp_path):
    """整列 1/0/5 不是标记列，但「a 档」里只剩 1/0：那一档的均值就是占比。"""

    rows = []
    for period in ("2024-09", "2024-10"):
        for i in range(800):
            online = i % 3 == 0
            rows.append(
                {
                    "app_id": f"{period}-{i}",
                    "period": period,
                    "org_num": float(i % 11),
                    "amount": float(1000 + i),
                    "channel": "线上" if online else "线下",
                    # 「线上」档里只有 0/1；「线下」档把整列拉到 2..9，
                    # 于是整列不是标记列，只有这一档才是。
                    "x_field": float(i % 2) if online else float(2 + i % 8),
                }
            )
    frame = pd.DataFrame(rows)
    ledger, snapshot_id = _ledger(tmp_path, frame)
    out = _describe(
        ledger, snapshot_id, "2024-10", column="x_field", dimensions=["channel"],
        spec=_spec(field_semantics={"x_field": {"missing": "unknown", "zero": "true_zero"}}),
    )
    assert out["status"] == "ok", out["summary"]
    cells = out["summary"]["by_dimension"][0]["cells"]
    for cell in cells:
        if cell.get("suppressed"):
            assert "metric" not in cell
            assert "0/1" in cell["suppressed_reason"]


def test_zero_one_codes_with_a_long_tail_are_still_gated(tmp_path):
    """0/1/2/3 这种「两个未知码」的列，脏码再多也还是标记列。"""

    values = [0.0] * 890 + [1.0] * 50 + [2.0] * 30 + [3.0] * 30
    frame = pd.DataFrame(
        {
            "app_id": [f"a{i}" for i in range(1000)],
            "period": ["2024-10"] * 1000,
            "x_field": values,
            "amount": [float(1000 + i) for i in range(1000)],
            "channel": ["线上"] * 1000,
        }
    )
    ledger, snapshot_id = _ledger(tmp_path, frame)
    out = _describe(
        ledger, snapshot_id, "2024-10", column="x_field", dimensions=[],
        spec=_spec(field_semantics={"x_field": {"missing": "unknown", "zero": "true_zero"}}),
    )
    assert out["status"] == "needs_human", out["summary"]


def test_codes_without_a_zero_stay_describable(tmp_path):
    """不含 0 的编码是文档写明的边界：1/2、0/2 仍可描述，不能因为收紧而误伤。"""

    for values in ([1.0] * 1400 + [2.0] * 200, [0.0] * 1400 + [2.0] * 200):
        frame = _frame().assign(x_field=values)
        ledger, snapshot_id = _ledger(tmp_path / f"c{values[0]}", frame)
        out = _describe(
            ledger, snapshot_id, "2024-10", column="x_field", dimensions=[],
            spec=_spec(field_semantics={"x_field": {"missing": "unknown", "zero": "true_zero"}}),
        )
        assert out["status"] == "ok", (values[0], out.get("summary"))


def test_a_single_valued_outcome_column_is_gated(tmp_path):
    """整列只剩「是」（其余缺失）：top_values 里它的计数除以总条数就是比率。"""

    for values in (
        ["是"] * 50 + [None] * 950,
        ["bad"] * 50 + [None] * 950,
        ["true"] * 50 + [None] * 950,
    ):
        frame = pd.DataFrame(
            {
                "app_id": [f"a{i}" for i in range(1000)],
                "period": ["2024-10"] * 1000,
                "x_field": values,
                "amount": [float(1000 + i) for i in range(1000)],
                "channel": ["线上"] * 1000,
            }
        )
        ledger, snapshot_id = _ledger(tmp_path / f"s{values[0]}", frame)
        out = _describe(
            ledger, snapshot_id, "2024-10", column="x_field", dimensions=[],
            spec=_spec(field_semantics={"x_field": {"missing": "unknown", "zero": "true_zero"}}),
        )
        assert out["status"] == "needs_human", (values[0], out["summary"])

    # 只有一个普通取值（全是「线上」）或只有一个脏码（全是 N/A）不算标记列。
    for value in ("线上", "N/A"):
        frame = pd.DataFrame(
            {
                "app_id": [f"a{i}" for i in range(1000)],
                "period": ["2024-10"] * 1000,
                "x_field": [value] * 50 + [None] * 950,
                "amount": [float(1000 + i) for i in range(1000)],
                "channel": ["线上"] * 1000,
            }
        )
        ledger, snapshot_id = _ledger(tmp_path / f"p{value}", frame)
        out = _describe(
            ledger, snapshot_id, "2024-10", column="x_field", dimensions=[],
            spec=_spec(field_semantics={"x_field": {"missing": "unknown", "zero": "true_zero"}}),
        )
        assert out["status"] == "ok", (value, out["summary"])


def test_outcome_wordings_beyond_good_bad_are_gated(tmp_path):
    """ok/error、成功/失败、有效/无效 这些写法不靠列名也能认出，不能只挡 good/bad。"""

    gated = [
        ("ok", "error"),
        ("成功", "失败"),
        ("正常", "异常"),
        ("好", "坏"),
        ("有效", "无效"),
        ("approved", "rejected"),
        ("accept", "decline"),
        ("passed", "failed"),
    ]
    for index, (pass_word, fail_word) in enumerate(gated):
        frame = _frame().assign(x_field=[pass_word] * 1520 + [fail_word] * 80)
        ledger, snapshot_id = _ledger(tmp_path / f"g{index}", frame)
        out = _describe(
            ledger, snapshot_id, "2024-10", column="x_field", dimensions=[],
            spec=_spec(field_semantics={"x_field": {"missing": "unknown", "zero": "true_zero"}}),
        )
        assert out["status"] == "needs_human", (pass_word, fail_word, out.get("summary"))

    # 普通两值分类字段不能被一起拦掉。
    for index, (left, right) in enumerate(
        [("男", "女"), ("产品A", "产品B"), ("正常", "冻结"), ("线上", "线下"), ("active", "inactive")]
    ):
        frame = _frame().assign(x_field=[left] * 1200 + [right] * 400)
        ledger, snapshot_id = _ledger(tmp_path / f"p{index}", frame)
        out = _describe(
            ledger, snapshot_id, "2024-10", column="x_field", dimensions=[],
            spec=_spec(field_semantics={"x_field": {"missing": "unknown", "zero": "true_zero"}}),
        )
        assert out["status"] == "ok", (left, right, out.get("summary"))


def test_compare_coerces_a_numeric_kind_column_before_testing(tmp_path):
    """文本 0/1 掺脏码被声明为 feature 后，describe 走数值画像，compare 也必须按数值比。"""

    rows = []
    for period in ("2024-09", "2024-10"):
        for i in range(1000):
            rows.append(
                {
                    "app_id": f"{period}-{i}",
                    "period": period,
                    # 96% 可转数值：describe 走数值画像，但列本身是文本 dtype
                    "x_field": "0" if i < 890 else ("1" if i < 960 else "N/A"),
                    "amount": float(1000 + i),
                    "channel": ["线上", "线下"][i % 2],
                }
            )
    frame = pd.DataFrame(rows)
    ledger, snapshot_id = _ledger(tmp_path, frame)
    ledger.save_snapshot(
        snapshot_id,
        {
            "grain": "application",
            "columns": list(frame.columns),
            "decision_time_column": "period",
            "column_roles": {"period": "decision_time", "x_field": "feature"},
            "explicit_roles": {"x_field": "feature"},
        },
    )
    spec = _spec(field_semantics={"x_field": {"missing": "unknown", "zero": "true_zero"}})
    base = _describe(ledger, snapshot_id, "2024-09", column="x_field", spec=spec)
    comp = _describe(ledger, snapshot_id, "2024-10", column="x_field", spec=spec)
    assert base["status"] == comp["status"] == "ok"
    assert base["summary"]["column"]["kind"] == "numeric"

    out = compare_population_distribution(base["run_id"], comp["run_id"], ledger=ledger)
    assert out["status"] == "ok", out.get("summary")
    ks = next(test for test in out["summary"]["tests"] if test["test"] == "ks_2samp")
    assert ks["p_value"] is not None


def test_one_sided_judgement_word_with_a_literal_missing_code_is_gated(tmp_path):
    """`50 个 "1" + 350 个 "N/A"`：字面缺失码不是第二个取值，这一列只有一个判定值。"""

    families = [
        ({"2024-10": ["1"] * 50 + ["N/A"] * 350}, "1 + N/A"),
        ({"2024-10": ["是"] * 300 + ["未知"] * 100}, "是 + 未知"),
        ({"2024-10": ["bad"] * 300 + ["N/A"] * 100}, "bad + N/A"),
        ({"2024-10": ["通过"] * 300 + [""] * 100}, "通过 + 空串"),
        ({"2024-10": ["正常"] * 300 + ["-"] * 100}, "正常 + 破折号"),
    ]
    for index, (periods, label) in enumerate(families):
        rows = []
        for period, values in periods.items():
            for i, value in enumerate(values):
                rows.append({"app_id": f"{period}-{i}", "period": period, "x_field": value})
        frame = pd.DataFrame(rows)
        ledger, snapshot_id = _ledger(tmp_path / f"m{index}", frame)
        out = _describe(
            ledger, snapshot_id, "2024-10", column="x_field", dimensions=[],
            spec=_spec(field_semantics={"x_field": {"missing": "unknown", "zero": "true_zero"}}),
        )
        assert out["status"] == "needs_human", (label, out.get("summary"))
        assert out["run_id"] is None


def test_a_literal_missing_code_is_counted_as_missing(tmp_path):
    """`N/A` 不是一档取值：否则 n_missing 报 0，而实际 87.5% 都是它。"""

    frame = pd.DataFrame(
        {
            "app_id": [f"a{i}" for i in range(400)],
            "period": ["2024-10"] * 400,
            "x_field": ["1"] * 100 + ["0"] * 250 + ["N/A"] * 50,
            "amount": [float(1000 + i) for i in range(400)],
            "channel": ["线上"] * 400,
        }
    )
    ledger, snapshot_id = _ledger(tmp_path, frame)
    ledger.save_snapshot(
        snapshot_id,
        {
            "grain": "application",
            "columns": list(frame.columns),
            "decision_time_column": "period",
            "column_roles": {"period": "decision_time", "x_field": "feature"},
            "explicit_roles": {"x_field": "feature"},
        },
    )
    out = _describe(
        ledger, snapshot_id, "2024-10", column="x_field", dimensions=[],
        spec=_spec(field_semantics={"x_field": {"missing": "unknown", "zero": "true_zero"}}),
    )
    assert out["status"] == "ok", out["summary"]
    column = out["summary"]["column"]
    assert column["n_missing"] == 50
    assert column["n_effective"] == 350
    assert column["missing_share"] == pytest.approx(0.125)
    # 零值只数非缺失里的 0，缺失不冒充零。
    assert column["binary"]["n_zero"] == 250
    assert column["binary"]["n_one"] == 100


def test_derived_negatives_and_longer_wordings_are_gated(tmp_path):
    """不成功/失效/拒批/审批拒绝/黑名单 都在词表外时，坏账率照样能读出来。"""

    pairs = [
        ("通过", "拒批"),
        ("通过", "审批拒绝"),
        ("成功", "不成功"),
        ("有效", "失效"),
        ("白名单", "黑名单"),
    ]
    for index, (left, right) in enumerate(pairs):
        frame = _frame().assign(x_field=[left] * 1520 + [right] * 80)
        ledger, snapshot_id = _ledger(tmp_path / f"d{index}", frame)
        out = _describe(
            ledger, snapshot_id, "2024-10", column="x_field", dimensions=[],
            spec=_spec(field_semantics={"x_field": {"missing": "unknown", "zero": "true_zero"}}),
        )
        assert out["status"] == "needs_human", (left, right, out.get("summary"))

    # 单字「差」只做精确匹配，不能把「差异」也当成分档。
    plain = _frame().assign(x_field=["正常"] * 1120 + ["差异"] * 480)
    ledger, snapshot_id = _ledger(tmp_path / "diff", plain)
    out = _describe(
        ledger, snapshot_id, "2024-10", column="x_field", dimensions=[],
        spec=_spec(field_semantics={"x_field": {"missing": "unknown", "zero": "true_zero"}}),
    )
    assert out["status"] == "ok", out["summary"]


def test_role_source_reflects_the_declaration_not_the_role_map(tmp_path):
    """column_roles 里每列都有角色；谁被声明过要从 explicit_roles 读。"""

    frame = _frame().assign(gb_flag=[1 if i % 20 == 0 else 0 for i in range(1600)])
    ledger, snapshot_id = _ledger(tmp_path, frame)
    ledger.save_snapshot(
        snapshot_id,
        {
            "grain": "application",
            "columns": list(frame.columns),
            "decision_time_column": "period",
            "column_roles": {"period": "decision_time", "org_num": "unclassified", "gb_flag": "feature"},
            "explicit_roles": {"gb_flag": "feature"},
        },
    )
    declared = dict(_spec()["field_semantics"])
    declared["gb_flag"] = {"missing": "unknown", "zero": "true_zero"}
    flagged = _describe(
        ledger, snapshot_id, "2024-10", column="gb_flag", spec=_spec(field_semantics=declared)
    )
    assert flagged["status"] == "ok", flagged["summary"]
    assert flagged["summary"]["described_column"]["role_source"] == "explicit"

    plain = _describe(ledger, snapshot_id, "2024-10", column="org_num")
    assert plain["status"] == "ok", plain["summary"]
    trace = plain["summary"]["described_column"]
    assert trace["role"] == "unclassified" and trace["role_source"] == "default"


def test_compare_restates_that_a_declared_feature_is_the_analysts_call(tmp_path):
    """比较一个 feature 声明的 0/1 列时，担保说明不能只在描述里出现。"""

    frame = _frame().assign(gb_flag=[1 if i % 20 == 0 else 0 for i in range(1600)])
    ledger, snapshot_id = _ledger(tmp_path, frame)
    ledger.save_snapshot(
        snapshot_id,
        {
            "grain": "application",
            "columns": list(frame.columns),
            "decision_time_column": "period",
            "column_roles": {"gb_flag": "feature"},
            "explicit_roles": {"gb_flag": "feature"},
        },
    )
    declared = dict(_spec()["field_semantics"])
    declared["gb_flag"] = {"missing": "unknown", "zero": "true_zero"}
    spec = _spec(field_semantics=declared)
    base = _describe(ledger, snapshot_id, "2024-09", column="gb_flag", spec=spec)
    comp = _describe(ledger, snapshot_id, "2024-10", column="gb_flag", spec=spec)
    out = compare_population_distribution(base["run_id"], comp["run_id"], ledger=ledger)
    assert out["status"] == "ok", out
    assert out["summary"]["described_column"]["role"] == "feature"
    assert any("由分析者担保" in note for note in out["summary"]["limitations"])


def test_a_literal_missing_code_reaches_the_semantics_gate(tmp_path):
    """闸门与描述必须看到同一批缺失：用原始 isna() 数会漏掉 ``N/A``。

    一列 88% 写成 ``"N/A"`` 时，原始 ``isna()`` 是 0——闸门据此放行，随后输出
    里却报 ``missing_share = 0.88``。等于让工具在一个自己都没搞清口径的列上
    算数字，而这正是 ``field_semantics`` 闸门存在的理由。
    """

    frame = pd.DataFrame(
        {
            "app_id": [f"a{i}" for i in range(100)],
            "period": ["2024-10"] * 100,
            "amount": [float(1000 + i) for i in range(100)],
            "channel": ["N/A"] * 88 + ["线上"] * 6 + ["线下"] * 6,
        }
    )
    ledger, snapshot_id = _ledger(tmp_path, frame)

    out = _describe(ledger, snapshot_id, "2024-10", column="channel", dimensions=[])
    assert out["status"] == "needs_human", out["summary"]
    assert out["human_gate"]["kind"] == "field_semantics"
    assert out["human_gate"]["columns"] == ["channel"]
    # 闸门要数得出这 88 条，否则它拦的是一个它以为干净的列
    assert out["summary"]["undecided"][0]["n_missing"] == 88

    declared = _describe(
        ledger,
        snapshot_id,
        "2024-10",
        column="channel",
        dimensions=[],
        spec=_spec(field_semantics={"channel": {"missing": "unknown"}}),
    )
    assert declared["status"] == "ok", declared["summary"]
    assert declared["summary"]["column"]["n_missing"] == 88
    assert declared["summary"]["column"]["missing_share"] == pytest.approx(0.88)


def test_a_zero_hidden_behind_a_literal_missing_code_is_still_asked_about(tmp_path):
    """零值也要在归一之后数，否则闸门对「有零值的文本列」是瞎的。

    ``"0"/"5"/"7"/"N/A"`` 这样的列拿原始值转数值会直接失败，``n_zero`` 记 0，
    于是只要求声明缺失含义、不要求声明零值——但描述层照样把那 25 个 0 当零值
    算进均值。缺了一半的口径问询，比不问更危险。
    """

    frame = pd.DataFrame(
        {
            "app_id": [f"a{i}" for i in range(100)],
            "period": ["2024-10"] * 100,
            "amount": [float(1000 + i) for i in range(100)],
            "spend": ["0", "5", "7", "N/A"] * 25,
        }
    )
    ledger, snapshot_id = _ledger(tmp_path, frame)
    out = _describe(ledger, snapshot_id, "2024-10", column="spend", dimensions=[])

    assert out["status"] == "needs_human", out["summary"]
    item = out["summary"]["undecided"][0]
    assert item["column"] == "spend"
    assert item["n_missing"] == 25
    assert item["n_zero"] == 25
    # 两侧都要问：只问缺失，等于默认零值含义已知
    assert item["missing_options"] and item["zero_options"]


def test_a_dimension_cell_never_turns_a_missing_code_into_a_business_value(tmp_path):
    """维度表里 88 个 ``"N/A"`` 不能变成一个占比 0.88 的业务类别。

    同一个缺失事实，写成 ``"N/A"`` 会变成一格「业务取值」，写成真空值又被
    ``astype(str)`` 变成一格 ``"nan"``——两种写法在维度表里得到相反的结果，而
    被描述的列本身早已把两者都算成缺失。维度必须跟着同一口径走。
    """

    frame = pd.DataFrame(
        {
            "app_id": [f"a{i}" for i in range(100)],
            "period": ["2024-10"] * 100,
            "org_num": [float(i % 9) for i in range(100)],
            "channel": ["N/A"] * 88 + ["线上"] * 6 + ["线下"] * 6,
        }
    )
    ledger, snapshot_id = _ledger(tmp_path, frame)
    spec = _spec(
        field_semantics={
            "org_num": {"missing": "no_record", "zero": "no_record"},
            "channel": {"missing": "unknown"},
        }
    )
    out = _describe(ledger, snapshot_id, "2024-10", spec=spec)
    assert out["status"] == "ok", out["summary"]

    dimension = out["summary"]["by_dimension"][0]
    assert dimension["distinct"] == 3
    cells = {cell["value"]: cell for cell in dimension["cells"]}
    assert set(cells) == {"<缺失>", "线上", "线下"}
    assert cells["<缺失>"]["is_missing"] is True
    assert cells["<缺失>"]["count"] == 88
    assert cells["<缺失>"]["share"] == pytest.approx(0.88)
    assert "is_missing" not in cells["线上"]
    # 维度格子里的占比要与「把 channel 当描述列」得到的缺失率是同一个数：
    # 否则同一份数据在维度表与列画像里各说各话。
    channels = _describe(
        ledger, snapshot_id, "2024-10", column="channel", dimensions=[], spec=spec
    )
    assert channels["status"] == "ok", channels["summary"]
    assert channels["summary"]["column"]["missing_share"] == pytest.approx(0.88)


def test_observation_points_survive_a_literal_missing_code(tmp_path):
    """观察点不能因为列里写了 ``N/A`` 就整段消失。

    ``as_numeric`` 遇到 ``"N/A"`` 会直接抛错，原实现据此返回空数组——画像里
    有均值分位数，``observation_points`` 却说这一列没有观察点，等于把一个
    「≥10 家机构的占比」无声吞掉。
    """

    frame = pd.DataFrame(
        {
            "app_id": [f"a{i}" for i in range(100)],
            "period": ["2024-10"] * 100,
            "org_num": [str(i % 14) if i % 4 else "N/A" for i in range(100)],
            "amount": [float(1000 + i) for i in range(100)],
            "channel": ["线上" if i % 2 else "线下" for i in range(100)],
        }
    )
    ledger, snapshot_id = _ledger(tmp_path, frame)
    out = _describe(ledger, snapshot_id, "2024-10", dimensions=[])
    assert out["status"] == "ok", out["summary"]

    column = out["summary"]["column"]
    points = out["summary"]["observation_points"]
    assert [point["name"] for point in points] == ["≥10 家机构", "策略阈值 ≥12"]
    observed = [
        int(item) for item in pd.Series(frame["org_num"]).replace("N/A", pd.NA).dropna()
    ]
    assert points[0]["count"] == sum(1 for value in observed if value >= 10)
    # 与画像同一批行：分位数与观察点都不含那 25 条 N/A
    assert column["n_missing"] == 25
    assert column["n_effective"] == 75


def test_the_missing_cell_survives_a_truncated_dimension(tmp_path):
    """档位再多，`<缺失>` 也不能被「取前 20 档」挤掉。

    20 个业务档 + 1 条缺失时，缺失格排在最后，`head(20)` 会把它切掉：文档说
    「统一进 `<缺失>` 一格」，表里却找不到这一格，那 1 条就此消失，而 cells 的
    计数之和也不再等于分母。缺失格要占一个固定名额。
    """

    values = [f"s{index:02d}" for index in range(20)]
    segment = [value for value in values for _ in range(5)] + ["N/A"]
    frame = pd.DataFrame(
        {
            "app_id": [f"a{i}" for i in range(101)],
            "period": ["2024-10"] * 101,
            "org_num": [float(i % 9) for i in range(101)],
            "segment": segment,
        }
    )
    ledger, snapshot_id = _ledger(tmp_path, frame)
    spec = _spec(
        field_semantics={
            "org_num": {"missing": "no_record", "zero": "no_record"},
            "segment": {"missing": "unknown"},
        }
    )
    out = _describe(ledger, snapshot_id, "2024-10", spec=spec, dimensions=["segment"])
    assert out["status"] == "ok", out["summary"]

    dimension = out["summary"]["by_dimension"][0]
    assert dimension["truncated"] is True
    assert len(dimension["cells"]) == 20  # 19 个业务档 + 缺失格
    missing_cells = [cell for cell in dimension["cells"] if cell.get("is_missing")]
    assert len(missing_cells) == 1
    assert missing_cells[0]["count"] == 1
    # 20 档里恰好被截掉一档：计数之和 = 101 - 5
    assert sum(cell["count"] for cell in dimension["cells"]) == 96
    assert dimension["distinct"] == 21


def test_a_dimension_cannot_declare_missing_as_zero(tmp_path):
    """维度列上的 `zero_equivalent` 必须被拒绝，不能「接受但不生效」。

    把缺失并进 0 会改写分组键；工具不做这件事。接受了声明却照原样分组，等于
    让口径变成一句文案——这与描述列上「分类列不许声明 zero_equivalent」是同
    一条规则，不能因为换了位置就松掉。
    """

    frame = pd.DataFrame(
        {
            "app_id": [f"a{i}" for i in range(100)],
            "period": ["2024-10"] * 100,
            "org_num": [float(i % 9) for i in range(100)],
            "channel": ["N/A"] * 20 + ["线上"] * 40 + ["线下"] * 40,
        }
    )
    ledger, snapshot_id = _ledger(tmp_path, frame)
    spec = _spec(
        field_semantics={
            "org_num": {"missing": "no_record", "zero": "no_record"},
            "channel": {"missing": "zero_equivalent"},
        }
    )
    out = _describe(ledger, snapshot_id, "2024-10", spec=spec, dimensions=["channel"])
    assert out["status"] == "blocked", out["summary"]
    assert out["block_code"] == "UNSUPPORTED_SCOPE"
    assert "channel" in out["summary"]["message"]


def test_a_zero_equivalent_column_cannot_also_be_a_dimension(tmp_path):
    """同一列既当描述列又当维度时，两个口径会并存，必须拒绝。

    描述列把缺失并进 0（`mean` 与 `n_imputed_zero` 都按 0 算），维度表却只能
    按原值分组、把同一批行单列成 `<缺失>`——同一封信里两种口径，读者按维度表
    加权平均对不上 `column.mean`。分组键改不动，所以这种组合直接拒绝，而不是
    让声明在半个工具里生效。
    """

    frame = pd.DataFrame(
        {
            "app_id": [f"a{i}" for i in range(100)],
            "period": ["2024-10"] * 100,
            "org_num": [None if i % 10 == 0 else float(i % 9) for i in range(100)],
        }
    )
    ledger, snapshot_id = _ledger(tmp_path, frame)
    spec = _spec(field_semantics={"org_num": {"missing": "zero_equivalent", "zero": "true_zero"}})
    out = _describe(ledger, snapshot_id, "2024-10", column="org_num", dimensions=["org_num"], spec=spec)
    assert out["status"] == "blocked", out["summary"]
    assert out["block_code"] == "UNSUPPORTED_SCOPE"
    assert "org_num" in out["summary"]["message"]

    # 作为普通维度、描述别的列时同样要拒绝（那里连描述列都不是）
    out = _describe(ledger, snapshot_id, "2024-10", column="period", dimensions=["org_num"], spec=spec)
    assert out["status"] == "blocked", out["summary"]
    assert out["block_code"] == "UNSUPPORTED_SCOPE"


def test_declared_but_unused_categories_are_not_dimension_cells(tmp_path):
    """`categorical` 里声明过、这一期没出现的类别，不能变成 count=0 的档位。

    `value_counts()` 对 categorical dtype 会把未出现的类别也列出来（count=0）：
    一期只用了 3 个渠道，却报 `distinct=6` 并塞进 3 格 0 计数的「业务档」——
    读者会以为这些渠道存在但没人用，进而写成「某渠道本期为零」。
    """

    frame = pd.DataFrame(
        {
            "app_id": [f"a{i}" for i in range(60)],
            "period": ["2024-10"] * 60,
            "org_num": [float(i % 9) for i in range(60)],
            "channel": pd.Categorical(
                ["线上"] * 30 + ["线下"] * 20 + ["代理"] * 10,
                categories=["线上", "线下", "代理", "电销", "直销", "其他"],
            ),
        }
    )
    ledger, snapshot_id = _ledger(tmp_path, frame)
    spec = _spec(
        field_semantics={
            "org_num": {"missing": "no_record", "zero": "no_record"},
            "channel": {"missing": "unknown"},
        }
    )
    out = _describe(ledger, snapshot_id, "2024-10", spec=spec, dimensions=["channel"])
    assert out["status"] == "ok", out["summary"]
    dimension = out["summary"]["by_dimension"][0]
    assert dimension["distinct"] == 3
    assert {cell["value"] for cell in dimension["cells"]} == {"线上", "线下", "代理"}
    assert all(cell["count"] > 0 for cell in dimension["cells"])


def test_a_text_numeric_column_accepts_zero_equivalent_like_a_float_one(tmp_path):
    """同一列存成文本还是浮点，`zero_equivalent` 不能用出两个结果。

    一列 `"0" / "5" / "7" / "N/A"` 拿原始值转数值会失败，于是被判成分类列而
    拒绝 `zero_equivalent`；同一份数据经 CSV 读成 `float64` 却能通过。判断
    是不是数值列，必须和描述、闸门用同一套「先归一」的判据。
    """

    rows = []
    for index in range(100):
        token = "N/A" if index % 4 == 0 else str(["0", "5", "7"][index % 3])
        rows.append(
            {
                "app_id": f"a{index}",
                "period": "2024-10",
                "org_num": float(index % 9) if index % 4 else None,
                "spend": token,
            }
        )
    frame = pd.DataFrame(rows)
    ledger, snapshot_id = _ledger(tmp_path, frame)
    spec = _spec(
        field_semantics={
            "org_num": {"missing": "no_record", "zero": "no_record"},
            "spend": {"missing": "zero_equivalent", "zero": "true_zero"},
        }
    )
    text = _describe(ledger, snapshot_id, "2024-10", column="spend", dimensions=[], spec=spec)
    assert text["status"] == "ok", text["summary"]
    column = text["summary"]["column"]
    assert column["n_missing"] == 25
    assert column["n_imputed_zero"] == 25
    # 缺失并入零后，均值 = 剩余取值之和 / 全部记录数
    kept = [int(token) for index, token in enumerate(
        ("N/A" if i % 4 == 0 else str(["0", "5", "7"][i % 3]) for i in range(100))
    ) if token != "N/A"]
    assert column["mean"] == pytest.approx(sum(kept) / 100)

    # 同一条声明，换成浮点列，结论必须一样（缺失位置也一致）
    numeric_frame = frame.copy()
    numeric_frame["spend"] = [
        None if token == "N/A" else float(token) for token in frame["spend"]
    ]
    numeric_ledger, numeric_snapshot = _ledger(tmp_path / "numeric", numeric_frame)
    numeric = _describe(
        numeric_ledger, numeric_snapshot, "2024-10", column="spend", dimensions=[], spec=spec
    )
    assert numeric["status"] == "ok", numeric["summary"]
    assert numeric["summary"]["column"]["mean"] == pytest.approx(column["mean"])
    assert numeric["summary"]["column"]["n_imputed_zero"] == column["n_imputed_zero"]


def test_a_missing_code_cannot_be_a_denominator_filter_value(tmp_path):
    """把 `"N/A"` 写进分母过滤值，切出来的是「这一列没填」的行。

    这比算错更糟：它给这批行冠上「已放款客群」的名字，于是「没填」被写成
    「放款」。而且 CSV 解析器已把 N/A 转成空值，同一条口径换个文件格式就
    从「出数」变成「报错」。两条路都必须直接拒绝，并说清原因。
    """

    frame = pd.DataFrame(
        {
            "app_id": [f"a{i}" for i in range(50)],
            "period": ["2024-10"] * 50,
            "amount": [float(i) for i in range(50)],
            "status": ["N/A"] * 20 + ["已放款"] * 30,
        }
    )
    ledger, snapshot_id = _ledger(tmp_path, frame)
    spec = _spec(
        population_label="已放款客群",
        denominator_rule="disbursed",
        denominator_filter={"column": "status", "values": ["N/A"]},
        field_semantics={
            "amount": {"missing": "unknown", "zero": "true_zero"},
            "status": {"missing": "unknown"},
        },
    )
    out = _describe(ledger, snapshot_id, "2024-10", column="amount", dimensions=[], spec=spec)
    assert out["status"] == "blocked", out["summary"]
    assert out["block_code"] == "UNSUPPORTED_SCOPE"
    assert "缺失码" in out["summary"]["message"]


def test_a_missing_code_cannot_be_a_period(tmp_path):
    """时间列没填不是「一期」，也不能和真实月份比。

    Parquet 会原样保留 `"N/A"`，于是它变成一个可选期次：描述返回 ok，那 20%
    缺失被当成一个总体，还能和真实月份做构成比较。而准入早就把它们报成缺失——
    同一份数据在两步里给两个答案。
    """

    frame = pd.DataFrame(
        {
            "app_id": [f"b{i}" for i in range(50)],
            "period": ["2024-10-01"] * 40 + ["N/A"] * 10,
            "amount": [float(i) for i in range(50)],
        }
    )
    ledger, snapshot_id = _ledger(tmp_path, frame)
    spec = _spec(field_semantics={"amount": {"missing": "unknown", "zero": "true_zero"}})
    out = _describe(ledger, snapshot_id, "N/A", column="amount", dimensions=[], spec=spec)
    assert out["status"] == "blocked", out["summary"]
    assert out["block_code"] == "PERIOD_MISMATCH"
    assert "缺失码" in out["summary"]["message"]
    # 真实期次照常
    real = _describe(ledger, snapshot_id, "2024-10-01", column="amount", dimensions=[], spec=spec)
    assert real["status"] == "ok", real["summary"]


def test_an_all_missing_column_merges_into_zero_whatever_its_dtype(tmp_path):
    """整列缺失时，文本与浮点必须给同一个答案。

    一列全写 `"N/A"`（Parquet 保留文本）声明 `zero_equivalent` 时，原来被判成
    分类列而拒绝；存成 float64 全 NaN 却通过并给出 `mean = 0`。口径能不能用
    由文件格式决定，正是这一批要消灭的东西。
    """

    text_frame = pd.DataFrame(
        {
            "app_id": [f"a{i}" for i in range(40)],
            "period": ["2024-10"] * 40,
            "org_num": [float(i % 9) for i in range(40)],
            "spend": ["N/A"] * 40,
        }
    )
    float_frame = text_frame.assign(spend=[None] * 40)
    spec = _spec(
        field_semantics={
            "org_num": {"missing": "no_record", "zero": "no_record"},
            "spend": {"missing": "zero_equivalent", "zero": "true_zero"},
        }
    )
    results = {}
    for label, frame in (("text", text_frame), ("float", float_frame)):
        ledger, snapshot_id = _ledger(tmp_path / label, frame)
        out = _describe(ledger, snapshot_id, "2024-10", column="spend", dimensions=[], spec=spec)
        assert out["status"] == "ok", out["summary"]
        results[label] = out["summary"]["column"]
    assert results["text"]["mean"] == pytest.approx(0.0)
    assert results["float"]["mean"] == pytest.approx(0.0)
    assert results["text"]["n_imputed_zero"] == results["float"]["n_imputed_zero"] == 40
    assert results["text"]["n_missing"] == results["float"]["n_missing"] == 40


def test_dimension_labels_do_not_print_binary_noise():
    """标签是给人读的：`float32` 的二进制展开不能写进报告。

    `np.float32(0.1)` 是 `0.10000000149011612`，直接渲染出来既不可读，也和同一
    份数据的 `float64` 版本对不上。整数值不带 `.0`；文本原样保留，`007` 不能被
    当成 7。
    """

    from credit_agent.tools.descriptive import _cell_label

    assert _cell_label(np.float32(0.1)) == "0.1"
    assert _cell_label(np.float64(0.1)) == "0.1"
    assert _cell_label(1.0) == "1"
    assert _cell_label(np.int64(3)) == "3"
    assert _cell_label("007") == "007"
    assert _cell_label(True) == "True"
    assert _cell_label(2 ** 60) == str(2 ** 60)


def test_an_all_missing_text_base_period_tests_the_same_numbers_as_a_float_one(tmp_path):
    """检验与观察点必须用补零后的序列，不能由文件格式决定。

    基期整列 `"N/A"`、口径声明「缺失算 0」时：存成 float64 全 NaN，四项检验
    全部出数；存成文本，`as_numeric` 失败，`_materialize` 回退原始序列，检验
    全是 None，`allowed_conclusions` 只剩 composition_change。画像数字两存法
    一致，分歧精确落在「这份报告能写哪些结论」上。
    """

    def build(spend_base, spend_comp):
        rows = []
        for i in range(60):
            rows.append({"app_id": f"b{i}", "period": "2024-09", "spend": spend_base,
                         "channel": "线上" if i % 2 else "线下"})
        for i in range(60):
            rows.append({"app_id": f"c{i}", "period": "2024-10",
                         "spend": spend_comp[i], "channel": "线上" if i % 2 else "线下"})
        return pd.DataFrame(rows)

    text_frame = build("N/A", [str(i % 7) for i in range(60)])
    float_frame = build(None, [float(i % 7) for i in range(60)])
    spec = _spec(
        observation_points=[{"name": "≥0", "threshold": 0.0, "kind": "observation_point"}],
        field_semantics={
            "spend": {"missing": "zero_equivalent", "zero": "true_zero"},
            "channel": {"missing": "unknown"},
        },
    )
    outcomes = {}
    for label, frame in (("text", text_frame), ("float", float_frame)):
        ledger, snapshot_id = _ledger(tmp_path / label, frame)
        base = _describe(ledger, snapshot_id, "2024-09", column="spend", dimensions=[], spec=spec)
        comp = _describe(ledger, snapshot_id, "2024-10", column="spend", dimensions=[], spec=spec)
        assert base["status"] == "ok" and comp["status"] == "ok"
        out = compare_population_distribution(base["run_id"], comp["run_id"], ledger=ledger)
        assert out["status"] == "ok", out["summary"]
        tests = {test["test"]: test["p_value"] for test in out["summary"]["tests"]}
        # 均值差两存法已经一致；检验与结论集合也必须一致
        assert out["summary"]["mean_delta"] == pytest.approx(
            sum(float(i % 7) for i in range(60)) / 60
        )
        outcomes[label] = {
            "tests": tests,
            "allowed": out["allowed_conclusions"],
            "points_base": [(p["name"], p["count"], p["share"]) for p in base["summary"]["observation_points"]],
        }
    # 文本基期的检验不再全是 None，且与浮点版同一结论集合
    assert all(value is not None for value in outcomes["text"]["tests"].values())
    assert set(outcomes["text"]["tests"]) == set(outcomes["float"]["tests"])
    for name, p_value in outcomes["float"]["tests"].items():
        if p_value is None:
            continue
        assert outcomes["text"]["tests"][name] == pytest.approx(p_value), name
    assert outcomes["text"]["allowed"] == outcomes["float"]["allowed"]
    assert "composition_change" in outcomes["text"]["allowed"]
    # 观察点：缺失当 0 时，基期 60 条全部 ≥ 0
    assert outcomes["text"]["points_base"] == outcomes["float"]["points_base"]
    assert outcomes["text"]["points_base"] == [("≥0", 60, 1.0)]


def test_an_all_missing_column_is_categorical_when_missing_means_unknown(tmp_path):
    """整列缺失在 `missing: unknown`（不补零）时也必须与 dtype 无关。

    第六轮验收打穿：全缺失文本列在 `missing: unknown` 下走分类画像（没有
    mean / n_zero / quantiles），同一份数据存成 float64 全 NaN 却走数值画像。
    画像的字段集合由文件格式决定。画像的 kind 判据必须与口径无关，和
    `_observation_points` / `_materialize` / `_is_numeric_column` 三处一致。
    """

    text_frame = pd.DataFrame(
        {
            "app_id": [f"a{i}" for i in range(20)],
            "period": ["2024-10"] * 20,
            "org_num": [float(i % 9) for i in range(20)],
            "spend": ["N/A"] * 20,
        }
    )
    float_frame = text_frame.assign(spend=[None] * 20)
    # missing: unknown —— 明确不把缺失补成零，走的是「不为 zero_equivalent」分支
    spec = _spec(field_semantics={"spend": {"missing": "unknown", "zero": "true_zero"}})
    results = {}
    for label, frame in (("text", text_frame), ("float", float_frame)):
        ledger, snapshot_id = _ledger(tmp_path / label, frame)
        out = _describe(ledger, snapshot_id, "2024-10", column="spend", dimensions=[], spec=spec)
        assert out["status"] == "ok", out["summary"]
        results[label] = out["summary"]["column"]
    # 口径是 unknown（不补零）：两存法都按分类画像，kind 不再由 dtype 决定。
    # 关键不是判成哪一类，而是两存法一致、字段集合一致（不是一列有 mean、
    # 一列没有）。
    assert results["text"]["kind"] == results["float"]["kind"] == "categorical"
    assert set(results["text"]) == set(results["float"])
    for field in ("n_missing", "missing_share", "n_effective", "distinct"):
        assert results["text"][field] == results["float"][field], field


def test_dimension_label_is_stable_across_float_widths():
    """float32 与 float64 的同一意图值必须渲染成同一个维度标签。

    第六轮验收：float32 走 `.7g`、float64 走完整 `str`，`3.14159265` 与
    `16777216.0` 在两个宽度下得到不同标签，两期 join 时一档静默分裂成两档。
    """

    from credit_agent.tools.descriptive import _cell_label

    for value in (3.14159265, 16777216.0, 0.1, 12.5, -7.25):
        assert _cell_label(np.float32(value)) == _cell_label(np.float64(value)), value
