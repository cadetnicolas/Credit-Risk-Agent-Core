"""交付级 HTML 报告渲染器（仅版式，不含业务口径）。

设计约束（这三条决定了所有样式选择）：

1. **任何内容都不得被裁掉**。宽表一律放进横向滚动容器，长字段名一律允许折行；
   不使用 ``overflow:hidden`` 去「修」溢出，那只会把内容切掉。
2. **一屏之内先给结论**。KPI 用自适应网格，窄屏自动降列，不做固定列数。
3. **打印与另存 PDF 要能看**。打印时展开滚动容器、去掉阴影与背景色。

用法::

    from credit_agent.render import page, kpi_grid, section, table, callout, wrap_tables

    body = section("一、概览", kpi_grid([("0.31", "OOT KS", "AUC .66")]))
    html = page("标题", "副标题", body, footer="数据源…")
"""

from __future__ import annotations

import html as _html
import re
from typing import Any, Iterable, Sequence

# --------------------------------------------------------------------------- #
# 设计令牌
# --------------------------------------------------------------------------- #
COLORS = {
    "ink": "#1d2733",
    "sub": "#5d6b7a",
    "line": "#dfe5ec",
    "bg": "#f6f8fa",
    "blue": "#2f5fd0",
    "green": "#1f7a4d",
    "amber": "#a8620a",
    "red": "#b3261e",
}

TAG_KINDS = {"ok", "warn", "bad", "info", "gray"}

