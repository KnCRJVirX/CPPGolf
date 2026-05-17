"""CLI entrypoint for `python -m cppgolf` and the `cppgolf` script."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from .clang_args import (
    MissingClangError,
    get_platform_clang_args,
    get_platform_undefines,
    normalize_platform_name,
)
from .config import load_config
from .control_flow_flatten import CfgHelperError
from .merge import build_macro_table, merge_files
from .static_dedup import deduplicate_static_defs
from .strip_comments import strip_comments
from .transforms import (
    golf_braces_single_stmt,
    golf_define_shortcuts,
    golf_endl_to_newline,
    golf_remove_inline,
    golf_remove_main_return,
    golf_std_namespace,
    golf_typedefs,
    golf_windows_lean,
    inject_warning_pragmas,
)
from .whitespace import compress_whitespace


class ChineseArgumentParser(argparse.ArgumentParser):
    def format_help(self) -> str:
        return super().format_help().replace("usage:", "用法:", 1)


def process(
    input_files: list[Path] | Path,
    include_dirs: list[Path],
    *,
    defines: list[str] | None = None,
    platform: str | None = None,
    preserve_conditionals: bool = False,
    config_path: Path | None = None,
    inject_defines: bool = False,
    merge_only: bool = False,
    no_merge: bool = False,
    no_strip_comments: bool = False,
    no_compress_ws: bool = False,
    no_std_ns: bool = False,
    no_typedefs: bool = False,
    no_win_lean: bool = False,
    suppress_warnings: bool = False,
    keep_main_return: bool = False,
    keep_endl: bool = False,
    keep_inline: bool = False,
    aggressive: bool = False,
    define_shortcuts: bool = False,
    rename_symbols: bool = False,
    rename_functions: bool = False,
    rename_types: bool = False,
    flatten_cfg: bool = False,
    flatten_cfg_functions: list[str] | None = None,
    cfg_helper_path: Path | None = None,
    cfg_helper_includes: list[Path] | None = None,
    flower: bool = False,
    flower_dead_code: bool = False,
    flower_decls: bool = False,
    flower_functions: list[str] | None = None,
    flower_seed: int | None = None,
    flower_dead_blocks: int | None = None,
    flower_decl_count: int | None = None,
    dedup_statics: bool = False,
    verbose: bool = False,
) -> tuple[str, int]:
    """Run the cppgolf pipeline and return `(output_code, merged_size)`."""
    dump_dir_env = os.environ.get("CPPGOLF_DEBUG_DUMP_DIR")
    dump_dir = Path(dump_dir_env) if dump_dir_env else None
    dump_stage_index = 0

    def dump_stage(stage_name: str, stage_code: str) -> None:
        nonlocal dump_stage_index
        if dump_dir is None:
            return
        dump_dir.mkdir(parents=True, exist_ok=True)
        safe_name = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in stage_name).strip("._")
        if not safe_name:
            safe_name = "stage"
        output_path = dump_dir / f"{dump_stage_index:02d}_{safe_name}.cpp"
        output_path.write_text(stage_code, encoding="utf-8")
        dump_stage_index += 1

    if isinstance(input_files, Path):
        input_files = [input_files]

    defines = list(defines or [])
    config = load_config(config_path)
    flatten_cfg_functions = list(flatten_cfg_functions or [])
    cfg_helper_includes = list(cfg_helper_includes or [])
    flower_functions = list(flower_functions or [])
    flatten_cfg_enabled = flatten_cfg or config.flatten_cfg.enabled
    flatten_cfg_targets = list(dict.fromkeys([*config.flatten_cfg.functions, *flatten_cfg_functions]))
    flatten_cfg_helper_includes = list(dict.fromkeys([*config.flatten_cfg.helper_includes, *cfg_helper_includes]))
    flower_dead_enabled = flower_dead_code or flower or (config.flower.enabled and config.flower.dead_code)
    flower_decls_enabled = flower_decls or flower or (config.flower.enabled and config.flower.declarations)
    flower_targets = list(dict.fromkeys([*config.flower.functions, *flower_functions]))
    flower_exclude = list(dict.fromkeys(config.flower.exclude))
    effective_flower_seed = config.flower.seed if flower_seed is None else flower_seed
    effective_flower_dead_blocks = (
        config.flower.dead_blocks_per_function if flower_dead_blocks is None else flower_dead_blocks
    )
    effective_flower_decl_count = config.flower.declaration_count if flower_decl_count is None else flower_decl_count
    normalized_platform = normalize_platform_name(platform)
    effective_defines = _build_effective_defines(defines, normalized_platform)
    effective_undefines = _build_effective_undefines(normalized_platform)
    macros = build_macro_table(effective_defines, undefines=effective_undefines)
    sys_includes: list[str] = []
    visited: set[Path] = set()
    once_included: set[Path] = set()
    file_char_ranges: list[tuple[int, int, str]] = []

    if not no_merge:
        file_parts: list[str] = []
        for source_file in input_files:
            part = merge_files(
                source_file,
                list(include_dirs),
                visited,
                sys_includes,
                macros,
                once_included,
                preserve_conditionals,
            )
            file_parts.append(part)

        header = "".join(sys_includes)
        code = header + "".join(file_parts)

        offset = len(header)
        for source_file, part in zip(input_files, file_parts):
            file_char_ranges.append((offset, offset + len(part), source_file.stem))
            offset += len(part)
    else:
        code = "".join(
            source_file.read_text(encoding="utf-8-sig", errors="replace")
            for source_file in input_files
        )

    merged_size = len(code.encode("utf-8"))
    if inject_defines and defines:
        code = _inject_defines(code, defines)
    dump_stage("merged", code)

    if merge_only:
        return code.strip() + "\n", merged_size

    if dedup_statics:
        language = "c" if input_files and input_files[0].suffix.lower() == ".c" else "c++"
        extra_args = _build_extra_args(include_dirs, defines, platform=normalized_platform)
        code = deduplicate_static_defs(
            code,
            lang=language,
            extra_args=extra_args,
            platform=normalized_platform,
            verbose=verbose,
            file_ranges=file_char_ranges if file_char_ranges else None,
        )
        dump_stage("dedup_statics", code)

    if not no_strip_comments:
        code = strip_comments(code)
        dump_stage("strip_comments", code)
    if not keep_endl:
        code = golf_endl_to_newline(code)
        dump_stage("endl_to_newline", code)
    if not no_std_ns:
        code = golf_std_namespace(code)
        dump_stage("std_namespace", code)
    if not no_typedefs:
        code = golf_typedefs(code)
        dump_stage("typedefs", code)
    if not keep_main_return:
        code = golf_remove_main_return(code)
        dump_stage("remove_main_return", code)
    if not keep_inline:
        code = golf_remove_inline(code)
        dump_stage("remove_inline", code)
    if not no_win_lean:
        code = golf_windows_lean(code)
        dump_stage("windows_lean", code)
    if aggressive:
        code = golf_braces_single_stmt(code)
        dump_stage("aggressive_braces", code)
    if define_shortcuts:
        code = golf_define_shortcuts(code)
        dump_stage("define_shortcuts", code)
    if flatten_cfg_enabled and flatten_cfg_targets:
        code = _flatten_cfg(
            code,
            include_dirs=include_dirs,
            defines=defines,
            platform=normalized_platform,
            functions=flatten_cfg_targets,
            exclude=config.flatten_cfg.exclude,
            helper_path=cfg_helper_path,
            config_helper_path=config.flatten_cfg.helper,
            helper_include_dirs=flatten_cfg_helper_includes,
            verbose=verbose,
        )
        dump_stage("flatten_cfg", code)
    if rename_symbols:
        code = _rename_symbols(
            code,
            include_dirs=include_dirs,
            defines=defines,
            platform=normalized_platform,
            rename_functions=rename_functions,
            verbose=verbose,
        )
        dump_stage("rename_symbols", code)
    if rename_types:
        code = _rename_types(
            code,
            input_files=input_files,
            include_dirs=include_dirs,
            defines=defines,
            platform=normalized_platform,
            verbose=verbose,
        )
        dump_stage("rename_types", code)
    if flower_dead_enabled or flower_decls_enabled:
        code = _insert_flowers(
            code,
            include_dirs=include_dirs,
            defines=defines,
            platform=normalized_platform,
            dead_code=flower_dead_enabled,
            declarations=flower_decls_enabled,
            functions=flower_targets,
            exclude=flower_exclude,
            seed=effective_flower_seed,
            dead_blocks_per_function=effective_flower_dead_blocks,
            declaration_count=effective_flower_decl_count,
            helper_path=cfg_helper_path,
            config_helper_path=config.flatten_cfg.helper,
            helper_include_dirs=flatten_cfg_helper_includes,
            verbose=verbose,
        )
        dump_stage("flower", code)

    if suppress_warnings:
        code = inject_warning_pragmas(code)
        dump_stage("suppress_warnings", code)

    if not no_compress_ws:
        code = compress_whitespace(code)
        dump_stage("compress_whitespace", code)

    return code.strip() + "\n", merged_size


def build_parser() -> argparse.ArgumentParser:
    parser = ChineseArgumentParser(
        prog="cppgolf",
        add_help=False,
        description="合并并可选压缩/混淆 C/C++ 源文件。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  cppgolf solution.cpp\n"
            "  cppgolf solution.cpp -o golf.cpp\n"
            "  cppgolf solution.cpp --merge-only -I include/\n"
            "  cppgolf solution.cpp -DDEBUG=1 --inject-define --merge-only\n"
            "  cppgolf solution.cpp --platform windows --merge-only\n"
            "  cppgolf solution.cpp --keep-conditionals --merge-only -DDEBUG=1\n"
            "  cppgolf solution.cpp --config cppgolf.toml --flatten-cfg\n"
            "  cppgolf solution.cpp -I include/ -DBOTZONE=1 --rename-functions --stats\n"
        ),
    )
    parser._positionals.title = "位置参数"
    parser._optionals.title = "选项"
    parser.add_argument("-h", "--help", action="help", help="显示此帮助信息并退出。")
    parser.add_argument(
        "input",
        type=Path,
        nargs="+",
        help="一个或多个 C/C++ 源文件，会按给定顺序合并。",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=None,
        help="将输出写入文件，而不是标准输出。",
    )
    parser.add_argument(
        "-I",
        "--include",
        dest="include_dirs",
        action="append",
        type=Path,
        default=[],
        metavar="DIR",
        help="添加 include 搜索目录，可重复传入。",
    )
    parser.add_argument(
        "-D",
        "--define",
        dest="defines",
        action="append",
        default=[],
        metavar="MACRO[=VALUE]",
        help="为合并和 libclang 步骤添加预处理宏，可重复传入。",
    )
    parser.add_argument(
        "--inject-define",
        action="store_true",
        help="把命令行 `-D` 宏定义写入生成源码。",
    )
    parser.add_argument(
        "--platform",
        choices=["current", "windows", "linux", "macos"],
        default="current",
        help="选择合并解析和 libclang 步骤使用的平台宏。",
    )
    parser.add_argument(
        "--keep-conditionals",
        action="store_true",
        help="保留已解析的 #if/#ifdef/#ifndef 条件块，不删除未选分支。",
    )
    parser.add_argument(
        "--config",
        dest="config_path",
        type=Path,
        default=None,
        metavar="PATH",
        help="从 TOML 配置文件读取 cppgolf 选项。",
    )

    parser.add_argument(
        "--cfg-helper",
        dest="cfg_helper_path",
        type=Path,
        default=None,
        metavar="PATH",
        help="指定必需的 cppgolf-cfg-helper 可执行文件路径。",
    )
    parser.add_argument(
        "--cfg-helper-include",
        dest="cfg_helper_includes",
        type=Path,
        action="append",
        default=[],
        metavar="PATH",
        help="追加仅供 CFG helper 解析使用的系统/跨平台头文件目录。",
    )

    common = parser.add_argument_group("Default Passes")
    common.add_argument(
        "--merge-only",
        action="store_true",
        help="Only merge files; skip comment stripping, compression, and code rewrites.",
    )
    common.add_argument("--no-merge", action="store_true", help='Skip inlining local `#include "..."` directives.')
    common.add_argument("--no-strip-comments", action="store_true", help="Keep comments.")
    common.add_argument("--no-compress-ws", action="store_true", help="Keep whitespace formatting.")
    common.add_argument("--no-std-ns", action="store_true", help="Do not add `using namespace std;`.")
    common.add_argument("--no-typedefs", action="store_true", help="Do not add shorthand typedef aliases.")
    common.add_argument("--no-rename", action="store_true", help="Disable symbol renaming.")
    common.add_argument(
        "--suppress-warnings",
        action="store_true",
        help="Inject GCC/Clang pragmas to silence noisy generated-code warnings.",
    )
    common.add_argument(
        "--no-win-lean",
        action="store_true",
        help="Do not inject `WIN32_LEAN_AND_MEAN` / `_HAS_STD_BYTE` guards.",
    )
    common.add_argument("--keep-main-return", action="store_true", help="Keep trailing `return 0;` in `main`.")
    common.add_argument("--keep-endl", action="store_true", help="Keep `endl`.")
    common.add_argument("--keep-inline", action="store_true", help="Keep the `inline` keyword.")
    common.add_argument(
        "--dedup-statics",
        dest="dedup_statics",
        action="store_true",
        help="Use libclang to deduplicate conflicting `static` definitions after merge.",
    )

    aggressive = parser.add_argument_group("Optional Aggressive Passes")
    aggressive.add_argument(
        "--aggressive",
        action="store_true",
        help="Remove braces from single-statement `if`/`for`/`while` bodies.",
    )
    aggressive.add_argument(
        "--shortcuts",
        dest="define_shortcuts",
        action="store_true",
        help="Add shortcut defines for frequent `cout` / `cin` usages.",
    )
    aggressive.add_argument(
        "--rename-functions",
        dest="rename_functions",
        action="store_true",
        help="Also rename user-defined free/member functions.",
    )
    aggressive.add_argument(
        "--rename-type",
        dest="rename_types",
        action="store_true",
        help="Generate typedef aliases for long user-defined type names.",
    )
    aggressive.add_argument(
        "--flatten-cfg",
        action="store_true",
        help="Flatten configured function bodies into a switch-based state machine.",
    )
    aggressive.add_argument(
        "--flatten-cfg-function",
        dest="flatten_cfg_functions",
        action="append",
        default=[],
        metavar="NAME",
        help="Add an exact function name for control-flow flattening.",
    )
    aggressive.add_argument(
        "--flower",
        action="store_true",
        help="启用函数死代码和声明插花混淆。",
    )
    aggressive.add_argument(
        "--flower-dead-code",
        action="store_true",
        help="只启用函数体内复杂恒假死代码插花。",
    )
    aggressive.add_argument(
        "--flower-decls",
        action="store_true",
        help="只启用全局/命名空间/类作用域声明插花。",
    )
    aggressive.add_argument(
        "--flower-function",
        dest="flower_functions",
        action="append",
        default=[],
        metavar="NAME",
        help="限制死代码插花到指定函数，可重复传入。",
    )
    aggressive.add_argument(
        "--flower-seed",
        type=int,
        default=None,
        metavar="N",
        help="设置插花随机种子，默认 1。",
    )
    aggressive.add_argument(
        "--flower-dead-blocks",
        type=int,
        default=None,
        metavar="N",
        help="每个函数最多插入几个死代码块，默认 1。",
    )
    aggressive.add_argument(
        "--flower-decl-count",
        type=int,
        default=None,
        metavar="N",
        help="声明插花总数上限，默认 24。",
    )

    parser.add_argument("-v", "--verbose", action="store_true", help="Print more rename/dedup details.")
    parser.add_argument("--stats", action="store_true", help="Print size statistics to stderr.")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    if args.merge_only and args.no_merge:
        parser.error("--merge-only 与 --no-merge 不能同时使用")

    missing = [path for path in args.input if not path.exists()]
    if missing:
        for path in missing:
            print(f"error: file does not exist: {path}", file=sys.stderr)
        raise SystemExit(1)

    try:
        result, original_size = process(
            args.input,
            args.include_dirs,
            defines=args.defines,
            platform=args.platform,
            preserve_conditionals=args.keep_conditionals,
            config_path=args.config_path,
            inject_defines=args.inject_define,
            merge_only=args.merge_only,
            no_merge=args.no_merge,
            no_strip_comments=args.no_strip_comments,
            no_compress_ws=args.no_compress_ws,
            no_std_ns=args.no_std_ns,
            no_typedefs=args.no_typedefs,
            no_win_lean=args.no_win_lean,
            suppress_warnings=args.suppress_warnings,
            keep_main_return=args.keep_main_return,
            keep_endl=args.keep_endl,
            keep_inline=args.keep_inline,
            aggressive=args.aggressive,
            define_shortcuts=args.define_shortcuts,
            rename_symbols=not args.no_rename,
            rename_functions=args.rename_functions,
            rename_types=args.rename_types,
            flatten_cfg=args.flatten_cfg,
            flatten_cfg_functions=args.flatten_cfg_functions,
            cfg_helper_path=args.cfg_helper_path,
            cfg_helper_includes=args.cfg_helper_includes,
            flower=args.flower,
            flower_dead_code=args.flower_dead_code,
            flower_decls=args.flower_decls,
            flower_functions=args.flower_functions,
            flower_seed=args.flower_seed,
            flower_dead_blocks=args.flower_dead_blocks,
            flower_decl_count=args.flower_decl_count,
            dedup_statics=args.dedup_statics,
            verbose=args.verbose,
        )
    except MissingClangError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc
    except CfgHelperError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    def print_stats(final_size: int) -> None:
        ratio = (1 - final_size / original_size) * 100 if original_size else 0
        print(
            f"[stats] merged: {original_size} B -> output: {final_size} B ({ratio:.1f}% saved)",
            file=sys.stderr,
        )

    if args.output:
        args.output.write_text(result, encoding="utf-8")
        if args.stats:
            print_stats(args.output.stat().st_size)
        else:
            print(f"wrote: {args.output}", file=sys.stderr)
        return

    if args.stats:
        print_stats(len(result.encode("utf-8")))
    sys.stdout.write(result)


def _rename_symbols(
    code: str,
    *,
    include_dirs: list[Path],
    defines: list[str],
    platform: str | None,
    rename_functions: bool,
    verbose: bool,
) -> str:
    from .golf_rename import golf_rename_symbols

    extra_args = _build_extra_args(include_dirs, defines, platform=platform)
    return golf_rename_symbols(
        code,
        rename_functions=rename_functions,
        verbose=verbose,
        extra_args=extra_args,
        platform=platform,
    )


def _rename_types(
    code: str,
    *,
    input_files: list[Path],
    include_dirs: list[Path],
    defines: list[str],
    platform: str | None,
    verbose: bool,
) -> str:
    from .golf_rename_types import golf_rename_types

    language = "c" if input_files and input_files[0].suffix.lower() == ".c" else "c++"
    extra_args = _build_extra_args(include_dirs, defines, platform=platform)
    return golf_rename_types(
        code,
        lang=language,
        extra_args=extra_args,
        platform=platform,
        verbose=verbose,
    )


def _flatten_cfg(
    code: str,
    *,
    include_dirs: list[Path],
    defines: list[str],
    platform: str | None,
    functions: list[str],
    exclude: list[str],
    helper_path: Path | None,
    config_helper_path: Path | None,
    helper_include_dirs: list[Path],
    verbose: bool,
) -> str:
    from .control_flow_flatten import flatten_control_flow

    extra_args = _build_extra_args(include_dirs, defines, platform=platform)
    return flatten_control_flow(
        code,
        functions=functions,
        exclude=exclude,
        extra_args=extra_args,
        platform=platform,
        helper_path=helper_path,
        config_helper_path=config_helper_path,
        helper_include_dirs=helper_include_dirs,
        verbose=verbose,
    )


def _insert_flowers(
    code: str,
    *,
    include_dirs: list[Path],
    defines: list[str],
    platform: str | None,
    dead_code: bool,
    declarations: bool,
    functions: list[str],
    exclude: list[str],
    seed: int,
    dead_blocks_per_function: int,
    declaration_count: int,
    helper_path: Path | None,
    config_helper_path: Path | None,
    helper_include_dirs: list[Path],
    verbose: bool,
) -> str:
    from .flower import insert_flowers

    extra_args = _build_extra_args(include_dirs, defines, platform=platform)
    return insert_flowers(
        code,
        dead_code=dead_code,
        declarations=declarations,
        functions=functions,
        exclude=exclude,
        seed=seed,
        dead_blocks_per_function=dead_blocks_per_function,
        declaration_count=declaration_count,
        extra_args=extra_args,
        platform=platform,
        helper_path=helper_path,
        config_helper_path=config_helper_path,
        helper_include_dirs=helper_include_dirs,
        verbose=verbose,
    )


def _build_extra_args(
    include_dirs: list[Path],
    defines: list[str],
    *,
    platform: str | None = None,
) -> list[str]:
    args = [f"-I{directory}" for directory in include_dirs]
    args.extend(_format_defines(defines))
    args.extend(get_platform_clang_args(platform=platform))
    return args


def _build_effective_defines(defines: list[str], platform: str | None) -> list[str]:
    return [*defines, *get_platform_clang_args(platform=platform)]


def _build_effective_undefines(platform: str | None) -> list[str]:
    return get_platform_undefines(platform=platform)


def _format_defines(defines: list[str]) -> list[str]:
    return [define if define.startswith("-D") else f"-D{define}" for define in defines]


def _inject_defines(code: str, defines: list[str]) -> str:
    lines = _render_define_lines(defines)
    if not lines:
        return code
    return "\n".join(lines) + "\n" + code


def _render_define_lines(defines: list[str]) -> list[str]:
    lines: list[str] = []
    for define in defines:
        text = define[2:] if define.startswith("-D") else define
        if not text:
            continue
        name, sep, value = text.partition("=")
        name = name.strip()
        if not name:
            continue
        if sep:
            lines.append(f"#define {name} {value}".rstrip())
        else:
            lines.append(f"#define {name}")
    return lines


if __name__ == "__main__":
    main()
