"""Recursively inline local includes while preserving preprocessor structure."""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path


_GUARD_TOP = re.compile(
    r"^\s*#\s*ifndef\s+([A-Za-z_]\w*)\s*\n\s*#\s*define\s+\1\b[^\n]*\n",
    re.MULTILINE,
)
_PRAGMA_ONCE = re.compile(r"^\s*#\s*pragma\s+once\s*$", re.MULTILINE)
_DIRECTIVE_RE = re.compile(r"^[ \t]*#\s*(\w+)(.*)$", re.DOTALL)
_LOCAL_INCLUDE_RE = re.compile(r'#\s*include\s*"([^"]+)"')
_SYSTEM_INCLUDE_RE = re.compile(r"#\s*include\s*<([^>]+)>")
_IDENT_RE = re.compile(r"[A-Za-z_]\w*")
_NUMBER_RE = re.compile(r"^0[xX][0-9A-Fa-f]+$|^\d+$")
_TOKEN_RE = re.compile(r"\s+|&&|\|\||!|\(|\)|defined|0[xX][0-9A-Fa-f]+|\d+|[A-Za-z_]\w*")
_UNKNOWN = object()
_UNDEFINED = object()


@dataclass
class _ConditionalFrame:
    parent_active: bool
    resolved: bool
    current_active: bool
    branch_taken: bool


def build_macro_table(
    defines: list[str] | None = None,
    undefines: list[str] | None = None,
) -> dict[str, str | None | object]:
    """Build an object-like macro table from CLI `-D` definitions."""
    macros: dict[str, str | None | object] = {}
    for define in defines or []:
        text = define[2:] if define.startswith("-D") else define
        if not text:
            continue
        name, sep, value = text.partition("=")
        name = name.strip()
        if not _IDENT_RE.fullmatch(name):
            continue
        macros[name] = value if sep else None
    for undef in undefines or []:
        name = undef[2:] if undef.startswith("-U") else undef
        name = name.strip()
        if not _IDENT_RE.fullmatch(name):
            continue
        if name not in macros:
            macros[name] = _UNDEFINED
    return macros


def strip_include_guard(code: str) -> str:
    """Remove `#pragma once` and a top-level include guard when safely detected."""
    code = _PRAGMA_ONCE.sub("", code, count=1)
    match = _GUARD_TOP.search(code)
    if match is None:
        return code

    guard_end = _find_matching_endif_span(code, match.end())
    if guard_end is None:
        return code

    return code[:match.start()] + code[match.end():guard_end[0]] + code[guard_end[1]:]


