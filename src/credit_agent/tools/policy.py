"""Single-condition rule mining and cutoff simulation on mature booked loans."""

from __future__ import annotations

import math
import re
from typing import Any, Dict, Optional, Sequence

import numpy as np
import pandas as pd

from credit_agent.analytics.columns import as_flag
from credit_agent.analytics.optimize import cutoff_optimize
from credit_agent.analytics.profit import breakeven_bad_rate, loan_pnl, resolve_economics
from credit_agent.analytics.rules import mine_rules
from credit_agent.analytics.strategy import marginal_bands, swap_table
from credit_agent.modeling.split import dev_test_split
from credit_agent.tools import envelope, new_id

_BOOKED_CAVEAT = "仅描述已放款样本，不代表被拒申请人的表现。"
_RULE_PATTERN = re.compile(r"^(?P<feature>.+?)\s*(?P<op><=|>=|==|<|>)\s*(?P<value>.+?)\s*$")
_OOT_TEST_SIZE = 0.3
_LATEST_CAVEATS = (
    "最新可观察样本，含未成熟批次，不构成目标表现期坏账率，不得用于建模或上线收益测算。",
    _BOOKED_CAVEAT,
    "本结果仅提供样本数、占比、单条件规则命中率与 Swap Set 各块笔数等描述性测算。",
    "所有 bad_rate / lift / recommended 固定为空；未把未成熟样本视为 Good，不做拒绝推断。",
    "不写成熟掩码，不产出 economics 收益，也不估算放宽审批后新增客群的真实收益或坏账率。",
)
_FALLBACK_OPTIONS = (
    {"method": "random_holdout", "meaning": "随机留出复核；没有跨期稳定性证据"},
    {"method": "descriptive_only", "meaning": "只在当前成熟样本探索规则；没有独立验证"},
    {"method": "stop", "meaning": "等待更多日期覆盖后再挖掘"},
)


def _blocked(tool: str, code: str, message: str, *, snapshot_id: Optional[str] = None) -> Dict[str, Any]:
    return envelope(
        tool=tool,
        status="blocked",
        block_code=code,
        snapshot_id=snapshot_id,
        summary={"message": message},
    )


def _ready(value: Any) -> Any:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, np.ndarray):
        return _ready(value.tolist())
    if isinstance(value, dict):
        return {str(key): _ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_ready(item) for item in value]
    return str(value)


def _load_mature(ledger: Any, snapshot_id: str):
    frame = ledger.load_frame(snapshot_id)
    try:
        mask = ledger.load_mask(snapshot_id)
    except KeyError:
        return None, _blocked("simulate_policy_rules", "LABEL_IMMATURE", "没有成熟样本掩码", snapshot_id=snapshot_id)
    values = mask.to_numpy(dtype=bool)
    if len(values) != len(frame) or not bool(values.any()):
        return None, _blocked("simulate_policy_rules", "LABEL_IMMATURE", "没有成熟样本", snapshot_id=snapshot_id)
    return frame.iloc[np.flatnonzero(values)].reset_index(drop=True), None


def _apply_rule(frame: pd.DataFrame, rule: str) -> pd.Series:
    match = _RULE_PATTERN.match(str(rule).strip())
    if not match:
        raise ValueError(f"规则不是单条件: {rule}")
    feature = match.group("feature")
    op = match.group("op")
    raw = match.group("value").strip()
    if feature not in frame.columns:
        raise ValueError(f"规则字段不存在: {feature}")
    column = frame[feature]
    if op == "==":
        # 数值相等用数值比较，避免 "1" 与 "1.0" 的字符串误判。
        numeric_raw = pd.to_numeric(pd.Series([raw]), errors="coerce").iloc[0]
        if pd.notna(numeric_raw):
            numeric_col = pd.to_numeric(column, errors="coerce")
            return (numeric_col == numeric_raw).fillna(False)
        return column.astype(str) == raw
    number = float(raw)
    numeric = pd.to_numeric(column, errors="coerce")
    if op == "<=":
        hit = numeric <= number
    elif op == "<":
        hit = numeric < number
    elif op == ">=":
        hit = numeric >= number
    else:
        hit = numeric > number
    return hit.fillna(False)


