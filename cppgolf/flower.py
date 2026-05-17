"""Deterministic junk-code and junk-declaration insertion."""

from __future__ import annotations

import hashlib
import json
import random
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .control_flow_flatten import (
    CfgHelperError,
    _contains_preprocessor_directive,
    _helper_compile_args,
    _helper_parse_code,
    _range,
    _resolve_helper,
)

_IDENT_RE = re.compile(r"\b[A-Za-z_]\w*\b")
_CPP_KEYWORDS = {
    "alignas", "alignof", "and", "and_eq", "asm", "auto", "bitand", "bitor", "bool",
    "break", "case", "catch", "char", "char8_t", "char16_t", "char32_t", "class",
    "compl", "concept", "const", "consteval", "constexpr", "constinit", "const_cast",
    "continue", "co_await", "co_return", "co_yield", "decltype", "default", "delete",
    "do", "double", "dynamic_cast", "else", "enum", "explicit", "export", "extern",
    "false", "float", "for", "friend", "goto", "if", "inline", "int", "long",
    "mutable", "namespace", "new", "noexcept", "not", "not_eq", "nullptr", "operator",
    "or", "or_eq", "private", "protected", "public", "register", "reinterpret_cast",
    "requires", "return", "short", "signed", "sizeof", "static", "static_assert",
    "static_cast", "struct", "switch", "template", "this", "thread_local", "throw",
    "true", "try", "typedef", "typeid", "typename", "union", "unsigned", "using",
    "virtual", "void", "volatile", "wchar_t", "while", "xor", "xor_eq",
}
_BORING_WORDS = {
    "std", "size_t", "uint8_t", "uint16_t", "uint32_t", "uint64_t",
    "int8_t", "int16_t", "int32_t", "int64_t", "cppgolf", "flower",
}
_FALLBACK_WORDS = [
    "state", "value", "guard", "trace", "node", "delta", "slot", "mask",
    "phase", "rank", "token", "frame", "pivot", "scale", "range", "cache",
]


@dataclass(frozen=True)
class _Edit:
    offset: int
    text: str


def insert_flowers(
    code: str,
    *,
    dead_code: bool = True,
    declarations: bool = True,
    functions: list[str] | None = None,
    exclude: list[str] | None = None,
    seed: int = 1,
    dead_blocks_per_function: int = 1,
    declaration_count: int = 24,
    extra_args: list[str] | None = None,
    platform: str | None = None,
    helper_path: Path | None = None,
    config_helper_path: Path | None = None,
    helper_include_dirs: list[Path] | None = None,
    verbose: bool = False,
) -> str:
    """Insert deterministic junk code/declarations at helper-approved ranges."""
    if (not dead_code or dead_blocks_per_function <= 0) and (not declarations or declaration_count <= 0):
        return code

    helper = _resolve_helper(helper_path, config_helper_path)
    targets = list(dict.fromkeys(functions or []))
    excludes = list(dict.fromkeys(exclude or []))
    helper_targets = targets
    if not dead_code or dead_blocks_per_function <= 0:
        helper_targets = ["__unused_target_for_plan__"]

    with tempfile.NamedTemporaryFile(suffix=".cpp", mode="w", encoding="utf-8", newline="", delete=False) as handle:
        handle.write(_helper_parse_code(code, platform=platform))
        source_path = Path(handle.name)
    try:
        plan = _run_flower_helper(source_path, helper, helper_targets, extra_args, platform, helper_include_dirs or [])
    finally:
        try:
            source_path.unlink()
        except OSError:
            pass

    edits: list[_Edit] = []
    if dead_code and dead_blocks_per_function > 0:
        edits.extend(
            _dead_code_edits(
                code,
                plan,
                functions=targets,
                exclude=excludes,
                seed=seed,
                blocks_per_function=dead_blocks_per_function,
                verbose=verbose,
            )
        )
    if declarations and declaration_count > 0:
        edits.extend(_declaration_edits(code, plan, seed=seed, count=declaration_count, verbose=verbose))
    return _apply_insertions(code, edits)