def merge_files(
    filepath: Path,
    include_dirs: list[Path],
    visited: set[Path],
    sys_includes: list[str],
    macros: dict[str, str | None] | None = None,
    once_included: set[Path] | None = None,
    preserve_conditionals: bool = False,
) -> str:
    """Inline local includes and preserve or resolve surrounding preprocessor logic."""
    macros = {} if macros is None else macros
    once_included = set() if once_included is None else once_included

    real_path = filepath.resolve()
    try:
        code = real_path.read_text(encoding="utf-8-sig", errors="replace")
    except FileNotFoundError:
        print(f"[warning] file not found: {real_path}", file=sys.stderr)
        return ""
    has_pragma_once = _PRAGMA_ONCE.search(code) is not None
    if has_pragma_once and real_path in once_included:
        return ""
    guard_match = _GUARD_TOP.search(code)
    guard_macro = guard_match.group(1) if guard_match else None
    if real_path in visited:
        if guard_macro is None or guard_macro not in macros:
            return ""
    else:
        visited.add(real_path)
    if has_pragma_once:
        once_included.add(real_path)
    code = _PRAGMA_ONCE.sub("", code, count=1)
    try:
        parts: list[str] = []
        cond_stack: list[_ConditionalFrame] = []

        for line in _iter_logical_lines(code):
            directive_match = _DIRECTIVE_RE.match(line)
            current_active = cond_stack[-1].current_active if cond_stack else True
            unresolved_active = _has_unresolved_context(cond_stack)

            if directive_match:
                directive = directive_match.group(1).lower()
                body = directive_match.group(2).strip()

                if directive in {"if", "ifdef", "ifndef"}:
                    _push_condition(
                        parts,
                        cond_stack,
                        directive,
                        body,
                        macros,
                        current_active,
                        unresolved_active,
                        line,
                        preserve_conditionals,
                    )
                    continue
                if directive == "elif":
                    _handle_elif(parts, cond_stack, body, macros, line)
                    continue
                if directive == "else":
                    _handle_else(parts, cond_stack, line)
                    continue
                if directive == "endif":
                    _handle_endif(parts, cond_stack, line)
                    continue
                if directive == "define":
                    if current_active:
                        if unresolved_active:
                            _mark_unknown_define(macros, body)
                            parts.append(line)
                        else:
                            _apply_define(macros, body)
                            parts.append(line)
                    continue
                if directive == "undef":
                    if current_active:
                        if unresolved_active:
                            _mark_unknown_undef(macros, body)
                            parts.append(line)
                        else:
                            _apply_undef(macros, body)
                            parts.append(line)
                    continue
                if directive == "include":
                    if not current_active:
                        continue
                    system_include = _SYSTEM_INCLUDE_RE.match(line.strip())
                    local_include = _LOCAL_INCLUDE_RE.match(line.strip())

                    if unresolved_active:
                        if local_include and preserve_conditionals:
                            found = _find_local_include(real_path, include_dirs, local_include.group(1))
                            if found is None:
                                print(f"[warning] local header not found: {local_include.group(1)}", file=sys.stderr)
                                parts.append(line)
                            else:
                                parts.append(f'\n// === inlined: {local_include.group(1)} ===\n')
                                parts.append(
                                    merge_files(
                                        found,
                                        include_dirs,
                                        visited,
                                        sys_includes,
                                        macros,
                                        once_included,
                                        preserve_conditionals,
                                    )
                                )
                                parts.append(f'\n// === end: {local_include.group(1)} ===\n')
                        else:
                            parts.append(line)
                        continue

                    if system_include:
                        if cond_stack:
                            parts.append(line)
                        else:
                            entry = f"#include <{system_include.group(1)}>\n"
                            if entry not in sys_includes:
                                sys_includes.append(entry)
                        continue

                    if local_include:
                        found = _find_local_include(real_path, include_dirs, local_include.group(1))
                        if found is None:
                            print(f"[warning] local header not found: {local_include.group(1)}", file=sys.stderr)
                            parts.append(line)
                        else:
                            parts.append(f'\n// === inlined: {local_include.group(1)} ===\n')
                            parts.append(
                                merge_files(
                                    found,
                                    include_dirs,
                                    visited,
                                    sys_includes,
                                    macros,
                                    once_included,
                                    preserve_conditionals,
                                )
                            )
                            parts.append(f'\n// === end: {local_include.group(1)} ===\n')
                        continue

            if current_active:
                parts.append(line)

        return "".join(parts)
    finally:
        visited.discard(real_path)


def _find_matching_endif_span(code: str, start_pos: int) -> tuple[int, int] | None:
    depth = 1
    offset = 0
    for line in _iter_logical_lines(code[start_pos:]):
        stripped = line.strip()
        if re.match(r"#\s*if(?:def|ndef)?\b", stripped):
            depth += 1
        elif re.match(r"#\s*endif\b", stripped):
            depth -= 1
            if depth == 0:
                start = start_pos + offset
                return start, start + len(line)
        offset += len(line)
    return None


def _push_condition(
    parts: list[str],
    cond_stack: list[_ConditionalFrame],
    directive: str,
    body: str,
    macros: dict[str, str | None | object],
    current_active: bool,
    unresolved_active: bool,
    line: str,
    preserve_conditionals: bool = False,
) -> None:
    if not current_active:
        cond_stack.append(_ConditionalFrame(False, True, False, False))
        return

    if preserve_conditionals:
        cond_stack.append(_ConditionalFrame(True, False, True, False))
        parts.append(line)
        return

    if unresolved_active:
        cond_stack.append(_ConditionalFrame(True, False, True, False))
        parts.append(line)
        return

    resolved = _resolve_condition(directive, body, macros)
    if resolved is None:
        cond_stack.append(_ConditionalFrame(True, False, True, False))
        parts.append(line)
        return

    cond_stack.append(_ConditionalFrame(True, True, resolved, resolved))


