# 版式令牌与组件标记

本文件是 [../SKILL.md](../SKILL.md) 的细则：具体色值、字号、间距与可复制的标记片段。所有值都与 `src/credit_agent/render/html_report.py` 中的常量保持一致，改一处就要同步另一处。

---

## 1. 设计令牌

### 1.1 色板

| 令牌 | 色值 | 用途 |
| --- | --- | --- |
| ink | `#1d2733` | 正文与标题 |
| sub | `#5d6b7a` | 辅助文字、导语、表头说明 |
| line | `#dfe5ec` | 边框、分隔线 |
| bg | `#f6f8fa` | 页面底色 |
| blue | `#2f5fd0` | 中性信息、主指标、坏率条 |
| green | `#1f7a4d` | 通过、已签、可交付 |
| amber | `#a8620a` | 需关注、待签、平台期 |
| red | `#b3261e` | 风险、阻断、不合格 |

语义色只允许这五个含义，不新增装饰色。

### 1.2 字号与行高

| 元素 | 字号 | 说明 |
| --- | --- | --- |
| 页标题 h1 | `clamp(20px, 3.2vw, 25px)` | 窄屏自动缩小 |
| 节标题 h2 | `clamp(16px, 2.4vw, 18px)` | 左侧 4px 色条 |
| 子节 h3 | `14.5px` | 仅用于节内分主题 |
| 正文 | `14px / 1.62` | 表格内降到 13px |
| KPI 数字 | `clamp(20px, 3vw, 25px)` | 等宽数字 |
| 辅助文字 | `12~12.8px` | 导语、注释、页脚 |

### 1.3 间距

- 页面容器：`max-width:1080px`，左右内边距 `clamp(14px, 4vw, 26px)`。
- 分节之间：`34px`；节标题下方 `14px`。
- KPI 网格间距 `11px`，卡片内边距 `14px 15px`。
- 表格单元格：`9px 12px`（表头）/ `8px 12px`（数据）；窄屏统一降到 `6px 8px`。

---

## 2. 组件标记

### 2.1 KPI 卡

```html
<div class="kpis">
  <div class="kpi">
    <div class="v" style="color:#a8620a">0.3135</div>
    <div class="l">OOT KS</div>
    <div class="s">AUC 0.6632 · n=4,023</div>
  </div>
</div>
```

要求：一个 KPI 只讲一个指标；补充说明放第三行，不要挤进标签行。

### 2.2 标准表格（必须带滚动容器）

```html
<div class="tbl">
  <table>
    <thead><tr>
      <th class="c-id">特征</th>
      <th class="num">IV</th>
      <th class="num">PSI</th>
      <th class="c-wide">说明</th>
    </tr></thead>
    <tbody>
      <tr>
        <td class="c-id">offline_days_since_last_odue</td>
        <td class="num">0.413</td>
        <td class="num">0.241</td>
        <td class="c-wide">缺失按「从未逾期」解释，独立成箱</td>
      </tr>
    </tbody>
  </table>
</div>
```

列样式只有三类：`num`（右对齐等宽不折行）、`c-id`（长标识符，限宽 + 折行）、`c-wide`（长文本，最小宽度 220px）。不要写内联 `style="min-width:..."`。

### 2.3 提示框

```html
<div class="box risk">
  <h3>十分位不单调，低分段存在平台期</h3>
  <p>D1 11.17% → D2 7.71% → D3 11.44%，前四档挤在 7.7%~11.4%。</p>
  <ul><li>这是坏样本量支撑的极限，不是调参能解决的。</li></ul>
</div>
```

四类：`box`（中性）、`box ok`、`box warn`、`box risk`。标题必须是一句结论，不是「注意事项」这种空话。

### 2.4 特征卡

```html
<div class="feat">
  <div class="hd">
    <span class="nm">offline_current_max_loan_balance</span>
    <span class="cn">系数 -0.3312</span>
    <span class="tag t-info">6 箱</span>
    <span class="tag t-gray">箱内样本 1,243</span>
  </div>
  <div class="tbl">
    <table><!-- 分箱区间 / 样本 / 坏数 / 坏率 / 坏率条 / WOE / 得分 --></table>
  </div>
</div>
```

卡头用 `flex-wrap`，长变量名允许折行；卡内表格不再加外边框。

### 2.5 标签

| 类名 | 语义 | 示例 |
| --- | --- | --- |
| `t-ok` | 通过、已签 | 已落账 |
| `t-warn` | 需关注、待签 | candidate |
| `t-bad` | 风险、不合格 | PSI 超标 |
| `t-info` | 中性信息 | 6 箱 |
| `t-gray` | 不可用、不适用 | unavailable |

标签只放 2~6 个字的短状态词，不放句子。

### 2.6 坏率条

```html
<div class="bar"><i style="width:63.2%"></i></div>
```

宽度按**同一张表内的最大坏率**归一，跨表不可比。表格里的条只用于看相对高低，阈值判断仍看数字。

---

## 3. 响应式与打印

