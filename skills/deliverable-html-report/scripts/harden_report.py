#!/usr/bin/env python3
"""单文件 HTML 报告交付加固器：注入打印、响应式、无障碍、深链与兜底。只改格式层。"""
import argparse
import pathlib
import re
import sys

CSS = r"""
/* REPORT-HARDEN-V1 */
:root{color-scheme:light}
.sr-only{position:absolute!important;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
.skip-link{position:absolute;left:-9999px;top:0;z-index:9999;background:#fff;color:#1f3a93;padding:8px 14px;border:1px solid #dfe5ef;border-radius:0 0 6px 0;font-size:13px}
.skip-link:focus{left:0}
:focus-visible{outline:2px solid #1f3a93;outline-offset:2px}
.print-only{display:none}
.kpis{grid-template-columns:repeat(auto-fit,minmax(170px,1fr))!important}
.grid3x{display:grid;gap:14px;margin-bottom:16px;grid-template-columns:repeat(auto-fit,minmax(260px,1fr))!important}
.tw-mx{overflow:auto;max-height:76vh}
.mini,.scrollhint{font-size:12px;color:#5b6572}
.mx-cell .b,.lft{font-size:11px}
table.mx{font-size:12px}
@media (max-width:640px){
  .mini,.scrollhint{font-size:11.5px}
  th,td{padding:6px 8px}
}
@page{size:A4 landscape;margin:10mm}
@media print{
  body{background:#fff!important;font-size:11.5pt;-webkit-print-color-adjust:exact;print-color-adjust:exact}
  .switch,.toolbar,.bal-toolbar,.no-print,.skip-link{display:none!important}
  .print-only{display:block!important}
  .print-title{font-size:16pt;font-weight:700;color:#1f3a93;margin:0 0 10px}
  .tw,.tw-mx,.tbl{overflow:visible!important;max-height:none!important}
  table{width:100%}
  thead th,td.lbl,th.lbl{position:static!important}
  .box,.kpi,.note,.sec,tr{break-inside:avoid}
  h2,h3{break-after:avoid}
  header{box-shadow:none}
}
"""

BODY_HTML = """<a class="skip-link" href="#main">跳到正文</a>
<div class="print-only print-title" id="printTitle"></div>
<noscript><div style="padding:18px 22px;background:#fdf3e2;border-left:3px solid #b9770e;color:#6b5215;font-size:13px;line-height:1.8">
本报告为交互式 HTML，需要启用 JavaScript 才能渲染正文。请在浏览器设置中启用 JavaScript 后刷新页面；如需纸质版，请联系报告提供方获取 PDF。
</div></noscript>
"""

BUTTONS = """<button type="button" class="tbtn no-print" id="copyLinkBtn" title="复制可还原当前视图的链接">复制视图链接</button>
<button type="button" class="tbtn no-print" id="printBtn" title="打印或另存为 PDF">打印 / PDF</button>
"""

