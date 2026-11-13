"""Normalise and validate analyst SQL before it hits a warehouse driver.

DataWorks / MaxCompute SQL often carries SET lines, ``${param}`` placeholders, or
column lists copied with ``|`` separators. DuckDB (the offline backend) cannot
execute those statements even when they are valid on ODPS.
"""

from __future__ import annotations

import re
from typing import List


class SqlPrepareError(ValueError):
    """Analyst SQL cannot be executed as-is on the configured backend."""

# Lines that are valid in an ODPS script but must not be sent to DuckDB.
_ODPS_SCRIPT_LINE = re.compile(
    r"(?i)^\s*(?:"
    r"set\s+(?:odps\.|hive\.|skynet\.|project\.|session\.|job\.|mapreduce\.|mapjoin\.|mapper\.|reducer\.)"
    r"|add\s+(?:jar|file|table)\s"
    r"|use\s+\S+"
    r"|alter\s+session\s"
    r"|cmd\s+"
    r"|list\s+"
    r"|whoami\s*$"
    r")"
)

_SELECT_FROM = re.compile(r"(?is)\bselect\s+(.*?)\s+from\s+")


def _strip_odps_script_lines(sql: str) -> str:
    kept: List[str] = []
    for line in sql.splitlines():
        if _ODPS_SCRIPT_LINE.match(line.strip()):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _find_unbound_question_mark(sql: str) -> bool:
    """Detect JDBC-style ``?`` placeholders outside literals/comments.

    MaxCompute identifiers use backticks; ``?`` inside `` `col?` `` or in ``--`` /
    ``/* */`` comments must not be treated as bind parameters.
    """
    in_single = in_double = in_backtick = False
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        if not in_single and not in_double and not in_backtick:
            if ch == "-" and nxt == "-":
                i += 2
                while i < n and sql[i] not in "\r\n":
                    i += 1
                continue
            if ch == "/" and nxt == "*":
                i += 2
                while i + 1 < n and not (sql[i] == "*" and sql[i + 1] == "/"):
                    i += 1
                i = min(i + 2, n)
                continue

        if ch == "'" and not in_double and not in_backtick:
            if in_single and nxt == "'":
                i += 2
                continue
            in_single = not in_single
            i += 1
            continue
        if ch == '"' and not in_single and not in_backtick:
            in_double = not in_double
            i += 1
            continue
        if ch == "`" and not in_single and not in_double:
            if in_backtick and nxt == "`":
                i += 2
                continue
            in_backtick = not in_backtick
            i += 1
            continue

        if ch == "?" and not in_single and not in_double and not in_backtick:
            return True
        i += 1
    return False


def _select_list_uses_pipes(sql: str) -> bool:
    m = _SELECT_FROM.search(sql)
    if not m:
        return False
    sel = m.group(1)
    if "|" not in sel:
        return False
    # Commas may still appear inside function calls; pipe-only lists are the smell.
    inner = re.sub(r"\([^)]*\)", "", sel)
    return "|" in inner and "," not in inner


def prepare_analyst_sql(sql: str, *, backend: str) -> str:
    """Return SQL safe to send to the configured backend."""
    text = (sql or "").strip()
    if not text:
        raise SqlPrepareError("SQL 为空")

    backend = (backend or "duckdb").strip().lower()

    # DataWorks / ODPS 语法（反引号字段、脚本参数、按位或）由 MaxCompute 校验；
    # 本地 DuckDB 才做模板占位符与列分隔符的启发式拦截。
    if backend == "duckdb":
        if "${" in text:
            raise SqlPrepareError(
                "SQL 仍含 DataWorks 参数占位符 ${...}，请先在 DataWorks 中「展开参数」或替换为具体分区/日期后再提交"
            )
        if _find_unbound_question_mark(text):
            raise SqlPrepareError(
                "SQL 含未替换的参数占位符「?」。请在 DataWorks 运行前替换分区参数（如 dt='20250101'），"
                "不要直接粘贴带问号的模板 SQL"
            )
        if _select_list_uses_pipes(text):
            raise SqlPrepareError(
                "SELECT 列名之间使用了「|」分隔（常见于从表头复制）。请改为英文逗号分隔，"
                "中文或特殊列名在 MaxCompute 中用反引号：`列名`"
            )

    if backend == "duckdb":
        text = _strip_odps_script_lines(text)
        if not text:
            raise SqlPrepareError(
                "去掉 MaxCompute 脚本行（SET/USE 等）后 SQL 为空；生产表请使用 CREDIT_AGENT_DATA_BACKEND=odps"
            )

    return text


