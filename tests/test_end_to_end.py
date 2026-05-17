from __future__ import annotations

from pathlib import Path

import pytest

from cppgolf import process
from cppgolf.__main__ import _format_defines, _render_define_lines


def test_process_merges_and_transforms(tmp_path: Path):
    header = tmp_path / "sum.h"
    header.write_text(
        "#pragma once\ninline int add(int a,int b){return a+b;}\n",
        encoding="utf-8",
    )
    source = tmp_path / "main.cpp"
    source.write_text(
        '#include <iostream>\n#include "sum.h"\nint main(){std::cout<<add(1,2)<<std::endl;return 0;}\n',
        encoding="utf-8",
    )

    result, merged_size = process(source, [], rename_symbols=False)

    assert merged_size > 0
    assert result == '#include <iostream>\nusing namespace std;int add(int a,int b){return a+b;}int main(){cout<<add(1,2)<<"\\n";}\n'


def test_process_optional_symbol_rename(tmp_path: Path):
    pytest.importorskip("clang.cindex", reason="libclang not installed")

    source = tmp_path / "main.cpp"
    source.write_text(
        "int combinevalue(int firstvalue,int secondvalue){int totalvalue=firstvalue+secondvalue;return totalvalue;}\n"
        "int main(){int finalvalue=combinevalue(1,2);return finalvalue==3?0:1;}\n",
        encoding="utf-8",
    )

    result, _ = process(
        source,
        [],
        no_std_ns=True,
        no_typedefs=True,
        no_compress_ws=True,
        rename_symbols=True,
        rename_functions=True,
    )

    assert "combinevalue" not in result
    assert "firstvalue" not in result
    assert "secondvalue" not in result
    assert "int main" in result


def test_process_defines_do_not_inject_into_output(tmp_path: Path):
    source = tmp_path / "main.cpp"
    source.write_text("int main(){return 0;}\n", encoding="utf-8")

    result, _ = process(
        source,
        [],
        defines=["BOTZONE_PIKAFISH_STANDALONE=1", "ZSTD_DISABLE_ASM"],
        rename_symbols=False,
    )

    assert "#define BOTZONE_PIKAFISH_STANDALONE=1" not in result
    assert "#define ZSTD_DISABLE_ASM" not in result
    assert "int main()" in result


def test_format_defines_prefixes_dash_d():
    assert _format_defines(["FOO=1", "-DBAR=2"]) == ["-DFOO=1", "-DBAR=2"]


def test_process_inject_defines_writes_defines_to_output(tmp_path: Path):
    source = tmp_path / "main.cpp"
    source.write_text("int main(){return 0;}\n", encoding="utf-8")

    result, _ = process(
        source,
        [],
        defines=["FOO=1", "BAR", "-DBAZ=2"],
        inject_defines=True,
        merge_only=True,
        rename_symbols=False,
    )

    assert result.startswith("#define FOO 1\n#define BAR\n#define BAZ 2\n")
    assert "int main(){return 0;}" in result


def test_process_suppress_warnings_injects_pragmas_before_compression(tmp_path: Path):
    source = tmp_path / "main.cpp"
    source.write_text("static int helper(){return 1;}int main(){return helper();}\n", encoding="utf-8")

    result, _ = process(
        source,
        [],
        suppress_warnings=True,
        rename_symbols=False,
    )

    assert result.startswith("#if defined(__GNUC__)\n")
    assert '#pragma GCC diagnostic ignored "-Wmisleading-indentation"' in result
    assert '#pragma GCC diagnostic ignored "-Wunused-function"' in result
    assert '#pragma GCC diagnostic ignored "-Wignored-attributes"' in result
    assert '#pragma GCC diagnostic ignored "-Wreturn-type"' in result
    assert '#pragma GCC diagnostic ignored "-Wmaybe-uninitialized"' in result
    assert '#pragma GCC diagnostic ignored "-Wshift-count-overflow"' in result
    assert "static int helper()" in result


def test_render_define_lines_formats_macro_definitions():
    assert _render_define_lines(["FOO=1", "BAR", "-DBAZ=2"]) == [
        "#define FOO 1",
        "#define BAR",
        "#define BAZ 2",
    ]


def test_process_merge_only_skips_all_transforms(tmp_path: Path):
    header = tmp_path / "sum.h"
    source = tmp_path / "main.cpp"
    header.write_text("#pragma once\ninline int add(int a,int b){return a+b;}\n", encoding="utf-8")
    source.write_text(
        '#include <iostream>\n#include "sum.h"\n// keep comment\nint main(){std::cout<<add(1,2)<<std::endl;return 0;}\n',
        encoding="utf-8",
    )

    result, merged_size = process(source, [tmp_path], merge_only=True, rename_symbols=False)

    assert merged_size > 0
    assert "using namespace std;" not in result
    assert "// keep comment" in result
    assert "inline int add" in result
    assert "std::cout<<add(1,2)<<std::endl;return 0;" in result


def test_process_keep_conditionals_preserves_pruned_branches(tmp_path: Path):
    source = tmp_path / "main.cpp"
    source.write_text(
        "#if defined(ENABLE_FEATURE)\n"
        "int selected_branch();\n"
        "#else\n"
        "int pruned_branch();\n"
        "#endif\n",
        encoding="utf-8",
    )

    result, _ = process(
        source,
        [],
        defines=["ENABLE_FEATURE=1"],
        preserve_conditionals=True,
        merge_only=True,
        rename_symbols=False,
    )

    assert "#if defined(ENABLE_FEATURE)" in result
    assert "#else" in result
    assert "#endif" in result
    assert "int selected_branch();" in result
    assert "int pruned_branch();" in result


def test_process_platform_affects_conditional_resolution(tmp_path: Path):
    source = tmp_path / "main.cpp"
    source.write_text(
        "#ifdef _WIN32\n"
        "int windows_branch();\n"
        "#else\n"
        "int other_branch();\n"
        "#endif\n",
        encoding="utf-8",
    )

    result, _ = process(
        source,
        [],
        platform="windows",
        merge_only=True,
        rename_symbols=False,
    )

    assert "int windows_branch();" in result
    assert "int other_branch();" not in result
    assert "#ifdef _WIN32" not in result
