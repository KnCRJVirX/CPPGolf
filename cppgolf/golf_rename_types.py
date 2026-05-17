"""Type renaming for long user-defined C/C++ type names."""

from __future__ import annotations

import itertools
import os
import re
import sys
import tempfile

from .clang_args import build_clang_parse_args, load_clang_cindex


_MIN_TYPE_LEN = 4

_CXX_KEYWORDS: frozenset[str] = frozenset({
    'if', 'else', 'for', 'while', 'do', 'switch', 'case', 'break',
    'continue', 'return', 'goto', 'try', 'catch', 'throw', 'new', 'delete',
    'class', 'struct', 'union', 'enum', 'namespace', 'template', 'typename',
    'public', 'private', 'protected', 'virtual', 'inline', 'static',
    'extern', 'const', 'volatile', 'mutable', 'friend', 'explicit',
    'operator', 'sizeof', 'alignof', 'decltype', 'typedef', 'using',
    'bool', 'char', 'short', 'int', 'long', 'float', 'double', 'void',
    'auto', 'true', 'false', 'nullptr', 'this',
    'BOOL', 'VOID', 'DWORD', 'WORD', 'BYTE', 'HANDLE',
    'TRUE', 'FALSE', 'NULL', 'LONG', 'LONGLONG', 'ULONGLONG', 'ULONG',
})


def golf_rename_types(
    code: str,
    lang: str = 'c++',
    extra_args: list[str] | None = None,
    platform: str | None = None,
    verbose: bool = False,
) -> str:
    """Add typedef aliases and rename TYPE_REF / TEMPLATE_REF occurrences."""
    ci = load_clang_cindex('type renaming')
    src_bytes = code.encode('utf-8')
    std = '-std=c++17' if lang == 'c++' else '-std=c17'
    args = build_clang_parse_args(lang=lang, std=std, extra_args=extra_args, platform=platform)

    suffix = '.cpp' if lang == 'c++' else '.c'
    fd, temp_path = tempfile.mkstemp(suffix=suffix)
    try:
        os.write(fd, src_bytes)
        os.close(fd)

        index = ci.Index.create()
        translation_unit = index.parse(
            temp_path,
            args=args,
            options=(
                ci.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
                | ci.TranslationUnit.PARSE_INCOMPLETE
            ),
        )

        type_decls = _collect_type_decls(ci, translation_unit, temp_path)
        if not type_decls:
            return code

        rename_map = _build_rename_map(code, type_decls, verbose)
        replacements = _collect_type_references(ci, translation_unit, temp_path, src_bytes, rename_map, type_decls)
        inserts = _build_typedef_inserts(src_bytes, type_decls, rename_map)

        if not replacements and not inserts:
            return code

        result = _apply_ops(src_bytes, replacements, inserts)
        return _rewrite_using_declarations(result, type_decls, rename_map)
    finally:
        try:
            os.unlink(temp_path)
        except OSError:
            pass


def _gen_type_short_names():
    for length in itertools.count(1):
        for combo in itertools.product('ABCDEFGHIJKLMNOPQRSTUVWXYZ', repeat=length):
            yield ''.join(combo)


def _collect_type_decls(ci, translation_unit, temp_path: str) -> dict[str, tuple[str, int, int]]:
    type_decl_kinds = frozenset({
        ci.CursorKind.STRUCT_DECL,      # type: ignore[attr-defined]
        ci.CursorKind.CLASS_DECL,       # type: ignore[attr-defined]
        ci.CursorKind.CLASS_TEMPLATE,   # type: ignore[attr-defined]
        ci.CursorKind.ENUM_DECL,        # type: ignore[attr-defined]
    })
    type_decls: dict[str, tuple[str, int, int]] = {}

    def visit(cursor) -> None:
        location = cursor.location
        in_user_file = False
        if location.file:
            try:
                in_user_file = os.path.samefile(location.file.name, temp_path)
            except OSError:
                in_user_file = False

        if in_user_file and cursor.kind in type_decl_kinds:
            name = cursor.spelling
            usr = cursor.get_usr() or ''
            if name and len(name) >= _MIN_TYPE_LEN and usr and cursor.is_definition():
                try:
                    start_offset = cursor.extent.start.offset
                    end_offset = cursor.extent.end.offset
                except Exception:
                    start_offset = end_offset = -1
                if start_offset >= 0 and usr not in type_decls:
                    type_decls[usr] = (name, start_offset, end_offset)

        for child in cursor.get_children():
            visit(child)

    visit(translation_unit.cursor)
    return type_decls


