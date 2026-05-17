"""
golf_rename.py — Pass 5: libclang-backed symbol renaming.

This implementation favors correctness over aggressiveness:
- only AST-backed or uniquely attributable token sites are renamed
- any ambiguous group is skipped as a whole
- no proximity-based or "nearest use" guessing is performed
"""
from __future__ import annotations

import itertools
import os
import re
import sys as _sys
import tempfile
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .clang_args import (
    build_clang_parse_args,
    get_specialized_cursor_template,
    load_clang_cindex,
)

ci = None
_clang_getSpecializedCursorTemplate = None

if TYPE_CHECKING:
    import clang.cindex as _ci


_MIN_RENAME_LEN = 2
_MIN_METHOD_RENAME_LEN = 4
_PROTECTED_NAMES = frozenset({
    "main", "WinMain", "wWinMain", "DllMain", "wmain",
})
_IDENT_RE = re.compile(r"\b[A-Za-z_]\w*\b")
_FUNCTION_KIND_NAMES = frozenset({"FUNCTION_DECL", "CXX_METHOD", "FUNCTION_TEMPLATE"})
_CLASS_KIND_NAMES = frozenset({"STRUCT_DECL", "CLASS_DECL", "CLASS_TEMPLATE"})
_RENAMEABLE_DECL_KIND_NAMES = frozenset({"VAR_DECL", "FIELD_DECL", "PARM_DECL"}) | _FUNCTION_KIND_NAMES
_REFERENCE_KIND_NAMES = frozenset({"DECL_REF_EXPR", "MEMBER_REF_EXPR", "MEMBER_REF"})

_CXX_KEYWORDS = frozenset({
    "auto", "break", "case", "char", "const", "continue", "default",
    "do", "double", "else", "enum", "extern", "float", "for", "goto",
    "if", "inline", "int", "long", "register", "restrict", "return",
    "short", "signed", "sizeof", "static", "struct", "switch", "typedef",
    "union", "unsigned", "void", "volatile", "while",
    "alignas", "alignof", "and", "and_eq", "asm", "bitand", "bitor",
    "bool", "catch", "class", "compl", "concept", "consteval", "constexpr",
    "constinit", "co_await", "co_return", "co_yield", "decltype", "delete",
    "explicit", "export", "false", "friend", "mutable", "namespace",
    "new", "noexcept", "not", "not_eq", "nullptr", "operator", "or",
    "or_eq", "private", "protected", "public", "requires", "static_assert",
    "static_cast", "dynamic_cast", "reinterpret_cast", "const_cast",
    "template", "this", "thread_local", "throw", "true", "try", "typeid",
    "typename", "using", "virtual", "wchar_t", "xor", "xor_eq",
    "NULL", "TRUE", "FALSE", "EOF", "stdin", "stdout", "stderr",
    "ll", "ull", "ld", "vi", "vll", "pii", "pll",
})


@dataclass(frozen=True)
class SymbolDecl:
    usr: str
    name: str
    kind: str
    offset: int
    length: int
    source_path: str
    parent_usr: str
    function_usr: str | None
    class_usr: str | None
    is_member: bool
    is_function: bool
    is_virtual: bool


@dataclass(frozen=True)
class FunctionContext:
    usr: str
    start: int
    end: int
    parent_usr: str
    class_usr: str | None


@dataclass
class RenameGroup:
    key: tuple[str, ...]
    name: str
    parent_usr: str
    function_usr: str | None
    class_usr: str | None
    is_member: bool
    is_function: bool
    is_virtual: bool
    decl_usrs: set[str] = field(default_factory=set)
    decl_offsets: set[int] = field(default_factory=set)
    use_offsets: set[int] = field(default_factory=set)
    skip_reasons: list[str] = field(default_factory=list)

    @property
    def first_offset(self) -> int:
        return min(self.decl_offsets) if self.decl_offsets else 0

    def mark_skipped(self, reason: str) -> None:
        if reason not in self.skip_reasons:
            self.skip_reasons.append(reason)


