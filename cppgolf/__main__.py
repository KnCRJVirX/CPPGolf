"""cppgolf.__main__ — CLI 入口，支持 python -m cppgolf 和 cppgolf 命令"""
import sys
import argparse
from pathlib import Path

from .strip_comments import strip_comments
from .merge import merge_files
from .whitespace import compress_whitespace
from .transforms import (
    golf_std_namespace, golf_typedefs, golf_remove_main_return,
    golf_endl_to_newline, golf_remove_inline, golf_windows_lean,
    golf_braces_single_stmt, golf_define_shortcuts,
)
from .golf_rename import golf_rename_symbols
from .golf_rename_types import golf_rename_types
from .static_dedup import deduplicate_static_defs


def process(
    input_files: 'list[Path] | Path',
    include_dirs: list,
    *,
    no_merge: bool = False,
    no_strip_comments: bool = False,
    no_compress_ws: bool = False,
    no_std_ns: bool = False,
    no_typedefs: bool = False,
    keep_main_return: bool = False,
    keep_endl: bool = False,
    keep_inline: bool = False,
    aggressive: bool = False,
    define_shortcuts: bool = False,
    rename_symbols: bool = False,
    rename_functions: bool = False,
    rename_types: bool = False,
    dedup_statics: bool = False,
    verbose: bool = False,
) -> tuple[str, int]:
    # 统一为列表，兼容旧版单文件调用
    if isinstance(input_files, Path):
        input_files = [input_files]

    sys_includes: list = []
    visited: set = set()

    # 逐个文件合并，跟踪每个文件在最终文本中的字符范围（供 dedup_statics 使用）
    file_char_ranges: list[tuple[int, int, str]] = []  # (start, end, stem)

    if not no_merge:
        file_parts: list[str] = []
        for f in input_files:
            part = merge_files(f, list(include_dirs), visited, sys_includes)
            file_parts.append(part)
        header = ''.join(sys_includes)
        code = header + ''.join(file_parts)
        # 记录字符范围（含 header 偏移）
        offset = len(header)
        for f, part in zip(input_files, file_parts):
            file_char_ranges.append((offset, offset + len(part), f.stem))
            offset += len(part)
    else:
        code = ''.join(f.read_text(encoding='utf-8-sig', errors='replace')
                       for f in input_files)

    # 合并后、变换前的大小（供统计用）
    merged_size = len(code.encode('utf-8'))

    # ── dedup_statics 在 strip_comments 之前执行，此时 file_char_ranges 有效 ──
    if dedup_statics:
        _ext = input_files[0].suffix.lower() if input_files else '.cpp'
        _lang = 'c' if _ext == '.c' else 'c++'
        _extra = [f'-I{d}' for d in include_dirs]
        code = deduplicate_static_defs(
            code, lang=_lang, extra_args=_extra, verbose=verbose,
            file_ranges=file_char_ranges if file_char_ranges else None,
        )

    if not no_strip_comments:
        code = strip_comments(code)
    if not keep_endl:
        code = golf_endl_to_newline(code)
    if not no_std_ns:
        code = golf_std_namespace(code)
    if not no_typedefs:
        code = golf_typedefs(code)
    if not keep_main_return:
        code = golf_remove_main_return(code)
    if not keep_inline:
        code = golf_remove_inline(code)
    code = golf_windows_lean(code)
    if aggressive:
        code = golf_braces_single_stmt(code)
    if define_shortcuts:
        code = golf_define_shortcuts(code)
    if rename_symbols:
        code = golf_rename_symbols(code, rename_functions=rename_functions, verbose=verbose)
    if rename_types:
        _ext2 = input_files[0].suffix.lower() if input_files else '.cpp'
        _lang2 = 'c' if _ext2 == '.c' else 'c++'
        _extra2 = [f'-I{d}' for d in include_dirs]
        code = golf_rename_types(code, lang=_lang2, extra_args=_extra2, verbose=verbose)

    if not no_compress_ws:
        code = compress_whitespace(code)

    return code.strip() + '\n', merged_size


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog='cppgolf',
        description='C++ 多文件合并 + 代码高尔夫工具',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""示例：
  cppgolf solution.cpp
  cppgolf solution.cpp -o golf.cpp
  cppgolf solution.cpp -I include/ --rename --stats