def _handle_elif(
    parts: list[str],
    cond_stack: list[_ConditionalFrame],
    body: str,
    macros: dict[str, str | None | object],
    line: str,
) -> None:
    if not cond_stack:
        parts.append(line)
        return

    frame = cond_stack[-1]
    if not frame.parent_active:
        frame.current_active = False
        return

    if not frame.resolved:
        frame.current_active = True
        parts.append(line)
        return

    if frame.branch_taken:
        frame.current_active = False
        return

    resolved = _resolve_if_expression(body, macros)
    if resolved is None:
        frame.current_active = False
        return

    frame.current_active = resolved
    frame.branch_taken = resolved


def _handle_else(parts: list[str], cond_stack: list[_ConditionalFrame], line: str) -> None:
    if not cond_stack:
        parts.append(line)
        return

    frame = cond_stack[-1]
    if not frame.parent_active:
        frame.current_active = False
        return

    if not frame.resolved:
        frame.current_active = True
        parts.append(line)
        return

    frame.current_active = not frame.branch_taken
    frame.branch_taken = True


def _handle_endif(parts: list[str], cond_stack: list[_ConditionalFrame], line: str) -> None:
    if not cond_stack:
        parts.append(line)
        return

    frame = cond_stack.pop()
    if frame.parent_active and not frame.resolved:
        parts.append(line)


def _find_local_include(real_path: Path, include_dirs: list[Path], include_name: str) -> Path | None:
    for directory in [real_path.parent] + list(include_dirs):
        candidate = (directory / include_name).resolve()
        if candidate.exists():
            return candidate
    return None


def _apply_define(macros: dict[str, str | None | object], body: str) -> None:
    match = re.match(r"([A-Za-z_]\w*)(?!\s*\()(?:\s+(.*?))?\s*$", body)
    if match is None:
        return

    name = match.group(1)
    value = match.group(2).strip() if match.group(2) else None
    macros[name] = value


def _apply_undef(macros: dict[str, str | None | object], body: str) -> None:
    match = re.match(r"([A-Za-z_]\w+)\b", body)
    if match is not None:
        macros.pop(match.group(1), None)


def _has_unresolved_context(cond_stack: list[_ConditionalFrame]) -> bool:
    return any(frame.parent_active and not frame.resolved for frame in cond_stack)


def _resolve_condition(
    directive: str,
    body: str,
    macros: dict[str, str | None | object],
) -> bool | None:
    if directive == "ifdef":
        return _macro_is_defined(body, macros)
    if directive == "ifndef":
        defined = _macro_is_defined(body, macros)
        return None if defined is None else not defined
    return _resolve_if_expression(body, macros)


def _macro_is_defined(name: str, macros: dict[str, str | None | object]) -> bool | None:
    name = name.strip()
    if not _IDENT_RE.fullmatch(name):
        return None
    if name not in macros:
        return None if _looks_external_macro(name) else False
    if macros[name] is _UNKNOWN:
        return None
    if macros[name] is _UNDEFINED:
        return False
    return True


def _resolve_if_expression(expr: str, macros: dict[str, str | None | object]) -> bool | None:
    expr = re.sub(r"\\\r?\n", " ", expr)
    tokens = [token for token in _TOKEN_RE.findall(expr) if not token.isspace()]
    if not tokens:
        return None
    parser = _ExprParser(tokens, macros)
    value = parser.parse_expression()
    if parser.pos != len(tokens):
        return None
    return value


