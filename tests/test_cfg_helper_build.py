from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_cfg_helper_builds_with_standard_llvm_discovery(tmp_path: Path):
    cmake = shutil.which("cmake")
    ninja = shutil.which("ninja")
    if cmake is None or ninja is None:
        pytest.skip("cmake or ninja not available")

    cmake_args = _llvm_cmake_args()
    if cmake_args is None:
        pytest.skip("LLVM/Clang CMake package not discoverable")

    build_dir = tmp_path / "cfg-helper-build"
    configure = subprocess.run(
        [
            cmake,
            "-S",
            str(ROOT / "tools" / "cfg-helper"),
            "-B",
            str(build_dir),
            "-G",
            "Ninja",
            "-DCMAKE_BUILD_TYPE=Release",
            *cmake_args,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if configure.returncode != 0:
        pytest.skip(f"LLVM package is not configurable in this environment: {configure.stderr or configure.stdout}")

    build = subprocess.run(
        [cmake, "--build", str(build_dir), "--config", "Release"],
        capture_output=True,
        text=True,
        check=False,
    )
    if build.returncode != 0:
        pytest.skip(f"LLVM package is not buildable in this environment: {build.stderr or build.stdout}")

    helper = build_dir / ("cppgolf-cfg-helper.exe" if os.name == "nt" else "cppgolf-cfg-helper")
    assert helper.exists()


def _llvm_cmake_args() -> list[str] | None:
    args: list[str] = []
    llvm_dir = os.environ.get("LLVM_DIR")
    clang_dir = os.environ.get("Clang_DIR")
    prefix = os.environ.get("CMAKE_PREFIX_PATH")
    if llvm_dir:
        args.append(f"-DLLVM_DIR={llvm_dir}")
        args.extend(_compiler_args_for_llvm_cmake_dir(Path(llvm_dir)))
    if clang_dir:
        args.append(f"-DClang_DIR={clang_dir}")
    if prefix:
        args.append(f"-DCMAKE_PREFIX_PATH={prefix}")
    if args:
        return args

    llvm_config = shutil.which("llvm-config")
    if llvm_config:
        proc = subprocess.run(
            [llvm_config, "--cmakedir"],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode == 0:
            llvm_cmake = Path(proc.stdout.strip())
            clang_cmake = llvm_cmake.parent / "clang"
            result = [f"-DLLVM_DIR={llvm_cmake}", *_compiler_args_for_llvm_cmake_dir(llvm_cmake)]
            if clang_cmake.exists():
                result.append(f"-DClang_DIR={clang_cmake}")
            return result

    for root in (Path("C:/LLVM"), Path("/usr/lib/llvm"), Path("/usr/local/opt/llvm")):
        llvm_cmake = root / "lib" / "cmake" / "llvm"
        clang_cmake = root / "lib" / "cmake" / "clang"
        if llvm_cmake.exists() and clang_cmake.exists():
            return [
                f"-DLLVM_DIR={llvm_cmake}",
                f"-DClang_DIR={clang_cmake}",
                *_compiler_args_for_llvm_cmake_dir(llvm_cmake),
            ]
    return None


def _compiler_args_for_llvm_cmake_dir(llvm_cmake: Path) -> list[str]:
    llvm_root = llvm_cmake.parent.parent.parent
    if os.name == "nt":
        clangxx = llvm_root / "bin" / "clang++.exe"
    else:
        clangxx = llvm_root / "bin" / "clang++"
    if clangxx.exists():
        return [f"-DCMAKE_CXX_COMPILER={clangxx}"]
    return []