```css
/* 窄屏 */
@media (max-width:640px){
  .wrap{padding:22px 14px 54px}
  .kpis{grid-template-columns:repeat(auto-fit,minmax(140px,1fr))}
  .tbl table{font-size:12px}
  th,td{padding:6px 8px}
  td.c-id{min-width:130px;max-width:220px;font-size:11px}
}
/* 打印 / 另存 PDF */
@page{size:A4 landscape;margin:10mm}
@media print{
  body{background:#fff;font-size:11.5pt;-webkit-print-color-adjust:exact;print-color-adjust:exact}
  .no-print,.skip-link{display:none!important}
  .print-only{display:block!important}
  .tbl,.tw{overflow:visible!important;max-height:none!important}
  .tbl table,table{width:100%}
  thead th,td.lbl,th.lbl{position:static!important}
  .box,.kpi,.feat,.note,tr{break-inside:avoid}
  h2,h3{break-after:avoid}
}
```

打印规则的关键是 `.tbl{overflow:visible}`——否则滚动容器会把右侧列的表格内容截在纸外；`print-color-adjust:exact` 保证红/橙/绿语义色不会被浏览器默认设置抹掉。

---

## 4. 加固旧报告

历史脚本直接拼 `<table>`，逐个改动成本高。渲染层提供一次性加固：补 `viewport`、把所有裸表包进滚动容器、追加覆盖样式。

```python
from credit_agent.render import harden_html
html = harden_html(html)   # 写盘前调用一次即可
```

覆盖样式只做三件事：容器化表格、允许表头折行、网格自适应。它不改原文档的色板与排版，因此不会让旧报告「看起来变成另一份」。

---

## 5. 自检探针

宽度类问题不能靠肉眼看截图判断，用脚本量：

```javascript
// 页面无横向滚动 = 没被裁
document.documentElement.scrollWidth === document.documentElement.clientWidth
// 表格是否仍需内部滚动（1280/900 下应为 0）
[...document.querySelectorAll('.tbl')].filter(el => el.scrollWidth > el.clientWidth + 1).length
```

在 1280 / 900 / 760 三个宽度各跑一次，前两项分别应为「相等」与「0（760 允许少量）」。

---

## 6. 交互单文件报告的加固细则

适用于「数据在 JS 里、正文由 JS 渲染」的看板型报告：

1. **打印**：`@page` 横向 A4；打印时隐藏交互工具栏、展开滚动容器、去 sticky、保色、避免提示框跨页；打印标题写入「报告名 + 当前视图 + 数据时点」。
2. **无 JS**：`<noscript>` 内放静态摘要表，至少覆盖各视图的关键指标；禁止只写一句「请开启 JavaScript」就结束。
3. **深链**：`location.hash` 记录分支/维度/年份/指标/排序；`hashchange` 还原状态；提供复制视图链接按钮。用 `history.replaceState` 避免污染前进后退栈。
4. **事件安全**：所有 `onclick="fn('${x}')"` 改为 `data-*`，由事件委托调用 `window[fn]`；含用户可控字符串时只用 `dataset`，不拼进 HTML 属性里的脚本片段。
5. **无障碍**：`.chip` → `<button aria-pressed>` 或 `role="button" tabindex="0"`；排序表头 `role="button" tabindex="0" aria-sort` 且响应 Enter/Space；表格运行时补 `<caption class="sr-only">` 与 `scope="col"`；跳转链接 + `:focus-visible`。
6. **响应式收口**：KPI/网格元素禁止内联 `grid-template-columns`；统一 `repeat(auto-fit,minmax(170px,1fr))`。若容器已有内联样式，用 `!important` 覆盖规则收口，不要逐个元素改。
7. **Sticky 表头**：`thead th{position:sticky;top:0}` 只有在滚动容器自身可纵向滚动时才生效；仅 `overflow-x:auto` 的 `.tw` 不会让表头吸附页面顶部，需 `overflow:auto;max-height:76vh`。

```css
.sr-only{position:absolute!important;width:1px;height:1px;padding:0;margin:-1px;
  overflow:hidden;clip:rect(0,0,0,0);white-space:nowrap;border:0}
.skip-link{position:absolute;left:-9999px;top:0;z-index:9999;background:#fff;
  color:#2f5fd0;padding:8px 14px;border:1px solid #dfe5ec}
.skip-link:focus{left:0}
:focus-visible{outline:2px solid #2f5fd0;outline-offset:2px}
```

---

## 7. 数据双轨

超过 500 KB 的单行内联 JSON 会让 diff 变成一行、编辑器卡顿，但拆成外链又破坏「双击即开」的离线交付。用双轨解决：

```bash
python3 scripts/data_split.py extract report.html src/     # src/data.js + src/report.source.html
python3 scripts/data_split.py inline src/report.source.html src/data.js dist/report.html
```

- 源码模板保留 `const D = /*__REPORT_DATA__*/ null;`，`data.js` 只存纯 JSON 对象。
- 交付版重新 inline 成单文件；拆分→合成必须字节一致（SHA256 可校验）。
- 审计脚本对「最大单行 > 100k」给出的 WARN 属于交付版的设计取舍，需在交付说明中解释，不要为了消除 WARN 牺牲离线可用性。
