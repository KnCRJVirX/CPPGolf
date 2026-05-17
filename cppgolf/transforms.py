"""Semantic text transforms used by the default pipeline."""

from __future__ import annotations

import re

from ._literals import mask_literals, restore_literals


_PREPROCESSOR_RE = re.compile(r"^[ \t]*#[ \t]*(\w+).*$", re.MULTILINE)
_PREPROCESSOR_BLOCK_RE = re.compile(r"[ \t]*#(?:[^\n\\]|\\.)*(?:\\\n(?:[^\n\\]|\\.)*)*")
_STD_NAMESPACE_UNSAFE_RE = re.compile(
    r"(^|\n)[ \t]*using\s+std::"
    r"|(^|\n)[ \t]*(?:struct|class)\s+std::"
    r"|\bstd::(?:mutex|recursive_mutex|timed_mutex|shared_mutex)\b"
    r"|\bstd::(?:unique_lock|lock_guard|scoped_lock)\s*<"
    r"|\bstd::hash\s*<",
    re.MULTILINE,
)
_WARNING_PRAGMAS = (
    '#if defined(__GNUC__)\n'
    '#pragma GCC diagnostic ignored "-Wmisleading-indentation"\n'
    '#pragma GCC diagnostic ignored "-Wunused-function"\n'
    '#pragma GCC diagnostic ignored "-Wunused-variable"\n'
    '#pragma GCC diagnostic ignored "-Wunused-but-set-variable"\n'
    '#pragma GCC diagnostic ignored "-Wunused-local-typedefs"\n'
    '#pragma GCC diagnostic ignored "-Wunused-value"\n'
    '#pragma GCC diagnostic ignored "-Wignored-attributes"\n'
    '#pragma GCC diagnostic ignored "-Wreturn-type"\n'
    '#pragma GCC diagnostic ignored "-Wuninitialized"\n'
    '#pragma GCC diagnostic ignored "-Wmaybe-uninitialized"\n'
    '#pragma GCC diagnostic ignored "-Waggressive-loop-optimizations"\n'
    '#pragma GCC diagnostic ignored "-Warray-bounds"\n'
    '#pragma GCC diagnostic ignored "-Wshift-count-overflow"\n'
    '#pragma GCC diagnostic ignored "-Wstrict-overflow"\n'
    '#pragma GCC diagnostic ignored "-Wtype-limits"\n'
    "#endif\n"
)


def golf_std_namespace(code: str) -> str:
    """Insert `using namespace std;` after includes and drop `std::` prefixes."""
    if _STD_NAMESPACE_UNSAFE_RE.search(code):
        return code

    masked, literals = mask_literals(code)
    masked = re.sub(r"[ \t]*using\s+namespace\s+std\s*;\n?", "", masked)

    insert_at = _find_include_insert_pos(masked)
    masked = masked[:insert_at] + "\nusing namespace std;" + masked[insert_at:]
    masked = re.sub(r"\bstd::", "", masked)
    return restore_literals(masked, literals)


def inject_warning_pragmas(code: str) -> str:
    """Add GCC-compatible pragmas for noisy generated-code warnings."""
    if '#pragma GCC diagnostic ignored "-Wmisleading-indentation"' in code:
        return code
    return _WARNING_PRAGMAS + code


def golf_typedefs(code: str) -> str:
    """Add typedef shortcuts for common long type spellings."""
    masked, literals = mask_literals(code)
    replacements = [
        (r"\bunsigned long long\b", "ull", "typedef unsigned long long ull;"),
        (r"\blong long\b", "ll", "typedef long long ll;"),
        (r"\blong double\b", "ld", "typedef long double ld;"),
        (r"\bvector<int>", "vi", "typedef vector<int> vi;"),
        (r"\bvector<ll>", "vll", "typedef vector<ll> vll;"),
        (r"\bpair<int,int>", "pii", "typedef pair<int,int> pii;"),
        (r"\bpair<ll,ll>", "pll", "typedef pair<ll,ll> pll;"),
    ]

    typedefs_to_add: list[str] = []
    for pattern, short_name, typedef_line in replacements:
        alias = typedef_line.rstrip(";").split()[-1]
        existing_re = re.compile(
            r"^[ \t]*(?:"
            r"typedef\b[^\n]+\b" + re.escape(alias) + r"\s*;"
            r"|#[ \t]*define[ \t]+" + re.escape(alias) + r"\b[^\n]*"
            r")[ \t]*\n?",
            re.MULTILINE,
        )
        existing = existing_re.search(masked)
        if existing:
            masked = masked[:existing.start()] + masked[existing.end():]
            typedefs_to_add.append(typedef_line)
            masked = _replace_type_pattern(masked, pattern, short_name, skip_neighbor_words={"unsigned", "signed"} if short_name == "ll" else set())
            continue

        if len(re.findall(pattern, masked)) >= 2:
            typedefs_to_add.append(typedef_line)
            masked = _replace_type_pattern(masked, pattern, short_name, skip_neighbor_words={"unsigned", "signed"} if short_name == "ll" else set())

    if typedefs_to_add:
        insert_at = _find_include_insert_pos(masked)
        masked = masked[:insert_at] + "\n" + "\n".join(typedefs_to_add) + "\n" + masked[insert_at:]

    return restore_literals(masked, literals)


