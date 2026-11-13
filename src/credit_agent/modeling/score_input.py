"""Explicit score direction declarations, independent of LLM availability."""
from __future__ import annotations

import re

DIRECTIONS = {'higher_good', 'higher_risk', 'unknown'}
LABELS = {'higher_good': '越高信用越好', 'higher_risk': '越高风险越高', 'unknown': '方向未知，按原始评分描述'}


def normalize_score_spec(value, columns):
    if not isinstance(value, dict) or set(value) - {'column', 'direction', 'directions'}:
        raise ValueError('评分口径格式无效')
    result = dict(value)
    if 'column' in result and (not isinstance(result['column'], str) or result['column'] not in columns):
        raise ValueError('评分字段不存在')
    if 'direction' in result and result['direction'] not in DIRECTIONS:
        raise ValueError('评分方向无效')
    specific = result.get('directions', {})
    if not isinstance(specific, dict) or any(c not in columns or d not in DIRECTIONS for c, d in specific.items()):
        raise ValueError('逐列评分方向无效')
    return result


def _direction(text):
    if re.search(r'不知道|不清楚|不确定|未确认|尚未确认|可能|是否|不是|并非|不能确认|[?？]', text):
        return None
    good = re.search(r'higher_good|higher_is_better|(?:越高|越大).{0,8}(?:越好|越低)|高分\s*[=＝是为].{0,8}(?:好|低风险)', text, re.I)
    risk = re.search(r'higher_risk|higher_is_worse|(?:越高|越大).{0,8}(?:越坏|风险越高)|高分\s*[=＝是为].{0,8}(?:坏|高风险)', text, re.I)
    return 'higher_good' if good and not risk else 'higher_risk' if risk and not good else None


def score_declaration(text, columns):
    """Unqualified score directions apply globally; named ones only to that column.

    We never infer direction from AUC, the numeric range, or a field name.
    Later explicit clauses supersede earlier clauses, just like dialogue answers.
    """
    result, specific = {}, {}
    for clause in re.split(r'[，,；;。\n]', text):
        named = [str(c) for c in columns if re.search(rf'(?<![A-Za-z0-9_]){re.escape(str(c))}(?![A-Za-z0-9_])', clause)]
        direction = _direction(clause)
        if not direction:
            continue
        if named:
            specific.update({c: direction for c in named})
        elif re.search(r'评分|分数|高分|higher_(?:good|risk|is_better|is_worse)', clause, re.I):
            result['direction'] = direction
    if specific:
        result['directions'] = specific
    return result


def merge_score_spec(base, patch):
    return {**base, **patch, **({'directions': {**base.get('directions', {}), **patch['directions']}}
                              if 'directions' in patch else {})}


def score_direction(config, column):
    return config.get('directions', {}).get(column, config.get('direction', 'unknown'))