def enrich_ingest_error(sql: str, backend: str, exc: Exception) -> str:
    """Turn a driver exception into an actionable message for analysts."""
    backend = (backend or "duckdb").strip().lower()
    name = type(exc).__name__
    detail = str(exc)
    lower_sql = sql.lower()

    hints: List[str] = []
    if backend == "duckdb":
        if "opmgt_dev" in lower_sql or "`" in sql or "maxcompute" in detail.lower():
            hints.append(
                "当前为 DuckDB 本地示例库（CREDIT_AGENT_DATA_BACKEND=duckdb），"
                "无法运行 DataWorks 上的 MaxCompute 宽表/SQL。"
                "请在 .env 设置 CREDIT_AGENT_DATA_BACKEND=odps、填写 CREDIT_AGENT_ODPS_* 凭证，"
                "并用 INSTALL_EXTRAS=odps 构建镜像后 restart 容器"
            )
        elif name == "ParserException":
            hints.append(
                "DuckDB 无法解析该语句。若 SQL 已在 DataWorks 验证通过，应切换为 ODPS 后端而非本地 DuckDB"
            )
    if not hints:
        return f"数仓查询失败: {name}: {detail}"
    return f"数仓查询失败: {name}: {detail}。提示：{' '.join(hints)}"


# --------------------------------------------------------------------------- #
# Verbatim guard: did the executed SQL stay inside what the analyst wrote?
# --------------------------------------------------------------------------- #
_WHERE = re.compile(r"(?is)\bwhere\b")
_FROM_SOURCE = re.compile(r"(?is)\bfrom\s+([A-Za-z0-9_.`\"]+)")


def _normalize_sql(sql: str) -> str:
    text = re.sub(r"\s+", " ", (sql or "").strip())
    return text.rstrip(";").strip().lower()


def _bare_column(item: str) -> str:
    """``a.appl_seq AS seq`` -> ``appl_seq``; keeps comparison alias-agnostic."""
    head = re.split(r"(?is)\s+as\s+", item.strip())[0]
    head = head.split(".")[-1]
    return head.strip("`\" ").lower()


def _select_items(sql: str) -> List[str]:
    match = _SELECT_FROM.search(sql)
    if not match:
        return []
    body = match.group(1).strip()
    # ``select a, b where ... from t`` is legal-ish spacing: cut the list at the
    # first clause keyword so the WHERE predicate never parses as a column.
    body = re.split(
        r"(?is)\bwhere\b|\bgroup\s+by\b|\border\s+by\b|\bhaving\b|\blimit\b", body
    )[0]
    body = re.sub(r"(?is)^\s*(distinct|all)\s+", "", body.strip())
    # Split on top-level commas only, so COALESCE(a, b) stays one item.
    items, depth, buf = [], 0, ""
    for char in body:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            items.append(buf)
            buf = ""
        else:
            buf += char
    items.append(buf)
    return [_bare_column(item) for item in items if item.strip()]


def describe_sql_deviation(user_sql: str, executed_sql: str) -> dict:
    """Report how the executed SQL differs from the analyst's original.

    The failure this guards against is quiet and expensive: the analyst writes
    one query, the agent "helpfully" adds twelve columns and a month filter, and
    every downstream number changes meaning while still looking authoritative.

    Only three kinds of drift are detected — extra/missing columns, a WHERE that
    the analyst did not write, and a different source table. Anything subtler
    (changed CASE logic, a swapped JOIN condition) falls back to the plain
    text-equality verdict in ``verbatim``.
    """
    original = _normalize_sql(user_sql)
    executed = _normalize_sql(executed_sql)
    if not original:
        return {"user_sql_provided": False, "verbatim": None,
                "added_columns": [], "removed_columns": [],
                "added_where": False, "changed_source": False, "note": ""}

    if original == executed:
        return {"user_sql_provided": True, "verbatim": True,
                "added_columns": [], "removed_columns": [],
                "added_where": False, "changed_source": False,
                "note": "与原始 SQL 逐字一致"}

    user_cols, exec_cols = _select_items(user_sql), _select_items(executed_sql)
    added = [c for c in exec_cols if c not in user_cols]
    removed = [c for c in user_cols if c not in exec_cols]
    added_where = bool(_WHERE.search(executed)) and not bool(_WHERE.search(original))

    user_from = _FROM_SOURCE.search(original)
    exec_from = _FROM_SOURCE.search(executed)
    changed_source = bool(
        user_from and exec_from and user_from.group(1).strip("`\" ").lower()
        != exec_from.group(1).strip("`\" ").lower()
    )

    reasons = []
    if added:
        reasons.append(f"新增 {len(added)} 列：{', '.join(added[:8])}")
    if removed:
        reasons.append(f"删除 {len(removed)} 列：{', '.join(removed[:8])}")
    if added_where:
        reasons.append("自行添加了 WHERE 过滤（取数范围已被改变）")
    if changed_source:
        reasons.append("来源表已更换")
    if not reasons:
        reasons.append("文本不一致（可能改动了表达式、连接或排序，需人工核对）")

    return {"user_sql_provided": True, "verbatim": False,
            "added_columns": added, "removed_columns": removed,
            "added_where": added_where, "changed_source": changed_source,
            "note": "；".join(reasons)}