JS = r"""
<script id="reportHardening">
(function(){
  function readState(){
    var s={};
    (location.hash||'').replace(/^#/,'').split('&').forEach(function(kv){
      if(!kv) return; var p=kv.split('='); 
      try{ s[p[0]]=decodeURIComponent(p[1]||''); }catch(e){}
    });
    return s;
  }
  var __writing=false;
  function writeState(){
    if(__writing) return; __writing=true;
    var st=readState(), next={};
    try{
      if(typeof S!=='undefined'){
        ['branch','dim','year','metric','balDim','sortKey','sortDir'].forEach(function(k){
          if(S[k]!=null) next[k]=S[k];
        });
      }
      var parts=Object.keys(next).map(function(k){return k+'='+encodeURIComponent(next[k]);});
      var h='#'+parts.join('&');
      if(h!==location.hash) history.replaceState(null,'',h);
    }catch(e){}
    __writing=false;
  }
  function applyState(st){
    try{
      if(typeof S==='undefined') return;
      if(st.branch && typeof B!=='undefined' && B[st.branch]) S.branch=st.branch;
      if(st.dim) S.dim=st.dim;
      if(st.year) S.year=st.year;
      if(st.metric) S.metric=st.metric;
      if(st.balDim) S.balDim=st.balDim;
      if(st.sortKey) S.sortKey=st.sortKey;
      if(st.sortDir) S.sortDir=st.sortDir;
    }catch(e){}
  }
  function a11y(){
    try{
      document.querySelectorAll('.tw').forEach(function(el){
        if(el.querySelector('table.mx')) el.classList.add('tw-mx');
      });
      document.querySelectorAll('.chip').forEach(function(el){
        if(!el.hasAttribute('role')){
          el.setAttribute('role','button'); el.setAttribute('tabindex','0');
          el.setAttribute('aria-pressed',el.classList.contains('on')?'true':'false');
          el.addEventListener('keydown',function(e){
            if(e.key==='Enter'||e.key===' '){e.preventDefault();el.click();}
          });
        }
      });
      document.querySelectorAll('th.sortable').forEach(function(th){
        if(!th.hasAttribute('tabindex')){
          th.setAttribute('tabindex','0'); th.setAttribute('role','button');
          th.addEventListener('keydown',function(e){
            if(e.key==='Enter'||e.key===' '){e.preventDefault();th.click();}
          });
        }
        var sort='none';
        if(th.classList.contains('act')) sort=(th.textContent.indexOf('▲')>=0?'ascending':'descending');
        th.setAttribute('aria-sort',sort);
      });
      document.querySelectorAll('.tw table,.tbl table').forEach(function(t){
        if(!t.querySelector('caption')){
          var host=t.closest('.box,.sec')||document;
          var head=host.querySelector('.box-h,.sec-h h2,h3');
          var cap=document.createElement('caption');
          cap.className='sr-only'; cap.textContent=head?head.textContent.trim():(document.title||'数据表');
          t.insertBefore(cap,t.firstChild);
        }
        t.querySelectorAll('thead th').forEach(function(th){ if(!th.hasAttribute('scope')) th.setAttribute('scope','col'); });
      });
      var cnt=document.getElementById('cnt'); if(cnt&&!cnt.hasAttribute('aria-live')) cnt.setAttribute('aria-live','polite');
      var pt=document.getElementById('printTitle');
      if(pt){
        var on=document.querySelector('.chip.on');
        pt.textContent=document.title+(on?' · '+on.textContent.trim():'')+' · '+((document.getElementById('asof')||{}).textContent||'');
      }
    }catch(e){}
  }
  function wrap(name,after){
    try{
      if(typeof window[name]!=='function') return;
      var orig=window[name];
      window[name]=function(){ var r=orig.apply(this,arguments); if(after) after(); return r; };
    }catch(e){}
  }
  ['setDim','setYear','setMetric','setBalDim','toggleBalZero','toggleBalActive','toggleVtActive','toggleC3Active','sortStaff'].forEach(function(n){ wrap(n,writeState); });
  wrap('select',function(){ a11y(); writeState(); });
  wrap('render',a11y);
  window.addEventListener('hashchange',function(){
    if(__writing) return;
    applyState(readState());
    try{ if(typeof render==='function') render(); }catch(e){}
  });
  document.addEventListener('click',function(e){
    var c=e.target.closest&&e.target.closest('[data-branch]');
    if(c&&typeof select==='function') select(c.getAttribute('data-branch'));
    var el=e.target.closest&&e.target.closest('[data-call]');
    if(!el) return;
    var fn=el.getAttribute('data-call');
    if(typeof window[fn]!=='function') return;
    window[fn](el.hasAttribute('data-arg')?el.getAttribute('data-arg'):undefined);
  });
  document.addEventListener('change',function(e){
    var el=e.target;
    if(!el.hasAttribute||!el.hasAttribute('data-call')) return;
    var fn=el.getAttribute('data-call');
    if(typeof window[fn]==='function') window[fn](el.value);
  });
  document.addEventListener('DOMContentLoaded',function(){
    applyState(readState());
    var pb=document.getElementById('printBtn'); if(pb) pb.addEventListener('click',function(){window.print();});
    var cb=document.getElementById('copyLinkBtn');
    if(cb) cb.addEventListener('click',function(){
      var url=location.href;
      if(navigator.clipboard&&navigator.clipboard.writeText){
        navigator.clipboard.writeText(url).then(function(){cb.textContent='已复制';setTimeout(function(){cb.textContent='复制视图链接';},1600);});
      }else{prompt('复制下面的链接：',url);}
    });
  });
  try{ applyState(readState()); if(typeof render==='function') render(); }catch(e){}
})();
</script>
"""