def _slice_metrics(frame: pd.DataFrame, bad: str, hit: pd.Series):
    y = as_flag(frame[bad])
    valid = y.notna()
    denom = int(valid.sum())
    if denom == 0:
        return None, None, None
    mask = hit.reindex(frame.index).fillna(False).astype(bool) & valid
    hits = int(mask.sum())
    hit_rate = hits / denom
    if hits == 0:
        return hit_rate, None, None
    bad_rate = float(y.loc[mask].mean())
    base = float(y.loc[valid].mean())
    lift = (bad_rate / base) if base else None
    return hit_rate, bad_rate, lift


def _same_direction(dev_lift: Any, oot_lift: Any) -> bool:
    if dev_lift is None or oot_lift is None:
        return False
    return (float(dev_lift) - 1.0) * (float(oot_lift) - 1.0) > 0


def _round(value: Any, digits: int = 6):
    if value is None:
        return None
    return round(float(value), digits)


def _registered_features(ledger: Any, snapshot_id: str, features):
    """Resolve features against the snapshot's bound dimension registry.

    Only ``point_in_time == "decision"`` registered columns are accepted.
    Unregistered / empty features are blocked (no whole-table scan), and
    observation-time or suspect-leakage columns are a TIME_POINT_VIOLATION.
    """

    if not features:
        return None, _blocked(
            "simulate_policy_rules", "DIMENSION_UNREGISTERED",
            "features 为空，必须显式给出已登记的决策时点维度，不允许全表扫描", snapshot_id=snapshot_id,
        )
    meta = ledger.get_snapshot(snapshot_id) or {}
    metric_id = meta.get("bound_metric_id")
    version = meta.get("bound_metric_version")
    binding = None
    if isinstance(metric_id, str) and isinstance(version, str):
        binding = ledger.get_metric(snapshot_id, metric_id, version)
    if not isinstance(binding, dict):
        return None, _blocked(
            "simulate_policy_rules", "DIMENSION_UNREGISTERED",
            "该快照没有已绑定的维度登记表", snapshot_id=snapshot_id,
        )
    registered = set()
    for item in binding.get("dimensions") or []:
        if isinstance(item, dict) and str(item.get("point_in_time") or "").strip().lower() == "decision":
            if item.get("column"):
                registered.add(str(item["column"]))
        elif isinstance(item, str):
            registered.add(item)
    observed = set()
    for column in binding.get("rejected_columns") or []:
        observed.add(str(column))
    for column in meta.get("suspect_leakage") or []:
        observed.add(str(column))
    requested = [str(item) for item in features]
    for feature in requested:
        if feature in observed:
            return None, _blocked(
                "simulate_policy_rules", "TIME_POINT_VIOLATION",
                f"维度属于观察时点或疑似穿越列：{feature}", snapshot_id=snapshot_id,
            )
    unregistered = [feature for feature in requested if feature not in registered]
    if unregistered:
        return None, _blocked(
            "simulate_policy_rules", "DIMENSION_UNREGISTERED",
            "维度未在登记表内：" + "、".join(unregistered), snapshot_id=snapshot_id,
        )
    return requested, None


