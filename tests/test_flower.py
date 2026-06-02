from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from cppgolf import process
from cppgolf.flower import (
    _class_declaration_template,
    _dead_code_edits,
    _declaration_edits,
    _namespace_declaration_template,
    _normalize_declaration_boundary,
    insert_flowers,
)


ROOT = Path(__file__).resolve().parents[1]
GPP = shutil.which("g++")
HELPER = ROOT / "build" / "cfg-helper" / ("cppgolf-cfg-helper.exe" if sys.platform == "win32" else "cppgolf-cfg-helper")


def _require_helper() -> Path:
    if not HELPER.exists():
        pytest.skip("cppgolf-cfg-helper is not built")
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "sample.cpp"
        source.write_text("int target(){return 1;}\n", encoding="utf-8")
        proc = subprocess.run(
            [str(HELPER), "-flower-plan", "-function=target", str(source), "--", "-std=c++20"],
            capture_output=True,
            text=True,
            check=False,
        )
    if proc.returncode != 0:
        pytest.skip(f"cppgolf-cfg-helper is not runnable: {proc.stderr.strip() or proc.stdout.strip()}")
    return HELPER


def _compile_and_run_cpp(code: str) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "sample.cpp"
        output = Path(directory) / "sample.exe"
        source.write_text(code, encoding="utf-8")
        compile_proc = subprocess.run(
            ["g++", str(source), "-std=c++17", "-O2", "-o", str(output)],
            capture_output=True,
            text=True,
            check=False,
        )
        if compile_proc.returncode != 0:
            return compile_proc
        return subprocess.run([str(output)], capture_output=True, text=True, check=False)


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_insert_flowers_adds_complex_dead_code_without_changing_behavior():
    helper = _require_helper()
    code = "int target(int x){x+=1;x*=2;return x;}int main(){return target(2)==6?0:1;}\n"

    result = insert_flowers(
        code,
        dead_code=True,
        declarations=False,
        functions=["target"],
        seed=3,
        dead_blocks_per_function=2,
        helper_path=helper,
    )
    run = _compile_and_run_cpp(result)

    assert run.returncode == 0, run.stderr
    assert "cppgolf_flower_" not in result
    assert "[[maybe_unused]]" not in result
    assert "if(false)" not in result
    assert result.count("unsigned ") >= 4
    assert result.count("==0u") >= 1


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_insert_flowers_uses_multiple_dead_code_templates():
    helper = _require_helper()
    code = "int target(int x){x+=1;x*=2;x-=3;x^=4;x+=5;return x;}int main(){return target(2)==12?0:1;}\n"

    result = insert_flowers(
        code,
        dead_code=True,
        declarations=False,
        functions=["target"],
        seed=17,
        dead_blocks_per_function=6,
        helper_path=helper,
    )
    run = _compile_and_run_cpp(result)

    assert run.returncode == 0, run.stderr
    markers = sum(marker in result for marker in ("switch(", "while(", "auto ", "enum{", "if((("))
    assert markers >= 3


def test_dead_code_edits_deduplicate_duplicate_helper_function_records():
    code = "int target(){int x=1;x+=2;return x;}int main(){return target()==3?0:1;}\n"
    start = code.index("{")
    end = code.index("}", start) + 1
    offset = code.index("x+=2;") + len("x+=2;")
    plan = {
        "functions": [
            {
                "qualified_name": "target",
                "simple_name": "target",
                "body": {"valid": True, "start": start, "end": end},
                "insert_offsets": [offset, offset],
                "diagnostics": [],
            },
            {
                "qualified_name": "target",
                "simple_name": "target",
                "body": {"valid": True, "start": start, "end": end},
                "insert_offsets": [offset],
                "diagnostics": [],
            },
        ]
    }

    edits = _dead_code_edits(
        code,
        plan,
        functions=["target"],
        exclude=[],
        seed=21,
        blocks_per_function=4,
        verbose=False,
    )

    assert len(edits) == 1


def test_declaration_edits_use_multiple_scope_offsets():
    code = "namespace N{int a;int b;}\n"
    first = code.index(";") + 1
    second = code.rindex(";") + 1
    end = code.index("}")
    plan = {
        "functions": [],
        "scopes": [
            {
                "kind": "namespace",
                "name": "N",
                "insert_offsets": [first, second, end],
                "insert_offset": end,
            }
        ],
    }

    edits = _declaration_edits(code, plan, seed=31, count=3, verbose=False)

    assert {edit.offset for edit in edits} == {first, second, end}


