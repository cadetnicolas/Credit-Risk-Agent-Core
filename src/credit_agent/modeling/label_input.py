"""Turn explicit human label descriptions into reviewable proposals, never guesses.

Only the confirmed business contract and the user's review input are parsed.
Knowledge retrieval and planner objectives cannot define label semantics.
"""
from __future__ import annotations

import re
from difflib import get_close_matches
from typing import Any

import pandas as pd

from .schema import norm_label_value, probe_label_columns

ROLE_KEYS = ("positive_values", "negative_values", "gray_values", "exclude_label_values")
_VALUE = r"(?:-?\d+(?:\.\d+)?|[A-Za-z_][A-Za-z0-9_-]*)"
_EXCLUDE = r"(?:剔除|排除|不纳入)"
_ROLE = rf"(?:好(?:样本|客户)?|坏(?:样本|客户)?|灰(?:样本|客户)?|无效(?:样本|客户)?|未放款(?:样本|客户)?|good|bad|gray|invalid|{_EXCLUDE})"
# Chinese prefixes such as “标签-1” are valid. Never start inside a signed
# number or an identifier, which would turn -1 into 1.
_VALUE_START = r"(?<![A-Za-z0-9_.+\-])"
_ENUM = rf"['\"]?{_VALUE}['\"]?"
_LIST_SEPARATOR = r"\s*(?:、|,|，|和|及|与)\s*"
_VALUES = rf"{_ENUM}(?:{_LIST_SEPARATOR}{_ENUM})*"
_ASSIGNMENTS = (
    (re.compile(rf"{_VALUE_START}({_VALUES})\s*(?:是|为|表示|代表|改为|改成|=|:)\s*({_ROLE})", re.I), False),
    (re.compile(rf"({_ROLE})\s*(?:是|为|=|:)\s*({_VALUES})", re.I), True),
    (re.compile(rf"{_VALUE_START}({_VALUES})\s*"
                r"(?:(?:是|为|表示|代表)\s*未到表现期\s*|(?:的)?(?:样本|客户|记录)?\s*)"
                rf"(?:全部|均|都|直接)?\s*({_EXCLUDE})", re.I), False),
    (re.compile(rf"({_EXCLUDE})\s*(?:标签(?:值)?\s*(?:是|为|=|:)?\s*)?({_VALUES})", re.I), True),
)


def _role(text: str) -> str:
    text = text.lower()
    if any(w in text for w in ("无效", "未放款", "剔除", "排除", "不纳入", "invalid")):
        return "exclude_label_values"
    if "灰" in text or "gray" in text:
        return "gray_values"
    if "坏" in text or "bad" in text:
        return "positive_values"
    return "negative_values"