BASE_CSS = """
*,*::before,*::after{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB",
     "Microsoft YaHei",sans-serif;color:#1d2733;background:#f6f8fa;line-height:1.62;
     font-size:14px;-webkit-font-smoothing:antialiased}
.wrap{max-width:1080px;margin:0 auto;padding:34px clamp(14px,4vw,26px) 70px}
header{border-bottom:2px solid #1d2733;padding-bottom:16px;margin-bottom:24px}
h1{font-size:clamp(20px,3.2vw,25px);letter-spacing:-.3px;font-weight:700;line-height:1.35}
.meta{color:#5d6b7a;font-size:12.5px;margin-top:6px;overflow-wrap:anywhere}
section{margin-bottom:34px}
h2{font-size:clamp(16px,2.4vw,18px);font-weight:700;margin:0 0 14px;padding-left:11px;
   border-left:4px solid #2f5fd0;line-height:1.4}
h3.sub{font-size:14.5px;font-weight:600;margin:20px 0 9px;color:#2c3a49}
p{color:#33404d;margin:0 0 9px;overflow-wrap:anywhere}
.lead{color:#5d6b7a;font-size:13px;margin-bottom:12px}
.note{font-size:12px;color:#5d6b7a;margin-top:8px}

/* KPI：自适应列数，窄屏自动降列，不做固定 4 列 */
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));
       gap:11px;margin:20px 0 30px}
.kpi{background:#fff;border:1px solid #dfe5ec;border-radius:9px;padding:14px 15px;min-width:0}
.kpi .v{font-size:clamp(20px,3vw,25px);font-weight:700;letter-spacing:-.5px;
        font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.kpi .l{font-size:11.5px;color:#5d6b7a;margin-top:3px}
.kpi .s{font-size:11px;color:#8b98a6;margin-top:5px;overflow-wrap:anywhere}

/* 表格：滚动容器兜住宽度，绝不用 overflow:hidden 切内容 */
.tbl{width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch;
     border:1px solid #dfe5ec;border-radius:8px;background:#fff;margin-bottom:12px}
/* 表格先按容器宽度自适配（文字列折行），实在放不下才在容器内横向滚动 */
.tbl table{width:100%;border-collapse:collapse;background:#fff;font-size:13px}
.tbl::-webkit-scrollbar{height:9px}
.tbl::-webkit-scrollbar-thumb{background:#c9d4e0;border-radius:5px}
th{background:#eef2f7;text-align:left;padding:9px 12px;font-weight:600;color:#2c3a49;
   font-size:12.5px;border-bottom:1px solid #dfe5ec;white-space:nowrap;vertical-align:bottom}
/* 列多的表：绝不能用 overflow-wrap:anywhere —— 它会让浏览器在计算最小宽度时
   认为每列可以窄到 1 个汉字，于是中文被逐字换行、竖着排成一列。
   break-word 只在真放不下时断，列宽保住下限；放不下就让 .tbl 横向滚动。 */
td{padding:8px 12px;border-bottom:1px solid #eef2f7;vertical-align:top;
   word-break:normal;overflow-wrap:break-word}
.tbl th{min-width:4.5em}
.tbl td{min-width:3.2em}
tr:last-child td{border-bottom:none}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
td.b{font-weight:700}
td.c-id{min-width:170px;max-width:340px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
        font-size:11.8px;color:#243244}
td.c-wide,th.c-wide{min-width:220px}
td.c-nowrap{white-space:nowrap}
caption{caption-side:top;text-align:left;padding:9px 12px;font-size:12px;color:#5d6b7a;
        background:#fff;border-bottom:1px solid #eef2f7}

code{background:#eef2f7;padding:1.5px 5px;border-radius:4px;font-size:11.5px;
     font-family:ui-monospace,SFMono-Regular,Menlo,monospace;color:#1d2733;
     overflow-wrap:anywhere;word-break:break-all}
.tag{display:inline-block;padding:1px 7px;border-radius:9px;font-size:11px;line-height:1.7;
     white-space:nowrap}
.t-ok{background:#e4f2ea;color:#1f7a4d}
.t-warn{background:#fdf0dc;color:#a8620a}
.t-bad{background:#fce8e6;color:#b3261e}
.t-info{background:#e8eef9;color:#2f5fd0}
.t-gray{background:#eef2f7;color:#5d6b7a}

.box{background:#fff;border:1px solid #dfe5ec;border-radius:9px;padding:15px 17px;margin:11px 0}
.box.warn{border-left:3px solid #a8620a;background:#fffdf8}
.box.ok{border-left:3px solid #1f7a4d;background:#fafffc}
.box.risk{border-left:3px solid #b3261e;background:#fffafa}
.box h3{font-size:13.5px;font-weight:700;margin:0 0 7px;color:#1d2733}
.box p{font-size:12.8px;margin-bottom:6px}
.box ul,.box ol{margin:6px 0 4px 18px;padding:0}
.box li{font-size:12.8px;color:#3d4550;margin-bottom:5px;overflow-wrap:anywhere}

/* 特征卡片：表头换行不撑破，坏率条固定宽度 */
.feat{background:#fff;border:1px solid #dfe5ec;border-radius:9px;margin-bottom:13px;
      overflow:hidden}
.feat .hd{padding:10px 14px;background:#f7f9fb;border-bottom:1px solid #e6ecf2;
          display:flex;flex-wrap:wrap;gap:8px;align-items:baseline}
.feat .nm{font-weight:700;font-size:13px;color:#1d2733;overflow-wrap:anywhere;min-width:0}
.feat .cn{color:#5d6b7a;font-size:12px;overflow-wrap:anywhere;min-width:0}
.feat .tbl{border:none;border-radius:0;margin-bottom:0}
.feat table{font-size:12px}
.feat th{background:#fff;border-bottom:1px solid #eef2f7;padding:6px 10px}
.feat td{padding:5px 10px}
.bar{height:9px;background:#e8eef9;border-radius:3px;position:relative;width:110px;min-width:70px}
.bar i{position:absolute;left:0;top:0;bottom:0;background:#2f5fd0;border-radius:3px}

footer{margin-top:40px;padding-top:14px;border-top:1px solid #dfe5ec;
       color:#8b98a6;font-size:11.5px;overflow-wrap:anywhere}

@media (max-width:640px){
  .wrap{padding:22px 14px 54px}
  .kpis{grid-template-columns:repeat(auto-fit,minmax(140px,1fr))}
  .box{padding:13px 14px}
  .tbl table{font-size:12px}
  th,td{padding:6px 8px}
  td.c-id{min-width:130px;max-width:220px;font-size:11px}
}
@media print{
  body{background:#fff;font-size:11.5pt}
  .wrap{max-width:none;padding:0}
  .tbl{overflow:visible;border-color:#bbb}
  .tbl table{width:100%}
  .box,.kpi,.feat{break-inside:avoid;box-shadow:none}
  section{break-inside:auto}
  h2{break-after:avoid}
}
"""

_META = ('<meta charset="utf-8">'
         '<meta name="viewport" content="width=device-width,initial-scale=1">'
         '<meta name="color-scheme" content="light">')


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def esc(value: Any) -> str:
    """转义文本，避免字段名里的 ``<`` ``&`` 破坏版式。"""

    return _html.escape("" if value is None else str(value), quote=False)


def tag(text: str, kind: str = "info") -> str:
    kind = kind if kind in TAG_KINDS else "info"
    return f'<span class="tag t-{kind}">{esc(text)}</span>'


