#!/usr/bin/env python3
"""内联数据双轨工具：extract 拆出 const D 数据块，inline 重新合成自包含单文件。"""
import argparse
import pathlib
import re
import sys

PLACEHOLDER = '/*__REPORT_DATA__*/ null'
PATTERN = re.compile(r'(const\s+D\s*=\s*)(\{.*?\})(\s*;\s*\n)', re.S)


def extract(report: pathlib.Path, srcdir: pathlib.Path) -> int:
    text = report.read_text(encoding='utf-8')
    m = PATTERN.search(text)
    if not m:
        print('未找到 const D = {...}; 数据块', file=sys.stderr)
        return 2
    srcdir.mkdir(parents=True, exist_ok=True)
    data = m.group(2)
    # data.js 只存纯 JSON 对象；模板保留 `const D = ` 前缀，inline 时直接替换占位符
    (srcdir / 'data.js').write_text(data + '\n', encoding='utf-8')
    templ = text[:m.start()] + m.group(1) + PLACEHOLDER + m.group(3) + text[m.end():]
    (srcdir / 'report.source.html').write_text(templ, encoding='utf-8')
    print('数据: %s (%d 字符)' % (srcdir / 'data.js', len(data)))
    print('模板: %s' % (srcdir / 'report.source.html'))
    return 0


def inline(templ: pathlib.Path, data_js: pathlib.Path, out: pathlib.Path) -> int:
    t = templ.read_text(encoding='utf-8')
    d = data_js.read_text(encoding='utf-8').strip()
    if PLACEHOLDER not in t:
        print('模板中未找到占位符，未做替换', file=sys.stderr)
        return 2
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(t.replace(PLACEHOLDER, d, 1), encoding='utf-8')
    print('交付版: %s (%d 字符)' % (out, len(out.read_text(encoding='utf-8'))))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest='cmd', required=True)
    e = sub.add_parser('extract')
    e.add_argument('report')
    e.add_argument('srcdir')
    i = sub.add_parser('inline')
    i.add_argument('template')
    i.add_argument('data_js')
    i.add_argument('output')
    a = ap.parse_args()
    if a.cmd == 'extract':
        return extract(pathlib.Path(a.report), pathlib.Path(a.srcdir))
    return inline(pathlib.Path(a.template), pathlib.Path(a.data_js), pathlib.Path(a.output))


if __name__ == '__main__':
    raise SystemExit(main())