def _rule_mining(mature: pd.DataFrame, *, bad: str, time_column: Optional[str], features, min_lift: float,
                 min_coverage: float, snapshot_id: str, validation_method: str = "oot") -> Dict[str, Any]:
    flags = as_flag(mature[bad])
    work = mature.iloc[np.flatnonzero(flags.notna().to_numpy(dtype=bool))].reset_index(drop=True)
    y = as_flag(work[bad]).to_numpy(dtype=int)
    try:
        split = dev_test_split(work, y, test_size=0.0 if validation_method == "descriptive_only" else _OOT_TEST_SIZE,
                               random_state=0, time_column=time_column if validation_method == "oot" else None)
    except ValueError as exc:
        return _blocked("simulate_policy_rules", "VALIDATION_UNAVAILABLE", str(exc), snapshot_id=snapshot_id)
    if validation_method == "oot" and split.method != "oot":
        return _blocked("simulate_policy_rules", "RANDOM_SPLIT_FORBIDDEN", "切分不是时间外样本", snapshot_id=snapshot_id)
    dev = work.iloc[np.flatnonzero(split.dev_mask)].reset_index(drop=True)
    oot = work.iloc[np.flatnonzero(split.test_mask)].reset_index(drop=True)
    try:
        mined = mine_rules(
            dev,
            bad=bad,
            features=features,
            min_coverage=min_coverage,
            min_lift=min_lift,
            exclude=(time_column,) if time_column else (),
            top_n=1000,
        )
    except ValueError as exc:
        return _blocked("simulate_policy_rules", "VALIDATION_UNAVAILABLE", str(exc), snapshot_id=snapshot_id)
    recommended = []
    candidates = []
    for rule in mined.get("rules") or []:
        try:
            oot_hit, oot_bad, oot_lift = (
                _slice_metrics(oot, bad, _apply_rule(oot, rule["rule"])) if len(oot) else (None, None, None))
        except (ValueError, KeyError, TypeError):
            return _blocked(
                "simulate_policy_rules", "UNSUPPORTED_SCOPE",
                f"非单条件或无法解析的规则串：{rule.get('rule')}", snapshot_id=snapshot_id,
            )
        if validation_method != "oot":
            candidates.append({"rule": rule["rule"], "hit_rate": rule.get("coverage"),
                               "bad_rate": rule.get("bad_rate"), "lift": rule.get("lift"),
                               "holdout_lift": _round(oot_lift)})
            if len(candidates) >= 10:
                break
            continue
        if not _same_direction(rule.get("lift"), oot_lift):
            continue
        recommended.append(
            {
                "rule": rule["rule"],
                "hit_rate": rule.get("coverage"),
                "bad_rate": rule.get("bad_rate"),
                "lift": rule.get("lift"),
                "oot_hit_rate": _round(oot_hit),
                "oot_bad_rate": _round(oot_bad),
                "oot_lift": _round(oot_lift),
                "same_direction": True,
            }
        )
        if len(recommended) >= 10:
            break
    return {
        "mode": "rule_mining",
        "trials": int(mined.get("trials_total") or 0),
        "recommended": recommended,
        "candidates": candidates,
        "validation_method": validation_method,
        "caveats": [_BOOKED_CAVEAT] + (["未完成时间外验证，仅可作为研究候选。"] if validation_method != "oot" else []),
    }


def _economics(mature: pd.DataFrame, *, score: str, bad: str, economics: Dict[str, Any],
               bands: Dict[str, Any], higher_is_safer: bool):
    """Real unit-economics: break-even bad rate, per-band marginal net gain, suggested cutoff range.

    Nothing here prices the swap-in (newly approved) population: every number
    is derived from the already-booked, already-flagged sample only.
    """

    econ, source = resolve_economics(economics)
    break_even = breakeven_bad_rate(econ)
    amount = float(econ["default_amount"])
    good = float(loan_pnl(np.array([amount]), np.array([0.0]), econ)["profit"][0])
    bad_profit = float(loan_pnl(np.array([amount]), np.array([1.0]), econ)["profit"][0])
    spread = good - bad_profit
    marginal = []
    for band in bands.get("bands") or []:
        rate = band.get("bad_rate")
        n = int(band.get("n") or 0)
        unit = None if rate is None else good - spread * float(rate)
        net_gain = None if unit is None else unit * n
        marginal.append({
            "band": band.get("band"),
            "lower": band.get("lower"),
            "upper": band.get("upper"),
            "n": n,
            "bad_rate": rate,
            "expected_unit_net_gain": _round(unit),
            "marginal_net_gain": _round(net_gain),
        })
    optimized = None
    optimize_note = None
    try:
        optimized = cutoff_optimize(mature, score=score, bad=bad, econ=econ)
    except ValueError as exc:
        optimize_note = str(exc)
    profitable = [
        band for band in (bands.get("bands") or [])
        if band.get("bad_rate") is not None and break_even is not None and float(band["bad_rate"]) <= break_even
    ]
    if profitable:
        suggested = {
            "lower": min(float(b["lower"]) for b in profitable),
            "upper": max(float(b["upper"]) for b in profitable),
            "basis": "边际坏账率不超过盈亏平衡坏账率的分数档（仅已放款样本，含选择偏差）",
            "bands": [b["band"] for b in profitable],
        }
    else:
        suggested = {
            "lower": None,
            "upper": None,
            "basis": "没有分数档的边际坏账率低于盈亏平衡坏账率",
            "bands": [],
        }
    if optimized is not None and isinstance(optimized.get("best"), dict):
        suggested["best_threshold_from_backtest"] = optimized["best"].get("threshold")
        suggested["backtest_note"] = "仅已放款有表现样本回测，含选择偏差，不代表放宽后新增客群"
    block: Dict[str, Any] = {
        "break_even_bad_rate": _round(break_even),
        "unit_net_gain_good": _round(good),
        "unit_net_gain_bad": _round(bad_profit),
        "source": source,
        "marginal_net_gain": marginal,
        "suggested_cutoff_range": suggested,
    }
    if optimized is not None:
        block["backtest"] = {
            "analysis": optimized.get("analysis"),
            "direction": optimized.get("direction"),
            "constraint_satisfied": optimized.get("constraint_satisfied"),
        }
    caveats = [
        "经济参数已按 resolve_economics 解析，未把放宽审批后的新增收益算成收益。",
        "逐档边际净收益只基于已放款有表现样本的观察坏账率。",
        _BOOKED_CAVEAT,
        "严禁估算放宽审批后新增通过客群的真实收益或坏账率；调入（swap-in）客群坏账率未知。",
    ]
    if optimize_note:
        caveats.append(f"cutoff_optimize 未产出回测结果：{optimize_note}")
    return block, caveats


