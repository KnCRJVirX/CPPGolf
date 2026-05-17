"""Clang CFG helper-backed source-level control-flow flattening."""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
import sys as _sys
import tempfile
from functools import lru_cache
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_IDENT_RE = re.compile(r"\b[A-Za-z_]\w*\b")
_DECLARATION_KEYWORDS = {
    "alignas", "auto", "bool", "char", "char16_t", "char32_t", "class", "const",
    "constexpr", "constinit", "consteval", "decltype", "double", "enum", "extern",
    "float", "inline", "int", "long", "mutable", "register", "short", "signed",
    "static", "struct", "thread_local", "unsigned", "volatile",
}
_CONDITION_DECL_PREFIXES = {
    "auto",
    "bool",
    "char",
    "char16_t",
    "char32_t",
    "const",
    "constexpr",
    "decltype",
    "double",
    "float",
    "int",
    "long",
    "short",
    "signed",
    "unsigned",
}
_CONTROL_REGION_KINDS = {"IfStmt", "WhileStmt", "ForStmt", "DoStmt", "SwitchStmt"}
_MAX_RECURSION_DEPTH = 4
_MAX_NESTED_REWRITE_CHARS = 8000


class CfgHelperError(RuntimeError):
    """Raised when the required CFG helper is unavailable or fails."""


@dataclass(frozen=True)
class RewriteEdit:
    start: int
    end: int
    replacement: str


@dataclass(frozen=True)
class StatementRegion:
    start: int
    end: int
    text: str
    kind: str
    category: str = "atomic"
    control: dict[str, Any] | None = None
    macro: str = "none"
    statement_id: int | None = None
    contains: dict[str, Any] | None = None
    then_block: dict[str, Any] | None = None
    else_block: dict[str, Any] | None = None
    body_block: dict[str, Any] | None = None
    block_plan: dict[str, Any] | None = None
    atomic_reason: str | None = None
    transfers: list[dict[str, Any]] | None = None
    control_id: int | None = None


@dataclass(frozen=True)
class CfgPlan:
    functions: list[dict[str, Any]]


@dataclass(frozen=True)
class RenderedBody:
    text: str
    case_count: int
    nested_case_count: int = 0
    linear_case_count: int = 0
    shuffled_case_count: int = 0


@dataclass(frozen=True)
class RenderedCase:
    state: int
    text: str
    kind: str


@dataclass(frozen=True)
class HoistedDeclaration:
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class TransferContext:
    active_loop_control_id: int
    state_name: str
    transfer_name: str
    exit_placeholder: str


def flatten_control_flow(
    code: str,
    *,
    functions: list[str] | None = None,
    exclude: list[str] | None = None,
    extra_args: list[str] | None = None,
    platform: str | None = None,
    verbose: bool = False,
    helper_path: Path | None = None,
    config_helper_path: Path | None = None,
    helper_include_dirs: list[Path] | None = None,
) -> str:
    """Flatten selected function bodies using the required CFG helper."""
    function_patterns = list(dict.fromkeys(functions or []))
    if not function_patterns:
        return code

    helper = _resolve_helper(helper_path, config_helper_path)
    with tempfile.NamedTemporaryFile(suffix=".cpp", mode="w", encoding="utf-8", newline="", delete=False) as handle:
        handle.write(_helper_parse_code(code, platform=platform))
        tmppath = Path(handle.name)

    try:
        exact_targets = _helper_targets(code, tmppath, helper, function_patterns, extra_args, platform)
        if not exact_targets:
            return code
        plan = _run_helper(tmppath, helper, exact_targets, extra_args, platform, helper_include_dirs or [])
        edits = _build_edits(code, plan, function_patterns, list(exclude or []), verbose)
        return _apply_edits(code, edits)
    finally:
        try:
            tmppath.unlink()
        except OSError:
            pass


def _helper_targets(
    code: str,
    source_path: Path,
    helper: Path,
    patterns: list[str],
    extra_args: list[str] | None,
    platform: str | None,
) -> list[str]:
    # Keep helper matching exact-only. Expanding globs on large merged files can
    # create huge helper outputs and memory pressure.
    return list(dict.fromkeys(patterns))


def _run_helper(
    source_path: Path,
    helper: Path,
    functions: list[str],
    extra_args: list[str] | None,
    platform: str | None,
    helper_include_dirs: list[Path],
) -> CfgPlan:
    cmd = [str(helper)]
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
            raise CfgHelperError(f"CFG helper failed: {detail}")
        try:
            with output_path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
        except json.JSONDecodeError as exc:
            raise CfgHelperError(f"CFG helper produced invalid JSON: {exc}") from exc
    finally:
        try:
            output_path.unlink()
        except OSError:
            pass
    if not isinstance(data, dict) or data.get("version") != 4 or not isinstance(data.get("functions"), list):
        raise CfgHelperError("CFG helper produced an unsupported JSON schema; rebuild cppgolf-cfg-helper")
    return CfgPlan(functions=data["functions"])


def _helper_compile_args(
    extra_args: list[str] | None,
    platform: str | None,
    helper_include_dirs: list[Path] | None = None,
) -> list[str]:
    args = ["-std=c++23"]
    helper_include_dirs = helper_include_dirs or []
    helper_include_args, helper_target = _helper_include_args(helper_include_dirs)
    if helper_target and not _has_target_arg(extra_args):
        args.append(f"--target={helper_target}")
    elif platform == "win32" or (platform is None and os.name == "nt"):
        args.append("--target=x86_64-w64-windows-gnu")
    resource_dir = _clang_resource_dir()
    if resource_dir is not None:
        args.extend(["-resource-dir", str(resource_dir)])
    if extra_args:
        args.extend(extra_args)
    args.extend(helper_include_args)
    args.extend(
        [
            "-Wno-error=macro-redefined",
            "-Wno-macro-redefined",
            "-Wno-error=deprecated-enum-enum-conversion",
            "-Wno-deprecated-enum-enum-conversion",
        ]
    )
    return args


def _has_target_arg(args: list[str] | None) -> bool:
    if not args:
        return False
    for index, arg in enumerate(args):
        if arg == "--target" and index + 1 < len(args):
            return True
        if arg.startswith("--target=") or arg.startswith("-target="):
            return True
    return False


def _helper_include_args(include_dirs: list[Path]) -> tuple[list[str], str | None]:
    expanded: list[str] = []
    target: str | None = None
    seen: set[str] = set()
    isolated_includes = False

    def add_flag_path(flag: str, path: Path) -> None:
        key = f"{flag}\0{str(path).lower() if os.name == 'nt' else str(path)}"
        if key in seen:
            return
        seen.add(key)
        expanded.extend([flag, str(path)])

    for directory in include_dirs:
        bundle = _msys_include_bundle(directory)
        if bundle is not None:
            target = target or "x86_64-pc-msys"
            if not isolated_includes:
                expanded.extend(["-nostdinc", "-nostdinc++"])
                isolated_includes = True
            resource_dir = _clang_resource_dir()
            if resource_dir is not None:
                add_flag_path("-isystem", resource_dir / "include")
            for include_dir in bundle:
                add_flag_path("-isystem", include_dir)
            continue
        add_flag_path("-isystem", directory)

    return expanded, target


def _msys_include_bundle(directory: Path) -> list[Path] | None:
    resolved = directory.resolve()
    if resolved.name.lower() != "include" or resolved.parent.name.lower() != "usr":
        return None
    root = resolved.parents[1]
    gcc_root = root / "usr" / "lib" / "gcc" / "x86_64-pc-msys"
    if not gcc_root.exists():
        return None
    versions = sorted((path for path in gcc_root.iterdir() if path.is_dir()), reverse=True)
    for version in versions:
        cxx = version / "include" / "c++"
        target_cxx = cxx / "x86_64-pc-msys"
        gcc_include = version / "include"
        if cxx.exists() and target_cxx.exists() and gcc_include.exists():
            return [
                cxx,
                target_cxx,
                cxx / "backward",
                gcc_include,
                version / "include-fixed",
                resolved,
            ]
    return None


