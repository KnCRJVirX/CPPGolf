"""merge.py — 递归内联本地 #include，去除 include guard / pragma once"""
import re
import sys
from pathlib import Path

_GUARD_TOP    = re.compile(
    r'^\s*#\s*ifndef\s+(\w+)\s*\n\s*#\s*define\s+\1\b[^\n]*\n', re.MULTILINE)
_GUARD_BOTTOM = re.compile(r'\n\s*#\s*endif\s*(?://[^\n]*)?\s*$')
_PRAGMA_ONCE  = re.compile(r'^\s*#\s*pragma\s+once\s*$', re.MULTILINE)


def strip_include_guard(code: str) -> str:
    code = _PRAGMA_ONCE.sub('', code, count=1)
    m = _GUARD_TOP.search(code)
    if m:
        code = code[m.end():]
        code = _GUARD_BOTTOM.sub('', code, count=1)
    return code


def merge_files(filepath: Path, include_dirs: list,
                visited: set, sys_includes: list) -> str:
    real_path = filepath.resolve()
    if real_path in visited:
        return ''
    visited.add(real_path)

    try:
        code = real_path.read_text(encoding='utf-8-sig', errors='replace')
    except FileNotFoundError:
        print(f'[警告] 找不到文件：{real_path}', file=sys.stderr)
        return ''

    code = strip_include_guard(code)
    parts = []

    # 跟踪预处理条件块嵌套深度：depth > 0 表示当前在 #if/#ifdef/#ifndef 内部
    # 处于条件块内的 #include <...> 必须保留在原位，不能提升到文件顶部
    cond_depth = 0

    for line in code.splitlines(keepends=True):
        s = line.strip()

        # 更新条件块深度
        if re.match(r'#\s*if(?:def|ndef)?\b', s):
            cond_depth += 1
        elif re.match(r'#\s*endif\b', s):
            cond_depth = max(0, cond_depth - 1)

        # 系统头文件 #include <...>
        m_sys = re.match(r'#\s*include\s*<([^>]+)>', s)
        if m_sys:
            if cond_depth > 0:
                # 在条件块内：保留在原位，维持条件上下文
                parts.append(line)
            else:
                # 无条件引用：提升到文件顶部统一去重管理
                entry = f'#include <{m_sys.group(1)}>\n'
                if entry not in sys_includes:
                    sys_includes.append(entry)
            continue

        # 本地头文件 #include "..."
        m_loc = re.match(r'#\s*include\s*"([^"]+)"', s)
        if m_loc:
            inc = m_loc.group(1)
            found = None
            for d in [real_path.parent] + list(include_dirs):
                c = (d / inc).resolve()
                if c.exists():
                    found = c; break
            if found:
                if cond_depth > 0:
                    # 在条件块内：不内联，保留原始 include 行
                    parts.append(line)
                else:
                    parts.append(f'\n// ── inlined: {inc} ──\n')
                    parts.append(merge_files(found, include_dirs, visited, sys_includes))
                    parts.append(f'\n// ── end: {inc} ──\n')
            else:
                print(f'[警告] 找不到本地头文件：{inc}', file=sys.stderr)
                parts.append(line)
            continue

        parts.append(line)

    return ''.join(parts)
