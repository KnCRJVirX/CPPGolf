"""strip_comments.py — 移除 C/C++ 注释（状态机，感知字符串/字符字面量）"""
import re


def strip_comments(code: str) -> str:
    """移除所有 C/C++ 注释，保留字符串/字符字面量内容不变。"""
    result = []
    i = 0
    n = len(code)

    while i < n:
        # 原始字符串 R"delimiter(...)delimiter"
        raw_m = re.match(r'R"([^()\\ \t\n]*)\(', code[i:])
        if raw_m:
            delim = raw_m.group(1)
            end_marker = ')' + delim + '"'
            end_idx = code.find(end_marker, i + raw_m.end())
            if end_idx == -1:
                result.append(code[i:]); break
            end_idx += len(end_marker)
            result.append(code[i:end_idx]); i = end_idx; continue

        # 字符串字面量
        if code[i] == '"':
            j = i + 1
            while j < n:
                if code[j] == '\\': j += 2
                elif code[j] == '"': j += 1; break
                else: j += 1
            result.append(code[i:j]); i = j; continue

        # 字符字面量
        if code[i] == "'":
            j = i + 1
            while j < n:
                if code[j] == '\\': j += 2
                elif code[j] == "'": j += 1; break
                else: j += 1
            result.append(code[i:j]); i = j; continue

        # 行注释  // ...
        if code[i:i+2] == '//':
            j = i + 2
            while j < n:
                if code[j] == '\\' and j+1 < n and code[j+1] == '\n': j += 2
                elif code[j] == '\n': break
                else: j += 1
            result.append(' '); i = j; continue

        # 块注释  /* ... */
        if code[i:i+2] == '/*':
            j = i + 2
            while j < n - 1:
                if code[j:j+2] == '*/': j += 2; break
                j += 1
            nl = code[i:j].count('\n')
            result.append('\n' * nl if nl else ' ')
            i = j; continue

        result.append(code[i]); i += 1

    return ''.join(result)