@dataclass(frozen=True)
class TokenSite:
    offset: int
    length: int
    name: str
    group_key: tuple[str, ...] | None
    is_member_access: bool
    is_qualified_access: bool
    in_macro_body: bool
    function_usr: str | None
    class_usr: str | None
    parent_scope_usr: str


@dataclass(frozen=True)
class RewriteEdit:
    offset: int
    length: int
    replacement: str
    group_key: tuple[str, ...]


class RenamePlanner:
    def __init__(
        self,
        *,
        ci_module,
        tmppath: str,
        src_bytes: bytes,
        code: str,
        rename_functions: bool,
        verbose: bool,
    ) -> None:
        self.ci = ci_module
        self.tmppath = tmppath
        self.src_bytes = src_bytes
        self.code = code
        self.rename_functions = rename_functions
        self.verbose = verbose
        self.index = ci_module.Index.create()
        self.tu = self.index.parse(
            tmppath,
            args=[],
            options=(
                ci_module.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
                | ci_module.TranslationUnit.PARSE_INCOMPLETE
            ),
        )
        self.decls: dict[str, SymbolDecl] = {}
        self.function_contexts: list[FunctionContext] = []
        self.groups: dict[tuple[str, ...], RenameGroup] = {}
        self.usr_to_group: dict[str, tuple[str, ...]] = {}
        self.name_to_groups: dict[str, list[tuple[str, ...]]] = defaultdict(list)
        self.tokens: list[TokenSite] = []
        self.function_ranges: list[FunctionContext] = []
        self.function_starts: list[int] = []

    def run(self) -> str:
        self._collect_ast(self.tu.cursor)
        if not self.decls:
            return self.code

        self._prepare_function_ranges()
        self._build_groups()
        self._collect_tokens()
        self._validate_groups()
        rename_map = self._assign_short_names()
        if not rename_map:
            return self.code

        edits = self._build_edits(rename_map)
        if not edits:
            return self.code
        return self._apply_edits(edits)

    def _collect_ast(self, cursor: "_ci.Cursor") -> None:
        if cursor.kind.is_invalid():
            return

        if self._is_user_file(cursor):
            kind_name = cursor.kind.name
            if kind_name in _FUNCTION_KIND_NAMES:
                self._collect_function_context(cursor)
            if kind_name in _RENAMEABLE_DECL_KIND_NAMES:
                decl = self._make_decl(cursor)
                if decl is not None:
                    self.decls[decl.usr] = decl

        for child in cursor.get_children():
            self._collect_ast(child)

    def _collect_function_context(self, cursor: "_ci.Cursor") -> None:
        if not cursor.is_definition():
            return
        usr = cursor.get_usr() or ""
        if not usr:
            return
        try:
            start = cursor.extent.start.offset
            end = cursor.extent.end.offset
        except Exception:
            return
        if end <= start:
            return
        parent_usr = self._safe_usr(cursor.semantic_parent)
        class_usr = self._enclosing_class_usr(cursor.semantic_parent)
        self.function_contexts.append(FunctionContext(usr, start, end, parent_usr, class_usr))

    def _make_decl(self, cursor: "_ci.Cursor") -> SymbolDecl | None:
        kind_name = cursor.kind.name
        name = cursor.spelling
        if not name:
            return None

        if kind_name == "PARM_DECL":
            parent = cursor.semantic_parent
            if parent is not None and parent.kind.name == "CXX_METHOD" and parent.is_virtual_method():
                return None

        is_function = kind_name in _FUNCTION_KIND_NAMES
        min_len = _MIN_METHOD_RENAME_LEN if is_function else _MIN_RENAME_LEN
        if len(name) < min_len:
            return None
        if is_function and (name in _PROTECTED_NAMES or name.startswith("operator")):
            return None
        if is_function and (name != name.lower() or "_" in name):
            return None

        usr = cursor.get_usr() or ""
        if not usr:
            return None
        offset = self._resolve_decl_offset(cursor, name)
        if offset is None:
            return None

        parent_usr = self._safe_usr(cursor.semantic_parent)
        function_usr = None if is_function else self._enclosing_function_usr(cursor.semantic_parent)
        class_usr = self._enclosing_class_usr(cursor.semantic_parent)
        is_member = kind_name == "FIELD_DECL" or (is_function and class_usr is not None)
        is_virtual = kind_name == "CXX_METHOD" and cursor.is_virtual_method()
        return SymbolDecl(
            usr=usr,
            name=name,
            kind=kind_name,
            offset=offset,
            length=len(name.encode("utf-8")),
            source_path=self._cursor_path(cursor),
            parent_usr=parent_usr,
            function_usr=function_usr,
            class_usr=class_usr,
            is_member=is_member,
            is_function=is_function,
            is_virtual=is_virtual,
        )

    def _resolve_decl_offset(self, cursor: "_ci.Cursor", spelling: str) -> int | None:
        encoded = spelling.encode("utf-8")
        for attr in ("location", "extent"):
            try:
                offset = cursor.location.offset if attr == "location" else cursor.extent.start.offset
            except Exception:
                continue
            if self.src_bytes[offset:offset + len(encoded)] == encoded:
                return offset
        return None

    def _build_groups(self) -> None:
        for decl in sorted(self.decls.values(), key=lambda d: d.offset):
            key = self._group_key(decl)
            group = self.groups.get(key)
            if group is None:
                group = RenameGroup(
                    key=key,
                    name=decl.name,
                    parent_usr=decl.parent_usr,
                    function_usr=decl.function_usr,
                    class_usr=decl.class_usr,
                    is_member=decl.is_member,
                    is_function=decl.is_function,
                    is_virtual=decl.is_virtual,
                )
                self.groups[key] = group
                self.name_to_groups[decl.name].append(key)
            group.decl_usrs.add(decl.usr)
            group.decl_offsets.add(decl.offset)
            self.usr_to_group[decl.usr] = key
        for group in self.groups.values():
            if group.is_virtual:
                group.mark_skipped("virtual methods are conservatively skipped")
            if any(_is_external_path(self.decls[usr].source_path) for usr in group.decl_usrs):
                group.mark_skipped("external library declarations are skipped")

    def _group_key(self, decl: SymbolDecl) -> tuple[str, ...]:
        if decl.is_function:
            if decl.is_virtual:
                return ("virtual", decl.name)
            return ("function", decl.parent_usr, decl.name)
        return ("decl", decl.usr)

    def _prepare_function_ranges(self) -> None:
        self.function_ranges = sorted(self.function_contexts, key=lambda ctx: (ctx.start, -(ctx.end - ctx.start)))
        self.function_starts = [ctx.start for ctx in self.function_ranges]

    def _collect_tokens(self) -> None:
        prev_spelling = ""
        for token in self.tu.get_tokens(extent=self.tu.cursor.extent):
            spelling = token.spelling
            if token.kind.name != "IDENTIFIER":
                prev_spelling = spelling
                continue

            location = token.location
            if not location.file:
                prev_spelling = spelling
                continue
            try:
                if not os.path.samefile(location.file.name, self.tmppath):
                    prev_spelling = spelling
                    continue
            except OSError:
                prev_spelling = spelling
                continue

            offset = location.offset
            encoded = spelling.encode("utf-8")
            if self.src_bytes[offset:offset + len(encoded)] != encoded:
                prev_spelling = spelling
                continue

            context = self._function_context_at(offset)
            cursor = token.cursor
            semantic_parent = None
            try:
                semantic_parent = cursor.semantic_parent
            except Exception:
                semantic_parent = None
            parent_scope_usr = self._safe_usr(semantic_parent) or (context.parent_usr if context is not None else "")
            class_usr = self._enclosing_class_usr(semantic_parent) or (context.class_usr if context is not None else None)
            function_usr = self._enclosing_function_usr(semantic_parent) or (context.usr if context is not None else None)
            is_member_access = prev_spelling in (".", "->")
            is_qualified_access = prev_spelling == "::"
            in_macro_body = cursor.kind.name == "MACRO_DEFINITION"

            group_key = self._resolve_token_group(cursor, offset, spelling)
            token_site = TokenSite(
                offset=offset,
                length=len(encoded),
                name=spelling,
                group_key=group_key,
                is_member_access=is_member_access,
                is_qualified_access=is_qualified_access,
                in_macro_body=in_macro_body,
                function_usr=function_usr,
                class_usr=class_usr,
                parent_scope_usr=parent_scope_usr,
            )
            self.tokens.append(token_site)
            if group_key is not None:
                self.groups[group_key].use_offsets.add(offset)
            prev_spelling = spelling

    def _resolve_token_group(self, cursor: "_ci.Cursor", offset: int, spelling: str) -> tuple[str, ...] | None:
        decl_usr = self._decl_usr_for_token(cursor, offset, spelling)
        if decl_usr is not None:
            return self.usr_to_group.get(decl_usr)

        referenced_usr = self._referenced_usr_for_token(cursor, spelling)
        if referenced_usr is not None:
            return self.usr_to_group.get(referenced_usr)
        return None

    def _decl_usr_for_token(self, cursor: "_ci.Cursor", offset: int, spelling: str) -> str | None:
        if self._cursor_decl_matches_token(cursor, offset, spelling):
            usr = cursor.get_usr() or ""
            if usr in self.decls:
                return usr

        if cursor.kind == self.ci.CursorKind.DECL_STMT:
            for child in cursor.get_children():
                if self._cursor_decl_matches_token(child, offset, spelling):
                    usr = child.get_usr() or ""
                    if usr in self.decls:
                        return usr
        return None

    def _cursor_decl_matches_token(self, cursor: "_ci.Cursor", offset: int, spelling: str) -> bool:
        if cursor.kind.name not in _RENAMEABLE_DECL_KIND_NAMES:
            return False
        if cursor.spelling != spelling:
            return False
        if spelling in _PROTECTED_NAMES or spelling.startswith("operator"):
            return False
        encoded = spelling.encode("utf-8")
        for candidate in self._candidate_offsets(cursor):
            if candidate == offset and self.src_bytes[candidate:candidate + len(encoded)] == encoded:
                return True
        return False

    def _referenced_usr_for_token(self, cursor: "_ci.Cursor", spelling: str) -> str | None:
        if cursor.kind.name not in _REFERENCE_KIND_NAMES and cursor.spelling != spelling:
            return None
        try:
            referenced = cursor.referenced
        except Exception:
            return None
        if referenced is None or referenced.kind.is_invalid():
            return None
        if referenced.kind.name not in _RENAMEABLE_DECL_KIND_NAMES:
            return None

        usr = referenced.get_usr() or ""
        if usr in self.decls:
            return usr
        if (
            "<" in usr
            and _clang_getSpecializedCursorTemplate is not None
            and referenced.kind.name in _FUNCTION_KIND_NAMES
        ):
            try:
                template_cursor = _clang_getSpecializedCursorTemplate(referenced)
            except Exception:
                template_cursor = None
            if template_cursor is not None and not template_cursor.kind.is_invalid():
                template_usr = template_cursor.get_usr() or ""
                if template_usr in self.decls:
                    return template_usr
        return None

    def _validate_groups(self) -> None:
        self._mark_collision_prone_groups()
        for token in self.tokens:
            if token.group_key is not None:
                continue
            candidate_keys = self._candidate_groups_for_token(token)
            if not candidate_keys:
                continue
            reason = f"unresolved token `{token.name}` at byte {token.offset}"
            for key in candidate_keys:
                self.groups[key].mark_skipped(reason)

    def _mark_collision_prone_groups(self) -> None:
        for name, keys in self.name_to_groups.items():
            if len(keys) <= 1:
                continue
            if not any(self.groups[key].is_function or self.groups[key].is_member for key in keys):
                continue
            reason = f"multiple rename groups share name `{name}`"
            for key in keys:
                group = self.groups[key]
                if group.is_function or group.is_member:
                    group.mark_skipped(reason)

    def _candidate_groups_for_token(self, token: TokenSite) -> list[tuple[str, ...]]:
        keys = list(self.name_to_groups.get(token.name, ()))
        if not keys:
            return []

        groups = [self.groups[key] for key in keys]
        if token.is_member_access:
            groups = [group for group in groups if group.is_member]
            if token.class_usr is not None:
                same_class = [group for group in groups if group.class_usr == token.class_usr]
                if same_class:
                    groups = same_class
            return [group.key for group in groups] or keys

        if token.is_qualified_access:
            scoped = []
            if token.class_usr is not None:
                scoped.extend(group for group in groups if group.class_usr == token.class_usr)
            if token.parent_scope_usr:
                scoped.extend(group for group in groups if group.parent_usr == token.parent_scope_usr)
            if scoped:
                return list(dict.fromkeys(group.key for group in scoped))
            return [group.key for group in groups if group.is_function or group.is_member] or keys

        scoped: list[RenameGroup] = []
        if token.function_usr is not None:
            scoped.extend(
                group for group in groups
                if group.function_usr == token.function_usr and not group.is_function
            )
        if token.class_usr is not None:
            scoped.extend(
                group for group in groups
                if group.class_usr == token.class_usr and group.is_member
            )
            scoped.extend(
                group for group in groups
                if group.is_function and group.parent_usr == token.class_usr
            )
        scoped.extend(
            group for group in groups
            if group.is_function and group.parent_usr == token.parent_scope_usr and not group.is_member
        )

        if scoped:
            return list(dict.fromkeys(group.key for group in scoped))
        fallback = [group.key for group in groups if not group.is_member]
        return fallback or keys

    def _assign_short_names(self) -> dict[tuple[str, ...], str]:
        all_existing = set(_IDENT_RE.findall(self.code))
        occupied = all_existing | _CXX_KEYWORDS
        groups = [group for group in self.groups.values() if not group.skip_reasons]
        groups.sort(key=lambda group: (-len(group.use_offsets), group.first_offset))

        rename_map: dict[tuple[str, ...], str] = {}
        generator = _gen_short_names()
        for group in groups:
            short_name = next(generator)
            while short_name in occupied or short_name == group.name:
                short_name = next(generator)
            rename_map[group.key] = short_name
            occupied.add(short_name)

        if self.verbose:
            for group in sorted(self.groups.values(), key=lambda value: value.first_offset):
                if group.key in rename_map:
                    print(f"[golf_rename] {group.name} -> {rename_map[group.key]}", file=_sys.stderr)
                else:
                    for reason in group.skip_reasons:
                        print(f"[golf_rename] skip {group.name}: {reason}", file=_sys.stderr)
        return rename_map

    def _build_edits(self, rename_map: dict[tuple[str, ...], str]) -> list[RewriteEdit]:
        raw_edits: list[RewriteEdit] = []
        for decl in self.decls.values():
            group_key = self.usr_to_group.get(decl.usr)
            if group_key in rename_map:
                raw_edits.append(RewriteEdit(decl.offset, decl.length, rename_map[group_key], group_key))

        for token in self.tokens:
            if token.group_key in rename_map:
                raw_edits.append(RewriteEdit(token.offset, token.length, rename_map[token.group_key], token.group_key))

        deduped: dict[int, RewriteEdit] = {}
        invalid_groups: set[tuple[str, ...]] = set()
        for edit in raw_edits:
            previous = deduped.get(edit.offset)
            if previous is None:
                deduped[edit.offset] = edit
            elif previous.replacement != edit.replacement or previous.length != edit.length:
                invalid_groups.update({previous.group_key, edit.group_key})

        edits = [edit for edit in deduped.values() if edit.group_key not in invalid_groups]
        edits.sort(key=lambda edit: edit.offset)
        for first, second in zip(edits, edits[1:]):
            if first.offset + first.length > second.offset:
                invalid_groups.update({first.group_key, second.group_key})

        if invalid_groups:
            for key in invalid_groups:
                self.groups[key].mark_skipped("overlapping rewrite edits")
            edits = [edit for edit in edits if edit.group_key not in invalid_groups]

        edits.sort(key=lambda edit: -edit.offset)
        return edits

    def _apply_edits(self, edits: list[RewriteEdit]) -> str:
        result = bytearray(self.src_bytes)
        for edit in edits:
            result[edit.offset:edit.offset + edit.length] = edit.replacement.encode("utf-8")
        return result.decode("utf-8")

    def _function_context_at(self, offset: int) -> FunctionContext | None:
        if not self.function_ranges:
            return None
        idx = bisect_right(self.function_starts, offset) - 1
        best: FunctionContext | None = None
        best_size = 1 << 62
        while idx >= 0:
            context = self.function_ranges[idx]
            if context.start > offset:
                idx -= 1
                continue
            if context.end >= offset:
                size = context.end - context.start
                if size < best_size:
                    best = context
                    best_size = size
            if best is not None and context.start < best.start - best_size:
                break
            idx -= 1
        return best

    def _candidate_offsets(self, cursor: "_ci.Cursor") -> list[int]:
        offsets: list[int] = []
        try:
            offsets.append(cursor.location.offset)
        except Exception:
            pass
        try:
            offsets.append(cursor.extent.start.offset)
        except Exception:
            pass
        return list(dict.fromkeys(offsets))

    def _is_user_file(self, cursor: "_ci.Cursor") -> bool:
        location = cursor.location
        if not location.file:
            return False
        try:
            return os.path.samefile(location.file.name, self.tmppath)
        except OSError:
            return False

    @staticmethod
    def _safe_usr(cursor: "_ci.Cursor | None") -> str:
        if cursor is None:
            return ""
        try:
            return cursor.get_usr() or ""
        except Exception:
            return ""

    def _enclosing_function_usr(self, cursor: "_ci.Cursor | None") -> str | None:
        while cursor is not None:
            if cursor.kind.name in _FUNCTION_KIND_NAMES:
                usr = self._safe_usr(cursor)
                return usr or None
            cursor = cursor.semantic_parent
        return None

    def _enclosing_class_usr(self, cursor: "_ci.Cursor | None") -> str | None:
        while cursor is not None:
            if cursor.kind.name in _CLASS_KIND_NAMES:
                usr = self._safe_usr(cursor)
                return usr or None
            cursor = cursor.semantic_parent
        return None

    @staticmethod
    def _cursor_path(cursor: "_ci.Cursor") -> str:
        try:
            if cursor.location.file:
                return os.path.normcase(cursor.location.file.name)
        except Exception:
            pass
        return ""


