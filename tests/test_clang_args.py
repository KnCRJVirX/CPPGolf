from __future__ import annotations

from cppgolf.clang_args import get_platform_clang_args, get_platform_undefines, normalize_platform_name


def test_windows_clang_args_include_windows_macros():
    args = get_platform_clang_args(platform="win32", os_name="nt", pointer_size=8)
    assert "-D_WIN32" in args
    assert "-DWIN32" in args
    assert "-D_WIN64" in args
    assert "-DWIN64" in args
    assert "-D_HAS_STD_BYTE=0" in args
    assert "-DWIN32_LEAN_AND_MEAN" in args


def test_linux_clang_args_do_not_include_windows_macros():
    args = get_platform_clang_args(platform="linux", os_name="posix", pointer_size=8)
    assert "-D__linux__" in args
    assert "-D__unix__" in args
    assert "-DLINUX" in args
    assert "-D_WIN32" not in args
    assert "-DWIN32_LEAN_AND_MEAN" not in args


def test_linux_platform_undefines_windows_compiler_macros():
    undefines = get_platform_undefines(platform="linux")
    assert "_WIN32" in undefines
    assert "_WIN64" in undefines
    assert "_MSC_VER" in undefines
    assert "__MINGW64__" in undefines


def test_normalize_platform_name_supports_cli_aliases():
    assert normalize_platform_name("windows") == "win32"
    assert normalize_platform_name("linux") == "linux"
    assert normalize_platform_name("macos") == "darwin"
    assert normalize_platform_name("current") is None
