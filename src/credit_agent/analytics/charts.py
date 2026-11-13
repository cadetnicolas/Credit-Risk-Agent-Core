"""PNG charts for analysis artifacts (matplotlib, Agg backend, CJK-capable).

Library-level / research capability: this module is NOT part of the 11 MCP
tools and is not exposed to Agent callers. Output paths are resolved by the
trusted host via ``get_runs_dir``; there is no HTTP serving layer in this
project. Rendering is best-effort: a missing matplotlib or font never fails
the analysis itself.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable, Dict, List

from ..config.paths import get_runs_dir

_CJK_FONTS = [
    "Noto Sans CJK SC",
    "Noto Sans SC",
    "Source Han Sans SC",
    "PingFang SC",
    "Heiti SC",
    "Hiragino Sans GB",
    "Microsoft YaHei",
    "SimHei",
    "WenQuanYi Zen Hei",
    "Arial Unicode MS",
]
_SAFE = re.compile(r"[^A-Za-z0-9_.-]+")
_ready = False


def chart_dir(session_id: str) -> Path:
    d = get_runs_dir() / session_id / "charts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _plt():
    global _ready
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if not _ready:
        from matplotlib import font_manager

        have = {f.name for f in font_manager.fontManager.ttflist}
        fonts = [f for f in _CJK_FONTS if f in have]
        plt.rcParams["font.sans-serif"] = fonts + ["DejaVu Sans"]
        plt.rcParams["axes.unicode_minus"] = False
        _ready = True
    return plt


def _save(fig, session_id: str, name: str) -> str:
    fname = _SAFE.sub("_", name)
    fig.savefig(chart_dir(session_id) / fname, dpi=110, bbox_inches="tight")
    return fname


def _vintage(plt, p: Dict[str, Any]):
    figs = []
    for seg, by_coh in list((p.get("curves") or {}).items())[:4]:
        fig, ax = plt.subplots(figsize=(7, 4))
        for coh, pts in sorted(by_coh.items()):
            mature = [q for q in pts if q.get("mature")]
            if not mature:
                continue
            ax.plot([q["mob"] for q in mature], [q["rate"] for q in mature], marker=".", label=str(coh))
        ax.set_xlabel("MOB")
        ax.set_ylabel("累计坏账率")
        ax.set_title(f"Vintage 曲线（{seg}，仅成熟账龄）")
        ax.yaxis.set_major_formatter(lambda v, _: f"{v:.1%}")
        ax.legend(fontsize=7, ncol=2)
        figs.append((f"vintage_{seg}", fig, f"Vintage 曲线 · {seg}"))
    return figs


def _migration(plt, p: Dict[str, Any]):
    order = p.get("buckets") or []
    mat = p.get("matrix") or {}
    rows = [b for b in order if b in mat]
    if not rows:
        return []
    data = [[mat[r].get(c, 0) for c in order] for r in rows]
    fig, ax = plt.subplots(figsize=(1 + 0.7 * len(order), 0.8 + 0.5 * len(rows)))
    im = ax.imshow(data, cmap="Reds", vmin=0, vmax=1)
    ax.set_xticks(range(len(order)), order)
    ax.set_yticks(range(len(rows)), rows)
    for i, r in enumerate(data):
        for j, v in enumerate(r):
            if v:
                ax.text(j, i, f"{v:.0%}", ha="center", va="center", fontsize=7, color="white" if v > 0.5 else "black")
    ax.set_xlabel("下一期阶段")
    ax.set_ylabel("本期阶段")
    ax.set_title("逾期阶段迁移矩阵")
    fig.colorbar(im, ax=ax, fraction=0.04)
    return [("migration_matrix", fig, "逾期阶段迁移矩阵")]


def _trend(plt, p: Dict[str, Any]):
    s = p.get("series") or []
    if not s:
        return []
    x = [r["period"] for r in s]
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(x, [r["rate"] for r in s], marker="o", label="比率")
    ax.plot(x, [r["ucl"] for r in s], "r--", lw=1, label="UCL(3σ)")
    ax.plot(x, [r["lcl"] for r in s], "g--", lw=1, label="LCL")
    ax.axhline(p.get("baseline_bad_rate") or 0, color="grey", lw=0.8, label="基期中心线")
    ooc = [(r["period"], r["rate"]) for r in s if r.get("out_of_control")]
    if ooc:
        ax.scatter([a for a, _ in ooc], [b for _, b in ooc], color="red", zorder=5, s=50, label="越限")
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.1%}")
    ax.set_title("趋势与控制图")
    ax.tick_params(axis="x", rotation=45, labelsize=7)
    ax.legend(fontsize=7)
    return [("trend_control", fig, "趋势与控制图")]


def _drill(plt, p: Dict[str, Any]):
    d = [x for x in (p.get("drivers") or []) if x.get("contribution") is not None][:10]
    if not d:
        return []
    fig, ax = plt.subplots(figsize=(7, 0.6 + 0.4 * len(d)))
    labels = [f"{x['dimension']}={x['value']}" for x in d][::-1]
    vals = [x["contribution"] for x in d][::-1]
    colors = ["#c0392b" if x.get("significant") and x.get("sample_ok") else "#bbbbbb" for x in d][::-1]
    ax.barh(labels, vals, color=colors)
    ax.xaxis.set_major_formatter(lambda v, _: f"{v:.2%}")
    ax.set_title("驱动切片贡献（红=显著且样本充足）")
    return [("drilldown_contrib", fig, "下钻贡献")]


def _decompose(plt, p: Dict[str, Any]):
    parts = [("结构效应", p.get("mix_effect") or 0), ("风险效应", p.get("rate_effect") or 0), ("交互", p.get("interaction") or 0)]
    fig, ax = plt.subplots(figsize=(6, 3.5))
    cum = 0.0
    for i, (lab, v) in enumerate(parts):
        ax.bar(i, v, bottom=cum if v >= 0 else cum + v, color="#c0392b" if v > 0 else "#27ae60")
        cum += v
    ax.bar(len(parts), p.get("total_change") or 0, color="#34495e")
    ax.set_xticks(range(len(parts) + 1), [a for a, _ in parts] + ["总变化"])
    ax.yaxis.set_major_formatter(lambda v, _: f"{v:.2%}")
    ax.set_title(f"结构分解（{p.get('dimension')}）")
    return [("decomposition", fig, "结构分解瀑布图")]


def _cutoff(plt, p: Dict[str, Any]):
    c = p.get("tradeoff_curve") or []
    if not c:
        return []
    fig, ax1 = plt.subplots(figsize=(7, 4))
    x = [r["approval_rate"] for r in c]
    ax1.plot(x, [r["bad_rate"] for r in c], "r-o", ms=3, label="坏账率")
    ax1.set_xlabel("通过率")
    ax1.set_ylabel("坏账率", color="r")
    ax1.xaxis.set_major_formatter(lambda v, _: f"{v:.0%}")
    ax1.yaxis.set_major_formatter(lambda v, _: f"{v:.1%}")
    ax2 = ax1.twinx()
    ax2.plot(x, [r["profit"] for r in c], "b-s", ms=3, label="利润")
    ax2.set_ylabel("利润", color="b")
    best = p.get("best")
    if best:
        ax1.axvline(best["approval_rate"], color="grey", ls="--", lw=1)
    max_bad = ((p.get("definition") or {}).get("constraints") or {}).get("max_bad_rate")
    if max_bad:
        ax1.axhline(float(max_bad), color="r", ls=":", lw=1)
    ax1.set_title("通过率-坏账率-利润 trade-off")
    return [("cutoff_tradeoff", fig, "Cut-off trade-off 曲线")]


def _funnel(plt, p: Dict[str, Any]):
    o = p.get("overall") or []
    if not o:
        return []
    fig, ax = plt.subplots(figsize=(6, 0.6 + 0.5 * len(o)))
    ax.barh([r["stage"] for r in o][::-1], [r["count"] for r in o][::-1], color="#2980b9")
    for i, r in enumerate(o[::-1]):
        ax.text(r["count"], i, f" {r['cumulative_conversion']:.1%}" if r.get("cumulative_conversion") is not None else "", va="center", fontsize=8)
    ax.set_title("转化漏斗")
    return [("funnel", fig, "转化漏斗")]


_RENDERERS: Dict[str, Callable] = {
    "vintage": _vintage,
    "migration": _migration,
    "trend": _trend,
    "control_chart": _trend,
    "drilldown": _drill,
    "decompose": _decompose,
    "cutoff_optimize": _cutoff,
    "funnel": _funnel,
}


def render_charts(session_id: str, step: str, artifact: str, payload: Dict[str, Any]) -> List[Dict[str, str]]:
    fn = _RENDERERS.get(step)
    if fn is None or not session_id:
        return []
    plt = _plt()
    stem = artifact.rsplit(".", 1)[0]
    out = []
    for key, fig, title in fn(plt, payload):
        try:
            name = _save(fig, session_id, f"{stem}__{key}.png")
            out.append({"name": name, "title": title, "url": f"/topics/{session_id}/artifacts/{name}"})
        finally:
            plt.close(fig)
    return out
