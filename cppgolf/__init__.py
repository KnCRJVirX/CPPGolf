"""
cppgolf — C++ multi-file merge & code golf tool

公开 API：
    process(input_file, include_dirs, **options) -> str
    golf_rename_symbols(code) -> str
    strip_comments(code) -> str
    merge_files(filepath, include_dirs, visited, sys_includes) -> str
    compress_whitespace(code) -> str
    golf_std_namespace / golf_typedefs / golf_endl_to_newline /
    golf_remove_main_return / golf_remove_inline /
    golf_braces_single_stmt / golf_define_shortcuts
"""

from .strip_comments import strip_comments
from .merge import merge_files, strip_include_guard
from .whitespace import compress_whitespace
from .transforms import (
    golf_std_namespace,
    golf_typedefs,
    golf_remove_main_return,
    golf_endl_to_newline,
    golf_remove_inline,
    golf_braces_single_stmt,
    golf_define_shortcuts,
)
from .golf_rename import golf_rename_symbols
from .__main__ import process

__all__ = [
    "process",
    "strip_comments",
    "merge_files",
    "strip_include_guard",
    "compress_whitespace",
    "golf_std_namespace",
    "golf_typedefs",
    "golf_remove_main_return",
    "golf_endl_to_newline",
    "golf_remove_inline",
    "golf_braces_single_stmt",
    "golf_define_shortcuts",
    "golf_rename_symbols",
]

__version__ = "0.1.8"