def _run_flower_helper(
    source_path: Path,
    helper: Path,
    functions: list[str],
    extra_args: list[str] | None,
    platform: str | None,
    helper_include_dirs: list[Path],
) -> dict[str, Any]:
    cmd = [str(helper), "-flower-plan"]
    for function in functions:
        cmd.append(f"-function={function}")
    cmd.append(str(source_path))
    cmd.append("--")
    cmd.extend(_helper_compile_args(extra_args, platform, helper_include_dirs))

    with tempfile.NamedTemporaryFile(suffix=".json", mode="w+", encoding="utf-8", newline="", delete=False) as output:
        output_path = Path(output.name)
        proc = subprocess.run(cmd, stdout=output, stderr=subprocess.PIPE, text=True, check=False)
    try:
        if proc.returncode != 0:
            detail = proc.stderr.strip() or f"exit code {proc.returncode}"
            raise CfgHelperError(f"CFG helper flower plan failed: {detail}")
        try:
            with output_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except json.JSONDecodeError as exc:
            raise CfgHelperError(f"CFG helper produced invalid flower JSON: {exc}") from exc
    finally:
        try:
            output_path.unlink()
        except OSError:
            pass
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("flower_plan"), dict):
        raise CfgHelperError("CFG helper produced an unsupported flower schema; rebuild cppgolf-cfg-helper")
    return data["flower_plan"]


def _dead_code_edits(
    code: str,
    plan: dict[str, Any],
    *,
    functions: list[str],
    exclude: list[str],
    seed: int,
    blocks_per_function: int,
    verbose: bool,
) -> list[_Edit]:
    edits: list[_Edit] = []
    seen_bodies: set[tuple[int, int]] = set()
    for function in plan.get("functions", []):
        if not isinstance(function, dict):
            continue
        qualified = str(function.get("qualified_name", ""))
        simple = str(function.get("simple_name", ""))
        if functions and not _matches_any(qualified, simple, functions):
            continue
        if _matches_any(qualified, simple, exclude):
            _log(verbose, f"skip {qualified}: excluded")
            continue
        if function.get("diagnostics"):
            _log(verbose, f"skip {qualified}: helper diagnostics: {function['diagnostics']}")
            continue
        body_range = _range(function.get("body"))
        if body_range in seen_bodies:
            continue
        if body_range is not None:
            seen_bodies.add(body_range)
        if body_range is None or _contains_preprocessor_directive(code[body_range[0] + 1:body_range[1] - 1]):
            _log(verbose, f"skip {qualified}: preprocessor directives inside function body")
            continue
        offsets = [value for value in function.get("insert_offsets", []) if isinstance(value, int)]
        offsets = [_normalize_statement_boundary(code, offset) for offset in offsets if body_range[0] < offset < body_range[1]]
        offsets = list(dict.fromkeys(offset for offset in offsets if body_range[0] < offset < body_range[1]))
        if not offsets:
            continue
        rng = _rng(seed, "dead", qualified, str(body_range))
        rng.shuffle(offsets)
        for index, offset in enumerate(offsets[:blocks_per_function]):
            edits.append(_Edit(offset, _dead_block(code, seed, qualified, offset, index)))
        _log(verbose, f"dead-code {qualified}: {min(len(offsets), blocks_per_function)} block(s)")
    return edits


def _declaration_edits(code: str, plan: dict[str, Any], *, seed: int, count: int, verbose: bool) -> list[_Edit]:
    scopes = [scope for scope in plan.get("scopes", []) if isinstance(scope, dict)]
    candidates: list[tuple[dict[str, Any], int]] = []
    seen: set[tuple[str, str, int]] = set()
    for scope in scopes:
        if scope.get("kind") not in {"global", "namespace", "class", "struct"}:
            continue
        raw_offsets = scope.get("insert_offsets")
        offsets = raw_offsets if isinstance(raw_offsets, list) else [scope.get("insert_offset")]
        for offset in offsets:
            if not isinstance(offset, int) or offset < 0:
                continue
            normalized = _normalize_declaration_boundary(code, offset)
            key = (str(scope.get("kind", "")), str(scope.get("name", "")), normalized)
            if key in seen:
                continue
            seen.add(key)
            candidates.append((scope, normalized))
    if not candidates:
        return []
    rng = _rng(seed, "decls", str(len(candidates)))
    rng.shuffle(candidates)
    edits: list[_Edit] = []
    for index in range(count):
        scope, offset = candidates[index % len(candidates)]
        edits.append(_Edit(offset, _declaration_text(seed, scope, offset, index, plan)))
    _log(verbose, f"declarations: {len(edits)} declaration(s)")
    return edits


