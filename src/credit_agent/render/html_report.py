"""交付级 HTML 报告渲染器（仅版式，不含业务口径）。

设计约束（五条决定了所有样式选择）：

1. **任何内容都不得被裁掉**。宽表一律放进横向滚动容器，长字段名一律允许折行；
   不使用 ``overflow:hidden`` 去「修」溢出，那只会把内容切掉。
2. **一屏之内先给结论**。KPI 用自适应网格，窄屏自动降列，不做固定列数。
3. **打印与另存 PDF 要能看**。打印时展开滚动容器、去掉阴影与背景色，保留语义色。
4. **没有 JS 也要有可读内容**。脚本渲染的报告必须带 ``<noscript>`` 静态摘要。
5. **交付物要能维护和分享**。内联事件改事件委托，状态可写入 URL，数据可拆分/合成。

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
html{-webkit-text-size-adjust:100%;color-scheme:light}
.sr-only{position:absolute!important;width:1px;height:1px;padding:0;margin:-1px;
         overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
.skip-link{position:absolute;left:-9999px;top:0;z-index:9999;background:#fff;
           color:#2f5fd0;padding:8px 14px;border:1px solid #dfe5ec;border-radius:0 0 6px 0;
           font-size:13px}
.skip-link:focus{left:0}
:focus-visible{outline:2px solid #2f5fd0;outline-offset:2px}
.print-only{display:none}
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
@page{size:A4 landscape;margin:10mm}
@media print{
  body{background:#fff;font-size:11.5pt;-webkit-print-color-adjust:exact;print-color-adjust:exact}
  .wrap{max-width:none;padding:0}
  .no-print,.skip-link{display:none!important}
  .print-only{display:block!important}
  .tbl,.tw{overflow:visible!important;max-height:none!important}
  .tbl table,table{width:100%}
  thead th,td.lbl,th.lbl{position:static!important}
  .box,.kpi,.feat,.note,tr{break-inside:avoid;box-shadow:none}
  section{break-inside:auto}
  h2,h3{break-after:avoid}
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
        f'<th scope="col"{_cell_class(h, num=num, wide=wide, ident=ident, nowrap=nowrap)}>{esc(h)}</th>'
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
/* ===== 交付加固（打印 / 无障碍 / 交互视图） ===== */
.sr-only{position:absolute!important;width:1px;height:1px;padding:0;margin:-1px;
         overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
.skip-link{position:absolute;left:-9999px;top:0;z-index:9999;background:#fff;
           color:#2f5fd0;padding:8px 14px;border:1px solid #dfe5ec}
.skip-link:focus{left:0}
:focus-visible{outline:2px solid #2f5fd0;outline-offset:2px}
.print-only{display:none}
.tw-mx{overflow:auto;max-height:76vh}
@page{size:A4 landscape;margin:10mm}
@media print{
  body{background:#fff;font-size:11.5pt;-webkit-print-color-adjust:exact;print-color-adjust:exact}
  .no-print,.skip-link{display:none!important}
  .print-only{display:block!important}
  .print-title{font-size:16pt;font-weight:700;color:#2f5fd0;margin:0 0 10px}
  .tbl,.tw,.tw-mx{overflow:visible!important;max-height:none!important}
  table{width:100%}
  thead th,td.lbl,th.lbl{position:static!important}
  .box,.kpi,.feat,.note,.sec,tr{break-inside:avoid}
  h2,h3{break-after:avoid}
}
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
    if 'name="color-scheme"' not in content:
        content = content.replace(
            "<head>", '<head><meta name="color-scheme" content="light">', 1
        )
    if 'class="skip-link"' not in content:
        content = content.replace(
            "<body>", '<body><a class="skip-link" href="#main">跳到正文</a>', 1
        )
    content = wrap_tables(content)
    if extra_css and extra_css not in content:
        block = f"<style>{extra_css}</style>"
        content = content.replace("</head>", block + "</head>", 1) if "</head>" in content else block + content
    return content


_INTERACTION_MARK = '<script id="reportDeliveryHardening">'

_INTERACTION_JS = r"""
<script id="reportDeliveryHardening">
(function(){
  function readState(){
    var s={};
    (location.hash||'').replace(/^#/,'').split('&').forEach(function(kv){
      if(!kv) return; var p=kv.split('=');
      try{ s[p[0]]=decodeURIComponent(p[1]||''); }catch(e){}
    });
    return s;
  }
  var keys=[];
  try{ keys=(window.REPORT_STATE_KEYS||[]).slice(); }catch(e){}
  var writing=false;
  function writeState(){
    if(writing) return; writing=true;
    try{
      var state={}, i;
      if(typeof S!=='undefined'){
        if(keys.length){ for(i=0;i<keys.length;i++){ if(S[keys[i]]!=null) state[keys[i]]=S[keys[i]]; } }
        else { ['branch','dim','year','metric','balDim','sortKey','sortDir'].forEach(function(k){
          if(S[k]!=null) state[k]=S[k];
        }); }
      }
      var parts=Object.keys(state).map(function(k){return k+'='+encodeURIComponent(state[k]);});
      var next='#'+parts.join('&');
      if(next!==location.hash) history.replaceState(null,'',next);
    }catch(e){}
    writing=false;
  }
  function applyState(st){
    if(typeof S==='undefined') return;
    Object.keys(st).forEach(function(k){ if(S[k]!=null||st[k]) S[k]=st[k]; });
  }
  function a11y(){
    document.querySelectorAll('.tw').forEach(function(el){
      if(el.querySelector('table.mx')) el.classList.add('tw-mx');
    });
    document.querySelectorAll('.chip').forEach(function(el){
      if(el.hasAttribute('role')) return;
      el.setAttribute('role','button'); el.setAttribute('tabindex','0');
      el.setAttribute('aria-pressed',el.classList.contains('on')?'true':'false');
      el.addEventListener('keydown',function(e){
        if(e.key==='Enter'||e.key===' '){e.preventDefault();el.click();}
      });
    });
    document.querySelectorAll('th.sortable').forEach(function(th){
      if(!th.hasAttribute('tabindex')){
        th.setAttribute('tabindex','0'); th.setAttribute('role','button');
        th.addEventListener('keydown',function(e){
          if(e.key==='Enter'||e.key===' '){e.preventDefault();th.click();}
        });
      }
      th.setAttribute('aria-sort',th.classList.contains('act')
        ? (th.textContent.indexOf('▲')>=0?'ascending':'descending') : 'none');
    });
    document.querySelectorAll('.tw table,.tbl table').forEach(function(t){
      if(!t.querySelector('caption')){
        var host=t.closest('.box,.sec')||document;
        var head=host.querySelector('.box-h,.sec-h h2,h3');
        var cap=document.createElement('caption');
        cap.className='sr-only'; cap.textContent=head?head.textContent.trim():(document.title||'数据表');
        t.insertBefore(cap,t.firstChild);
      }
      t.querySelectorAll('thead th').forEach(function(th){
        if(!th.hasAttribute('scope')) th.setAttribute('scope','col');
      });
    });
    var cnt=document.getElementById('cnt');
    if(cnt&&!cnt.hasAttribute('aria-live')) cnt.setAttribute('aria-live','polite');
    var pt=document.getElementById('printTitle');
    if(pt){
      var on=document.querySelector('.chip.on');
      var asof=document.getElementById('asof');
      pt.textContent=document.title+(on?' · '+on.textContent.trim():'')+
        ' · '+(asof?asof.textContent:'');
    }
  }
  function wrap(name,after){
    if(typeof window[name]!=='function') return;
    var orig=window[name];
    window[name]=function(){ var r=orig.apply(this,arguments); if(after) after(); return r; };
  }
  ['setDim','setYear','setMetric','setBalDim','toggleBalZero','toggleBalActive',
   'toggleVtActive','toggleC3Active','sortStaff'].forEach(function(n){ wrap(n,writeState); });
  wrap('select',function(){ a11y(); writeState(); });
  wrap('render',a11y);
  window.addEventListener('hashchange',function(){
    if(writing) return;
    applyState(readState());
    if(typeof render==='function') render();
  });
  document.addEventListener('click',function(e){
    var target=e.target;
    if(!target||!target.closest) return;
    var c=target.closest('[data-branch]');
    if(c&&typeof select==='function') select(c.getAttribute('data-branch'));
    var el=target.closest('[data-call]');
    if(el&&typeof window[el.getAttribute('data-call')]==='function'){
      window[el.getAttribute('data-call')](
        el.hasAttribute('data-arg')?el.getAttribute('data-arg'):undefined);
    }
  });
  document.addEventListener('change',function(e){
    var el=e.target;
    if(!el||!el.hasAttribute||!el.hasAttribute('data-call')) return;
    if(typeof window[el.getAttribute('data-call')]==='function'){
      window[el.getAttribute('data-call')](el.value);
    }
  });
  document.addEventListener('DOMContentLoaded',function(){
    applyState(readState());
    var pb=document.getElementById('printBtn');
    if(pb) pb.addEventListener('click',function(){window.print();});
    var cb=document.getElementById('copyLinkBtn');
    if(cb) cb.addEventListener('click',function(){
      var url=location.href;
      if(navigator.clipboard&&navigator.clipboard.writeText){
        navigator.clipboard.writeText(url).then(function(){
          cb.textContent='已复制'; setTimeout(function(){cb.textContent='复制视图链接';},1600);
        });
      }else{ prompt('复制下面的链接：',url); }
    });
  });
  applyState(readState());
  if(typeof render==='function') render();
})();
</script>
"""


def harden_interactive_report(content: str, static_summary: str = "") -> str:
    """加固「数据内联 + JS 渲染正文」的单文件报告，保留原数据与交互。

    ``static_summary`` 是可信的 HTML 片段，会被原样写进 ``<noscript>``；
    传入纯文本前请先用 :func:`esc` 转义。
    """

    if _INTERACTION_MARK in content:
        return content
    if 'name="color-scheme"' not in content:
        content = content.replace("<head>", '<head><meta name="color-scheme" content="light">', 1)
    if 'class="skip-link"' not in content:
        content = content.replace("<body>", '<body><a class="skip-link" href="#main">跳到正文</a>', 1)
    if 'id="printTitle"' not in content:
        content = content.replace(
            "<body>",
            '<body><div class="print-only print-title" id="printTitle"></div>', 1,
        )
    if "<noscript" not in content:
        summary = static_summary or (
            '<p>交互式明细需要启用 JavaScript 才能渲染。请启用后刷新，'
            '或向报告提供方索取 PDF 版本。</p>'
        )
        content = content.replace(
            "<body>", f"<body><noscript>{summary}</noscript>", 1
        )
    if HARDEN_CSS not in content:
        content = content.replace("</head>", f"<style>{HARDEN_CSS}</style></head>", 1)
    content, _ = re.subn(
        r"onclick=\"select\('([^']*)'\)\"", r'data-branch="\1"', content
    )
    for pattern, replacement in (
        (r"onclick=\"([A-Za-z_$][\w$]*)\('([^']*)'\)\"", r'data-call="\1" data-arg="\2"'),
        (r'onclick="([A-Za-z_$][\w$]*)\(\)"', r'data-call="\1"'),
        (r'onchange="([A-Za-z_$][\w$]*)\(this\.value\)"', r'data-call="\1"'),
    ):
        content = re.sub(pattern, replacement, content)
    content = re.sub(
        r'(<div class="(?:kpis|grid3x)[^"]*)"\s+style="[^"]*grid-template-columns[^"]*"',
        r'\1"', content,
    )
    if "</body>" in content:
        content = content.replace("</body>", _INTERACTION_JS + "\n</body>", 1)
    return content


def audit_html(content: str) -> list[tuple[str, str]]:
    """静态交付审计，返回 ``(PASS|WARN|FAIL, 检查项)`` 列表。不改文档。"""

    rows: list[tuple[str, str]] = []

    def add(level: str, message: str) -> None:
        rows.append((level, message))

    def has(pattern: str, flags: int = 0) -> bool:
        return re.search(pattern, content, flags) is not None

    add("PASS" if has(r"<!DOCTYPE html>", re.I) else "FAIL", "DOCTYPE")
    add("PASS" if has(r'<meta[^>]*charset', re.I) else "FAIL", "charset meta")
    add("PASS" if has(r'<meta[^>]*viewport', re.I) else "FAIL", "viewport meta")
    add("PASS" if has(r'name="color-scheme"') else "WARN", "color-scheme meta")
    external = re.findall(
        r'<link\b|<script[^>]*\bsrc=|\@import|url\(\s*["\']?https?://', content, re.I
    )
    add("PASS" if not external else "WARN", "外部依赖（%d 处）" % len(external))
    add("PASS" if has(r"\@media\s+print") else "FAIL", "@media print")
    add("PASS" if has(r"print-color-adjust|print-color") else "WARN", "打印颜色保真")
    add("PASS" if has(r"\@page\s*\{") else "WARN", "@page 纸张设置")
    add("PASS" if has(r"<noscript") else "FAIL", "无 JS 兜底")
    add("PASS" if has(r"location\.hash|history\.replaceState") else "WARN", "URL 深链状态")
    add("PASS" if has(r"aria-|role=|tabindex") else "FAIL", "无障碍属性")
    add("PASS" if has(r"<caption") else "WARN", "表格 caption")
    add("PASS" if has(r"aria-pressed|aria-sort") else "WARN", "控件状态语义")
    onclick = len(re.findall(r"\bonclick\s*=", content, re.I))
    add("PASS" if onclick == 0 else "WARN", "内联事件 onclick（%d 处）" % onclick)
    grids = len(re.findall(r'style="[^"]*grid-template-columns', content, re.I))
    add("PASS" if grids == 0 else "WARN", "内联网格列数（%d 处）" % grids)
    longest = max((len(line) for line in content.splitlines()), default=0)
    add("PASS" if longest < 100000 else "WARN", "最大单行长度（%d 字符）" % longest)
    tables = len(re.findall(r"<table\b", content, re.I))
    wrappers = len(re.findall(r'class="[^"]*\b(tw|tbl)\b', content, re.I))
    add("PASS" if tables <= wrappers else "WARN", "宽表滚动容器（%d 表 / %d 容器）" % (tables, wrappers))
    add("FAIL" if has(r"table\s*\{[^}]*overflow:\s*hidden", re.I | re.S) else "PASS",
        "表格 overflow:hidden 裁切")
    add("PASS" if has(r"repeat\(\s*auto-fit") else "WARN", "auto-fit 自适应网格")
    add("PASS" if has(r"focus-visible") else "WARN", "键盘焦点可见样式")
    return rows


_DATA_PATTERN = re.compile(r"(const\s+D\s*=\s*)(\{.*?\})(\s*;\s*\n)", re.S)
_DATA_PLACEHOLDER = "/*__REPORT_DATA__*/ null"


def split_inline_data(content: str) -> tuple[str, str]:
    """拆出 ``const D = {...};``：返回 ``(模板, 纯 JSON 数据)``。"""

    match = _DATA_PATTERN.search(content)
    if not match:
        raise ValueError("未找到 const D = {...}; 数据块")
    template = (
        content[:match.start()] + match.group(1) + _DATA_PLACEHOLDER
        + match.group(3) + content[match.end():]
    )
    return template, match.group(2)


def inline_data(template: str, data: str) -> str:
    """把纯 JSON 数据写回模板占位符，合成自包含单文件。"""

    if _DATA_PLACEHOLDER not in template:
        raise ValueError("模板中未找到数据占位符")
    return template.replace(_DATA_PLACEHOLDER, data.strip(), 1)


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
    nojs = (
        '<noscript><p>本页含交互脚本。正文仍需可读：请启用 JavaScript 后刷新，'
        '或向报告提供方索取 PDF 版本。</p></noscript>'
        if "<script" in body.lower() else ""
    )
    return (
        f'<!DOCTYPE html><html lang="{lang}"><head>{_META}<title>{esc(title)}</title>'
        f"<style>{BASE_CSS}{extra_css}</style></head><body>"
        f'<a class="skip-link" href="#main">跳到正文</a>{nojs}'
        f'<div class="wrap"><header><h1>{esc(title)}</h1>'
        f'<div class="meta">{subtitle}</div></header><main id="main">{body}</main>{footer_html}</div>'
        f"</body></html>"
    )
