#!/usr/bin/env python3
"""HTML 报告交付审计：静态检查并输出 PASS / WARN / FAIL。"""
import argparse
import pathlib
import re
import sys


def check(text: str) -> list[tuple[str, str]]:
    r = []

    def add(level, msg):
        r.append((level, msg))

    def has(pat, flags=0):
        return re.search(pat, text, flags) is not None

    add('PASS' if has(r'<!DOCTYPE html>', re.I) else 'FAIL', 'DOCTYPE')
    add('PASS' if has(r'<meta[^>]*charset', re.I) else 'FAIL', 'charset meta')
    add('PASS' if has(r'<meta[^>]*viewport', re.I) else 'FAIL', 'viewport meta')
    add('PASS' if has(r'name="color-scheme"') else 'WARN', 'color-scheme meta')

    ext = re.findall(r'<link\b|<script[^>]*\bsrc=|\@import|url\(\s*["\']?https?://', text, re.I)
    add('PASS' if not ext else 'WARN', '外部依赖（%d 处）' % len(ext))

    add('PASS' if has(r'\@media\s+print') else 'FAIL', '@media print 打印样式')
    add('PASS' if has(r'print-color-adjust|print-color') else 'WARN', '打印背景色保真')
    add('PASS' if has(r'\@page\s*\{') else 'WARN', '@page 纸张设置')
    add('PASS' if has(r'<noscript') else 'FAIL', '无 JS 兜底')

    add('PASS' if has(r'location\.hash|history\.replaceState') else 'WARN', 'URL 深链状态')
    add('PASS' if has(r'aria-|role=|tabindex') else 'FAIL', '无障碍属性')
    add('PASS' if has(r'<caption') else 'WARN', '表格 caption')
    add('PASS' if has(r'aria-pressed|aria-sort') else 'WARN', '控件状态语义')

    onclick = len(re.findall(r'\bonclick\s*=', text, re.I))
    add('PASS' if onclick == 0 else 'WARN', '内联事件 onclick（%d 处）' % onclick)

    inline_grid = len(re.findall(r'style="[^"]*grid-template-columns', text, re.I))
    add('PASS' if inline_grid == 0 else 'WARN', '内联网格列数（%d 处，需 !important 收口）' % inline_grid)

    maxline = max((len(l) for l in text.splitlines()), default=0)
    add('PASS' if maxline < 100000 else 'WARN', '最大单行长度（%d 字符，>100k 建议拆分数据）' % maxline)

    tables = len(re.findall(r'<table\b', text, re.I))
    wrappers = len(re.findall(r'class="[^"]*\b(tw|tbl)\b', text, re.I))
    add('PASS' if tables <= wrappers else 'WARN', '宽表滚动容器（%d 表 / %d 容器）' % (tables, wrappers))

    add('FAIL' if has(r'table\s*\{[^}]*overflow:\s*hidden', re.I | re.S) else 'PASS', '表格 overflow:hidden 裁切')

    add('PASS' if has(r'repeat\(\s*auto-fit') else 'WARN', 'auto-fit 自适应网格')
    add('PASS' if has(r'focus-visible') else 'WARN', '键盘焦点可见样式')
    return r


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('report')
    args = ap.parse_args()
    p = pathlib.Path(args.report)
    if not p.exists():
        print('文件不存在: %s' % p, file=sys.stderr)
        return 2
    rows = check(p.read_text(encoding='utf-8'))
    for level, msg in rows:
        print('[%s] %s' % (level, msg))
    fails = sum(1 for lv, _ in rows if lv == 'FAIL')
    warns = sum(1 for lv, _ in rows if lv == 'WARN')
    print('\n汇总: FAIL=%d  WARN=%d  PASS=%d' % (fails, warns, sum(1 for lv, _ in rows if lv == 'PASS')))
    return 1 if fails else 0


if __name__ == '__main__':
    raise SystemExit(main())