def _candidate_plans(bands: Dict[str, Any], higher_is_safer: bool):
    rows = list(bands.get("bands") or [])
    if not rows:
        return []
    ordered = sorted(rows, key=lambda b: float(b.get("lower") or 0.0))
    if not higher_is_safer:
        ordered = list(reversed(ordered))
    total_bad = float(bands.get("bad_cnt") or 0)
    picks = [
        ("aggressive", ordered[0]),
        ("balanced", ordered[len(ordered) // 2]),
        ("conservative", ordered[-1]),
    ]
    plans = []
    for name, band in picks:
        threshold = band.get("lower") if higher_is_safer else band.get("upper")
        approved_bad_cnt = float(band.get("approved_bad_cnt") or 0)
        capture = (approved_bad_cnt / total_bad) if total_bad else None
        plans.append({
            "plan": name,
            "threshold": threshold,
            "approval_rate": band.get("approval_rate"),
            "bad_rate": band.get("approved_bad_rate"),
            "bad_capture": _round(capture),
            "approved_n": band.get("approved_n"),
        })
    return plans


def _trial_design() -> Dict[str, Any]:
    return {
        "status": "pending_approval",
        "challenger_traffic": {"min": 0.05, "max": 0.10, "note": "5%~10% 小流量冠军/挑战者试验"},
        "observation_window": "至少覆盖一个完整目标表现期（按已签收指标的 MOB/FPD 口径），未成熟不得评估",
        "rollback_triggers": [
            "挑战者分群坏账率显著高于冠军且超出已签收阈值",
            "样本量不足或数据漂移导致结论不可比",
            "出现合规或客诉事件",
        ],
        "champion": "现行审批阈值 / 策略（不改动）",
        "challenger": "候选切点区间内的阈值",
        "note": "不触及生产决策，须经人工审批后方可开展小流量试验。",
    }


def _cutoff(mature: pd.DataFrame, *, bad: str, score: Optional[str], edges, current_threshold, candidate_threshold,
            higher_is_safer: bool, economics, snapshot_id: str) -> Dict[str, Any]:
    if not score or edges is None or (isinstance(edges, (list, tuple)) and len(edges) == 0):
        return _blocked("simulate_policy_rules", "UNSUPPORTED_SCOPE", "cutoff 需要 score 与 edges", snapshot_id=snapshot_id)
    if not bool((as_flag(mature[bad]).dropna() > 0).any()):
        return _blocked("simulate_policy_rules", "VALIDATION_UNAVAILABLE", "成熟样本中没有坏样本，无法进行策略测算", snapshot_id=snapshot_id)
    try:
        bands = marginal_bands(mature, score, bad, edges=edges, higher_is_safer=higher_is_safer)
    except ValueError as exc:
        return _blocked("simulate_policy_rules", "VALIDATION_UNAVAILABLE", str(exc), snapshot_id=snapshot_id)
    caveats = [_BOOKED_CAVEAT, *list(bands.get("caveats") or [])]
    summary: Dict[str, Any] = {
        "mode": "cutoff",
        "bands": bands["bands"],
        "n": bands["n"],
        "bad_rate": bands["bad_rate"],
        "caveats": caveats,
        "candidate_plans": _candidate_plans(bands, higher_is_safer),
        "trial_design": _trial_design(),
    }
    if current_threshold is not None and candidate_threshold is not None:
        try:
            swap = swap_table(
                mature,
                score,
                bad,
                current_threshold=current_threshold,
                candidate_threshold=candidate_threshold,
                higher_is_safer=higher_is_safer,
            )
        except ValueError as exc:
            return _blocked("simulate_policy_rules", "VALIDATION_UNAVAILABLE", str(exc), snapshot_id=snapshot_id)
        # SKILL 红线：调入集坏账率未知，不给出可被引用的数字。
        summary["swap_in"] = {
            "n": swap["swap_in"]["n"],
            "note": "调入集坏账率未知：现行拒绝客群不在已放款样本内，不给出可被引用的数字",
        }
        summary["swap_in_bad_rate"] = None
        summary["keep_rejected"] = swap.get("keep_rejected")
        caveats.append("调入集（swap_in）坏账率未知，不给出可被引用的数字。")
        caveats.extend(swap.get("caveats") or [])
    if economics is not None:
        if not isinstance(economics, dict):
            return _blocked("simulate_policy_rules", "VALIDATION_UNAVAILABLE", "economics 参数非法：必须是字典", snapshot_id=snapshot_id)
        econ_block, econ_caveats = _economics(
            mature, score=score, bad=bad, economics=economics, bands=bands, higher_is_safer=higher_is_safer,
        )
        summary["economics"] = econ_block
        caveats.extend(econ_caveats)
    return summary


def _descriptive_bands(frame: pd.DataFrame, score: Optional[str], edges) -> list:
    if not score or edges is None or score not in frame.columns:
        return []
    values = pd.to_numeric(frame[score], errors="coerce")
    try:
        binned = pd.cut(values, bins=[float(item) for item in edges], right=True)
    except (ValueError, TypeError):
        return []
    total = int(values.notna().sum())
    out = []
    for category in binned.cat.categories:
        n = int(binned.eq(category).sum())
        out.append({
            "band": str(category),
            "lower": float(category.left),
            "upper": float(category.right),
            "n": n,
            "share": (n / total) if total else None,
        })
    return out


def _descriptive_rule_hits(frame: pd.DataFrame, features) -> list:
    total = len(frame)
    out = []
    for feature in features or []:
        if feature not in frame.columns:
            continue
        x = frame[feature]
        num = pd.to_numeric(x, errors="coerce")
        if num.notna().mean() > 0.9 and num.nunique() > 10:
            try:
                q = pd.qcut(num, q=10, duplicates="drop")
                cats = list(q.cat.categories)
                conds = [
                    (f"{feature} <= {cats[0].right:.4g}", num <= cats[0].right),
                    (f"{feature} > {cats[-1].left:.4g}", num > cats[-1].left),
                ]
            except ValueError:
                conds = []
        elif x.nunique() <= 30:
            conds = [(f"{feature} == {v}", x.astype(str) == str(v)) for v in x.dropna().astype(str).unique()]
        else:
            conds = []
        for name, hit in conds:
            hits = int(hit.fillna(False).sum())
            out.append({
                "rule": name,
                "hits": hits,
                "hit_rate": (hits / total) if total else None,
                "bad_rate": None,
                "lift": None,
            })
    return out


def _descriptive_swap(frame: pd.DataFrame, score: Optional[str], bad: str,
                      current_threshold, candidate_threshold, higher_is_safer):
    if not score or current_threshold is None or candidate_threshold is None or score not in frame.columns:
        return None
    try:
        swap = swap_table(frame, score, bad, current_threshold=current_threshold,
                          candidate_threshold=candidate_threshold, higher_is_safer=higher_is_safer)
    except ValueError:
        return None
    return {key: {"n": swap[key]["n"]} for key in ("keep_approved", "swap_out", "swap_in", "keep_rejected")}


def _latest_descriptive(
    ledger: Any, snapshot_id: str, mode: str, bad: str, features,
    score: Optional[str], edges, current_threshold, candidate_threshold,
    higher_is_safer: bool, latest_review_id: Optional[str],
) -> Dict[str, Any]:
    """Descriptive-only measurement on the newest observable (incl. immature) sample."""

    if not latest_review_id:
        return _blocked("simulate_policy_rules", "ADMISSION_REQUIRED", "latest_observed 必须提供 latest_review_id", snapshot_id=snapshot_id)
    run = ledger.get_run(latest_review_id)
    if not run or run.get("tool") != "check_label_maturity" or run.get("snapshot_id") != snapshot_id:
        return _blocked("simulate_policy_rules", "ADMISSION_REQUIRED", "latest_review_id 不存在或不属于本快照", snapshot_id=snapshot_id)
    run_summary = run.get("summary") or {}
    if run_summary.get("observation_mode") != "latest_observed" or run_summary.get("mask_kind") != "latest_observed":
        return _blocked("simulate_policy_rules", "ADMISSION_REQUIRED", "latest_review_id 不是由 latest_observed 成熟度检查产生", snapshot_id=snapshot_id)
    if not hasattr(ledger, "load_latest_mask"):
        return _blocked("simulate_policy_rules", "ADMISSION_REQUIRED", "账本不支持最新可观察样本掩码（等待 Ledger.load_latest_mask）", snapshot_id=snapshot_id)
    try:
        frame = ledger.load_frame(snapshot_id)
        mask = ledger.load_latest_mask(snapshot_id).to_numpy(dtype=bool)
    except (KeyError, OSError, ValueError, TypeError):
        return _blocked("simulate_policy_rules", "ADMISSION_REQUIRED", "最新可观察样本掩码不可用", snapshot_id=snapshot_id)
    if len(mask) != len(frame):
        return _blocked("simulate_policy_rules", "ADMISSION_REQUIRED", "最新样本掩码长度与快照不一致", snapshot_id=snapshot_id)
    latest = frame.iloc[np.flatnonzero(mask)].reset_index(drop=True)
    accepted = []
    if features:
        accepted, blocked_reg = _registered_features(ledger, snapshot_id, features)
        if blocked_reg:
            return blocked_reg
    summary: Dict[str, Any] = {
        "mode": mode,
        "observation_mode": "latest_observed",
        "latest_review_id": latest_review_id,
        "latest_row_count": int(len(latest)),
        "bad_rate": None,
        "lift": None,
        "recommended": [],
        "candidates": _descriptive_rule_hits(latest, accepted),
        "bands": _descriptive_bands(latest, score, edges),
        "swap_set": _descriptive_swap(latest, score, bad, current_threshold, candidate_threshold, higher_is_safer),
        "validation_method": "descriptive_only",
        "caveats": list(_LATEST_CAVEATS),
    }
    return summary


def simulate_policy_rules(
    snapshot_id: str,
    *,
    ledger: Any,
    mode: str,
    time_column: Optional[str],
    bad: str,
    features: Optional[Sequence[str]] = None,
    score: Optional[str] = None,
    depth: int = 1,
    min_lift: float = 2.0,
    min_coverage: float = 0.01,
    edges=None,
    current_threshold: Optional[float] = None,
    candidate_threshold: Optional[float] = None,
    higher_is_safer: bool = True,
    economics: Optional[Dict[str, Any]] = None,
    request_loosen_benefit: bool = False,
    fallback_review_id: Optional[str] = None,
    observation_mode: str = "mature_only",
    latest_review_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Mine one-condition rules or a cutoff table. Do not price a looser approval."""

    if depth > 1 or request_loosen_benefit:
        return _blocked(
            "simulate_policy_rules",
            "UNSUPPORTED_SCOPE",
            "不支持多条件规则，也不估算放宽审批后的新增收益",
            snapshot_id=snapshot_id,
        )
    if mode not in ("rule_mining", "cutoff"):
        return _blocked("simulate_policy_rules", "UNSUPPORTED_SCOPE", "mode 只接受 rule_mining 或 cutoff", snapshot_id=snapshot_id)
    if observation_mode not in ("mature_only", "latest_observed"):
        return _blocked("simulate_policy_rules", "UNSUPPORTED_SCOPE", "observation_mode 只接受 mature_only 或 latest_observed", snapshot_id=snapshot_id)

    if observation_mode == "latest_observed":
        summary = _latest_descriptive(
            ledger, snapshot_id, mode, bad, features, score, edges,
            current_threshold, candidate_threshold, higher_is_safer, latest_review_id,
        )
        if summary.get("status") == "blocked" or summary.get("block_code"):
            return summary
        body = _ready(summary)
        run_id = new_id("run")
        artifact = ledger.save_run(run_id, "simulate_policy_rules", snapshot_id, body)
        return envelope(
            tool="simulate_policy_rules", status="degraded", summary=body,
            snapshot_id=snapshot_id, run_id=run_id, artifacts=[artifact],
        )

    admission = ledger.get_admission(snapshot_id)
    if admission is None or admission.get("status") == "blocked":
        return _blocked("simulate_policy_rules", "ADMISSION_REQUIRED", "该快照没有可用的准入记录", snapshot_id=snapshot_id)
    mature, blocked = _load_mature(ledger, snapshot_id)
    if blocked:
        return blocked
    if mode == "rule_mining":
        accepted, blocked_reg = _registered_features(ledger, snapshot_id, features)
        if blocked_reg:
            return blocked_reg
        request = {"bad": bad, "time_column": time_column, "features": list(accepted),
                   "min_lift": float(min_lift), "min_coverage": float(min_coverage)}
        summary = _rule_mining(
            mature,
            bad=bad,
            time_column=time_column,
            features=accepted,
            min_lift=min_lift,
            min_coverage=min_coverage,
            snapshot_id=snapshot_id,
        )
        if summary.get("block_code") == "RANDOM_SPLIT_FORBIDDEN":
            if fallback_review_id:
                review = ledger.get_run(fallback_review_id)
                if not review or review["tool"] != "policy_validation_fallback" or review["snapshot_id"] != snapshot_id or review["summary"] != request:
                    return _blocked("simulate_policy_rules", "VALIDATION_UNAVAILABLE",
                                    "回退选择与当前策略任务不匹配", snapshot_id=snapshot_id)
                decision = ledger.get_decision("validation_fallback", fallback_review_id)
                method = (decision or {}).get("payload", {}).get("method") if decision and decision.get("channel") == "approval_card" else None
            else:
                fallback_review_id = new_id("review")
                ledger.save_run(fallback_review_id, "policy_validation_fallback", snapshot_id, request)
                method = None
            if method == "stop":
                return _blocked("simulate_policy_rules", "USER_CHOSE_STOP", "用户选择等待更多数据", snapshot_id=snapshot_id)
            if method not in ("random_holdout", "descriptive_only"):
                return envelope(tool="simulate_policy_rules", status="needs_human", block_code="RANDOM_SPLIT_FORBIDDEN",
                                snapshot_id=snapshot_id,
                                summary={"reason": summary["summary"]["message"], "fallback_review_id": fallback_review_id,
                                         "options": [dict(item) for item in _FALLBACK_OPTIONS]},
                                human_gate={"gate": "validation_fallback", "review_id": fallback_review_id})
            summary = _rule_mining(mature, bad=bad, time_column=time_column, features=accepted,
                                   min_lift=min_lift, min_coverage=min_coverage,
                                   snapshot_id=snapshot_id, validation_method=method)
            summary["fallback_review_id"] = fallback_review_id
        if summary.get("status") == "blocked" or summary.get("block_code"):
            return summary
    else:
        summary = _cutoff(
            mature,
            bad=bad,
            score=score,
            edges=edges,
            current_threshold=current_threshold,
            candidate_threshold=candidate_threshold,
            higher_is_safer=higher_is_safer,
            economics=economics,
            snapshot_id=snapshot_id,
        )
        if summary.get("status") == "blocked" or summary.get("block_code"):
            return summary
    body = _ready(summary)
    run_id = new_id("run")
    artifact = ledger.save_run(run_id, "simulate_policy_rules", snapshot_id, body)
    return envelope(
        tool="simulate_policy_rules",
        status="degraded" if mode == "rule_mining" and body.get("validation_method") != "oot" else "ok",
        summary=body,
        snapshot_id=snapshot_id,
        run_id=run_id,
        artifacts=[artifact],
    )
