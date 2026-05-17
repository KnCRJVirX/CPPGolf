from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from cppgolf.golf_rename import golf_rename_symbols


GPP = shutil.which("g++")


def _compile_cpp(code: str, *, extra_args: list[str] | None = None) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "sample.cpp"
        output = Path(directory) / "sample.exe"
        source.write_text(code, encoding="utf-8")
        cmd = ["g++", str(source), "-std=c++17", "-o", str(output)]
        if extra_args:
            cmd.extend(extra_args)
        return subprocess.run(cmd, capture_output=True, text=True, check=False)


@pytest.mark.skipif(not shutil.which("g++"), reason="g++ not available")
def test_rename_symbols_renames_locals_params_and_fields():
    code = (
        "struct Box{int value;int get(int add){int local=value+add;return local;}};"
        "int main(){Box box;box.value=1;return box.get(2)-3;}"
    )
    result = golf_rename_symbols(code)
    compiled = _compile_cpp(result)

    assert compiled.returncode == 0, compiled.stderr
    assert " value" not in result
    assert " add" not in result
    assert " local" not in result


@pytest.mark.skipif(not shutil.which("g++"), reason="g++ not available")
def test_rename_symbols_handles_constructor_init_field_references():
    code = (
        "struct Box{int value;Box(int seed):value(seed){}int get()const{return value;}};"
        "int main(){Box b(3);return b.get()-3;}"
    )
    result = golf_rename_symbols(code)
    compiled = _compile_cpp(result)

    assert compiled.returncode == 0, compiled.stderr
    assert " value" not in result
    assert " seed" not in result


@pytest.mark.skipif(not shutil.which("g++"), reason="g++ not available")
def test_rename_symbols_can_rename_simple_overload_group():
    code = (
        "int combine(int a){return a+1;}"
        "int combine(int a,int b){return a+b;}"
        "int main(){return combine(1)+combine(2,3)-7;}"
    )
    result = golf_rename_symbols(code, rename_functions=True)
    compiled = _compile_cpp(result)

    assert compiled.returncode == 0, compiled.stderr
    assert "combine(" not in result


@pytest.mark.skipif(not shutil.which("g++"), reason="g++ not available")
def test_rename_symbols_skips_ambiguous_macro_dependent_names():
    code = (
        "#define USE_VALUE(x) x\n"
        "struct Box{int value;int get(){int value=1;return USE_VALUE(value)+this->value;}};"
        "int main(){Box b;b.value=2;return b.get()-3;}"
    )
    result = golf_rename_symbols(code)
    compiled = _compile_cpp(result)

    assert compiled.returncode == 0, compiled.stderr
    assert "value" in result


@pytest.mark.skipif(not shutil.which("g++"), reason="g++ not available")
def test_rename_symbols_skips_virtual_methods_conservatively():
    code = (
        "struct Base{virtual int score(int v){return v;}};"
        "struct Child:Base{int score(int v) override{return v+1;}};"
        "int call(Base& b){return b.score(1);}int main(){Child c;return call(c)-2;}"
    )
    result = golf_rename_symbols(code, rename_functions=True)
    compiled = _compile_cpp(result)

    assert compiled.returncode == 0, compiled.stderr
    assert "score" in result


@pytest.mark.skipif(
    not os.environ.get("CPPGOLF_REAL_PROJECTS"),
    reason="real project compile checks disabled",
)
def test_rename_symbols_kapifish_compile_check():
    pytest.importorskip("clang.cindex", reason="libclang not installed")
    if GPP is None:
        pytest.skip("g++ not available")

    root = Path(r"C:\Users\KnCRJVirX\Desktop\Study\Works\ChineseChess\KapiFish")
    if not root.exists():
        pytest.skip("KapiFish project not available")

    engine = root / "kapifish"
    inputs = [
        root / "main.cpp",
        root / "adapter" / "botzone_engine.cpp",
        root / "adapter" / "botzone_json.cpp",
        engine / "benchmark.cpp",
        engine / "bitboard.cpp",
        engine / "engine.cpp",
        engine / "evaluate.cpp",
        engine / "memory.cpp",
        engine / "misc.cpp",
        engine / "movegen.cpp",
        engine / "movepick.cpp",
        engine / "position.cpp",
        engine / "score.cpp",
        engine / "search.cpp",
        engine / "thread.cpp",
        engine / "timeman.cpp",
        engine / "tt.cpp",
        engine / "tune.cpp",
        engine / "uci.cpp",
        engine / "ucioption.cpp",
        engine / "nnue" / "features" / "full_threats.cpp",
        engine / "nnue" / "features" / "half_ka_v2_hm.cpp",
        engine / "nnue" / "network.cpp",
        engine / "nnue" / "nnue_accumulator.cpp",
        engine / "nnue" / "nnue_misc.cpp",
        engine / "external" / "common" / "debug.cpp",
        engine / "external" / "common" / "entropy_common.cpp",
        engine / "external" / "common" / "error_private.cpp",
        engine / "external" / "common" / "fse_decompress.cpp",
        engine / "external" / "common" / "pool.cpp",
        engine / "external" / "common" / "threading.cpp",
        engine / "external" / "common" / "xxhash.cpp",
        engine / "external" / "common" / "zstd_common.cpp",
        engine / "external" / "decompress" / "huf_decompress.cpp",
        engine / "external" / "decompress" / "zstd_ddict.cpp",
        engine / "external" / "decompress" / "zstd_decompress.cpp",
        engine / "external" / "decompress" / "zstd_decompress_block.cpp",
    ]
    include_dirs = [
        root,
        root / "adapter",
        engine,
        engine / "nnue",
        engine / "nnue" / "features",
        engine / "nnue" / "layers",
        engine / "external" / "common",
        engine / "external" / "decompress",
        Path(r"C:\vcpkg\packages\jsoncpp_x64-mingw-static\include"),
    ]

    from cppgolf import process  # imported lazily to keep test startup light

    result, _ = process(
        inputs,
        include_dirs,
        defines=["KAPIFISH_BOT_STANDALONE"],
        inject_defines=True,
        no_compress_ws=True,
        rename_symbols=True,
        rename_functions=True,
    )
    compiled = _compile_cpp(
        result,
        extra_args=[
            "-O2",
            "-DBOTZONE_ONLINE",
            "-I",
            r"C:\vcpkg\packages\jsoncpp_x64-mingw-static\include",
            "-L",
            r"C:\vcpkg\installed\x64-mingw-static\lib",
            "-ljsoncpp",
            "-mavx512f",
            "-mavx512bw",
            "-mavx512dq",
            "-mavx512vl",
            "-mavx512vnni",
            "-mbmi2",
            "-mpopcnt",
            "-pthread",
        ],
    )

    assert compiled.returncode == 0, compiled.stderr