""",
    )
    p.add_argument('input', type=Path, nargs='+', help='一个或多个 C++ 源文件（顺序合并，类似编译器多文件输入）')
    p.add_argument('-o', '--output', type=Path, default=None, help='输出文件（默认 stdout）')
    p.add_argument('-I', '--include', dest='include_dirs', action='append',
                   type=Path, default=[], metavar='DIR', help='追加 include 目录（可多次）')

    g = p.add_argument_group('功能开关（默认全部开启）')
    g.add_argument('--no-merge',          action='store_true', help='跳过多文件合并')
    g.add_argument('--no-strip-comments', action='store_true', help='保留注释')
    g.add_argument('--no-compress-ws',    action='store_true', help='保留空白格式')
    g.add_argument('--no-std-ns',         action='store_true', help='不添加 using namespace std')
    g.add_argument('--no-typedefs',       action='store_true', help='不添加 ll/ld 等类型宏')
    g.add_argument('--no-rename',         action='store_true', help='不将用户变量/成员名压缩为短名')
    g.add_argument('--keep-main-return',  action='store_true', help='保留 main 末尾 return 0')
    g.add_argument('--keep-endl',         action='store_true', help='保留 endl')
    g.add_argument('--keep-inline',       action='store_true', help='保留 inline 关键字')
    g.add_argument('--dedup-statics', dest='dedup_statics', action='store_true',
                   help='用 libclang 对 static 函数/变量重复定义去重（多文件合并时使用）')

    g2 = p.add_argument_group('激进优化（有风险，默认关闭）')
    g2.add_argument('--aggressive', action='store_true',
                    help='单语句 if/for/while 去花括号')
    g2.add_argument('--shortcuts', dest='define_shortcuts', action='store_true',
                    help='高频 cout/cin 用 #define 缩写')
    g2.add_argument('--rename-functions', dest='rename_functions', action='store_true',
                    help='重命名用户定义的自由函数和成员函数（不含构造/析构/main）')
    g2.add_argument('--rename-type', dest='rename_types', action='store_true',
                    help='为长类型名（struct/class/enum，名称≥5字符）添加 typedef 短名并重命名所有引用')
    p.add_argument('-v', '--verbose', action='store_true', help='显示重命名映射详情（变量/函数/类型）')
    p.add_argument('--stats', action='store_true', help='显示压缩率统计')
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    missing = [f for f in args.input if not f.exists()]
    if missing:
        for f in missing:
            print(f'错误：文件不存在 —— {f}', file=sys.stderr)
        sys.exit(1)

    result, original_size = process(
        args.input, args.include_dirs,
        no_merge=args.no_merge,
        no_strip_comments=args.no_strip_comments,
        no_compress_ws=args.no_compress_ws,
        no_std_ns=args.no_std_ns,
        no_typedefs=args.no_typedefs,
        keep_main_return=args.keep_main_return,
        keep_endl=args.keep_endl,
        keep_inline=args.keep_inline,
        aggressive=args.aggressive,
        define_shortcuts=args.define_shortcuts,
        rename_symbols=not(args.no_rename),
        rename_functions=args.rename_functions,
        rename_types=args.rename_types,
        dedup_statics=args.dedup_statics,
        verbose=args.verbose,
    )

    def print_stats(final_size: int):
        ratio = (1 - final_size / original_size) * 100 if original_size else 0
        print(f'[统计] 合并后：{original_size} B  →  高尔夫后：{final_size} B  （压缩 {ratio:.1f}%）',
              file=sys.stderr)

    if args.output:
        args.output.write_text(result, encoding='utf-8')
        if args.stats:
            print_stats(args.output.stat().st_size)
        else:
            print(f'已写入：{args.output}', file=sys.stderr)
    else:
        if args.stats:
            print_stats(len(result.encode('utf-8')))
        sys.stdout.write(result)


if __name__ == '__main__':
    main()
