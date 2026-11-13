"""Synthetic checks for the configurable score grid, marginal bands, and swap table."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from credit_agent.analytics.strategy import marginal_bands, score_grid, swap_table


def _cell(result, left_a, right_a, left_b, right_b):
    for cell in result["cells"]:
        if (cell["left_a"], cell["right_a"], cell["left_b"], cell["right_b"]) == (left_a, right_a, left_b, right_b):
            return cell
    raise AssertionError(f"missing cell {(left_a, right_a, left_b, right_b)}")


def _band(result, lower, upper):
    for band in result["bands"]:
        if band["lower"] == lower and band["upper"] == upper:
            return band
    raise AssertionError(f"missing band {(lower, upper)}")


def _assert_explained(result):
    text = str(result["definition"])
    assert "分子" in text and "分母" in text
    assert isinstance(result["caveats"], list) and result["caveats"]


def test_score_grid_missing_column():
    frame = pd.DataFrame({"score_a": [1, 2], "score_b": [1, 2]})
    with pytest.raises(ValueError, match="缺少列 bad"):
        score_grid(frame, "score_a", "score_b", "bad", edges_a=[0, 10], edges_b=[0, 10])


def test_score_grid_rejects_bad_edges_blank_scores_and_paths():
    frame = pd.DataFrame({"a": [1.0, 2.0], "b": [1.0, 2.0], "bad": [0, 1]})
    with pytest.raises(ValueError, match="分箱边界至少需要 2 个"):
        score_grid(frame, "a", "b", "bad", edges_a=[10], edges_b=[0, 10])
    with pytest.raises(ValueError, match="分箱边界必须严格递增"):
        score_grid(frame, "a", "b", "bad", edges_a=[10, 0], edges_b=[0, 10])
    with pytest.raises(ValueError, match="分箱边界必须是数值"):
        score_grid(frame, "a", "b", "bad", edges_a=["low", "high"], edges_b=[0, 10])
    blank = pd.DataFrame({"a": [None, None], "b": [1, 2], "bad": [0, 1]})
    with pytest.raises(ValueError, match="分数全空"):
        score_grid(blank, "a", "b", "bad", edges_a=[0, 10], edges_b=[0, 10])
    with pytest.raises(ValueError, match="输入必须是 DataFrame"):
        score_grid("scores.csv", "a", "b", "bad", edges_a=[0, 10], edges_b=[0, 10])


def test_score_grid_cell_bad_rate_is_hand_calculable():
    # Right-closed: 10 and 20 fall inside; 0 and 100 stay outside.
    frame = pd.DataFrame(
        {
            "a": [0, 1, 10, 11, 20, 5, 100],
            "b": [5, 1, 5, 15, 20, 15, 5],
            "bad": [1, 0, 1, 1, 0, 0, 1],
        }
    )
    result = score_grid(frame, "a", "b", "bad", edges_a=[0, 10, 20], edges_b=[0, 10, 20])
    _assert_explained(result)
    assert any("只描述已经有坏账标记的样本" in note for note in result["caveats"])
    assert len(result["cells"]) == 4
    assert result["n"] == 5

    low_low = _cell(result, 0, 10, 0, 10)
    low_high = _cell(result, 0, 10, 10, 20)
    high_low = _cell(result, 10, 20, 0, 10)
    high_high = _cell(result, 10, 20, 10, 20)
    assert (low_low["n"], low_low["bad_cnt"], low_low["bad_rate"], low_low["share"]) == (2, 1, 0.5, 0.4)
    assert (low_high["n"], low_high["bad_cnt"], low_high["bad_rate"], low_high["share"]) == (1, 0, 0.0, 0.2)
    assert high_low["n"] == 0
    assert high_low["bad_rate"] is None
    assert high_low["share"] == 0
    assert (high_high["n"], high_high["bad_cnt"], high_high["bad_rate"], high_high["share"]) == (2, 1, 0.5, 0.4)
    assert sum(cell["n"] for cell in result["cells"]) == 5
    assert sum(cell["share"] for cell in result["cells"]) == pytest.approx(1)


def test_score_grid_weight_and_textual_bad_flag():
    weighted = pd.DataFrame(
        {"a": [5, 5, 15], "b": [5, 5, 15], "bad": [1, 0, 1], "w": [1.0, 3.0, 1.0]}
    )
    result = score_grid(weighted, "a", "b", "bad", edges_a=[0, 10, 20], edges_b=[0, 10, 20], weight="w")
    cell = _cell(result, 0, 10, 0, 10)
    assert cell["n"] == 4
    assert cell["bad_cnt"] == 1
    assert cell["bad_rate"] == 0.25
    assert cell["share"] == 0.8

    negative = weighted.assign(w=[1.0, -1.0, 1.0])
    with pytest.raises(ValueError, match="权重必须为有限非负数"):
        score_grid(negative, "a", "b", "bad", edges_a=[0, 10], edges_b=[0, 10], weight="w")

    textual = pd.DataFrame({"a": [5, 6, 7], "b": [5, 6, 7], "bad": ["bad", "good", "yes"]})
    text_result = score_grid(textual, "a", "b", "bad", edges_a=[0, 10], edges_b=[0, 10])
    only = text_result["cells"][0]
    assert (only["n"], only["bad_cnt"], only["bad_rate"]) == (3, 2, pytest.approx(2 / 3))


def test_marginal_bands_cumulative_approval():
    frame = pd.DataFrame(
        {
            "score": [0, 10, 15, 20, 25, None, 15],
            "bad": [1, 0, 1, 0, 1, 0, None],
        }
    )
    result = marginal_bands(frame, "score", "bad", edges=[0, 10, 20], higher_is_safer=True)
    _assert_explained(result)
    assert "profit" not in result
    assert result["n"] == 5
    assert sum(band["n"] for band in result["bands"]) == 3

    low = _band(result, 0, 10)
    high = _band(result, 10, 20)
    assert (low["n"], low["bad_rate"], low["approval_rate"], low["approved_bad_rate"]) == (1, 0.0, 1.0, 0.6)
    assert high["n"] == 2
    assert high["bad_rate"] == 0.5
    assert high["approval_rate"] == pytest.approx(0.8)
    assert high["approved_n"] == 4
    assert high["approved_bad_rate"] == 0.5

    lower_safer = marginal_bands(frame, "score", "bad", edges=[0, 10, 20], higher_is_safer=False)
    low_tail = _band(lower_safer, 0, 10)
    high_tail = _band(lower_safer, 10, 20)
    assert low_tail["approval_rate"] == pytest.approx(0.4)
    assert low_tail["approved_bad_rate"] == 0.5
    assert high_tail["approved_n"] == 4
    assert high_tail["approved_bad_rate"] == 0.5
    assert "econ" not in marginal_bands.__code__.co_varnames


def test_swap_equal_thresholds_are_empty():
    frame = pd.DataFrame({"score": [10, 20, 30], "bad": [1, 0, 1]})
    result = swap_table(frame, "score", "bad", current_threshold=20, candidate_threshold=20)
    _assert_explained(result)
    assert result["swap_out"]["n"] == 0
    assert result["swap_in"]["n"] == 0
    assert result["swap_out"]["bad_rate"] is None
    assert result["swap_in"]["bad_rate"] is None
    assert result["keep_approved"]["n"] == 2
    assert result["keep_approved"]["bad_rate"] == 0.5


def test_swap_in_bad_rate_is_none_without_outcome_column():
    frame = pd.DataFrame({"score": [10, 20, 30, 40]})
    result = swap_table(frame, "score", "bad", current_threshold=25, candidate_threshold=15)
    assert result["swap_in"]["n"] == 1
    assert result["swap_in"]["bad_rate"] is None
    assert result["swap_in"]["bad_cnt"] is None
    assert result["keep_approved"]["n"] == 2
    assert result["keep_approved"]["bad_rate"] is None
    assert result["swap_out"]["n"] == 0
    assert any("不是被拒申请人的坏账率" in note for note in result["caveats"])
    assert any("预期收益" in note for note in result["caveats"])
    assert any("拒绝推断" in note for note in result["caveats"])
    assert "profit" not in result


def test_swap_in_without_outcomes_stays_null_and_labeled_rows_keep_their_rate():
    unlabeled_new = pd.DataFrame({"score": [10, 20, 30], "bad": [None, 0, 1]})
    opened = swap_table(unlabeled_new, "score", "bad", current_threshold=15, candidate_threshold=5)
    assert opened["swap_in"]["n"] == 1
    assert opened["swap_in"]["bad_rate"] is None
    assert opened["keep_approved"]["n"] == 2
    assert opened["keep_approved"]["bad_rate"] == 0.5

    labeled = pd.DataFrame({"score": [10, 20, 30], "bad": [1, 0, 1]})
    observed = swap_table(labeled, "score", "bad", current_threshold=15, candidate_threshold=5)
    assert observed["swap_in"]["n"] == 1
    assert observed["swap_in"]["bad_rate"] == 1
    assert any("不是被拒申请人的坏账率" in note for note in observed["caveats"])
    assert "profit" not in observed

    tightened = swap_table(labeled, "score", "bad", current_threshold=15, candidate_threshold=25)
    assert tightened["swap_out"]["n"] == 1
    assert tightened["swap_out"]["bad_rate"] == 0
    assert tightened["swap_in"]["n"] == 0
    assert tightened["keep_approved"]["bad_rate"] == 1


def test_swap_approved_filter_and_lower_is_safer():
    frame = pd.DataFrame(
        {"score": [10, 20, 30], "bad": [1, 0, 1], "approved": [0, 1, 1]}
    )
    result = swap_table(
        frame,
        "score",
        "bad",
        current_threshold=25,
        candidate_threshold=15,
        approved="approved",
    )
    assert result["swap_in"]["n"] == 1
    assert result["swap_in"]["bad_rate"] == 0
    assert result["n"] == 2

    lower_safer = swap_table(
        pd.DataFrame({"score": [10, 20, 30], "bad": [1, 0, 1]}),
        "score",
        "bad",
        current_threshold=20,
        candidate_threshold=10,
        higher_is_safer=False,
    )
    assert lower_safer["keep_approved"]["n"] == 1
    assert lower_safer["keep_approved"]["bad_rate"] == 1
    assert lower_safer["swap_out"]["n"] == 1
    assert lower_safer["swap_out"]["bad_rate"] == 0
    assert lower_safer["swap_in"]["n"] == 0


def test_edges_are_not_hardcoded_in_the_module():
    source = (
        Path(__file__).resolve().parents[1] / "src" / "credit_agent" / "analytics" / "strategy.py"
    ).read_text(encoding="utf-8")
    assert "auto_model_code" not in source
    assert "490" not in source
    assert "810" not in source


def test_swap_table_exposes_keep_rejected_without_rates():
    """A2: 矩阵下半行（现行拒绝客群）只给笔数，坏率恒为空。"""

    frame = pd.DataFrame({"score": [5, 10, 20, 30, 40], "bad": [0, 1, 0, 1, 0]})
    result = swap_table(frame, "score", "bad", current_threshold=25, candidate_threshold=15)
    _assert_explained(result)
    rejected = result["keep_rejected"]
    assert set(rejected) == {"n", "bad_cnt", "bad_rate"}
    assert rejected["n"] == 2
    assert rejected["bad_cnt"] is None and rejected["bad_rate"] is None
    assert any("下半行" in note or "不在已放款样本内" in note for note in result["caveats"])
    # 现有三块的键与计算语义保持不变。
    assert result["keep_approved"]["n"] == 2 and result["keep_approved"]["bad_rate"] == 0.5
    assert result["swap_out"]["n"] == 0
    assert result["swap_in"]["n"] == 1 and result["swap_in"]["bad_rate"] == 0


def test_keep_rejected_stays_null_without_outcome_column():
    frame = pd.DataFrame({"score": [5, 10, 20, 30]})
    result = swap_table(frame, "score", "bad", current_threshold=25, candidate_threshold=15)
    assert result["keep_rejected"]["n"] == 2
    assert result["keep_rejected"]["bad_cnt"] is None
    assert result["keep_rejected"]["bad_rate"] is None
