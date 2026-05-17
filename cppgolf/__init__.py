"""Public package API for cppgolf."""

from __future__ import annotations

from .merge import merge_files, strip_include_guard
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
)
from .whitespace import compress_whitespace


def process(*args, **kwargs):
    """Lazy wrapper around the main processing pipeline."""
    from .__main__ import process as impl

    return impl(*args, **kwargs)


def golf_rename_symbols(*args, **kwargs):
    """Lazy wrapper around the libclang-backed symbol renamer."""
    from .golf_rename import golf_rename_symbols as impl

    return impl(*args, **kwargs)


def flatten_control_flow(*args, **kwargs):
    """Lazy wrapper around the libclang-backed CFG flattener."""
    from .control_flow_flatten import flatten_control_flow as impl

    return impl(*args, **kwargs)


def insert_flowers(*args, **kwargs):
    """Lazy wrapper around the helper-backed flower obfuscator."""
    from .flower import insert_flowers as impl

    return impl(*args, **kwargs)


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
    "golf_windows_lean",
    "golf_braces_single_stmt",
    "golf_define_shortcuts",
    "golf_rename_symbols",
    "flatten_control_flow",
    "insert_flowers",
]

__version__ = "0.1.10"