def interpret_label_input(candidates: list[dict], base: dict | None, text: str,
                          *, population: str = "") -> dict[str, Any]:
    """Conservative Chinese/English enum grammar; incomplete input stays editable."""
    spec = {"positive_values": [], "negative_values": [], "gray_values": [],
            "exclude_label_values": [], "gray_policy": "exclude", **(base or {})}
    spec = {k: v for k, v in spec.items() if k in (*ROLE_KEYS, "label", "gray_policy", "time_column")}
    text = text.replace("＝", "=").replace("：", ":")
    # An explicit disposition immediately following this description belongs
    # to the same value. The description alone does not imply exclusion.
    text = re.sub(r"(未到表现期)\s*[,，]\s*((?:剔除|排除)(?:该类|这些)?(?:样本|客户|记录))", r"\1\2", text)
    columns = [c["column"] for c in candidates]
    notes, issues = [], []
    named = re.findall(r"\b([A-Za-z_][\w]*)\s*=\s*(?:-?\d|['\"])", text)
    named += re.findall(r"标签列(?:名)?\s*(?:是|为|=|:)?\s*[`'\"]?([A-Za-z_][\w]*)", text)
    resolved = set()
    for name in named:
        if name in columns:
            resolved.add(name)
        else:
            matches = get_close_matches(name, columns, n=2, cutoff=0.75)
            if len(matches) == 1:
                resolved.add(matches[0])
                notes.append(f"文字中的 {name} 对应实际列 {matches[0]}，请核对。")
            else:
                issues.append(f"无法唯一对应标签列 {name}，请填写实际列名。")
    if len(resolved) > 1:
        issues.append("描述涉及多个标签列，请明确使用哪一列。")
    elif resolved:
        label = next(iter(resolved))
        if spec.get("label") and spec["label"] != label:
            for key in ROLE_KEYS:
                spec[key] = []
        spec["label"] = label
    elif not spec.get("label") and len(columns) == 1:
        spec["label"] = columns[0]
        notes.append(f"当前唯一标签候选为 {columns[0]}，请核对。")

    assignments: dict[Any, str] = {}
    recognized = bool(named)
    # Column declarations are not enum values in a following comma list.
    text = re.sub(r"标签列(?:名)?\s*(?:是|为|=|:)?\s*[`'\"]?[A-Za-z_][\w]*[`'\"]?", "", text)
    # Commas inside an explicit enum list belong to that assignment, rather
    # than splitting “-1，1排除” into an undefined -1 and an excluded 1.
    for pattern, _ in _ASSIGNMENTS:
        text = pattern.sub(lambda m: m[0].replace(",", "、").replace("，", "、"), text)
    for clause in re.split(r"[，,；;。\n]", text):
        # Negation, uncertainty and inequalities require clarification, not substring matching.
        if re.search(r"不是|不为|非好|非坏|非灰|可能|不确定|是否|要不要|能否|可否|能不能|建议|考虑|或|另行处理|待定|待确认|无需(?:剔除|排除)|不(?:要|应|应该|需要|能|可|会|再|想|予以)?(?:剔除|排除)|[<>≤≥?？]", clause):
            if re.search(_ROLE, clause, re.I):
                issues.append(f"请明确每个取值的角色：{clause.strip()}")
            continue
        for pattern, reversed_groups in _ASSIGNMENTS:
            match = pattern.search(clause)
            if not match:
                continue
            values, role_text = (match[2], match[1]) if reversed_groups else (match[1], match[2])
            role = _role(role_text)
            for item in re.split(_LIST_SEPARATOR, values):
                value = norm_label_value(item.strip("'\""))
                if value in assignments and assignments[value] != role:
                    issues.append(f"取值 {value} 出现冲突定义，请明确其唯一角色。")
                assignments[value] = role
            recognized = True
            break
    for value, role in assignments.items():
        for key in ROLE_KEYS:
            spec[key] = [v for v in spec.get(key, []) if norm_label_value(v) != value]
        spec[role].append(value)

    cand = next((c for c in candidates if c["column"] == spec.get("label")), None)
    actual = [norm_label_value(v["value"]) for v in (cand or {}).get("value_counts", []) if v["value"] is not None]
    assigned = {norm_label_value(v) for k in ROLE_KEYS for v in spec.get(k, [])}
    remaining = [v for v in actual if v not in assigned]
    remainder_excluded = re.search(r"(?:其他|其它|其余|剩余)(?:取值|值|样本|客户)?\s*(?:是|为|均为|都为|全部)?\s*(?:无效|剔除|排除)", text)
    remainder_unfunded = re.search(r"(?:其他|其它|其余)(?:取值|值|样本|客户)?\s*(?:是|为|均为|都为)?\s*未放款", text)
    if remainder_excluded or (remainder_unfunded and re.search(r"(?:剔除|排除)未放款", population + text)):
        spec["exclude_label_values"] = [*spec.get("exclude_label_values", []), *remaining]
        recognized = True
    if re.search(r"灰(?:样本|客户)?(?:按|视作|作为|当作|视为)好", text):
        spec["gray_policy"] = "as_good"
        recognized = True
    elif re.search(r"灰(?:样本|客户)?(?:按|视作|作为|当作|视为)坏", text):
        spec["gray_policy"] = "as_bad"
        recognized = True
    elif re.search(r"(?:剔除|排除)灰|灰(?:样本|客户)?(?:剔除|排除)", text):
        spec["gray_policy"] = "exclude"
        recognized = True
    if text.strip() and not recognized:
        issues.append("未能明确识别本次补充，请说明实际列名和取值含义，例如：ylabel=2是坏客户。")
    issues.extend(label_spec_issues(spec, candidates))
    return {"modeling_label": spec, "issues": list(dict.fromkeys(issues)),
            "notes": list(dict.fromkeys(notes)), "complete": not issues}