def _replace_type_pattern(
    text: str,
    pattern: str,
    replacement: str,
    *,
    skip_neighbor_words: set[str] | None = None,
) -> str:
    skip_neighbor_words = skip_neighbor_words or set()
    regex = re.compile(pattern)

    def _replace(match: re.Match[str]) -> str:
        if skip_neighbor_words:
            prev_word = _neighbor_word(text, match.start(), backward=True)
            if prev_word in skip_neighbor_words:
                return match.group(0)
            next_word = _neighbor_word(text, match.end(), backward=False)
            if next_word in skip_neighbor_words:
                return match.group(0)
        return replacement

    return regex.sub(_replace, text)


def _neighbor_word(text: str, index: int, *, backward: bool) -> str | None:
    if backward:
        prefix = text[:index]
        match = re.search(r"([A-Za-z_]\w*)\s*$", prefix)
    else:
        suffix = text[index:]
        match = re.match(r"\s*([A-Za-z_]\w*)", suffix)
    if match is None:
        return None
    return match.group(1)


def golf_remove_main_return(code: str) -> str:
    """Remove the trailing top-level `return 0;` from `int main(...)`."""
    masked, literals = mask_literals(code)
    main_re = re.compile(r"\bint\s+main\s*\(")

    search_start = 0
    while True:
        match = main_re.search(masked, search_start)
        if match is None:
            return restore_literals(masked, literals)

        open_paren = masked.find("(", match.start())
        close_paren = _find_matching(masked, open_paren, "(", ")")
        if close_paren < 0:
            return restore_literals(masked, literals)

        body_start = close_paren + 1
        while body_start < len(masked) and masked[body_start].isspace():
            body_start += 1
        if body_start >= len(masked) or masked[body_start] != "{":
            search_start = match.end()
            continue

        body_end = _find_matching(masked, body_start, "{", "}")
        if body_end < 0:
            return restore_literals(masked, literals)

        inner_start = body_start + 1
        trailing_return = re.search(r"\breturn\s+0\s*;\s*$", masked[inner_start:body_end])
        if trailing_return is None:
            search_start = body_end + 1
            continue

        remove_start = inner_start + trailing_return.start()
        if _brace_depth(masked, inner_start, remove_start) != 0:
            search_start = body_end + 1
            continue

        line_start = masked.rfind("\n", inner_start, remove_start)
        if line_start >= 0 and not masked[line_start + 1:remove_start].strip():
            remove_start = line_start + 1

        masked = masked[:remove_start] + masked[body_end:]
        return restore_literals(masked, literals)


def golf_endl_to_newline(code: str) -> str:
    r"""Replace `endl` with `"\n"`."""
    masked, literals = mask_literals(code)
    newline_literal = r'"\n"'
    masked = re.sub(r"<<\s*(?:std::)?endl\b", lambda _: "<<" + newline_literal, masked)
    masked = re.sub(r"(?<!\w)(?:std::)?endl\b(?=\s*[;,)])", lambda _: newline_literal, masked)
    return restore_literals(masked, literals)


def golf_remove_inline(code: str) -> str:
    """Remove `inline`, except for `inline static`."""
    masked, literals = mask_literals(code)
    masked, preprocessor_blocks = _mask_preprocessor(masked)
    masked = re.sub(r"\binline[ \t]+(?!static\b)", "", masked)
    masked = _restore_preprocessor(masked, preprocessor_blocks)
    return restore_literals(masked, literals)