def _cell_class(column: str, *, num: Iterable[str], wide: Iterable[str],
                ident: Iterable[str], nowrap: Iterable[str] = ()) -> str:
    classes = []
    if column in num:
        classes.append("num")
    if column in wide:
        classes.append("c-wide")
    if column in ident:
        classes.append("c-id")
    if column in nowrap:
        classes.append("c-nowrap")
    return f' class="{" ".join(classes)}"' if classes else ""


def _short_column(values: Sequence[Any]) -> bool:
    """True when every non-empty cell is a short single token.

    Dates, codes and 是/否 flags read badly when wrapped — ``2025-02-03`` split
    across two lines looks like a data problem. Long prose still wraps.
    """
    seen = False
    for value in values:
        if value is None or value == "":
            continue
        seen = True
        if len(str(value).strip()) > 10:
            return False
    return seen


# --------------------------------------------------------------------------- #
# 组件
# --------------------------------------------------------------------------- #
def table(
    headers: Sequence[str],
    rows: Sequence[Sequence[Any]],
    *,
    num: Iterable[str] = (),
    wide: Iterable[str] = (),
    ident: Iterable[str] = (),
    caption: str = "",
    empty: str = "无数据",
) -> str:
    """标准表格：外层滚动容器 + 可换行长字段，宽表不会把内容顶出去。

    ``headers`` 与每行按位置对应；``num`` / ``wide`` / ``ident`` 用**表头名**
    指定列样式（右对齐等宽 / 最小宽度 / 等宽长字段）。
    """

    num, wide, ident = set(num), set(wide), set(ident)
    # Short columns (dates, codes, flags) read badly when wrapped: keep them on
    # one line and let the container scroll instead.
    nowrap = {
        header for idx, header in enumerate(headers)
        if header not in wide and header not in num and header not in ident
        and _short_column([row[idx] if idx < len(row) else None for row in rows])
    }
    head = "".join(
        f"<th{_cell_class(h, num=num, wide=wide, ident=ident, nowrap=nowrap)}>{esc(h)}</th>"
        for h in headers
    )
    body_rows = []
    for row in rows:
        cells = []
        for header, value in zip(headers, row):
            cls = _cell_class(header, num=num, wide=wide, ident=ident, nowrap=nowrap)
            cells.append(f"<td{cls}>{value if value is not None else '—'}</td>")
        body_rows.append(f"<tr>{''.join(cells)}</tr>")
    if not body_rows:
        body_rows.append(f'<tr><td colspan="{max(len(headers), 1)}">{esc(empty)}</td></tr>')
    cap = f"<caption>{esc(caption)}</caption>" if caption else ""
    return (
        '<div class="tbl"><table>'
        f"{cap}<thead><tr>{head}</tr></thead><tbody>{''.join(body_rows)}</tbody></table></div>"
    )


def kpi_grid(items: Sequence[Sequence[Any]]) -> str:
    """KPI 卡片组。每项 ``(数值, 标签, 说明[, 颜色])``。"""

    cards = []
    for item in items:
        value, label, sub = item[0], item[1], item[2] if len(item) > 2 else ""
        color = item[3] if len(item) > 3 and item[3] else ""
        style = f' style="color:{color}"' if color else ""
        cards.append(
            f'<div class="kpi"><div class="v"{style}>{value}</div>'
            f'<div class="l">{esc(label)}</div><div class="s">{sub}</div></div>'
        )
    return f'<div class="kpis">{"".join(cards)}</div>'


def callout(kind: str, title: str, body: str, *, items: Sequence[str] = ()) -> str:
    """提示框。``kind`` ∈ ``info`` / ``ok`` / ``warn`` / ``risk``。"""

    kind = kind if kind in {"info", "ok", "warn", "risk"} else "info"
    cls = "box" if kind == "info" else f"box {kind}"
    listing = ""
    if items:
        listing = "<ul>" + "".join(f"<li>{item}</li>" for item in items) + "</ul>"
    return f'<div class="{cls}"><h3>{esc(title)}</h3>{body}{listing}</div>'


def section(title: str, body: str, *, anchor: str = "") -> str:
    id_attr = f' id="{esc(anchor)}"' if anchor else ""
    return f"<section{id_attr}><h2>{esc(title)}</h2>{body}</section>"


