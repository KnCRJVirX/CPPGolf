"""Shared helpers for libclang-backed features."""

from __future__ import annotations

import os
import struct
import sys
from collections.abc import Sequence

_PLATFORM_DEFINE_SETS: dict[str, list[str]] = {
    "win32": ["-D_WIN32", "-DWIN32", "-D_HAS_STD_BYTE=0", "-DWIN32_LEAN_AND_MEAN"],
    "linux": ["-D__linux__", "-D__unix__", "-DLINUX"],
    "darwin": ["-D__APPLE__", "-D__unix__", "-D__MACH__"],
}
_EXPLICIT_PLATFORM_UNDEFINES = ["__ANDROID__"]
_WINDOWS_COMPILER_MACROS = [
    "_MSC_VER",
    "_MSC_FULL_VER",
    "_MSC_BUILD",
    "_MSVC_LANG",
    "__MINGW32__",
    "__MINGW64__",
    "__MINGW64_VERSION_MAJOR",
    "__MINGW64_VERSION_MINOR",
    "__MINGW32_MAJOR_VERSION",
    "__MINGW32_MINOR_VERSION",
]


class MissingClangError(RuntimeError):
    """Raised when an optional libclang-backed feature is requested without clang."""


def normalize_platform_name(platform: str | None) -> str | None:
    """Normalize CLI/platform aliases to the values used by this module."""
    if platform is None:
        return None

    value = platform.strip().lower()
    aliases = {
        "current": None,
        "host": None,
        "windows": "win32",
        "win": "win32",
        "win32": "win32",
        "linux": "linux",
        "gnu/linux": "linux",
        "macos": "darwin",
        "mac": "darwin",
        "osx": "darwin",
        "darwin": "darwin",
    }
    if value not in aliases:
        raise ValueError(f"unsupported platform: {platform}")
    return aliases[value]


def get_platform_clang_args(
    *,
    platform: str | None = None,
    os_name: str | None = None,
    pointer_size: int | None = None,
) -> list[str]:
    """Return platform-specific clang preprocessor defines."""
    normalized = normalize_platform_name(platform)
    platform = sys.platform if normalized is None else normalized
    if os_name is None:
        if platform == "win32":
            os_name = "nt"
        elif platform.startswith("linux") or platform == "darwin":
            os_name = "posix"
        else:
            os_name = os.name
    pointer_size = struct.calcsize("P") if pointer_size is None else pointer_size

    args: list[str] = []
    if platform == "win32" or os_name == "nt":
        args.extend(_PLATFORM_DEFINE_SETS["win32"])
        if pointer_size == 8:
            args.extend(["-D_WIN64", "-DWIN64"])
    elif platform.startswith("linux"):
        args.extend(_PLATFORM_DEFINE_SETS["linux"])
    elif platform == "darwin":
        args.extend(_PLATFORM_DEFINE_SETS["darwin"])
    return args


def get_platform_undefines(*, platform: str | None = None) -> list[str]:
    """Return macros that should be considered explicitly undefined for a target platform."""
    normalized = normalize_platform_name(platform)
    active = sys.platform if normalized is None else normalized

    undefines: list[str] = list(_EXPLICIT_PLATFORM_UNDEFINES)
    for platform_name, defines in _PLATFORM_DEFINE_SETS.items():
        if platform_name == active:
            continue
        for define in defines:
            name = define[2:].split("=", 1)[0]
            if name not in {"__unix__"} or active not in {"linux", "darwin"}:
                undefines.append(name)

    if active != "win32":
        undefines.extend(["_WIN64", "WIN64", *_WINDOWS_COMPILER_MACROS])
    return undefines


def build_clang_parse_args(
    *,
    lang: str,
    std: str,
    extra_args: Sequence[str] | None = None,
    include_spellcheck_flag: bool = True,
    platform: str | None = None,
) -> list[str]:
    """Build parse arguments for libclang consumers in this project."""
    args = ["-x", "c++" if lang == "c++" else "c", std, "-w"]
    if include_spellcheck_flag:
        args.append("-fno-spell-checking")
    args.extend(get_platform_clang_args(platform=platform))
    if extra_args:
        args.extend(extra_args)
    return args


def load_clang_cindex(feature_name: str):
    """Import clang.cindex or raise a project-specific error."""
    try:
        import clang.cindex as ci  # type: ignore
    except ImportError as exc:
        raise MissingClangError(
            f"{feature_name} requires the optional 'libclang' dependency"
        ) from exc
    return ci


def get_specialized_cursor_template(ci):
    """Return clang_getSpecializedCursorTemplate when available."""
    try:
        func = ci.conf.lib.clang_getSpecializedCursorTemplate
    except AttributeError:
        return None
    func.restype = ci.Cursor
    func.argtypes = [ci.Cursor]
    return func