def golf_windows_lean(code: str) -> str:
    """Inject Windows header conflict guards before common SDK headers."""
    window_header_re = re.compile(
        r"[ \t]*#[ \t]*include[ \t]*<"
        r"(?:[Ww]indows|[Ww]in[Ss]ock2?|[Ww]internl|[Ww]s2tcpip)\.h>"
    )
    match = window_header_re.search(code)
    if match is None:
        return code

    inject = ""
    if "WIN32_LEAN_AND_MEAN" not in code:
        inject += "#ifndef WIN32_LEAN_AND_MEAN\n#define WIN32_LEAN_AND_MEAN\n#endif\n"
    if "_HAS_STD_BYTE" not in code:
        inject += "#ifndef _HAS_STD_BYTE\n#define _HAS_STD_BYTE 0\n#endif\n"

    if not inject:
        return code
    return code[:match.start()] + inject + code[match.start():]


def golf_braces_single_stmt(code: str) -> str:
    """Remove braces from single-statement if/for/while bodies."""
    masked, literals = mask_literals(code)
    keyword_re = re.compile(r"\b(if|for|while)\s*")

    parts: list[str] = []
    i = 0
    n = len(masked)
    while i < n:
        match = keyword_re.search(masked, i)
        if match is None:
            parts.append(masked[i:])
            break

        parts.append(masked[i:match.start()])
        keyword = match.group(1)
        open_paren = match.end()
        if open_paren >= n or masked[open_paren] != "(":
            parts.append(match.group(0))
            i = match.end()
            continue

        close_paren = _find_matching(masked, open_paren, "(", ")")
        if close_paren < 0:
            parts.append(match.group(0))
            i = match.end()
            continue

        body_start = close_paren + 1
        while body_start < n and masked[body_start] in " \t\n":
            body_start += 1
        if body_start >= n or masked[body_start] != "{":
            parts.append(match.group(0))
            i = match.end()
            continue

        body_end = _find_matching(masked, body_start, "{", "}")
        if body_end < 0:
            parts.append(match.group(0))
            i = match.end()
            continue

        body = masked[body_start + 1:body_end].strip()
        if "{" not in body and "}" not in body and body.count(";") == 1 and body.endswith(";"):
            condition = masked[open_paren:close_paren + 1]
            parts.append(f"{keyword}{condition}{body}")
            i = body_end + 1
            continue

        parts.append(match.group(0))
        i = match.end()

    return restore_literals("".join(parts), literals)


def golf_define_shortcuts(code: str) -> str:
    """Insert shortcut defines for frequently used `cout`/`cin`."""
    masked, literals = mask_literals(code)
    shortcuts = [
        (r"\bcout\b", "co", "#define co cout"),
        (r"\bcin\b", "ci", "#define ci cin"),
    ]

    defines_to_add: list[str] = []
    for pattern, short_name, define_line in shortcuts:
        if define_line in masked:
            continue
        if len(re.findall(pattern, masked)) >= 5:
            defines_to_add.append(define_line)
            masked = re.sub(pattern, short_name, masked)

    if defines_to_add:
        insert_at = max(
            (match.end() for match in re.finditer(r"^#(?:include|define)\b.*$", masked, re.MULTILINE)),
            default=0,
        )
        masked = masked[:insert_at] + "\n" + "\n".join(defines_to_add) + "\n" + masked[insert_at:]

    return restore_literals(masked, literals)


def _find_include_insert_pos(code: str) -> int:
    offset = 0
    last_include_end = 0

    for line in code.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped:
            offset += len(line)
            continue

        match = _PREPROCESSOR_RE.match(line)
        if match is None:
            break

        if match.group(1).lower() == "include":
            last_include_end = offset + len(line.rstrip("\r\n"))
        offset += len(line)

    return last_include_end


def _find_matching(code: str, start: int, open_char: str, close_char: str) -> int:
    depth = 0
    for index in range(start, len(code)):
        char = code[index]
        if char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return index
    return -1


def _brace_depth(code: str, start: int, end: int) -> int:
    depth = 0
    for char in code[start:end]:
        if char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
    return depth


def _mask_preprocessor(code: str) -> tuple[str, list[str]]:
    blocks: list[str] = []

    def replace(match: re.Match[str]) -> str:
        index = len(blocks)
        blocks.append(match.group(0))
        return f"\x00P{index}\x00"

    return _PREPROCESSOR_BLOCK_RE.sub(replace, code), blocks


def _restore_preprocessor(code: str, blocks: list[str]) -> str:
    return re.sub(r"\x00P(\d+)\x00", lambda match: blocks[int(match.group(1))], code)