def _dead_block(code: str, seed: int, qualified: str, offset: int, index: int) -> str:
    rng = _rng(seed, "dead-template", qualified, str(offset), str(index))
    template = rng.randrange(6)
    names = _name_set(code, offset, seed, f"{qualified}:{offset}", index, 6)
    a = _u32(seed, qualified, index, "a") | 1
    b = _u32(seed, qualified, index, "b") | 1
    c = _u32(seed, qualified, index, "c")
    n0, n1, n2, n3, n4, n5 = names
    if template == 0:
        return (
            "{"
            f"unsigned {n0}={a}u;"
            f"unsigned {n1}={b}u;"
            f"unsigned {n2}=({n0}^{n1})+{c}u;"
            f"if((({n2})==({n2}+1u))&&(({n0}|{n1})==0u))"
            "{"
            f"{n0}^={n1};"
            "}"
            "}"
        )
    if template == 1:
        return (
            f"unsigned {n0}={a}u;"
            f"unsigned {n1}=({n0}^{b}u);"
            f"if((({n1}|1u)==0u)&&(({n0}&{n1})==~{n0}))"
            f"{n0}+={n1};"
        )
    if template == 2:
        return (
            "{"
            f"unsigned {n0}={a}u;"
            f"switch(({n0}^{n0})&3u)"
            "{"
            f"case 1:{n0}+={b}u;break;"
            f"case 2:{n0}^={c}u;break;"
            "default:break;"
            "}"
            "}"
        )
    if template == 3:
        return (
            "{"
            f"unsigned {n0}={a}u;"
            f"while((({n0}|1u)==0u)&&(({n0}^{n0})!=0u))"
            "{"
            f"{n0}^={b}u;"
            "}"
            "}"
        )
    if template == 4:
        return (
            "{"
            f"auto {n0}=[](unsigned {n1}){{return ({n1}^{c}u)+{b}u;}};"
            f"unsigned {n2}={a}u;"
            f"if((({n2}|1u)==0u)&&({n0}({n2})==0u))"
            "{"
            f"{n2}={n0}({n2});"
            "}"
            "}"
        )
    return (
        "{"
        f"enum{{{n0}={int(a & 0x7FFF)}}};"
        f"unsigned {n1}=unsigned({n0})+{b}u;"
        f"for(unsigned {n2}=0u;(({n1}|1u)==0u)&&{n2}<3u;++{n2})"
        "{"
        f"{n1}^={n2};"
        "}"
        "}"
    )


def _declaration_text(seed: int, scope: dict[str, Any], offset: int, index: int, plan: dict[str, Any]) -> str:
    kind = str(scope.get("kind", "global"))
    scope_name = str(scope.get("name", ""))
    source_hint = _scope_source_hint(plan, scope)
    name = _name_from_words(source_hint, offset, seed, scope_name or kind, index, "decl")
    value = _u32(seed, scope_name, index, "decl")
    alt = _u32(seed, scope_name, index, "decl-alt")
    wide = ((value << 32) ^ alt) & 0xFFFFFFFFFFFFFFFF
    template = _rng(seed, "decl-template", kind, scope_name, str(offset), str(index)).randrange(8)
    if kind in {"class", "struct"}:
        return _class_declaration_template(name, value, alt, wide, template)
    return _namespace_declaration_template(name, value, alt, wide, template)


def _class_declaration_template(name: str, value: int, alt: int, wide: int, template: int) -> str:
    if template == 0:
        return f"\nstatic constexpr unsigned {name}={value}u;\n"
    if template == 1:
        return f"\nstatic constexpr unsigned long long {name}={wide}ull;\n"
    if template == 2:
        return f"\nusing {name}=unsigned;\n"
    if template == 3:
        return f"\ntypedef unsigned {name};\n"
    if template == 4:
        return f"\nenum{{{name}=0x{value & 0x7FFF:x}}};\n"
    if template == 5:
        return f"\nstatic unsigned {name}(unsigned x){{return (x^{value}u)+{(value >> 7) | 1}u;}}\n"
    if template == 6:
        return (
            f"\ntemplate<unsigned N> static constexpr unsigned {name}(unsigned x)"
            f"{{return (x^N)+{(alt | 1)}u;}}\n"
        )
    return _macro_template(name, value, alt)


def _namespace_declaration_template(name: str, value: int, alt: int, wide: int, template: int) -> str:
    if template == 0:
        return f"\nstatic constexpr unsigned {name}={value}u;\n"
    if template == 1:
        return f"\nstatic constexpr unsigned long long {name}={wide}ull;\n"
    if template == 2:
        return f"\nusing {name}=unsigned;\n"
    if template == 3:
        return f"\ntypedef unsigned {name};\n"
    if template == 4:
        return f"\nenum{{{name}=0x{value & 0x7FFF:x}}};\n"
    if template == 5:
        return f"\nstatic unsigned {name}(unsigned x){{return (x^{value}u)+{(value >> 11) | 1}u;}}\n"
    if template == 6:
        return (
            f"\ntemplate<unsigned N> static constexpr unsigned {name}(unsigned x)"
            f"{{return (x+N)^{(alt | 1)}u;}}\n"
        )
    return _macro_template(name, value, alt)