def test_normalize_declaration_boundary_skips_using_pack_expansion():
    code = "template <typename... Ts> struct overload : Ts... {\n  using Ts::\n  operator()...;\n};\n"
    offset = code.index("operator()")

    normalized = _normalize_declaration_boundary(code, offset)

    assert normalized == code.index(";") + 1


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_declaration_templates_are_diverse_and_compile_in_namespace_and_class():
    namespace_decls = "".join(
        _namespace_declaration_template(f"ns_decl_{index}", 1000 + index, 2000 + index, 3000 + index, index)
        for index in range(8)
    )
    class_decls = "".join(
        _class_declaration_template(f"cls_decl_{index}", 4000 + index, 5000 + index, 6000 + index, index)
        for index in range(8)
    )
    code = (
        f"namespace N{{{namespace_decls}int use(){{return 1;}}}}\n"
        f"struct S{{{class_decls}int x;}};\n"
        "int main(){return N::use()==1&&sizeof(S)==sizeof(int)?0:1;}\n"
    )
    run = _compile_and_run_cpp(code)

    assert run.returncode == 0, run.stderr
    assert "using ns_decl_2=unsigned;" in code
    assert "typedef unsigned ns_decl_3;" in code
    assert "enum{ns_decl_4=" in code
    assert "template<unsigned N> static constexpr unsigned ns_decl_6" in code
    assert "#define NS_DECL_7" in code
    assert "using cls_decl_2=unsigned;" in code
    assert "template<unsigned N> static constexpr unsigned cls_decl_6" in code
    assert "#define CLS_DECL_7" in code


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flower_helper_reports_multiple_scope_insert_offsets():
    helper = _require_helper()
    code = (
        "int g0;\n"
        "namespace N{int a;int b;void f(){}}\n"
        "struct S{int x;int y;void f(){}};\n"
        "int main(){return g0;}\n"
    )
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "sample.cpp"
        source.write_text(code, encoding="utf-8")
        proc = subprocess.run(
            [str(helper), "-flower-plan", str(source), "--", "-std=c++20"],
            capture_output=True,
            text=True,
            check=False,
        )

    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    scopes = data["flower_plan"]["scopes"]
    namespace = next(scope for scope in scopes if scope.get("kind") == "namespace" and scope.get("name") == "N")
    struct = next(scope for scope in scopes if scope.get("kind") == "struct" and scope.get("name") == "S")
    global_scope = next(scope for scope in scopes if scope.get("kind") == "global")

    assert len(set(namespace["insert_offsets"])) >= 3
    assert len(set(struct["insert_offsets"])) >= 3
    assert len(set(global_scope["insert_offsets"])) >= 3
    assert namespace["insert_offsets"] != [namespace["insert_offset"]]


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_insert_flowers_declarations_do_not_change_class_layout():
    helper = _require_helper()
    code = (
        "namespace N{struct Box{int x;int y;};int use(){return sizeof(Box);}}"
        "int main(){return N::use()==sizeof(int)*2?0:1;}\n"
    )

    result = insert_flowers(
        code,
        dead_code=False,
        declarations=True,
        seed=5,
        declaration_count=8,
        helper_path=helper,
    )
    run = _compile_and_run_cpp(result)

    assert run.returncode == 0, run.stderr
    assert "cppgolf_flower_" not in result
    assert "[[maybe_unused]]" not in result
    assert re.search(r"struct Box\{[^}]*\n(static |using |typedef |enum\{|#if )", result)
    assert any(marker in result for marker in ("static unsigned ", "using ", "typedef unsigned ", "enum{", "#define "))


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_insert_flowers_declarations_skip_class_template_specializations():
    helper = _require_helper()
    code = (
        "template<class T>struct Box{T x;};\n"
        "template struct Box<int>;\n"
        "int main(){return sizeof(Box<int>)==sizeof(int)?0:1;}\n"
    )

    result = insert_flowers(
        code,
        dead_code=False,
        declarations=True,
        seed=23,
        declaration_count=12,
        helper_path=helper,
    )
    run = _compile_and_run_cpp(result)

    assert run.returncode == 0, run.stderr
    assert "Box<int> static" not in result


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_insert_flowers_declarations_do_not_split_using_pack_expansion():
    helper = _require_helper()
    code = (
        "#include <string>\n"
        "template <typename... Ts> struct overload : Ts... {\n"
        "  using Ts::\n"
        "  operator()...;\n"
        "};\n"
        "template <typename... Ts> overload(Ts...) -> overload<Ts...>;\n"
        "int main(){return 0;}\n"
    )

    result = insert_flowers(
        code,
        dead_code=False,
        declarations=True,
        seed=41,
        declaration_count=8,
        helper_path=helper,
    )
    run = _compile_and_run_cpp(result)

    assert run.returncode == 0, run.stderr
    assert "using Ts::\n  operator() static" not in result


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_insert_flowers_respects_function_filter_and_exclude():
    helper = _require_helper()
    code = "int a(){return 1;}int b(){return 2;}int main(){return a()+b()-3;}\n"

    result = insert_flowers(
        code,
        dead_code=True,
        declarations=False,
        functions=["a", "b"],
        exclude=["b"],
        seed=9,
        helper_path=helper,
    )
    run = _compile_and_run_cpp(result)

    assert run.returncode == 0, run.stderr
    assert "int a(){return 1;{" in result
    assert "int b(){return 2;}" in result
    assert "cppgolf_flower_" not in result


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_insert_flowers_skips_constexpr_functions_and_preprocessor_bodies():
    helper = _require_helper()
    code = (
        "constexpr int c(int x){return x+1;}\n"
        "int target(int x){\n"
        "#if 1\n"
        "x+=1;\n"
        "#endif\n"
        "return x;\n"
        "}\n"
        "int main(){return c(1)+target(1)==4?0:1;}\n"
    )

    result = insert_flowers(
        code,
        dead_code=True,
        declarations=False,
        functions=["c", "target"],
        seed=11,
        helper_path=helper,
    )
    run = _compile_and_run_cpp(result)

    assert run.returncode == 0, run.stderr
    assert result == code


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_process_enables_flower_from_config_and_cli(tmp_path: Path):
    helper = _require_helper()
    source = tmp_path / "sample.cpp"
    config = tmp_path / "cppgolf.toml"
    source.write_text("int target(int x){x+=2;return x;}int main(){return target(3)==5?0:1;}\n", encoding="utf-8")
    config.write_text(
        "[flower]\n"
        "enabled = true\n"
        "dead_code = true\n"
        "declarations = false\n"
        "functions = [\"target\"]\n"
        "seed = 13\n",
        encoding="utf-8",
    )

    result, _ = process(
        source,
        [],
        config_path=config,
        cfg_helper_path=helper,
        no_compress_ws=True,
        rename_symbols=False,
    )
    run = _compile_and_run_cpp(result)

    assert run.returncode == 0, run.stderr
    assert "cppgolf_flower_" not in result
    assert "unsigned " in result