def feature_card(
    name: str,
    meta: Sequence[str] = (),
    *,
    headers: Sequence[str] = (),
    rows: Sequence[Sequence[Any]] = (),
    num: Iterable[str] = (),
    wide: Iterable[str] = (),
    body: str = "",
) -> str:
    """单特征卡片：标题行放特征名与标签，下面是该特征的分箱表。"""

    chips = "".join(meta)
    inner = body or (table(headers, rows, num=num, wide=wide) if headers else "")
    return (
        f'<div class="feat"><div class="hd"><span class="nm">{esc(name)}</span>{chips}</div>'
        f"{inner}</div>"
    )


def bar_cell(rate: float, maximum: float, *, width: str = "110px") -> str:
    ratio = 0.0 if not maximum else max(1.0, min(100.0, rate / maximum * 100))
    return f'<div class="bar" style="width:{width}"><i style="width:{ratio:.1f}%"></i></div>'


# --------------------------------------------------------------------------- #
# 整页
# --------------------------------------------------------------------------- #
_WRAPPED_MARK = '<div class="tbl">'
_TABLE_TOKEN = re.compile(r"<table(\s[^>]*)?>|</table>", re.IGNORECASE)


def wrap_tables(content: str) -> str:
    """给所有**裸** ``<table>`` 套上横向滚动容器（幂等）。

    历史报告脚本大量直接拼 ``<table>``，漏一个就会在窄屏被裁掉；统一在这里兜住。
    已经用 :func:`table` / :func:`feature_card` 生成、自带容器的表格会按栈跳过，
    不会出现容器套容器。
    """

    out = []
    stack: list[bool] = []
    pos = 0
    for match in _TABLE_TOKEN.finditer(content):
        out.append(content[pos:match.start()])
        token = match.group(0)
        if token.startswith("</"):
            wrapped = stack.pop() if stack else False
            out.append("</table></div>" if wrapped else "</table>")
        else:
            already = content[max(0, match.start() - len(_WRAPPED_MARK)):match.start()] == _WRAPPED_MARK
            stack.append(not already)
            out.append(token if already else f"{_WRAPPED_MARK}{token}")
        pos = match.end()
    out.append(content[pos:])
    return "".join(out)


HARDEN_CSS = """
/* ===== 溢出加固（追加覆盖，不改原有视觉风格） ===== */
.wrap{max-width:min(1240px,100%)}
.grid{grid-template-columns:repeat(auto-fit,minmax(320px,1fr))!important}
.tbl{width:100%;overflow-x:auto;-webkit-overflow-scrolling:touch;border-radius:8px}
.tbl>table{width:100%;border-collapse:collapse;margin:0}
/* 表头允许折行，表才能缩到容器宽度；数字列仍不折行。
   注意：这里不能用 overflow-wrap:anywhere —— 多列表格会被压到每列一个字宽，
   中文随即逐字竖排。break-word 保住列宽下限，超宽交给 .tbl 横向滚动。 */
.tbl th{white-space:normal;min-width:4.5em}
.tbl td,.tbl th{overflow-wrap:break-word;word-break:normal}
.tbl td{min-width:3.2em}
.tbl .num{white-space:nowrap}
.tbl table{min-width:100%}
"""


def harden_html(content: str, extra_css: str = HARDEN_CSS) -> str:
    """给一份已有 HTML 加固溢出：补 viewport、包表格、追加覆盖样式。

    用于历史上直接拼字符串的报告脚本——不改它们的内容与风格，只在最后一步
    把「宽表把页面顶出去」这条路堵死。
    """

    if 'name="viewport"' not in content:
        content = content.replace(
            "<head>",
            '<head><meta name="viewport" content="width=device-width,initial-scale=1">',
            1,
        )
    content = wrap_tables(content)
    if extra_css:
        block = f"<style>{extra_css}</style>"
        content = content.replace("</head>", block + "</head>", 1) if "</head>" in content else block + content
    return content


def page(    title: str,
    subtitle: str = "",
    body: str = "",
    *,
    footer: str = "",
    lang: str = "zh-CN",
    extra_css: str = "",
) -> str:
    """完整页面。``body`` 由 ``section()`` 拼装；``wrap_tables`` 会自动兜住裸表。"""

    body = wrap_tables(body)
    footer_html = f"<footer>{footer}</footer>" if footer else ""
    return (
        f'<!DOCTYPE html><html lang="{lang}"><head>{_META}<title>{esc(title)}</title>'
        f"<style>{BASE_CSS}{extra_css}</style></head><body>"
        f'<div class="wrap"><header><h1>{esc(title)}</h1>'
        f'<div class="meta">{subtitle}</div></header>{body}{footer_html}</div></body></html>'
    )