class _ExprParser:
    def __init__(self, tokens: list[str], macros: dict[str, str | None | object]) -> None:
        self.tokens = tokens
        self.macros = macros
        self.pos = 0

    def parse_expression(self) -> bool | None:
        return self._parse_or()

    def _parse_or(self) -> bool | None:
        value = self._parse_and()
        while self._peek() == "||":
            self.pos += 1
            rhs = self._parse_and()
            value = _or_bool(value, rhs)
        return value

    def _parse_and(self) -> bool | None:
        value = self._parse_unary()
        while self._peek() == "&&":
            self.pos += 1
            rhs = self._parse_unary()
            value = _and_bool(value, rhs)
        return value

    def _parse_unary(self) -> bool | None:
        token = self._peek()
        if token == "!":
            self.pos += 1
            value = self._parse_unary()
            return None if value is None else not value
        return self._parse_primary()

    def _parse_primary(self) -> bool | None:
        token = self._peek()
        if token is None:
            return None
        if token == "(":
            self.pos += 1
            value = self.parse_expression()
            if self._peek() != ")":
                return None
            self.pos += 1
            return value
        if token == "defined":
            self.pos += 1
            if self._peek() == "(":
                self.pos += 1
                name = self._peek()
                if name is None or not _IDENT_RE.fullmatch(name):
                    return None
                self.pos += 1
                if self._peek() != ")":
                    return None
                self.pos += 1
                if name not in self.macros:
                    return None if _looks_external_macro(name) else False
                if self.macros[name] is _UNKNOWN:
                    return None
                if self.macros[name] is _UNDEFINED:
                    return False
                return True
            name = self._peek()
            if name is None or not _IDENT_RE.fullmatch(name):
                return None
            self.pos += 1
            if name not in self.macros:
                return None if _looks_external_macro(name) else False
            if self.macros[name] is _UNKNOWN:
                return None
            if self.macros[name] is _UNDEFINED:
                return False
            return True
        if _NUMBER_RE.fullmatch(token):
            self.pos += 1
            return int(token, 0) != 0
        if _IDENT_RE.fullmatch(token):
            self.pos += 1
            if token not in self.macros:
                return None if _looks_external_macro(token) else False
            value = self.macros[token]
            if value is _UNKNOWN:
                return None
            if value is _UNDEFINED:
                return False
            if value is None or value == "":
                return True
            value = value.strip()
            if _NUMBER_RE.fullmatch(value):
                return int(value, 0) != 0
            if _IDENT_RE.fullmatch(value):
                return self._resolve_macro_identifier(value, {token})
            return True
        return None

    def _peek(self) -> str | None:
        if self.pos >= len(self.tokens):
            return None
        return self.tokens[self.pos]

    def _resolve_macro_identifier(self, name: str, seen: set[str]) -> bool | None:
        if name not in self.macros:
            return None if _looks_external_macro(name) else False
        value = self.macros[name]
        if value is _UNKNOWN:
            return None
        if value is _UNDEFINED:
            return False
        if value is None or value == "":
            return True
        value = value.strip()
        if _NUMBER_RE.fullmatch(value):
            return int(value, 0) != 0
        if _IDENT_RE.fullmatch(value):
            if value in seen:
                return None
            return self._resolve_macro_identifier(value, seen | {value})
        return True


def _and_bool(lhs: bool | None, rhs: bool | None) -> bool | None:
    if lhs is False or rhs is False:
        return False
    if lhs is True and rhs is True:
        return True
    return None


def _or_bool(lhs: bool | None, rhs: bool | None) -> bool | None:
    if lhs is True or rhs is True:
        return True
    if lhs is False and rhs is False:
        return False
    return None


def _iter_logical_lines(code: str):
    physical_lines = code.splitlines(keepends=True)
    i = 0
    while i < len(physical_lines):
        line = physical_lines[i]
        if _DIRECTIVE_RE.match(line):
            logical = line
            while logical.rstrip("\r\n").endswith("\\") and i + 1 < len(physical_lines):
                i += 1
                logical += physical_lines[i]
            yield logical
        else:
            yield line
        i += 1


def _looks_external_macro(name: str) -> bool:
    return name.startswith("_")


def _mark_unknown_define(macros: dict[str, str | None | object], body: str) -> None:
    match = re.match(r"([A-Za-z_]\w*)(?!\s*\()", body)
    if match is not None:
        macros[match.group(1)] = _UNKNOWN


def _mark_unknown_undef(macros: dict[str, str | None | object], body: str) -> None:
    match = re.match(r"([A-Za-z_]\w+)\b", body)
    if match is not None:
        macros[match.group(1)] = _UNKNOWN