@pytest.mark.skipif(
    not os.environ.get("CPPGOLF_REAL_PROJECTS"),
    reason="real project compile checks disabled",
)
def test_rename_symbols_botzone_compile_check():
    pytest.importorskip("clang.cindex", reason="libclang not installed")
    if GPP is None:
        pytest.skip("g++ not available")

    root = Path(r"C:\Users\KnCRJVirX\Desktop\Study\Works\ChineseChess\BotZone")
    if not root.exists():
        pytest.skip("BotZone project not available")

    inputs = [
        root / "main.cpp",
        root / "adapter" / "botzone_engine.cpp",
        root / "adapter" / "botzone_json.cpp",
        root / "pikafish" / "benchmark.cpp",
        root / "pikafish" / "bitboard.cpp",
        root / "pikafish" / "engine.cpp",
        root / "pikafish" / "evaluate.cpp",
        root / "pikafish" / "memory.cpp",
        root / "pikafish" / "misc.cpp",
        root / "pikafish" / "movegen.cpp",
        root / "pikafish" / "movepick.cpp",
        root / "pikafish" / "position.cpp",
        root / "pikafish" / "score.cpp",
        root / "pikafish" / "search.cpp",
        root / "pikafish" / "thread.cpp",
        root / "pikafish" / "timeman.cpp",
        root / "pikafish" / "tt.cpp",
        root / "pikafish" / "tune.cpp",
        root / "pikafish" / "uci.cpp",
        root / "pikafish" / "ucioption.cpp",
        root / "pikafish" / "nnue" / "features" / "full_threats.cpp",
        root / "pikafish" / "nnue" / "features" / "half_ka_v2_hm.cpp",
        root / "pikafish" / "nnue" / "network.cpp",
        root / "pikafish" / "nnue" / "nnue_accumulator.cpp",
        root / "pikafish" / "nnue" / "nnue_misc.cpp",
        root / "pikafish" / "external" / "common" / "debug.cpp",
        root / "pikafish" / "external" / "common" / "entropy_common.cpp",
        root / "pikafish" / "external" / "common" / "error_private.cpp",
        root / "pikafish" / "external" / "common" / "fse_decompress.cpp",
        root / "pikafish" / "external" / "common" / "pool.cpp",
        root / "pikafish" / "external" / "common" / "threading.cpp",
        root / "pikafish" / "external" / "common" / "xxhash.cpp",
        root / "pikafish" / "external" / "common" / "zstd_common.cpp",
        root / "pikafish" / "external" / "decompress" / "huf_decompress.cpp",
        root / "pikafish" / "external" / "decompress" / "zstd_ddict.cpp",
        root / "pikafish" / "external" / "decompress" / "zstd_decompress.cpp",
        root / "pikafish" / "external" / "decompress" / "zstd_decompress_block.cpp",
    ]
    include_dirs = [
        root,
        root / "adapter",
        root / "pikafish",
        root / "pikafish" / "nnue",
        root / "pikafish" / "nnue" / "features",
        root / "pikafish" / "nnue" / "layers",
        root / "pikafish" / "external" / "common",
        root / "pikafish" / "external" / "decompress",
    ]

    from cppgolf import process

    result, _ = process(
        inputs,
        include_dirs,
        defines=["BOTZONE_PIKAFISH_STANDALONE=1"],
        inject_defines=True,
        no_compress_ws=True,
        rename_symbols=True,
        rename_functions=True,
    )
    compiled = _compile_cpp(
        result,
        extra_args=[
            "-O2",
            "-DBOTZONE_ONLINE",
            "-DZSTD_TRACE=0",
            "-mavx512f",
            "-mavx512bw",
            "-mavx512dq",
            "-mavx512vl",
            "-mavx512vnni",
            "-mbmi2",
            "-mpopcnt",
            "-pthread",
        ],
    )

    assert compiled.returncode == 0, compiled.stderr
