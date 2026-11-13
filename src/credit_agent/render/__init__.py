"""Render layer: 把工具与账本的聚合结果渲染成可交付的 HTML 报告。

只负责版式，不含任何业务口径。页面骨架、组件与样式规范见
``skills/deliverable-html-report``；本模块是该规范的可执行实现，
交付脚本应直接复用，避免每个报告各写一套 CSS 又各踩一次溢出。
"""

from .html_report import (
    BASE_CSS,
    HARDEN_CSS,
    harden_html,
    bar_cell,
    callout,
    esc,
    feature_card,
    kpi_grid,
    page,
    section,
    table,
    tag,
    wrap_tables,
)

__all__ = [
    "BASE_CSS",
    "HARDEN_CSS",
    "harden_html",
    "bar_cell",
    "callout",
    "esc",
    "feature_card",
    "kpi_grid",
    "page",
    "section",
    "table",
    "tag",
    "wrap_tables",
]