def _macro_template(name: str, value: int, alt: int) -> str:
    macro = _macro_name(name)
    return (
        f"\n#if !defined({macro})\n"
        f"#define {macro}(x) (((unsigned)(x)^{value}u)+{(alt | 1)}u)\n"
        f"#undef {macro}\n"
        "#endif\n"
    )


def _apply_insertions(code: str, edits: list[_Edit]) -> str:
    if not edits:
        return code
    grouped: dict[int, list[str]] = {}
    for edit in edits:
        if 0 <= edit.offset <= len(code):
            grouped.setdefault(edit.offset, []).append(edit.text)
    result = code
    for offset, texts in sorted(grouped.items(), reverse=True):
        result = result[:offset] + "".join(texts) + result[offset:]
    return result


def _normalize_statement_boundary(code: str, offset: int) -> int:
    index = offset
    while index < len(code) and code[index] in " \t\r":
        index += 1
    if index < len(code) and code[index] == ";":
        return index + 1
    return offset


def _normalize_declaration_boundary(code: str, offset: int) -> int:
    index = offset
    while index < len(code) and code[index].isspace():
        index += 1
    if index < len(code) and code[index] == ";":
        return index + 1
    if index < len(code) and code[index] == ",":
        semicolon = code.find(";", index + 1)
        if semicolon >= 0:
            return semicolon + 1
    return offset


def _matches_any(qualified_name: str, simple_name: str, patterns: list[str]) -> bool:
    return any(pattern == qualified_name or pattern == simple_name for pattern in patterns)


def _name_set(code: str, offset: int, seed: int, key: str, index: int, count: int) -> list[str]:
    return [_name_from_words(code, offset, seed, key, index, str(i)) for i in range(count)]


def _name_from_words(code: str, offset: int, seed: int, key: str, index: int, role: str) -> str:
    words = _context_words(code, offset, seed, key, index, role)
    digest = _suffix(seed, f"{key}:{role}", index)
    stem = "_".join(words[:2])
    name = f"{stem}_{digest}"
    if not name[0].isalpha():
        name = f"{words[0]}_{name}"
    return name


def _macro_name(name: str) -> str:
    macro = re.sub(r"\W+", "_", name).strip("_").upper()
    if not macro or not macro[0].isalpha():
        macro = f"M_{macro}"
    return macro


def _context_words(code: str, offset: int, seed: int, key: str, index: int, role: str) -> list[str]:
    offset = max(0, min(offset, len(code)))
    start = max(0, offset - 260)
    end = min(len(code), offset + 260)
    tokens = []
    seen = set()
    for token in _IDENT_RE.findall(code[start:end]):
        word = token.strip("_").lower()
        if not _usable_word(word) or word in seen:
            continue
        seen.add(word)
        tokens.append(word)
    if len(tokens) < 2:
        tokens.extend(word for word in _FALLBACK_WORDS if word not in seen)
    rng = _rng(seed, "words", key, str(index), role, str(offset))
    rng.shuffle(tokens)
    return tokens[:2] if len(tokens) >= 2 else ["state", "value"]


def _usable_word(word: str) -> bool:
    if len(word) < 2 or len(word) > 24:
        return False
    if word in _CPP_KEYWORDS or word in _BORING_WORDS:
        return False
    if not word[0].isalpha():
        return False
    if "__" in word:
        return False
    if word.startswith("_"):
        return False
    return True


def _scope_source_hint(plan: dict[str, Any], scope: dict[str, Any]) -> str:
    parts = [str(scope.get("name", "")), str(scope.get("kind", ""))]
    for function in plan.get("functions", []):
        if isinstance(function, dict):
            parts.append(str(function.get("qualified_name", "")))
            parts.append(str(function.get("simple_name", "")))
    return " ".join(parts) or "state value"


def _rng(seed: int, *parts: str) -> random.Random:
    digest = hashlib.blake2s(":".join([str(seed), *parts]).encode("utf-8"), digest_size=8).digest()
    return random.Random(int.from_bytes(digest, "big"))


def _suffix(seed: int, key: str, index: int) -> str:
    digest = hashlib.blake2s(f"{seed}:{key}:{index}".encode("utf-8"), digest_size=5).hexdigest()
    return digest


def _u32(seed: int, key: str, index: int, salt: str) -> int:
    digest = hashlib.blake2s(f"{seed}:{key}:{index}:{salt}".encode("utf-8"), digest_size=4).digest()
    return int.from_bytes(digest, "little")


def _log(verbose: bool, message: str) -> None:
    if verbose:
        import sys

        print(f"[flower] {message}", file=sys.stderr)