@lru_cache(maxsize=1)
def _clang_resource_dir() -> Path | None:
    for compiler in _candidate_clang_binaries():
        proc = subprocess.run(
            [str(compiler), "-print-resource-dir"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            path = Path(proc.stdout.strip())
            if (path / "include").exists():
                return path

    for root in (Path("C:/LLVM/lib/clang"), Path("/usr/lib/clang"), Path("/usr/local/lib/clang")):
        if not root.exists():
            continue
        versions = sorted((path for path in root.iterdir() if (path / "include").exists()), reverse=True)
        if versions:
            return versions[0]
    return None


def _candidate_clang_binaries() -> list[Path]:
    candidates: list[Path] = []
    if os.name == "nt":
        candidates.extend([Path("C:/LLVM/bin/clang++.exe"), Path("C:/LLVM/bin/clang.exe")])
    for name in ("clang++", "clang"):
        found = shutil.which(name)
        if found:
            candidates.append(Path(found))

    result: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).lower() if os.name == "nt" else str(candidate)
        if key not in seen and candidate.exists():
            seen.add(key)
            result.append(candidate)
    return result


def _helper_parse_code(code: str, *, platform: str | None = None) -> str:
    # The final rewrite is applied to the original source. This parser-only
    # copy only removes syntax that can make Clang reject GCC-accepted merged
    # code, while preserving every byte offset returned by the helper.
    result = list(code)
    for start, end in _standard_attribute_ranges(code):
        for index in range(start, end):
            if result[index] not in "\r\n":
                result[index] = " "
    return "".join(result)


def _standard_attribute_ranges(code: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    index = 0
    while index < len(code) - 1:
        if code[index:index + 2] != "[[":
            index += 1
            continue
        end = _find_standard_attribute_end(code, index)
        if end < 0:
            index += 2
            continue
        ranges.append((index, end))
        index = end
    return ranges


def _find_standard_attribute_end(code: str, start: int) -> int:
    index = start + 2
    while index < len(code) - 1:
        if code[index:index + 2] == "]]":
            return index + 2
        index += 1
    return -1


def _build_edits(
    code: str,
    plan: CfgPlan,
    patterns: list[str],
    exclude: list[str],
    verbose: bool,
) -> list[RewriteEdit]:
    edits: list[RewriteEdit] = []
    seen_bodies: set[tuple[int, int]] = set()
    matched_patterns: set[str] = set()
    for function in plan.functions:
        qualified = str(function.get("qualified_name", ""))
        simple = str(function.get("simple_name", ""))
        if not _matches_any(qualified, simple, patterns):
            continue
        matched_patterns.update(pattern for pattern in patterns if qualified == pattern or simple == pattern)
        if _matches_any(qualified, simple, exclude):
            _log(verbose, f"skip {qualified}: excluded by config")
            continue
        if function.get("diagnostics"):
            _log(verbose, f"skip {qualified}: helper diagnostics: {function['diagnostics']}")
            continue
        raw_body = _range(function.get("body"))
        if raw_body is not None:
            if raw_body in seen_bodies:
                continue
            seen_bodies.add(raw_body)

        edits.extend(_function_edits(code, function, verbose))
    for pattern in patterns:
        if pattern not in matched_patterns:
            _log(verbose, f"skip {pattern}: target not found by helper")
    return edits


def _function_edits(code: str, function: dict[str, Any], verbose: bool) -> list[RewriteEdit]:
    body_edit = _function_body_edit(code, function, verbose)
    if body_edit is None:
        return []
    return [body_edit]


def _function_body_edit(code: str, function: dict[str, Any], verbose: bool) -> RewriteEdit | None:
    qualified = str(function.get("qualified_name", ""))
    raw_body = _range(function.get("body"))
    if raw_body is None:
        _log(verbose, f"skip {qualified}: invalid body range")
        return None
    body_start = raw_body[0]
    body_end = raw_body[1]
    if body_start < 0 or body_end > len(code) or code[body_start] != "{" or code[body_end - 1] != "}":
        _log(verbose, f"skip {qualified}: invalid body range")
        return None
    if _is_constexpr_function(code, function, body_start):
        _log(verbose, f"skip {qualified}: constexpr/consteval function")
        return None
    if _contains_preprocessor_directive(code[body_start + 1:body_end - 1]):
        _log(verbose, f"skip {qualified}: preprocessor directives inside function body")
        return None

    block_plan = function.get("block_plan")
    if not isinstance(block_plan, dict):
        _log(verbose, f"skip {qualified}: invalid v4 block plan")
        return None
    skip_reason = _helper_block_skip_reason(block_plan)
    if skip_reason is not None:
        _log(verbose, f"skip {qualified}: {skip_reason}")
        return None
    rendered = _render_helper_block(code, block_plan, depth=0, force=True)
    if rendered.case_count == 0:
        _log(verbose, f"skip {qualified}: no helper statements")
        return None
    _log(
        verbose,
        f"flatten {qualified}: {rendered.case_count} case(s), "
        f"{rendered.case_count - rendered.nested_case_count} top-level case(s), "
        f"{rendered.nested_case_count} nested case(s), "
        f"{rendered.linear_case_count} linear case(s), "
        f"{rendered.shuffled_case_count} shuffled case(s), "
        "helper v4 recursive plan",
    )
    return RewriteEdit(body_start, body_end, rendered.text)


def _is_constexpr_function(code: str, function: dict[str, Any], body_start: int) -> bool:
    has_helper_constexpr_info = "is_constexpr" in function or "is_consteval" in function
    if has_helper_constexpr_info:
        return function.get("is_constexpr") is True or function.get("is_consteval") is True
    signature_range = _range(function.get("signature"))
    if signature_range is None:
        return False
    start, _ = signature_range
    if start < 0 or start >= body_start:
        return False
    return re.search(r"\b(?:constexpr|consteval)\b", code[start:body_start]) is not None


def _regions_from_helper_statements(
    code: str,
    statements: list[Any],
    inner_start: int,
    inner_end: int,
) -> list[StatementRegion]:
    regions: list[StatementRegion] = []
    for item in statements:
        if not isinstance(item, dict):
            continue
        statement_range = _range(item.get("range"))
        if statement_range is None:
            continue
        start, end = statement_range
        if not (inner_start <= start <= end <= inner_end):
            continue
        text = code[start:end]
        if not text.strip():
            continue
        regions.append(
            StatementRegion(
                start,
                end,
                text,
                _region_kind_from_category(str(item.get("category", "atomic")), str(item.get("kind", "Statement"))),
                category=str(item.get("category", "atomic")),
                control=item.get("control") if isinstance(item.get("control"), dict) else None,
                macro=str(item.get("macro", "none")),
                statement_id=item.get("id") if isinstance(item.get("id"), int) else None,
                contains=item.get("contains") if isinstance(item.get("contains"), dict) else None,
                then_block=_dict_or_none((item.get("control") or {}).get("then_block") if isinstance(item.get("control"), dict) else None),
                else_block=_dict_or_none((item.get("control") or {}).get("else_block") if isinstance(item.get("control"), dict) else None),
                body_block=_dict_or_none((item.get("control") or {}).get("body_block") if isinstance(item.get("control"), dict) else None),
                block_plan=_dict_or_none(item.get("block_plan")),
                atomic_reason=str(item.get("atomic_reason")) if isinstance(item.get("atomic_reason"), str) else None,
                transfers=[value for value in item.get("transfers", []) if isinstance(value, dict)]
                if isinstance(item.get("transfers"), list)
                else [],
                control_id=item.get("control_id") if isinstance(item.get("control_id"), int) else None,
            )
        )
    regions.sort(key=lambda region: (region.start, region.end))
    return regions


def _region_kind_from_category(category: str, fallback: str) -> str:
    return {
        "decl": "DeclStmt",
        "linear": "Statement",
        "if": "IfStmt",
        "loop": "LoopStmt",
        "switch": "SwitchStmt",
        "block": "CompoundStmt",
        "return": "ReturnStmt",
        "atomic": fallback,
    }.get(category, fallback)


def _dict_or_none(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _apply_helper_local_policy(
    code: str,
    regions: list[StatementRegion],
    locals_info: list[Any],
) -> tuple[list[HoistedDeclaration], list[StatementRegion]]:
    if not regions:
        return [], regions
    by_id = {region.statement_id: index for index, region in enumerate(regions) if region.statement_id is not None}
    prefix_decl_ids = _leading_declaration_statement_ids(regions)
    remove_ranges: set[tuple[int, int]] = set()
    hoisted: list[HoistedDeclaration] = []
    merge_statement_spans: list[tuple[int, int]] = []
    locals_by_statement: dict[int, list[dict[str, Any]]] = {}
    barrier_statement_ids: set[int] = set()

    for item in locals_info:
        if not isinstance(item, dict):
            continue
        decl_statement_id = item.get("decl_statement_id")
        if isinstance(decl_statement_id, int):
            locals_by_statement.setdefault(decl_statement_id, []).append(item)

    hoisted_statement_ids: set[int] = set()
    handled_statement_ids: set[int] = set()
    for statement_id, items in locals_by_statement.items():
        if statement_id in prefix_decl_ids or statement_id not in by_id:
            continue
        decl_index = by_id[statement_id]
        region = regions[decl_index]
        if region.category != "decl":
            continue
        if len(items) == 1 and items[0].get("split_decl_init") is True:
            hoist_text = items[0].get("hoist_text")
            replacement_text = items[0].get("replacement_text")
            if isinstance(hoist_text, str) and isinstance(replacement_text, str):
                hoisted.append(HoistedDeclaration(region.start, region.start, hoist_text))
                regions[decl_index] = _replace_region_payload(region, replacement_text, "Statement", "linear")
                handled_statement_ids.add(statement_id)
                continue
        if items and all(item.get("safe_hoist") is True for item in items) and _decl_region_has_explicit_initializer(region, items):
            barrier_statement_ids.add(statement_id)
            handled_statement_ids.add(statement_id)
            continue
        if items and all(item.get("safe_hoist") is True for item in items):
            hoisted.append(HoistedDeclaration(region.start, region.end, code[region.start:region.end]))
            remove_ranges.add((region.start, region.end))
            hoisted_statement_ids.add(statement_id)
            handled_statement_ids.add(statement_id)
            continue
        merge_until_ids = [
            item.get("must_merge_until_statement_id")
            for item in items
            if isinstance(item.get("must_merge_until_statement_id"), int)
            and item.get("must_merge_until_statement_id") in by_id
        ]
        if merge_until_ids:
            merge_until = max(merge_until_ids, key=lambda value: by_id[value])
            if region.category == "decl":
                barrier_statement_ids.add(statement_id)
                handled_statement_ids.add(statement_id)
            elif by_id[merge_until] > decl_index:
                merge_statement_spans.append((statement_id, merge_until))
                handled_statement_ids.add(statement_id)

    for item in locals_info:
        if not isinstance(item, dict):
            continue
        decl_range = _range(item.get("decl_range"))
        if decl_range is None:
            continue
        decl_statement_id = item.get("decl_statement_id")
        if not isinstance(decl_statement_id, int) or decl_statement_id not in by_id:
            continue
        if decl_statement_id in handled_statement_ids:
            continue
        if decl_statement_id in hoisted_statement_ids:
            continue
        if decl_statement_id in prefix_decl_ids:
            continue
        decl_index = by_id[decl_statement_id]
        statement_items = locals_by_statement.get(decl_statement_id, [])
        if (
            item.get("safe_hoist") is True
            and regions[decl_index].category == "decl"
            and len(statement_items) == 1
        ):
            region = regions[decl_index]
            if _decl_region_has_explicit_initializer(region, statement_items):
                barrier_statement_ids.add(decl_statement_id)
                continue
            hoisted.append(HoistedDeclaration(region.start, region.end, code[region.start:region.end]))
            remove_ranges.add((region.start, region.end))
            hoisted_statement_ids.add(decl_statement_id)
            continue
        if (
            item.get("split_decl_init") is True
            and regions[decl_index].category == "decl"
            and len(statement_items) == 1
        ):
            hoist_text = item.get("hoist_text")
            replacement_text = item.get("replacement_text")
            if isinstance(hoist_text, str) and isinstance(replacement_text, str):
                hoisted.append(HoistedDeclaration(regions[decl_index].start, regions[decl_index].start, hoist_text))
                regions[decl_index] = _replace_region_payload(regions[decl_index], replacement_text, "Statement", "linear")
                continue
        merge_until = item.get("must_merge_until_statement_id")
        if item.get("safe_hoist") is True:
            hoisted.append(HoistedDeclaration(decl_range[0], decl_range[1], code[decl_range[0]:decl_range[1]]))
            remove_ranges.add(decl_range)
            continue
        if isinstance(merge_until, int) and merge_until in by_id:
            if regions[decl_index].category == "decl":
                barrier_statement_ids.add(decl_statement_id)
                continue
            if by_id[merge_until] > decl_index:
                merge_statement_spans.append((decl_statement_id, merge_until))

    if remove_ranges:
        regions = [_remove_ranges_from_region(code, region, remove_ranges) for region in regions]
        regions = [region for region in regions if region.text.strip()]
    if barrier_statement_ids:
        regions = [_mark_barrier_region(region) if region.statement_id in barrier_statement_ids else region for region in regions]
    merge_spans: list[tuple[int, int]] = []
    if merge_statement_spans:
        by_id = {region.statement_id: index for index, region in enumerate(regions) if region.statement_id is not None}
        for start_id, end_id in merge_statement_spans:
            if start_id in by_id and end_id in by_id and by_id[end_id] > by_id[start_id]:
                merge_spans.append((by_id[start_id], by_id[end_id]))
    if merge_spans:
        regions = _merge_region_spans(code, regions, merge_spans)
    hoisted.sort(key=lambda decl: decl.start)
    return hoisted, regions


def _leading_declaration_statement_ids(regions: list[StatementRegion]) -> set[int]:
    result: set[int] = set()
    for region in regions:
        if region.category != "decl" or region.statement_id is None:
            break
        result.add(region.statement_id)
    return result


def _decl_region_has_explicit_initializer(region: StatementRegion, locals_info: list[dict[str, Any]]) -> bool:
    if not locals_info:
        return False
    for item in locals_info:
        init_range = _range(item.get("init_range"))
        if init_range is not None and init_range[0] < init_range[1]:
            return True
    return "=" in region.text or bool(re.search(r"\b[A-Za-z_]\w*\s*\([^;{}]*\)\s*;", region.text))


def _remove_ranges_from_region(
    code: str,
    region: StatementRegion,
    remove_ranges: set[tuple[int, int]],
) -> StatementRegion:
    replacements = sorted((start, end) for start, end in remove_ranges if region.start <= start <= end <= region.end)
    if not replacements:
        return region
    pieces: list[str] = []
    cursor = region.start
    for start, end in replacements:
        pieces.append(code[cursor:start])
        cursor = end
    pieces.append(code[cursor:region.end])
    return StatementRegion(
        region.start,
        region.end,
        "".join(pieces),
        region.kind,
        region.category,
        region.control,
        region.macro,
        region.statement_id,
        region.contains,
        region.then_block,
        region.else_block,
        region.body_block,
        region.block_plan,
        region.atomic_reason,
        region.transfers,
        region.control_id,
    )


def _replace_region_payload(region: StatementRegion, text: str, kind: str, category: str) -> StatementRegion:
    return StatementRegion(
        region.start,
        region.end,
        text,
        kind,
        category,
        region.control,
        region.macro,
        region.statement_id,
        region.contains,
        region.then_block,
        region.else_block,
        region.body_block,
        region.block_plan,
        region.atomic_reason,
        region.transfers,
        region.control_id,
    )


def _mark_barrier_region(region: StatementRegion) -> StatementRegion:
    return StatementRegion(
        region.start,
        region.end,
        region.text,
        region.kind,
        "barrier",
        region.control,
        region.macro,
        region.statement_id,
        region.contains,
        region.then_block,
        region.else_block,
        region.body_block,
        region.block_plan,
        "lifetime barrier declaration",
        region.transfers,
        region.control_id,
    )


def _merge_region_spans(
    code: str,
    regions: list[StatementRegion],
    spans: list[tuple[int, int]],
) -> list[StatementRegion]:
    if not spans:
        return regions
    spans.sort()
    collapsed: list[tuple[int, int]] = []
    for start, end in spans:
        if collapsed and start <= collapsed[-1][1] + 1:
            collapsed[-1] = (collapsed[-1][0], max(collapsed[-1][1], end))
        else:
            collapsed.append((start, end))
    result: list[StatementRegion] = []
    span_iter = iter(collapsed)
    current = next(span_iter, None)
    index = 0
    while index < len(regions):
        if current is not None and index == current[0]:
            start_index, end_index = current
            start = regions[start_index].start
            end = regions[end_index].end
            merged_regions = regions[start_index:end_index + 1]
            result.append(_merged_block_region(code, start, end, merged_regions))
            index = end_index + 1
            current = next(span_iter, None)
            continue
        result.append(regions[index])
        index += 1
    return result


def _merged_block_region(
    code: str,
    start: int,
    end: int,
    regions: list[StatementRegion],
) -> StatementRegion:
    contains = _merge_contains(regions)
    atomic_reason = next((region.atomic_reason for region in regions if region.atomic_reason), None)
    block_plan = None
    if atomic_reason is None:
        block_plan = {
            "range": {"valid": True, "start": start, "end": end},
            "statements": [_statement_item_from_region(region) for region in regions],
            "locals": [],
            "diagnostics": [],
        }
    return StatementRegion(
        start,
        end,
        code[start:end],
        "CompoundStmt" if block_plan is not None else "Atomic",
        "block" if block_plan is not None else "atomic",
        contains=contains,
        block_plan=block_plan,
        atomic_reason=atomic_reason,
        transfers=[transfer for region in regions for transfer in (region.transfers or [])],
    )


def _merge_contains(regions: list[StatementRegion]) -> dict[str, Any]:
    keys = ("lambda", "switch", "goto", "label", "try", "break", "continue")
    merged = {key: False for key in keys}
    for region in regions:
        if not isinstance(region.contains, dict):
            continue
        for key in keys:
            merged[key] = bool(merged[key] or region.contains.get(key))
    return merged


def _statement_item_from_region(region: StatementRegion) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": region.statement_id,
        "kind": region.kind,
        "category": region.category,
        "range": {"valid": True, "start": region.start, "end": region.end},
        "macro": region.macro,
        "control": region.control or {},
        "contains": region.contains or _merge_contains([]),
        "transfers": region.transfers or [],
    }
    if region.control_id is not None:
        item["control_id"] = region.control_id
    if region.block_plan is not None:
        item["block_plan"] = region.block_plan
    if region.atomic_reason is not None:
        item["atomic_reason"] = region.atomic_reason
    return item


def _top_level_statement_regions(code: str, inner_start: int, inner_end: int) -> list[StatementRegion]:
    regions: list[StatementRegion] = []
    index = inner_start
    while index < inner_end:
        start = _skip_ws(code, index)
        if start >= inner_end:
            break
        end = _statement_end(code, start)
        if end < start or end > inner_end:
            return []
        text = code[start:end]
        kind = _top_level_statement_kind(text)
        regions.append(StatementRegion(start, end, text, kind))
        index = end
    return regions


def _top_level_statement_kind(text: str) -> str:
    stripped = _strip_leading_comments(text)
    if _starts_keyword(stripped, 0, "if"):
        return "IfStmt"
    if _starts_keyword(stripped, 0, "while"):
        return "WhileStmt"
    if _starts_keyword(stripped, 0, "for"):
        return "ForStmt"
    if _starts_keyword(stripped, 0, "do"):
        return "DoStmt"
    if _starts_keyword(stripped, 0, "switch"):
        return "SwitchStmt"
    if _looks_like_declaration(stripped):
        return "DeclStmt"
    return "Statement"


def _looks_like_declaration(text: str) -> bool:
    text = _strip_leading_comments(text)
    if not text or text.startswith(("return", "case", "default", "break", "continue", "goto")):
        return False
    if text.startswith(("auto ", "const ", "constexpr ", "static ", "thread_local ", "volatile ")):
        return True
    first = text.split(None, 1)[0].strip("*&")
    if "(" in first:
        return False
    if first in _DECLARATION_KEYWORDS:
        return True
    return _looks_like_type_declarator(text)


def _looks_like_type_declarator(text: str) -> bool:
    head = text.split("=", 1)[0].strip().rstrip(";")
    if not head:
        return False
    match = re.match(
        r"^(?:[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*(?:\s*<[^;=(){}]+>)?)"
        r"(?:\s*[*&]+|\s+)+[A-Za-z_]\w*(?:\s*\[[^\]]*\]|\s*\([^;{}]*\))?(?:\s*,|$)",
        head,
    )
    if not match:
        return False
    first = head.split(None, 1)[0]
    return first not in {"if", "while", "for", "switch", "return", "sizeof"}


def _strip_leading_comments(text: str) -> str:
    stripped = text.lstrip()
    while True:
        if stripped.startswith("//"):
            newline = stripped.find("\n")
            if newline < 0:
                return ""
            stripped = stripped[newline + 1:].lstrip()
            continue
        if stripped.startswith("/*"):
            end = stripped.find("*/", 2)
            if end < 0:
                return ""
            stripped = stripped[end + 2:].lstrip()
            continue
        return stripped


def _render_helper_block(
    code: str,
    block_plan: dict[str, Any],
    *,
    depth: int,
    force: bool = False,
    protect_loop_transfer: bool = False,
    transfer_context: TransferContext | None = None,
) -> RenderedBody:
    block_range = _range(block_plan.get("range"))
    if block_range is None:
        return RenderedBody("", 0)
    block_start, block_end = block_range
    if block_start < 0 or block_end > len(code) or block_start > block_end:
        return RenderedBody("", 0)
    original = code[block_start:block_end]
    diagnostics = block_plan.get("diagnostics")
    if isinstance(diagnostics, list) and diagnostics:
        return RenderedBody(original, 0)

    statements = block_plan.get("statements")
    locals_info = block_plan.get("locals")
    if not isinstance(statements, list) or not isinstance(locals_info, list):
        return RenderedBody(original, 0)
    if protect_loop_transfer and transfer_context is None and _block_has_loop_transfer(statements):
        return RenderedBody(original, 0)
    if transfer_context is not None and _has_unconvertible_active_transfer(statements, transfer_context.active_loop_control_id):
        return RenderedBody(original, 0)

    inner_start, inner_end = _block_inner_range(code, block_start, block_end)
    regions = _regions_from_helper_statements(code, statements, inner_start, inner_end)
    if not regions:
        return RenderedBody(original, 0)
    hoisted, regions = _apply_helper_local_policy(code, regions, locals_info)
    prelude, flatten_regions = _split_prelude_regions(regions)
    if not force and not flatten_regions:
        return RenderedBody(original, 0)
    return _render_block_state_machine(
        code,
        block_start,
        block_end,
        prelude,
        flatten_regions,
        depth=depth,
        hoisted=hoisted,
        transfer_context=transfer_context,
    )


def _block_inner_range(code: str, start: int, end: int) -> tuple[int, int]:
    if start < end and code[start] == "{" and code[end - 1] == "}":
        return start + 1, end - 1
    return start, end


def _block_has_loop_transfer(statements: list[Any]) -> bool:
    for statement in statements:
        if not isinstance(statement, dict):
            continue
        contains = statement.get("contains")
        if isinstance(contains, dict) and (contains.get("break") or contains.get("continue")):
            return True
    return False


def _has_unconvertible_active_transfer(statements: list[Any], active_loop_control_id: int) -> bool:
    for statement in statements:
        if not isinstance(statement, dict):
            continue
        for transfer in _statement_transfers(statement):
            if transfer.get("target_control_id") != active_loop_control_id:
                continue
            if transfer.get("safe") is not True:
                return True
        control = statement.get("control")
        if isinstance(control, dict):
            for key in ("then_block", "else_block", "body_block"):
                nested = control.get(key)
                if isinstance(nested, dict):
                    nested_statements = nested.get("statements")
                    if isinstance(nested_statements, list) and _has_unconvertible_active_transfer(nested_statements, active_loop_control_id):
                        return True
        nested_block = statement.get("block_plan")
        if isinstance(nested_block, dict):
            nested_statements = nested_block.get("statements")
            if isinstance(nested_statements, list) and _has_unconvertible_active_transfer(nested_statements, active_loop_control_id):
                return True
    return False


def _statement_transfers(statement: dict[str, Any]) -> list[dict[str, Any]]:
    transfers = statement.get("transfers")
    if not isinstance(transfers, list):
        return []
    return [transfer for transfer in transfers if isinstance(transfer, dict)]


def _render_flattened_body(
    code: str,
    body_start: int,
    body_end: int,
    prelude: list[StatementRegion],
    regions: list[StatementRegion],
    hoisted: list[HoistedDeclaration] | None = None,
) -> RenderedBody:
    return _render_block_state_machine(code, body_start, body_end, prelude, regions, depth=0, hoisted=hoisted or [])


def _render_block_state_machine(
    code: str,
    body_start: int,
    body_end: int,
    prelude: list[StatementRegion],
    regions: list[StatementRegion],
    *,
    depth: int,
    hoisted: list[HoistedDeclaration] | None = None,
    transfer_context: TransferContext | None = None,
) -> RenderedBody:
    pieces: list[str] = ["{"]
    state_name = _fresh_state_name(code[body_start:body_end], _reserved_state_names(depth))
    if transfer_context is not None and transfer_context.state_name != state_name:
        transfer_name = transfer_context.transfer_name
        if transfer_name == state_name:
            transfer_name = _fresh_state_name(
                code[body_start:body_end],
                _reserved_state_names(depth) | {state_name},
            )
        transfer_context = TransferContext(
            transfer_context.active_loop_control_id,
            state_name,
            transfer_name,
            transfer_context.exit_placeholder,
        )
    pieces.extend(_protect_preprocessor_directives(region.text) for region in prelude)
    for declaration in hoisted or []:
        pieces.append(declaration.text)
        if not declaration.text.rstrip().endswith(";"):
            pieces.append(";")
    state = 0
    cases: list[RenderedCase] = []
    case_count = 0
    nested_case_count = 0
    linear_case_count = 0
    shuffled_case_count = 0

    def flush_cases(last_region: StatementRegion | None) -> None:
        nonlocal state, cases, shuffled_case_count
        if not cases:
            state = 0
            return
        shuffled_cases = _shuffle_cases(cases, code[body_start:body_end], body_start + len(pieces), depth)
        shuffled_case_count += sum(1 for original, shuffled in zip(cases, shuffled_cases) if original.state != shuffled.state)
        joined_cases = "".join(item.text for item in shuffled_cases).replace("__CPPGOLF_TRANSFER_EXIT__", str(state))
        transfer_decl = ""
        transfer_tail = ""
        if transfer_context is not None:
            transfer_decl = f"unsigned {transfer_context.transfer_name}=0;"
            transfer_tail = (
                f"if({transfer_context.transfer_name}==1)break;"
                f"if({transfer_context.transfer_name}==2)continue;"
            )
        pieces.append(
            f"{{unsigned {state_name}=0;{transfer_decl}"
            f"while({state_name}!={state})switch({state_name}){{{joined_cases}}}"
            f"{transfer_tail}}}"
        )
        if last_region is not None and _guarantees_exit(last_region.text):
            pieces.append("for(;;){}")
        cases = []
        state = 0

    previous_region: StatementRegion | None = None
    for region in regions:
        if region.category == "barrier":
            flush_cases(previous_region)
            pieces.append(_protect_preprocessor_directives(region.text))
            if not region.text.rstrip().endswith(";") and not _is_complete_block_like_statement(region.text):
                pieces.append(";")
            previous_region = region
            continue
        next_state = state + 1
        text = region.text.strip()
        if region.category == "if" or region.kind == "IfStmt":
            if_parts = _if_region_parts(code, region)
            if if_parts is not None:
                condition, then_text, else_text = if_parts
                body_state = next_state
                after_state = next_state + 1
                if else_text is None:
                    cases.append(
                        RenderedCase(
                            state,
                            f"case {state}:{{if({condition}){state_name}={body_state};"
                            f"else {state_name}={after_state};break;}}",
                            "control",
                        )
                    )
                    then_rendered = _render_nested_block_or_text(code, region.then_block, then_text, depth + 1)
                    nested_case_count += then_rendered.nested_case_count
                    cases.append(_render_case(body_state, then_rendered.text, f"{state_name}={after_state};break;", "control"))
                    case_count += 2
                    state = after_state
                    previous_region = region
                    continue
                else_state = next_state + 1
                after_state = next_state + 2
                cases.append(
                    RenderedCase(
                        state,
                        f"case {state}:{{if({condition}){state_name}={body_state};"
                        f"else {state_name}={else_state};break;}}",
                        "control",
                    )
                )
                then_rendered = _render_nested_block_or_text(code, region.then_block, then_text, depth + 1)
                else_rendered = _render_nested_block_or_text(code, region.else_block, else_text, depth + 1)
                nested_case_count += then_rendered.nested_case_count + else_rendered.nested_case_count
                cases.append(_render_case(body_state, then_rendered.text, f"{state_name}={after_state};break;", "control"))
                cases.append(_render_case(else_state, else_rendered.text, f"{state_name}={after_state};break;", "control"))
                case_count += 3
                state = after_state
                previous_region = region
                continue
        # Keep helper-classified linear/decl/return regions out of the legacy
        # nested text scanner. Large expression-heavy regions can otherwise
        # trigger very expensive speculative parsing without adding useful CFG
        # flattening.
        rendered_payload = _rewrite_region_payload(code, region, text, depth, transfer_context)
        nested_case_count += rendered_payload.nested_case_count
        payload_text = rendered_payload.text
        if transfer_context is not None:
            payload_text = _rewrite_active_transfers(region, payload_text, transfer_context)
        kind = "linear" if region.category == "linear" else "atomic"
        if kind == "linear":
            linear_case_count += 1
        cases.append(_render_case(state, payload_text, f"{state_name}={next_state};break;", kind))
        case_count += 1
        state = next_state
        previous_region = region
    flush_cases(previous_region)
    pieces.append("}")
    return RenderedBody(
        "".join(pieces),
        case_count + nested_case_count,
        nested_case_count,
        linear_case_count,
        shuffled_case_count,
    )


def _render_case(state: int, payload_text: str, transition: str, kind: str) -> RenderedCase:
    payload = _case_payload(payload_text)
    return RenderedCase(state, f"case {state}:{{\n{payload}\n{transition}\n}}", kind)


def _rewrite_region_payload(
    code: str,
    region: StatementRegion,
    text: str,
    depth: int,
    transfer_context: TransferContext | None = None,
) -> NestedRewrite:
    if region.macro != "none":
        return NestedRewrite(text, 0)
    if region.category == "loop" or region.kind in {"WhileStmt", "ForStmt", "DoStmt", "LoopStmt"}:
        return _rewrite_loop_body_from_plan(code, region, text, depth + 1)
    if region.category == "block" or region.kind == "CompoundStmt":
        return _render_nested_block_or_text(
            code,
            region.block_plan,
            text,
            depth + 1,
            transfer_context=transfer_context,
        )
    if region.atomic_reason:
        return NestedRewrite(text, 0)
    return NestedRewrite(text, 0)


def _render_nested_block_or_text(
    code: str,
    block_plan: dict[str, Any] | None,
    fallback_text: str,
    depth: int,
    *,
    protect_loop_transfer: bool = False,
    transfer_context: TransferContext | None = None,
) -> NestedRewrite:
    if block_plan is None or depth > _MAX_RECURSION_DEPTH:
        return NestedRewrite(fallback_text, 0)
    rendered = _render_helper_block(
        code,
        block_plan,
        depth=depth,
        force=True,
        protect_loop_transfer=protect_loop_transfer,
        transfer_context=transfer_context,
    )
    if rendered.case_count == 0:
        return NestedRewrite(fallback_text, 0)
    return NestedRewrite(rendered.text, rendered.case_count)


def _rewrite_loop_body_from_plan(code: str, region: StatementRegion, text: str, depth: int) -> NestedRewrite:
    if region.body_block is None:
        return NestedRewrite(text, 0)
    body_range = _range(region.body_block.get("range"))
    if body_range is None or not (region.start <= body_range[0] <= body_range[1] <= region.end):
        return NestedRewrite(text, 0)
    active_loop_control_id = region.body_block.get("active_loop_control_id")
    transfer_context = None
    if isinstance(active_loop_control_id, int):
        body_text_for_names = code[body_range[0]:body_range[1]]
        transfer_name = _fresh_state_name(body_text_for_names, _reserved_state_names(depth) | {"qcf"})
        transfer_context = TransferContext(
            active_loop_control_id,
            "qcf",
            transfer_name,
            "__CPPGOLF_TRANSFER_EXIT__",
        )
    body_text = code[body_range[0]:body_range[1]]
    rendered = _render_nested_block_or_text(
        code,
        region.body_block,
        body_text,
        depth,
        protect_loop_transfer=transfer_context is None,
        transfer_context=transfer_context,
    )
    if rendered.nested_case_count == 0:
        return NestedRewrite(text, 0)
    rel_start = body_range[0] - region.start
    rel_end = body_range[1] - region.start
    return NestedRewrite(text[:rel_start] + rendered.text + text[rel_end:], rendered.nested_case_count)


def _rewrite_active_transfers(region: StatementRegion, payload_text: str, context: TransferContext) -> str:
    transfers = region.transfers or []
    active = [
        transfer
        for transfer in transfers
        if transfer.get("safe") is True
        and transfer.get("target_control_id") == context.active_loop_control_id
        and transfer.get("kind") in {"break", "continue"}
    ]
    if not active:
        return payload_text

    original = region.text
    leading = len(original) - len(original.lstrip())
    payload_origin = region.start + leading
    if payload_text != original.strip():
        return payload_text

    replacements: list[tuple[int, int, str]] = []
    for transfer in active:
        transfer_range = _range(transfer.get("range"))
        if transfer_range is None:
            continue
        abs_start, abs_end = transfer_range
        if abs_end < region.end and region.text[abs_end - region.start:abs_end - region.start + 1] == ";":
            abs_end += 1
        rel_start = abs_start - payload_origin
        rel_end = abs_end - payload_origin
        if rel_start < 0 or rel_end > len(payload_text) or rel_start >= rel_end:
            continue
        mode = "1" if transfer.get("kind") == "break" else "2"
        replacement = (
            f"{{{context.transfer_name}={mode};"
            f"{context.state_name}={context.exit_placeholder};break;}}"
        )
        replacements.append((rel_start, rel_end, replacement))

    if not replacements:
        return payload_text

    result = payload_text
    for start, end, replacement in sorted(replacements, reverse=True):
        result = result[:start] + replacement + result[end:]
    return result


def _helper_control_text(code: str, region: StatementRegion, key: str) -> str | None:
    control_range = _helper_control_range(region, key)
    if control_range is None:
        return None
    return code[control_range[0]:control_range[1]].strip()


def _if_region_parts(code: str, region: StatementRegion) -> tuple[str, str, str | None] | None:
    condition = _helper_control_text(code, region, "condition")
    body_text = _helper_if_bodies(code, region)
    if condition is None or body_text is None:
        text = region.text.strip()
        if not _starts_keyword(_strip_leading_comments(text), 0, "if"):
            return None
        condition = _extract_condition(text, 0)
        body_text = _extract_control_body(text, 0)
    if condition is None or body_text is None or not _can_split_control_condition(condition):
        return None
    then_text, else_text = body_text
    if _if_branch_has_unowned_loop_transfer(region, then_text, else_text):
        return None
    return condition, then_text, else_text


def _if_branch_has_unowned_loop_transfer(region: StatementRegion, then_text: str, else_text: str | None) -> bool:
    if region.then_block is None and (else_text is None or region.else_block is None):
        branch_text = then_text if else_text is None else then_text + "\n" + else_text
        return _contains_loop_control_transfer(branch_text)
    if _block_has_unowned_loop_transfer(region.then_block, then_text):
        return True
    if else_text is not None and _block_has_unowned_loop_transfer(region.else_block, else_text):
        return True
    return False


def _block_has_unowned_loop_transfer(block_plan: dict[str, Any] | None, fallback_text: str) -> bool:
    if block_plan is None:
        return _contains_loop_control_transfer(fallback_text)
    statements = block_plan.get("statements")
    if not isinstance(statements, list):
        return _contains_loop_control_transfer(fallback_text)
    if not statements:
        return False
    owned_control_ids = _collect_owned_control_ids(statements)
    return _statements_have_unowned_loop_transfer(statements, owned_control_ids)


def _collect_owned_control_ids(statements: list[Any]) -> set[int]:
    result: set[int] = set()
    for statement in statements:
        if not isinstance(statement, dict):
            continue
        control_id = statement.get("control_id")
        if isinstance(control_id, int) and control_id >= 0:
            result.add(control_id)
        control = statement.get("control")
        if isinstance(control, dict):
            for key in ("then_block", "else_block", "body_block"):
                nested = control.get(key)
                if isinstance(nested, dict):
                    nested_statements = nested.get("statements")
                    if isinstance(nested_statements, list):
                        result.update(_collect_owned_control_ids(nested_statements))
        nested_block = statement.get("block_plan")
        if isinstance(nested_block, dict):
            nested_statements = nested_block.get("statements")
            if isinstance(nested_statements, list):
                result.update(_collect_owned_control_ids(nested_statements))
    return result


def _statements_have_unowned_loop_transfer(statements: list[Any], owned_control_ids: set[int]) -> bool:
    for statement in statements:
        if not isinstance(statement, dict):
            continue
        for transfer in _statement_transfers(statement):
            if transfer.get("kind") not in {"break", "continue"}:
                continue
            target_control_id = transfer.get("target_control_id")
            if not isinstance(target_control_id, int):
                return True
            if target_control_id < 0:
                continue
            if target_control_id not in owned_control_ids:
                return True
    return False


def _helper_if_bodies(code: str, region: StatementRegion) -> tuple[str, str | None] | None:
    then_range = _helper_control_range(region, "then_body")
    if then_range is None:
        return None
    else_range = _helper_control_range(region, "else_body")
    then_text = code[then_range[0]:then_range[1]].strip()
    else_text = code[else_range[0]:else_range[1]].strip() if else_range is not None else None
    return then_text, else_text


def _helper_control_range(region: StatementRegion, key: str) -> tuple[int, int] | None:
    if not isinstance(region.control, dict):
        return None
    return _range(region.control.get(key))


def _shuffle_cases(cases: list[RenderedCase], seed_text: str, body_start: int, depth: int) -> list[RenderedCase]:
    if len(cases) <= 2:
        return cases
    seed = int.from_bytes(hashlib.blake2s(f"{body_start}:{depth}:{seed_text}".encode("utf-8"), digest_size=8).digest(), "big")
    shuffled = sorted(cases, key=lambda item: hashlib.blake2s(f"{seed}:{item.state}".encode("ascii"), digest_size=8).digest())
    if [item.state for item in shuffled] == [item.state for item in cases]:
        shuffled = shuffled[1:] + shuffled[:1]
    return shuffled


def _guarantees_exit(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if _matching_outer_braces(stripped):
        return _guarantees_exit(stripped[1:-1])

    regions = _top_level_statement_regions(stripped, 0, len(stripped))
    if regions and (len(regions) > 1 or regions[0].text.strip() != stripped):
        return _guarantees_exit(regions[-1].text)

    stripped = _strip_leading_comments(stripped)
    if stripped.startswith(("return", "co_return", "throw")):
        return True
    if not stripped.startswith("if"):
        return False

    body = _extract_control_body(stripped, 0)
    if body is None:
        return False
    then_text, else_text = body
    return else_text is not None and _guarantees_exit(then_text) and _guarantees_exit(else_text)


def _case_payload(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    if stripped.startswith("{") and stripped.endswith("}") and _find_matching(stripped, 0, "{", "}") == len(stripped) - 1:
        stripped = stripped[1:-1].strip()
    stripped = _protect_preprocessor_directives(stripped)
    if stripped.endswith(";"):
        return stripped
    if stripped.endswith("}") and _is_complete_block_like_statement(stripped):
        return stripped
    return stripped + ";"


def _is_complete_block_like_statement(text: str) -> bool:
    stripped = _strip_leading_comments(text)
    return (
        stripped.startswith("{")
        or _starts_keyword(stripped, 0, "if")
        or _starts_keyword(stripped, 0, "while")
        or _starts_keyword(stripped, 0, "for")
        or _starts_keyword(stripped, 0, "do")
        or _starts_keyword(stripped, 0, "switch")
        or _starts_keyword(stripped, 0, "try")
    )


def _contains_preprocessor_directive(text: str) -> bool:
    return bool(re.search(r"(?m)^\s*#", text))


def _protect_preprocessor_directives(text: str) -> str:
    if not _contains_preprocessor_directive(text):
        return text
    text = re.sub(
        r"(?m)^([ \t]*#(?:if|ifdef|ifndef|elif|else|endif|define|undef|include|pragma|error|warning)\b[^\r\n]*?)(?=[ \t]*#(?:if|ifdef|ifndef|elif|else|endif|define|undef|include|pragma|error|warning)\b)",
        r"\1\n",
        text,
    )
    return text



@dataclass(frozen=True)
class NestedRewrite:
    text: str
    nested_case_count: int = 0


def _rewrite_nested_blocks(text: str, depth: int) -> NestedRewrite:
    if depth > _MAX_RECURSION_DEPTH:
        return NestedRewrite(text, 0)
    if len(text) > _MAX_NESTED_REWRITE_CHARS:
        return NestedRewrite(text, 0)
    stripped = text.strip()
    if not stripped or _contains_lambda_like_text(stripped):
        return NestedRewrite(text, 0)

    if stripped.startswith("switch"):
        return NestedRewrite(text, 0)
    if stripped.startswith("if"):
        return _rewrite_nested_if(stripped, depth)
    if stripped.startswith(("while", "for")):
        return _rewrite_nested_loop(stripped, depth)
    if stripped.startswith("do"):
        return _rewrite_nested_do_while(stripped, depth)
    if stripped.startswith("{") and _matching_outer_braces(stripped):
        rendered = _render_nested_brace_block(stripped, depth)
        if rendered is not None:
            return rendered
    scanned = _rewrite_nested_control_regions_in_text(stripped, depth)
    if scanned.nested_case_count:
        return scanned
    return NestedRewrite(text, 0)


def _rewrite_nested_if(text: str, depth: int) -> NestedRewrite:
    open_paren = text.find("(")
    close_paren = _find_matching(text, open_paren, "(", ")") if open_paren >= 0 else -1
    if close_paren < 0:
        return NestedRewrite(text, 0)
    condition = text[open_paren + 1:close_paren].strip()
    if not _can_split_control_condition(condition):
        return NestedRewrite(text, 0)
    then_start = _skip_ws(text, close_paren + 1)
    if then_start >= len(text):
        return NestedRewrite(text, 0)
    then_end = _statement_end(text, then_start)
    if then_end < 0:
        return NestedRewrite(text, 0)
    then_text = text[then_start:then_end].strip()
    else_text: str | None = None
    else_end = then_end
    tail_start = _skip_ws(text, then_end)
    if text.startswith("else", tail_start):
        else_body_start = _skip_ws(text, tail_start + 4)
        if else_body_start >= len(text):
            return NestedRewrite(text, 0)
        else_end = _statement_end(text, else_body_start)
        if else_end < 0:
            return NestedRewrite(text, 0)
        else_text = text[else_body_start:else_end].strip()

    branch_text = then_text if else_text is None else then_text + "\n" + else_text
    if _contains_loop_control_transfer(branch_text):
        return NestedRewrite(text, 0)

    then_rendered = _rewrite_nested_blocks(then_text, depth + 1)
    else_rendered = _rewrite_nested_blocks(else_text, depth + 1) if else_text is not None else NestedRewrite("", 0)
    state_name = _fresh_state_name(text, _reserved_state_names(depth))
    if else_text is None:
        after_state = 2
        cases = [
            RenderedCase(0, f"case 0:{{if({condition}){state_name}=1;else {state_name}=2;break;}}", "control"),
            _render_case(1, then_rendered.text, f"{state_name}=2;break;", "control"),
        ]
    else:
        after_state = 3
        cases = [
            RenderedCase(0, f"case 0:{{if({condition}){state_name}=1;else {state_name}=2;break;}}", "control"),
            _render_case(1, then_rendered.text, f"{state_name}=3;break;", "control"),
            _render_case(2, else_rendered.text, f"{state_name}=3;break;", "control"),
        ]
    shuffled_cases = _shuffle_cases(cases, text, 0, depth)
    rewritten = f"{{unsigned {state_name}=0;while({state_name}!={after_state})switch({state_name}){{{''.join(item.text for item in shuffled_cases)}}}}}"
    nested_cases = len(cases) + then_rendered.nested_case_count + else_rendered.nested_case_count
    return NestedRewrite(rewritten + text[else_end:], nested_cases)


def _rewrite_nested_loop(text: str, depth: int) -> NestedRewrite:
    open_paren = text.find("(")
    close_paren = _find_matching(text, open_paren, "(", ")") if open_paren >= 0 else -1
    if close_paren < 0:
        return NestedRewrite(text, 0)
    body_start = _skip_ws(text, close_paren + 1)
    if body_start >= len(text) or text[body_start] != "{":
        return NestedRewrite(text, 0)
    body_end = _statement_end(text, body_start)
    if body_end < 0:
        return NestedRewrite(text, 0)
    body_rendered = _rewrite_nested_blocks(text[body_start:body_end], depth + 1)
    return NestedRewrite(text[:body_start] + body_rendered.text + text[body_end:], body_rendered.nested_case_count)


def _rewrite_nested_do_while(text: str, depth: int) -> NestedRewrite:
    body_start = _skip_ws(text, 2)
    if body_start >= len(text) or text[body_start] != "{":
        return NestedRewrite(text, 0)
    body_end = _statement_end(text, body_start)
    if body_end < 0:
        return NestedRewrite(text, 0)
    body_rendered = _rewrite_nested_blocks(text[body_start:body_end], depth + 1)
    return NestedRewrite(text[:body_start] + body_rendered.text + text[body_end:], body_rendered.nested_case_count)


def _render_nested_brace_block(text: str, depth: int) -> NestedRewrite | None:
    inner_start = 1
    inner_end = len(text) - 1
    regions = _top_level_statement_regions(text, inner_start, inner_end)
    if not regions or not _should_flatten_nested_regions(regions):
        return NestedRewrite(text, 0)
    if _contains_loop_control_transfer(text):
        return NestedRewrite(text, 0)
    prelude, flatten_regions = _split_prelude_regions(regions)
    flatten_regions = _merge_declaration_regions(text, flatten_regions, protect_nontrivial_lifetimes=True)
    rendered = _render_block_state_machine(text, 0, len(text), prelude, flatten_regions, depth=depth)
    return NestedRewrite(rendered.text, rendered.case_count)


def _rewrite_nested_control_regions_in_text(text: str, depth: int) -> NestedRewrite:
    regions = _top_level_statement_regions(text, 0, len(text))
    if not regions:
        return NestedRewrite(text, 0)
    pieces: list[str] = []
    cursor = 0
    nested_cases = 0
    for index, region in enumerate(regions):
        pieces.append(text[cursor:region.start])
        if region.kind in {"IfStmt", "WhileStmt", "ForStmt", "DoStmt"}:
            rewritten = _rewrite_nested_blocks(region.text, depth)
            pieces.append(rewritten.text)
            nested_cases += rewritten.nested_case_count
        else:
            pieces.append(region.text)
        cursor = region.end
    pieces.append(text[cursor:])
    return NestedRewrite("".join(pieces), nested_cases)


def _should_flatten_nested_regions(regions: list[StatementRegion]) -> bool:
    return any(region.kind in _CONTROL_REGION_KINDS or _contains_early_exit(region.text) for region in regions)


def _matching_outer_braces(text: str) -> bool:
    return bool(text.startswith("{") and text.endswith("}") and _find_matching(text, 0, "{", "}") == len(text) - 1)


def _contains_lambda_like_text(text: str) -> bool:
    return bool(re.search(r"\[[^\]]*\]\s*(?:<[^>]*>\s*)?\([^)]*\)\s*(?:mutable\s*)?(?:->\s*[^{]+)?\{", text))


def _contains_loop_control_transfer(text: str) -> bool:
    return bool(re.search(r"\b(break|continue)\b", text))


def _split_prelude_regions(regions: list[StatementRegion]) -> tuple[list[StatementRegion], list[StatementRegion]]:
    prelude: list[StatementRegion] = []
    index = 0
    while index < len(regions):
        region = regions[index]
        if region.kind != "DeclStmt":
            break
        prelude.append(region)
        index += 1
    return prelude, regions[index:]


def _contains_early_exit(text: str) -> bool:
    return bool(re.search(r"\b(return|break|continue|co_return|throw)\b", text))


def _merge_declaration_regions(
    code: str,
    regions: list[StatementRegion],
    *,
    protect_nontrivial_lifetimes: bool = False,
) -> list[StatementRegion]:
    if not regions:
        return regions

    merged_spans: list[tuple[int, int]] = []
    for index, region in enumerate(regions):
        if region.kind != "DeclStmt":
            continue
        declared = _declared_names(region.text)
        if not declared:
            continue
        last_use = len(regions) - 1 if protect_nontrivial_lifetimes else index
        for later_index in range(index + 1, len(regions)):
            later_text = regions[later_index].text
            if any(re.search(rf"\b{re.escape(name)}\b", later_text) for name in declared):
                last_use = later_index
        if last_use > index:
            merged_spans.append((index, last_use))

    if not merged_spans:
        return regions

    merged_spans.sort()
    collapsed: list[tuple[int, int]] = []
    for start, end in merged_spans:
        if collapsed and start <= collapsed[-1][1] + 1:
            collapsed[-1] = (collapsed[-1][0], max(collapsed[-1][1], end))
        else:
            collapsed.append((start, end))

    result: list[StatementRegion] = []
    span_iter = iter(collapsed)
    current = next(span_iter, None)
    index = 0
    while index < len(regions):
        if current is not None and index == current[0]:
            start_index, end_index = current
            start = regions[start_index].start
            end = regions[end_index].end
            result.append(StatementRegion(start, end, code[start:end], "Atomic"))
            index = end_index + 1
            current = next(span_iter, None)
            continue
        result.append(regions[index])
        index += 1
    return result


def _declared_names(text: str) -> list[str]:
    stripped = _strip_leading_comments(text)
    if not stripped:
        return []
    names: list[str] = []
    for part in _split_declarators(stripped.rstrip(";")):
        left = part.split("=", 1)[0].strip()
        match = re.search(r"([A-Za-z_]\w*)\s*(?:\[[^\]]*\]|\([^;{}]*\))?$", left)
        if match:
            name = match.group(1)
            if name not in _DECLARATION_KEYWORDS:
                names.append(name)
    return names


def _split_declarators(text: str) -> list[str]:
    parts: list[str] = []
    start = 0
    paren = bracket = brace = angle = 0
    for index, char in enumerate(text):
        if char == "(":
            paren += 1
        elif char == ")":
            paren = max(0, paren - 1)
        elif char == "[":
            bracket += 1
        elif char == "]":
            bracket = max(0, bracket - 1)
        elif char == "{":
            brace += 1
        elif char == "}":
            brace = max(0, brace - 1)
        elif char == "<":
            angle += 1
        elif char == ">":
            angle = max(0, angle - 1)
        elif char == "," and paren == bracket == brace == angle == 0:
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return parts


def _can_split_control_condition(condition: str) -> bool:
    # `if (int x = f())` and `if (init; cond)` introduce names whose scope
    # includes the branch body. Splitting such statements across cases would
    # change visibility, so keep them atomic.
    if ";" in condition:
        return False
    if _contains_assignment_like_operator(condition):
        return False
    prefix = condition.lstrip().split(None, 1)[0] if condition.lstrip() else ""
    return prefix not in _CONDITION_DECL_PREFIXES


def _contains_assignment_like_operator(text: str) -> bool:
    for index, char in enumerate(text):
        if char != "=":
            continue
        prev_char = text[index - 1] if index else ""
        next_char = text[index + 1] if index + 1 < len(text) else ""
        if next_char == "=":
            continue
        if prev_char in {"=", "!", "<", ">"}:
            continue
        if prev_char in {"+", "-", "*", "/", "%", "&", "|", "^"}:
            return True
        return True
    return False


def _helper_block_skip_reason(block_plan: dict[str, Any]) -> str | None:
    diagnostics = block_plan.get("diagnostics")
    if isinstance(diagnostics, list) and diagnostics:
        return "; ".join(str(item) for item in diagnostics)
    statements = block_plan.get("statements")
    if not isinstance(statements, list):
        return None
    for statement in statements:
        if not isinstance(statement, dict):
            continue
        contains = statement.get("contains")
        if not isinstance(contains, dict):
            continue
        if contains.get("goto") or contains.get("label") or contains.get("try"):
            return "unsupported goto/label/try structure"
        control = statement.get("control")
        if isinstance(control, dict):
            for key in ("then_block", "else_block", "body_block"):
                nested = control.get(key)
                if isinstance(nested, dict):
                    reason = _helper_block_skip_reason(nested)
                    if reason is not None:
                        return reason
        nested_block = statement.get("block_plan")
        if isinstance(nested_block, dict):
            reason = _helper_block_skip_reason(nested_block)
            if reason is not None:
                return reason
    return None


def _range(value: Any) -> tuple[int, int] | None:
    if not isinstance(value, dict) or value.get("valid") is not True:
        return None
    start = value.get("start")
    end = value.get("end")
    if not isinstance(start, int) or not isinstance(end, int):
        return None
    return start, end


def _resolve_helper(helper_path: Path | None, config_helper_path: Path | None = None) -> Path:
    if helper_path is not None:
        if helper_path.exists() and helper_path.is_file():
            return helper_path.resolve()
        raise CfgHelperError(f"control-flow flattening requires cppgolf-cfg-helper; not found: {helper_path}")

    candidates: list[Path] = []
    if config_helper_path is not None:
        candidates.append(config_helper_path)
    env_path = os.environ.get("CPPGOLF_CFG_HELPER")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path.cwd() / "build" / "cfg-helper" / _helper_exe_name())
    candidates.append(Path(__file__).resolve().parents[1] / "build" / "cfg-helper" / _helper_exe_name())
    which = shutil.which(_helper_exe_name())
    if which:
        candidates.append(Path(which))

    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate.resolve()
    raise CfgHelperError("control-flow flattening requires cppgolf-cfg-helper; pass --cfg-helper or set CPPGOLF_CFG_HELPER")


def _helper_exe_name() -> str:
    return "cppgolf-cfg-helper.exe" if os.name == "nt" else "cppgolf-cfg-helper"


def _matches_any(qualified_name: str, simple_name: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if qualified_name == pattern or simple_name == pattern:
            return True
    return False


def _extract_condition(code: str, offset: int) -> str | None:
    open_paren = code.find("(", offset)
    if open_paren < 0:
        return None
    close_paren = _find_matching(code, open_paren, "(", ")")
    if close_paren < 0:
        return None
    return code[open_paren + 1:close_paren].strip()


def _extract_control_body(code: str, offset: int) -> tuple[str, str | None] | None:
    open_paren = code.find("(", offset)
    close_paren = _find_matching(code, open_paren, "(", ")") if open_paren >= 0 else -1
    if close_paren < 0:
        return None
    then_start = _skip_ws(code, close_paren + 1)
    then_end = _statement_end(code, then_start)
    if then_end < then_start:
        return None
    then_text = code[then_start:then_end].strip()
    else_text = None
    after_then = _skip_ws(code, then_end)
    if code.startswith("else", after_then):
        else_start = _skip_ws(code, after_then + 4)
        else_end = _statement_end(code, else_start)
        if else_end < else_start:
            return None
        else_text = code[else_start:else_end].strip()
    return then_text, else_text


def _skip_ws(code: str, index: int) -> int:
    while index < len(code) and code[index].isspace():
        index += 1
    return index


def _statement_end(code: str, start: int) -> int:
    if start >= len(code):
        return -1
    keyword_end = _control_statement_end(code, start)
    if keyword_end >= 0:
        return keyword_end
    if code[start] == "{":
        match = _find_matching(code, start, "{", "}")
        return match + 1 if match >= 0 else -1
    paren = bracket = brace = 0
    for index in range(start, len(code)):
        char = code[index]
        if char == "(":
            paren += 1
        elif char == ")":
            paren = max(0, paren - 1)
        elif char == "[":
            bracket += 1
        elif char == "]":
            bracket = max(0, bracket - 1)
        elif char == "{":
            brace += 1
        elif char == "}":
            if paren == bracket == brace == 0:
                return index
            brace = max(0, brace - 1)
        elif char == ";" and paren == bracket == brace == 0:
            return index + 1
    return -1


def _control_statement_end(code: str, start: int) -> int:
    if _starts_keyword(code, start, "if"):
        return _if_statement_end(code, start)
    if _starts_keyword(code, start, "while") or _starts_keyword(code, start, "for") or _starts_keyword(code, start, "switch"):
        return _paren_control_statement_end(code, start)
    if _starts_keyword(code, start, "do"):
        return _do_statement_end(code, start)
    return -1


def _if_statement_end(code: str, start: int) -> int:
    open_paren = code.find("(", start + 2)
    close_paren = _find_matching(code, open_paren, "(", ")") if open_paren >= 0 else -1
    if close_paren < 0:
        return -1
    then_start = _skip_ws(code, close_paren + 1)
    then_end = _statement_end(code, then_start)
    if then_end < 0:
        return -1
    after_then = _skip_ws(code, then_end)
    if _starts_keyword(code, after_then, "else"):
        else_start = _skip_ws(code, after_then + 4)
        else_end = _statement_end(code, else_start)
        return else_end
    return then_end


def _paren_control_statement_end(code: str, start: int) -> int:
    open_paren = code.find("(", start)
    close_paren = _find_matching(code, open_paren, "(", ")") if open_paren >= 0 else -1
    if close_paren < 0:
        return -1
    body_start = _skip_ws(code, close_paren + 1)
    return _statement_end(code, body_start)


def _do_statement_end(code: str, start: int) -> int:
    body_start = _skip_ws(code, start + 2)
    body_end = _statement_end(code, body_start)
    if body_end < 0:
        return -1
    while_start = _skip_ws(code, body_end)
    if not _starts_keyword(code, while_start, "while"):
        return -1
    open_paren = code.find("(", while_start + 5)
    close_paren = _find_matching(code, open_paren, "(", ")") if open_paren >= 0 else -1
    if close_paren < 0:
        return -1
    semicolon = _skip_ws(code, close_paren + 1)
    return semicolon + 1 if semicolon < len(code) and code[semicolon] == ";" else close_paren + 1


def _starts_keyword(code: str, index: int, keyword: str) -> bool:
    if not code.startswith(keyword, index):
        return False
    before = code[index - 1] if index > 0 else ""
    after_index = index + len(keyword)
    after = code[after_index] if after_index < len(code) else ""
    return not (before.isalnum() or before == "_") and not (after.isalnum() or after == "_")


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


def _constructor_initializer_colon(code: str, scan: int) -> int | None:
    paren = bracket = angle = 0
    while scan >= 0:
        char = code[scan]
        if char == ")":
            paren += 1
        elif char == "(":
            if paren:
                paren -= 1
        elif char == "]":
            bracket += 1
        elif char == "[":
            if bracket:
                bracket -= 1
        elif char == ">":
            angle += 1
        elif char == "<" and angle:
            angle -= 1
        elif (
            char == ":"
            and paren == bracket == angle == 0
            and code[scan - 1:scan] != ":"
            and code[scan + 1:scan + 2] != ":"
        ):
            return scan
        elif char in ";{}" and paren == bracket == angle == 0:
            return None
        scan -= 1
    return None


def _reserved_state_names(depth: int) -> set[str]:
    return {f"qcf{suffix}" for suffix in itertools.islice(_state_suffixes(), depth)}


def _state_suffixes():
    yield ""
    for length in itertools.count(1):
        for combo in itertools.product("abcdefghijklmnopqrstuvwxyz", repeat=length):
            yield "".join(combo)


def _fresh_state_name(text: str, reserved: set[str] | None = None) -> str:
    existing = set(_IDENT_RE.findall(text))
    if reserved:
        existing.update(reserved)
    for suffix in _state_suffixes():
        candidate = f"qcf{suffix}"
        if candidate not in existing:
            return candidate
    raise RuntimeError("unreachable")


def _apply_edits(code: str, edits: list[RewriteEdit]) -> str:
    result = code
    for edit in sorted(edits, key=lambda value: value.start, reverse=True):
        result = result[:edit.start] + edit.replacement + result[edit.end:]
    return result


def _log(verbose: bool, message: str) -> None:
    if verbose:
        print(f"[flatten_cfg] {message}", file=_sys.stderr)