def patch(html: str) -> tuple[str, list[str]]:
    changes = []
    if '/* REPORT-HARDEN-V1 */' not in html:
        if '</style>' in html:
            html = html.replace('</style>', CSS + '\n</style>', 1)
            changes.append('CSS:打印/响应式/无障碍/样式收口')
        else:
            changes.append('WARN:未找到 </style>，未注入 CSS')

    if '<meta name="color-scheme"' not in html:
        html = re.sub(r'(<meta[^>]*charset[^>]*>)', r'\1\n<meta name="color-scheme" content="light">', html, count=1)
        changes.append('meta:color-scheme')

    if 'class="skip-link"' not in html:
        if '<body>' in html:
            html = html.replace('<body>', '<body>\n' + BODY_HTML, 1)
            changes.append('body:跳转链接/打印标题/noscript')
        else:
            changes.append('WARN:未找到 <body>，未注入兜底元素')

    if 'id="copyLinkBtn"' not in html:
        if '<div class="switch-row">' in html:
            html = html.replace('<div class="switch-row">', '<div class="switch-row">\n    ' + BUTTONS, 1)
            changes.append('toolbar:复制视图/打印按钮')
        else:
            # 退化为在 header 后插入
            html = re.sub(r'(</header>)', r'\1<div class="switch no-print">' + BUTTONS + '</div>', html, count=1)
            changes.append('header:复制视图/打印按钮(退化位置)')

    if 'id="reportHardening"' not in html:
        if '</body>' in html:
            html = html.replace('</body>', JS + '\n</body>', 1)
            changes.append('script:深链/键盘/表格语义/打印标题')
        else:
            changes.append('WARN:未找到 </body>，未注入脚本')

    # 把用户可控的分支名从 onclick 字符串拼接改为 data-* + 事件委托
    html, n = re.subn(r"onclick=\"select\('([^']*)'\)\"", r'data-branch="\1"', html)
    if n:
        changes.append('security:%d 处 select() 拼接改为 data-branch' % n)

    # 固定字面量事件改为委托，消除内联 onclick/onchange
    counts = []
    for pat, rep in [
        (r"onclick=\"([A-Za-z_$][\w$]*)\('([^']*)'\)\"", r'data-call="\1" data-arg="\2"'),
        (r'onclick="([A-Za-z_$][\w$]*)\(\)"', r'data-call="\1"'),
        (r'onchange="([A-Za-z_$][\w$]*)\(this\.value\)"', r'data-call="\1"'),
    ]:
        html, k = re.subn(pat, rep, html)
        if k:
            counts.append(k)
    if counts:
        changes.append('security:%d 处内联事件改为事件委托' % sum(counts))

    # 收口 KPI / 网格上的内联列数，避免击穿窄屏断点
    html, n = re.subn(r'(<div class="(?:kpis|grid3x)[^"]*)"\s+style="[^"]*grid-template-columns[^"]*"', r'\1"', html)
    if n:
        changes.append('responsive:移除 %d 处内联 grid-template-columns' % n)

    return html, changes


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('src')
    ap.add_argument('dst')
    args = ap.parse_args()
    src = pathlib.Path(args.src)
    dst = pathlib.Path(args.dst)
    if not src.exists():
        print('输入文件不存在: %s' % src, file=sys.stderr)
        return 2
    text = src.read_text(encoding='utf-8')
    out, changes = patch(text)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(out, encoding='utf-8')
    print('已写入: %s' % dst)
    for c in changes:
        print('  - ' + c)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