def _gen_short_names():
    yield "q"
    for length in itertools.count(1):
        for combo in itertools.product("abcdefghijklmnopqrstuvwxyz", repeat=length):
            yield "q" + "".join(combo)


def _load_clang():
    global ci, _clang_getSpecializedCursorTemplate
    if ci is None:
        ci = load_clang_cindex("symbol renaming")
        _clang_getSpecializedCursorTemplate = get_specialized_cursor_template(ci)
    return ci


def golf_rename_symbols(
    code: str,
    rename_functions: bool = False,
    verbose: bool = False,
    extra_args: list[str] | None = None,
    platform: str | None = None,
) -> str:
    """Rename user-defined symbols with a correctness-first libclang pipeline."""
    ci_module = _load_clang()
    src_bytes = code.encode("utf-8")

    with tempfile.NamedTemporaryFile(suffix=".cpp", mode="wb", delete=False) as handle:
        handle.write(src_bytes)
        tmppath = handle.name

    try:
        planner = RenamePlanner(
            ci_module=ci_module,
            tmppath=tmppath,
            src_bytes=src_bytes,
            code=code,
            rename_functions=rename_functions,
            verbose=verbose,
        )
        planner.tu = planner.index.parse(
            tmppath,
            args=build_clang_parse_args(
                lang="c++",
                std="-std=c++23",
                extra_args=extra_args,
                platform=platform,
            ),
            options=(
                ci_module.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
                | ci_module.TranslationUnit.PARSE_INCOMPLETE
            ),
        )
        return planner.run()
    finally:
        try:
            os.unlink(tmppath)
        except OSError:
            pass


def _is_external_path(path: str) -> bool:
    if not path:
        return False
    normalized = path.replace("\\", "/").lower()
    return "/external/" in normalized