def label_spec_issues(spec: dict, candidates: list[dict]) -> list[str]:
    issues = []
    cand = next((c for c in candidates if c["column"] == spec.get("label")), None)
    if not cand:
        return ["请填写数据中存在的标签列，并核对其取值分布。"]
    if spec.get("gray_policy", "exclude") not in {"exclude", "as_good", "as_bad"}:
        issues.append("灰样本策略无效。")
    seen = set()
    normalized = {}
    for key in ROLE_KEYS:
        if not isinstance(spec.get(key, []), (list, tuple)):
            issues.append(f"{key} 必须为取值列表。")
            continue
        vals = {norm_label_value(v) for v in spec.get(key, [])}
        normalized[key] = vals
        if None in vals or seen & vals:
            issues.append("标签取值必须互斥，且不能包含空值。")
        seen |= vals
    actual = {norm_label_value(v["value"]) for v in cand.get("value_counts", []) if v["value"] is not None}
    missing = actual - seen
    if missing:
        issues.append("尚未定义角色的取值：" + "、".join(map(str, sorted(missing, key=str))))
    if seen - actual:
        issues.append("映射包含数据中不存在的取值：" + "、".join(map(str, sorted(seen - actual, key=str))))
    if not normalized.get("positive_values", set()) & actual:
        issues.append("至少明确一个实际存在的坏样本取值。")
    if not normalized.get("negative_values", set()) & actual and not (normalized.get("gray_values", set()) and spec.get("gray_policy") == "as_good"):
        issues.append("至少明确一个好样本取值。")
    if cand.get("nunique", 0) > len(actual):
        issues.append("标签取值分布未完整返回，请使用完整枚举后确认。")
    return issues


def label_proposal(df: pd.DataFrame, context: dict, message: str = "", base: dict | None = None) -> dict:
    candidates = probe_label_columns(df)
    spec = dict(context.get("modeling_label_spec") or {}) if base is None else dict(base)
    # A known column outside the heuristic candidates remains selectable and inspectable.
    label = spec.get("label")
    if label in df.columns and not any(c["column"] == label for c in candidates):
        s = df[label]
        candidates.append({"column": label, "nunique": int(s.nunique()), "value_counts": [
            {"value": norm_label_value(v), "count": int(n)} for v, n in s.value_counts(dropna=False).head(100).items()]})
    contract = context.get("business_contract") or {}
    source_text = str(contract.get("metric_definition") or "")
    proposal = interpret_label_input(candidates, spec, source_text, population=str(contract.get("population") or ""))
    # A later explicit label review supersedes the earlier planning prose.
    if spec.get("positive_values") and not label_spec_issues(spec, candidates):
        proposal = interpret_label_input(candidates, spec, "")
        confirmed_source = True
    else:
        confirmed_source = False
    if message:
        original_notes = proposal["notes"]
        proposal = interpret_label_input(candidates, proposal["modeling_label"], message,
                                         population=str(contract.get("population") or ""))
        proposal["notes"] = list(dict.fromkeys(original_notes + proposal["notes"]))
    return {**proposal, "label_candidates": candidates, "source_text": source_text,
            "source": "本次对话补充" if message else "已确认标签映射" if confirmed_source else "已确认的业务口径" if source_text else "当前标签配置",
            "message": message}
