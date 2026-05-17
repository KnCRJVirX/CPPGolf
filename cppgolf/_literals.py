"""Helpers for masking C/C++ literals during text transforms."""

from __future__ import annotations

import re


_LITERAL_RE = re.compile(r'\x00L(\d+)\x00')
_RAW_STRING_START_RE = re.compile(r'R"([^()\\ \t\n]*)\(')


def mask_literals(source: str) -> tuple[str, list[str]]:
    """Replace string and character literals with placeholders."""
    literals: list[str] = []
    result: list[str] = []
    i = 0
    n = len(source)

    while i < n:
        raw_match = _RAW_STRING_START_RE.match(source, i)
        if raw_match:
            delimiter = raw_match.group(1)
            end_marker = ")" + delimiter + '"'
            end_index = source.find(end_marker, raw_match.end())
            if end_index == -1:
                result.append(source[i:])
                break
            end_index += len(end_marker)
            result.append(_store_literal(literals, source[i:end_index]))
            i = end_index
            continue

        if source[i] == '"':
            end_index = _scan_quoted_literal(source, i, '"')
            result.append(_store_literal(literals, source[i:end_index]))
            i = end_index
            continue

        if source[i] == "'":
            end_index = _scan_quoted_literal(source, i, "'")
            result.append(_store_literal(literals, source[i:end_index]))
            i = end_index
            continue

        result.append(source[i])
        i += 1

    return "".join(result), literals


def restore_literals(source: str, literals: list[str]) -> str:
    """Restore masked literals back into source text."""
    return _LITERAL_RE.sub(lambda match: literals[int(match.group(1))], source)


def _store_literal(literals: list[str], literal: str) -> str:
    index = len(literals)
    literals.append(literal)
    return f"\x00L{index}\x00"


def _scan_quoted_literal(source: str, start: int, quote: str) -> int:
    i = start + 1
    n = len(source)
    while i < n:
        if source[i] == "\\":
            i += 2
            continue
        if source[i] == quote:
            return i + 1
        i += 1
    return n
