"""交付报告渲染层的版式约束（只测格式，不测业务）。"""

from __future__ import annotations

import re

from credit_agent.render import (
    BASE_CSS,
    HARDEN_CSS,
    bar_cell,
    callout,
    harden_html,
    page,
    table,
    tag,
    wrap_tables,
)


def test_table_always_ships_a_scroll_container():
    html = table(["特征", "IV"], [["f1", "0.42"]], num={"IV"}, ident={"特征"})
    assert html.startswith('<div class="tbl"><table>')
    assert html.endswith("</table></div>")
    assert 'class="num"' in html and 'class="c-id"' in html


def test_wrap_tables_wraps_raw_and_skips_wrapped():
    raw = "<p>a</p><table><tbody><tr><td>1</td></tr></tbody></table>"
    out = wrap_tables(raw)
    assert out.count('<div class="tbl">') == 1
    assert out.count("</table></div>") == 1
    assert wrap_tables(out) == out  # 幂等

    mixed = table(["a"], [["1"]]) + "<table><tr><td>2</td></tr></table>"
    out2 = wrap_tables(mixed)
    assert out2.count('<div class="tbl">') == 2
    assert out2.count("</table></div>") == 2


def test_page_carries_viewport_and_wraps_body_tables():
    html = page("标题", "副标题 <code>run_x</code>", "<table><tr><td>1</td></tr></table>", footer="脚注")
    assert 'name="viewport"' in html
    assert 'name="color-scheme" content="light"' in html
    assert '<div class="tbl"><table>' in html
    assert "<footer>脚注</footer>" in html
    assert BASE_CSS in html


def test_base_css_never_clips_tables():
    # 红线：表格不得靠 overflow:hidden 修溢出
    table_rules = [line for line in BASE_CSS.splitlines() if line.startswith(".tbl")]
    assert table_rules and all("overflow:hidden" not in line for line in table_rules)
    assert "overflow-x:auto" in BASE_CSS
    assert re.search(r"\.tbl table\{width:100%", BASE_CSS)
    assert "@media print" in BASE_CSS


def test_harden_html_is_idempotent_and_keeps_original_style():
    legacy = (
        '<!DOCTYPE html><html><head><style>.grid{grid-template-columns:repeat(2,1fr)}</style></head>'
        "<body><table><tr><td>x</td></tr></table></body></html>"
    )
    once = harden_html(legacy)
    assert once.count('<div class="tbl">') == 1
    assert 'name="viewport"' in once
    assert HARDEN_CSS in once
    assert ".grid{grid-template-columns:repeat(2,1fr)}" in once  # 原样式保留
    twice = harden_html(once)
    assert twice.count('<div class="tbl">') == 1
    assert twice.count('name="viewport"') == 1


def test_components_escape_and_defaults():
    assert tag("已签", "ok") == '<span class="tag t-ok">已签</span>'
    assert tag("x", "unknown-kind").startswith('<span class="tag t-info">')
    assert callout("warn", "结论", "<p>依据</p>", items=["一", "二"]).count("<li>") == 2
    assert table(["a"], [])  # 空表也要有结构
    assert "无数据" in table(["a"], [])
    assert bar_cell(0.2, 0.4).count("width:50.0%") == 1
    assert bar_cell(0.2, 0.0).count("width:0.0%") == 1
