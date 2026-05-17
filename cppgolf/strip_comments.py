"""Remove C/C++ comments while preserving literals."""

from __future__ import annotations


def strip_comments(code: str) -> str:
    """Remove C/C++ comments while preserving string, char, and raw literals."""
    result: list[str] = []
    append = result.append
    i = 0
    n = len(code)
    line_start = True

    while i < n:
        if line_start:
            directive_start = _find_preprocessor_start(code, i)
            if directive_start is not None:
                if directive_start > i:
                    append(code[i:directive_start])
                end_idx = _scan_preprocessor_directive(code, directive_start)
                append(code[directive_start:end_idx])
                line_start = code[end_idx - 1] == "\n" if end_idx > directive_start else True
                i = end_idx
                continue

        ch = code[i]

        if ch == "R" and i + 1 < n and code[i + 1] == '"':
            end_idx = _scan_raw_string(code, i)
            if end_idx is not None:
                append(code[i:end_idx])
                line_start = _ends_with_newline(code, i, end_idx)
                i = end_idx
                continue

        if ch == '"':
            end_idx = _scan_quoted_literal(code, i, '"')
            append(code[i:end_idx])
            line_start = _ends_with_newline(code, i, end_idx)
            i = end_idx
            continue

        if ch == "'":
            end_idx = _scan_quoted_literal(code, i, "'")
            append(code[i:end_idx])
            line_start = _ends_with_newline(code, i, end_idx)
            i = end_idx
            continue

        if ch == "/" and i + 1 < n:
            next_ch = code[i + 1]
            if next_ch == "/":
                append(" ")
                i = _skip_line_comment(code, i + 2)
                continue
            if next_ch == "*":
                end_idx, newline_count = _skip_block_comment(code, i + 2)
                append("\n" * newline_count if newline_count else " ")
                line_start = newline_count > 0 and end_idx <= n and code[end_idx - 1] == "\n"
                i = end_idx
                continue

        append(ch)
        line_start = ch == "\n"
        i += 1

    return "".join(result)


def _scan_raw_string(code: str, start: int) -> int | None:
    """Return the end offset of a raw string literal, or None if not a raw string."""
    i = start + 2
    n = len(code)

    while i < n:
        ch = code[i]
        if ch == "(":
            delimiter = code[start + 2:i]
            end_marker = ")" + delimiter + '"'
            end_idx = code.find(end_marker, i + 1)
            return n if end_idx == -1 else end_idx + len(end_marker)
        if ch in ')"\\ \t\n':
            return None
        i += 1

    return None


def _scan_quoted_literal(code: str, start: int, quote: str) -> int:
    """Return the end offset for a normal string or character literal."""
    i = start + 1
    n = len(code)

    while i < n:
        ch = code[i]
        if ch == "\\":
            i += 2
            continue
        if ch == quote:
            return i + 1
        i += 1

    return n


def _skip_line_comment(code: str, start: int) -> int:
    """Return the offset of the newline ending a `//` comment, or len(code)."""
    i = start
    n = len(code)

    while i < n:
        ch = code[i]
        if ch == "\\" and i + 1 < n and code[i + 1] == "\n":
            i += 2
            continue
        if ch == "\n":
            return i
        i += 1

    return n


def _skip_block_comment(code: str, start: int) -> tuple[int, int]:
    """Return `(end_offset, newline_count)` for a `/* ... */` comment."""
    n = len(code)
    end_marker = code.find("*/", start)
    if end_marker == -1:
        return n, code.count("\n", start, n)
    end_idx = end_marker + 2
    return end_idx, code.count("\n", start, end_idx)


def _find_preprocessor_start(code: str, start: int) -> int | None:
    i = start
    n = len(code)
    while i < n and code[i] in " \t":
        i += 1
    if i < n and code[i] == "#":
        return i
    return None


def _scan_preprocessor_directive(code: str, start: int) -> int:
    i = start
    n = len(code)
    while i < n:
        newline = code.find("\n", i)
        if newline == -1:
            return n
        line_end = newline
        while line_end > i and code[line_end - 1] == "\r":
            line_end -= 1
        if line_end > i and code[line_end - 1] == "\\":
            i = newline + 1
            continue
        return newline + 1
    return n


def _ends_with_newline(code: str, start: int, end: int) -> bool:
    segment = code[start:end]
    return segment.endswith("\n")