def _build_rename_map(
    code: str,
    type_decls: dict[str, tuple[str, int, int]],
    verbose: bool,
) -> dict[str, str]:
    occupied = set(re.findall(r'\b[A-Za-z_]\w*\b', code)) | _CXX_KEYWORDS
    sorted_usrs = sorted(type_decls, key=lambda usr: type_decls[usr][1])

    rename_map: dict[str, str] = {}
    generator = _gen_type_short_names()
    for usr in sorted_usrs:
        original_name = type_decls[usr][0]
        short_name = next(generator)
        while short_name in occupied or short_name == original_name:
            short_name = next(generator)
        rename_map[usr] = short_name
        occupied.add(short_name)
        if verbose:
            print(f'[golf_rename_types] {original_name} -> {short_name}', file=sys.stderr)
    return rename_map


def _collect_type_references(
    ci,
    translation_unit,
    temp_path: str,
    src_bytes: bytes,
    rename_map: dict[str, str],
    type_decls: dict[str, tuple[str, int, int]],
) -> list[tuple[int, int, str]]:
    type_ref_kinds = {ci.CursorKind.TYPE_REF}  # type: ignore[attr-defined]
    template_ref = getattr(ci.CursorKind, 'TEMPLATE_REF', None)
    if template_ref is not None:
        type_ref_kinds.add(template_ref)

    self_extents = {usr: (data[1], data[2]) for usr, data in type_decls.items()}
    replacements: list[tuple[int, int, str]] = []

    for token in translation_unit.get_tokens(extent=translation_unit.cursor.extent):
        if token.kind.name != 'IDENTIFIER':
            continue

        location = token.location
        if not location.file:
            continue
        try:
            if not os.path.samefile(location.file.name, temp_path):
                continue
        except OSError:
            continue

        offset = location.offset
        spelling = token.spelling
        byte_length = len(spelling.encode('utf-8'))
        if src_bytes[offset:offset + byte_length] != spelling.encode('utf-8'):
            continue

        cursor = token.cursor
        if cursor.kind not in type_ref_kinds:
            continue

        referenced = cursor.referenced
        if referenced is None:
            continue

        referenced_usr = referenced.get_usr() or ''
        if referenced_usr not in rename_map:
            continue

        self_start, self_end = self_extents.get(referenced_usr, (-1, -1))
        if self_start <= offset <= self_end:
            continue

        replacements.append((offset, byte_length, rename_map[referenced_usr]))

    return replacements


def _build_typedef_inserts(
    src_bytes: bytes,
    type_decls: dict[str, tuple[str, int, int]],
    rename_map: dict[str, str],
) -> list[tuple[int, str]]:
    inserts: list[tuple[int, str]] = []
    for usr, (original_name, _start_offset, end_offset) in type_decls.items():
        short_name = rename_map.get(usr)
        if short_name is None:
            continue

        position = end_offset
        while position < len(src_bytes) and src_bytes[position:position + 1] not in (b';', b'}'):
            position += 1
        insert_pos = position + 1 if position < len(src_bytes) else len(src_bytes)
        inserts.append((insert_pos, f'\ntypedef {original_name} {short_name};'))
    return inserts


def _apply_ops(
    src_bytes: bytes,
    replacements: list[tuple[int, int, str]],
    inserts: list[tuple[int, str]],
) -> str:
    operations: list[tuple[int, int, str, bool]] = []
    for offset, byte_length, new_name in replacements:
        operations.append((offset, byte_length, new_name, False))
    for insert_offset, text in inserts:
        operations.append((insert_offset, 0, text, True))

    operations.sort(key=lambda item: (-item[0], item[3]))

    result = bytearray(src_bytes)
    for offset, byte_length, new_text, is_insert in operations:
        encoded = new_text.encode('utf-8')
        if is_insert:
            result[offset:offset] = encoded
        else:
            result[offset:offset + byte_length] = encoded
    return result.decode('utf-8', errors='replace')


def _rewrite_using_declarations(
    code: str,
    type_decls: dict[str, tuple[str, int, int]],
    rename_map: dict[str, str],
) -> str:
    original_to_short = {
        type_decls[usr][0]: short_name
        for usr, short_name in rename_map.items()
        if usr in type_decls
    }

    def replace(match: re.Match[str]) -> str:
        prefix = match.group(1)
        name = match.group(2)
        suffix = match.group(3)
        return prefix + original_to_short.get(name, name) + suffix

    return re.sub(
        r'(\busing\s+(?:\w+::)+)([A-Za-z_]\w*)([ \t]*;)',
        replace,
        code,
    )
