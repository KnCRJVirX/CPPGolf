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
from cppgolf.control_flow_flatten import CfgHelperError, _helper_compile_args, _helper_parse_code, flatten_control_flow


ROOT = Path(__file__).resolve().parents[1]
GPP = shutil.which("g++")
HELPER = ROOT / "build" / "cfg-helper" / ("cppgolf-cfg-helper.exe" if sys.platform == "win32" else "cppgolf-cfg-helper")


def _compile_cpp(code: str, *, std: str = "-std=c++17") -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "sample.cpp"
        output = Path(directory) / "sample.exe"
        source.write_text(code, encoding="utf-8")
        return subprocess.run(
            ["g++", str(source), std, "-o", str(output)],
            capture_output=True,
            text=True,
            check=False,
        )


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


def _write_json_helper(tmp_path: Path, payload: dict) -> Path:
    script = tmp_path / "json_helper_payload.py"
    script.write_text(
        "import json\n"
        f"print(json.dumps({payload!r}))\n",
        encoding="utf-8",
    )
    if sys.platform == "win32":
        helper = tmp_path / "json-helper.bat"
        helper.write_text(f'@echo off\n"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    else:
        helper = tmp_path / "json-helper"
        helper.write_text(f'#!/bin/sh\n"{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
        helper.chmod(0o755)
    return helper


def _make_plan(code: str, *, qualified_name: str = "target", simple_name: str = "target") -> dict:
    body_start = code.index("{")
    body_end = code.rindex("}") + 1
    signature_start = code.rfind("\n", 0, body_start) + 1
    statements = []
    cursor = body_start + 1
    while cursor < body_end - 1:
        next_semicolon = code.find(";", cursor)
        if next_semicolon < 0 or next_semicolon >= body_end:
            break
        statements.append((cursor, next_semicolon + 1))
        cursor = next_semicolon + 1
    statement_items = []
    for statement_id, (start, end) in enumerate(statements, start=1):
        text = code[start:end].strip()
        kind = "DeclStmt" if text.startswith(("int ", "auto ", "long ", "const ")) else "BinaryOperator"
        category = "decl" if kind == "DeclStmt" else "return" if text.startswith("return") else "linear"
        statement_items.append(
            {
                "id": statement_id,
                "kind": kind,
                "category": category,
                "range": {"valid": True, "start": start, "end": end},
                "macro": "none",
                "control": {},
                "contains": {
                    "lambda": False,
                    "switch": False,
                    "goto": False,
                    "label": False,
                    "try": False,
                    "break": False,
                    "continue": False,
                },
            }
        )
    return {
        "version": 4,
        "functions": [
            {
                "qualified_name": qualified_name,
                "simple_name": simple_name,
                "signature": {"valid": True, "start": signature_start, "end": body_start},
                "body": {"valid": True, "start": body_start, "end": body_end},
                "is_constructor_or_destructor": False,
                "statements": statement_items,
                "locals": [],
                "block_plan": {
                    "range": {"valid": True, "start": body_start, "end": body_end},
                    "statements": statement_items,
                    "locals": [],
                    "diagnostics": [],
                },
                "diagnostics": [],
            }
        ],
    }


def _if_plan(code: str) -> dict:
    body_start = code.index("{")
    body_end = code.rindex("}") + 1
    signature_start = code.rfind("\n", 0, body_start) + 1
    if_start = code.index("if")
    if_end = code.index("return", if_start)
    then_start = code.index("{", if_start)
    then_end = code.index("}", then_start) + 1
    else_start = code.index("{", then_end)
    else_end = code.index("}", else_start) + 1
    return_start = code.index("return", if_start)
    return_end = code.index(";", return_start) + 1
    return {
        "version": 4,
        "functions": [
            {
                "qualified_name": "target",
                "simple_name": "target",
                "signature": {"valid": True, "start": signature_start, "end": body_start},
                "body": {"valid": True, "start": body_start, "end": body_end},
                "is_constructor_or_destructor": False,
                "statements": [
                    {
                        "id": 1,
                        "kind": "IfStmt",
                        "category": "if",
                        "range": {"valid": True, "start": if_start, "end": if_end},
                        "macro": "none",
                        "control": {
                            "condition": {
                                "valid": True,
                                "start": code.index("(", if_start) + 1,
                                "end": code.index(")", if_start),
                            },
                            "then_body": {"valid": True, "start": then_start, "end": then_end},
                            "else_body": {"valid": True, "start": else_start, "end": else_end},
                        },
                        "contains": {
                            "lambda": False,
                            "switch": False,
                            "goto": False,
                            "label": False,
                            "try": False,
                            "break": False,
                            "continue": False,
                        },
                    },
                    {
                        "id": 2,
                        "kind": "ReturnStmt",
                        "category": "return",
                        "range": {"valid": True, "start": return_start, "end": return_end},
                        "macro": "none",
                        "control": {},
                        "contains": {
                            "lambda": False,
                            "switch": False,
                            "goto": False,
                            "label": False,
                            "try": False,
                            "break": False,
                            "continue": False,
                        },
                    },
                ],
                "locals": [],
                "block_plan": {
                    "range": {"valid": True, "start": body_start, "end": body_end},
                    "statements": [
                        {
                            "id": 1,
                            "kind": "IfStmt",
                            "category": "if",
                            "range": {"valid": True, "start": if_start, "end": if_end},
                            "macro": "none",
                            "control": {
                                "condition": {
                                    "valid": True,
                                    "start": code.index("(", if_start) + 1,
                                    "end": code.index(")", if_start),
                                },
                                "then_body": {"valid": True, "start": then_start, "end": then_end},
                                "else_body": {"valid": True, "start": else_start, "end": else_end},
                                "then_block": {
                                    "range": {"valid": True, "start": then_start, "end": then_end},
                                    "statements": [
                                        {
                                            "id": 1,
                                            "kind": "CompoundAssignOperator",
                                            "category": "linear",
                                            "range": {
                                                "valid": True,
                                                "start": code.index("x+=1", then_start),
                                                "end": code.index(";", then_start) + 1,
                                            },
                                            "macro": "none",
                                            "control": {},
                                            "contains": {
                                                "lambda": False,
                                                "switch": False,
                                                "goto": False,
                                                "label": False,
                                                "try": False,
                                                "break": False,
                                                "continue": False,
                                            },
                                        }
                                    ],
                                    "locals": [],
                                    "diagnostics": [],
                                },
                                "else_block": {
                                    "range": {"valid": True, "start": else_start, "end": else_end},
                                    "statements": [
                                        {
                                            "id": 1,
                                            "kind": "CompoundAssignOperator",
                                            "category": "linear",
                                            "range": {
                                                "valid": True,
                                                "start": code.index("x-=1", else_start),
                                                "end": code.index(";", else_start) + 1,
                                            },
                                            "macro": "none",
                                            "control": {},
                                            "contains": {
                                                "lambda": False,
                                                "switch": False,
                                                "goto": False,
                                                "label": False,
                                                "try": False,
                                                "break": False,
                                                "continue": False,
                                            },
                                        }
                                    ],
                                    "locals": [],
                                    "diagnostics": [],
                                },
                            },
                            "contains": {
                                "lambda": False,
                                "switch": False,
                                "goto": False,
                                "label": False,
                                "try": False,
                                "break": False,
                                "continue": False,
                            },
                        },
                        {
                            "id": 2,
                            "kind": "ReturnStmt",
                            "category": "return",
                            "range": {"valid": True, "start": return_start, "end": return_end},
                            "macro": "none",
                            "control": {},
                            "contains": {
                                "lambda": False,
                                "switch": False,
                                "goto": False,
                                "label": False,
                                "try": False,
                                "break": False,
                                "continue": False,
                            },
                        },
                    ],
                    "locals": [],
                    "diagnostics": [],
                },
                "diagnostics": [],
            }
        ],
    }


def _require_helper() -> Path:
    if not HELPER.exists():
        pytest.skip("cppgolf-cfg-helper is not built")
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "sample.cpp"
        source.write_text("int target(){return 1;}\n", encoding="utf-8")
        proc = subprocess.run(
            [str(HELPER), "-function=target", str(source), "--", "-std=c++17"],
            capture_output=True,
            text=True,
            check=False,
        )
    if proc.returncode != 0:
        pytest.skip(f"cppgolf-cfg-helper is not runnable: {proc.stderr.strip() or proc.stdout.strip()}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        pytest.skip("cppgolf-cfg-helper is not the JSON helper build")
    if not isinstance(data, dict) or data.get("version") != 4 or not isinstance(data.get("functions"), list):
        pytest.skip("cppgolf-cfg-helper has an unsupported JSON schema")
    return HELPER


def test_flatten_control_flow_requires_helper(tmp_path: Path):
    missing = tmp_path / "missing-helper.exe"

    with pytest.raises(CfgHelperError, match="requires cppgolf-cfg-helper"):
        flatten_control_flow("int target(){return 1;}\n", functions=["target"], helper_path=missing)


def test_flatten_control_flow_rejects_invalid_helper_json(tmp_path: Path):
    helper = tmp_path / ("bad-helper.bat" if sys.platform == "win32" else "bad-helper")
    if sys.platform == "win32":
        helper.write_text("@echo not-json\n", encoding="utf-8")
    else:
        helper.write_text("#!/bin/sh\necho not-json\n", encoding="utf-8")
        helper.chmod(0o755)

    with pytest.raises(CfgHelperError, match="invalid JSON"):
        flatten_control_flow("int target(){return 1;}\n", functions=["target"], helper_path=helper)


def test_flatten_control_flow_rejects_v3_helper_schema(tmp_path: Path):
    helper = _write_json_helper(tmp_path, {"version": 3, "functions": []})

    with pytest.raises(CfgHelperError, match="rebuild cppgolf-cfg-helper"):
        flatten_control_flow("int target(){return 1;}\n", functions=["target"], helper_path=helper)


def test_flatten_control_flow_uses_json_helper_plan(tmp_path: Path):
    code = "int target(){int x=1;x+=2;return x;}\n"
    helper = _write_json_helper(tmp_path, _make_plan(code))

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)

    assert "switch(qcf)" in result
    assert "case 0" in result
    assert "int x=1;" in result
    assert re.search(r"case 0:\{\s*x\+=2;", result)
    assert "return x" in result


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_splits_linear_statements_into_cases():
    helper = _require_helper()
    code = "int target(int x){x+=1;x*=2;x-=3;return x;}int main(){return target(5)-9;}\n"

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)
    compiled = _compile_cpp(result, std="-std=c++20")

    assert compiled.returncode == 0, compiled.stderr
    assert result.count("case ") >= 3
    assert "x+=1" in result
    assert "x*=2" in result
    assert "x-=3" in result


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_shuffles_case_source_order_but_preserves_execution():
    helper = _require_helper()
    code = "int target(int x){x+=1;x*=2;x-=3;x^=7;return x;}int main(){return target(5)-14;}\n"

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)
    compiled = _compile_cpp(result, std="-std=c++20")
    case_order = [int(value) for value in re.findall(r"case (\d+):", result)]

    assert compiled.returncode == 0, compiled.stderr
    assert len(case_order) >= 5
    assert case_order != sorted(case_order)


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_splits_position_set_style_linear_block():
    helper = _require_helper()
    code = (
        "#include <cstring>\n"
        "#include <optional>\n"
        "struct S{int a;int b[2];int* p;std::optional<int> set(const S& o,int* q){"
        "a=o.a;std::memcpy(b,o.b,sizeof(b));p=q;std::memcpy(p,o.p,sizeof(int));return std::nullopt;"
        "}};"
        "int main(){int x=3;S a{1,{2,3},&x};int y=0;S b{};auto r=b.set(a,&y);return r.has_value()||b.a!=1||y!=3;}\n"
    )

    result = flatten_control_flow(code, functions=["S::set"], helper_path=helper)
    compiled = _compile_cpp(result, std="-std=c++20")

    assert compiled.returncode == 0, compiled.stderr
    assert result.count("case ") >= 3
    assert re.search(r"case 0:\{\s*a=o\.a;", result)
    assert "std::memcpy(b,o.b,sizeof(b))" in result
    assert "return std::nullopt" in result


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_does_not_treat_qualified_calls_as_declarations():
    helper = _require_helper()
    code = (
        "#include <cstring>\n"
        "struct S{int a[2];int b[2];void copy(const S& o){"
        "std::memcpy(a,o.a,sizeof(a));std::memcpy(b,o.b,sizeof(b));a[0]+=b[1];"
        "}};"
        "int main(){S x{{1,2},{3,4}},y{};y.copy(x);return y.a[0]!=5;}\n"
    )

    result = flatten_control_flow(code, functions=["S::copy"], helper_path=helper)
    compiled = _compile_cpp(result, std="-std=c++20")
    copy_text = result[result.index("copy"):result.index("int main")]

    assert compiled.returncode == 0, compiled.stderr
    assert copy_text.count("case ") >= 3
    assert re.search(r"case \d+:\{\s*std::memcpy\(a,o\.a,sizeof\(a\)\);", copy_text)
    assert re.search(r"case \d+:\{\s*std::memcpy\(b,o\.b,sizeof\(b\)\);", copy_text)


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_hoists_multi_declarator_statement_once():
    helper = _require_helper()
    code = (
        "#include <iostream>\n"
        "struct Box{int v=0;};"
        "int target(){Box a,b;a.v=2;b.v=3;return a.v+b.v;}"
        "int main(){std::cout<<target();return 0;}\n"
    )

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)
    run = _compile_and_run_cpp(result)
    target_text = result[result.index("target"):result.index("int main")]

    assert run.returncode == 0, run.stderr
    assert run.stdout == "5"
    assert target_text.count("Box a,b;") == 1
    assert "Box a;Box a,b;" not in target_text
    assert "case" in target_text


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_preserves_structured_binding_scope():
    helper = _require_helper()
    code = (
        "#include <utility>\n"
        "int target(){auto [a,b]=std::pair<int,int>{2,3};int c=a*b;return c+a;}"
        "int main(){return target()-8;}\n"
    )

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)
    compiled = _compile_cpp(result, std="-std=c++20")
    target_text = result[result.index("target"):result.index("int main")]

    assert compiled.returncode == 0, compiled.stderr
    assert "auto [a,b]" in target_text
    assert "return c+a" in target_text


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_recurses_inside_merged_structured_binding_lifetime_block():
    helper = _require_helper()
    code = (
        "#include <utility>\n"
        "int target(bool flag){auto [a,b]=std::pair<int,int>{2,3};if(flag){a+=4;b+=5;}return a+b;}"
        "int main(){return target(true)-14;}\n"
    )

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)
    compiled = _compile_cpp(result, std="-std=c++20")
    target_text = result[result.index("target"):result.index("int main")]

    assert compiled.returncode == 0, compiled.stderr
    assert "auto [a,b]" in target_text
    assert len(re.findall(r"switch\(qcf\w*\)", target_text)) >= 2
    assert "if(flag){a+=4;b+=5;}" not in target_text


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_adds_semicolon_after_braced_return_initializer():
    helper = _require_helper()
    code = (
        "#include <string>\n"
        "std::string target(){return std::string{char('a'),char('b')};}"
        "int main(){return target()!=\"ab\";}\n"
    )

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)
    compiled = _compile_cpp(result, std="-std=c++20")

    assert compiled.returncode == 0, compiled.stderr
    assert "return std::string{char('a'),char('b')};" in result


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_preserves_cross_case_local_lifetime():
    helper = _require_helper()
    code = (
        "#include <iostream>\n"
        "struct Session{int v=3;~Session(){}};"
        "Session* g=nullptr;"
        "int target(){int x=1;Session s;g=&s;x+=g->v;return x;}"
        "int main(){std::cout<<target();return 0;}\n"
    )

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)
    run = _compile_and_run_cpp(result)
    target_text = result[result.index("target"):result.index("int main")]

    assert run.returncode == 0, run.stderr
    assert run.stdout == "4"
    assert "Session s;" in target_text
    assert not re.search(r"case \d+:\{\s*Session s;", target_text)


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_splits_safe_mid_block_scalar_declaration():
    helper = _require_helper()
    code = "int target(int x){x+=1;int y=x*2;y+=3;return y;}int main(){return target(4)-13;}\n"

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)
    compiled = _compile_cpp(result, std="-std=c++20")
    target_text = result[result.index("target"):result.index("int main")]

    assert compiled.returncode == 0, compiled.stderr
    assert "int y;" in target_text
    assert "y = x*2;" in target_text
    assert not re.search(r"case \d+:\{\s*int y=x\*2;", target_text)
    assert target_text.count("case ") >= 4


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_split_bool_decl_uses_cpp_bool_spelling():
    helper = _require_helper()
    code = "int target(int x){x+=1;bool flag=x>0;if(flag)x+=2;return x;}int main(){return target(1)-4;}\n"

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)
    compiled = _compile_cpp(result, std="-std=c++20")
    target_text = result[result.index("target"):result.index("int main")]

    assert compiled.returncode == 0, compiled.stderr
    assert "_Bool" not in target_text
    assert "bool flag;" in target_text
    assert "flag = x>0;" in target_text


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_split_const_pointer_decl_preserves_const():
    helper = _require_helper()
    code = (
        "#include <cstring>\n"
        "const char PieceToChar[]=\" abc\";"
        "int target(char token){int z=0;z+=1;const char* pieceChar=std::strchr(PieceToChar,token);"
        "if(pieceChar==nullptr)return -1;return int(pieceChar-PieceToChar);}"
        "int main(){return target('b')==2?0:1;}\n"
    )

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)
    run = _compile_and_run_cpp(result)
    target_text = result[result.index("target"):result.index("int main")]

    assert run.returncode == 0, run.stderr
    assert re.search(r"const char\s*\*\s*pieceChar;", target_text)
    assert "pieceChar = std::strchr(PieceToChar,token);" in target_text


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_uses_one_switch_for_linear_and_if_regions():
    helper = _require_helper()
    code = (
        "int target(int x){x+=1;if(x%2){x*=3;}else{x*=5;}x-=2;return x;}"
        "int main(){return target(1)-4;}\n"
    )

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)
    compiled = _compile_cpp(result, std="-std=c++20")

    assert compiled.returncode == 0, compiled.stderr
    target_start = result.index("target")
    target_end = result.index("int main")
    target_text = result[target_start:target_end]
    assert target_text.count("switch(qcf)") == 1
    assert "x+=1" in target_text
    assert "if(x%2)" in target_text
    assert "x-=2" in target_text


def test_flatten_control_flow_does_not_inject_anti_optimization_attribute(tmp_path: Path):
    code = "int target(){int x=1;x+=2;return x;}\n"
    helper = _write_json_helper(tmp_path, _make_plan(code))

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)

    assert "CPPGOLF_CFG_FLATTEN_ATTR" not in result
    assert "int target()" in result
    assert "switch(qcf)" in result


def test_flatten_control_flow_leaves_existing_attribute_macro_unused(tmp_path: Path):
    code = "#define CPPGOLF_CFG_FLATTEN_ATTR\nint target(){int x=1;x+=2;return x;}\n"
    helper = _write_json_helper(tmp_path, _make_plan(code))

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)

    assert result.count("CPPGOLF_CFG_FLATTEN_ATTR") == 1
    assert "CPPGOLF_CFG_FLATTEN_ATTR int target()" not in result
    assert "switch(qcf)" in result


def test_helper_parse_code_blanks_standard_attributes_without_shifting_offsets():
    code = 'MACRO [[maybe_unused]] static int target(){return 1;}\n'

    parsed = _helper_parse_code(code)

    assert len(parsed) == len(code)
    assert "[[maybe_unused]]" in code
    assert "[[maybe_unused]]" not in parsed
    assert parsed.index("static int target") == code.index("static int target")


def test_cfg_helper_compile_args_accept_helper_include_dirs(tmp_path: Path):
    include_dir = tmp_path / "sysroot" / "include"

    args = _helper_compile_args([], "linux", [include_dir])

    assert "-isystem" in args
    assert str(include_dir) in args
    assert args[args.index("-isystem") + 1] == str(include_dir)


def test_flatten_control_flow_does_not_inject_attribute_into_constructor_initializer(tmp_path: Path):
    code = (
        "struct Engine{int a;int b;"
        "Engine(int x):\n"
        "    a(x),\n"
        "    b(x+1)\n"
        "{a+=1;return;}};\n"
    )
    plan = _make_plan(code, qualified_name="Engine::Engine", simple_name="Engine")
    body_start = code.index("{", code.index("b(x+1)"))
    body_end = code.index("}", body_start) + 1
    plan["functions"][0]["body"] = {"valid": True, "start": body_start, "end": body_end}
    plan["functions"][0]["statements"][0]["range"] = {
        "valid": True,
        "start": body_start + 1,
        "end": code.index(";", body_start) + 1,
    }
    plan["functions"][0]["statements"][0]["category"] = "linear"
    helper = _write_json_helper(tmp_path, plan)

    result = flatten_control_flow(code, functions=["Engine::Engine"], helper_path=helper)

    assert ":CPPGOLF_CFG_FLATTEN_ATTR" not in result
    assert "CPPGOLF_CFG_FLATTEN_ATTR a(x)" not in result
    assert "CPPGOLF_CFG_FLATTEN_ATTR b(x+1)" not in result
    assert "Engine(int x):" in result
    assert "switch(qcf)" in result


def test_flatten_control_flow_uses_config_helper_path(tmp_path: Path):
    code = "int target(){return 1;}\n"
    helper = _write_json_helper(tmp_path, _make_plan(code))

    result = flatten_control_flow(code, functions=["target"], config_helper_path=helper)

    assert "switch(qcf)" in result


def test_flatten_control_flow_skips_helper_diagnostics(tmp_path: Path):
    code = "int target(){return 1;}\n"
    plan = _make_plan(code)
    plan["functions"][0]["diagnostics"] = ["unsafe"]
    helper = _write_json_helper(tmp_path, plan)

    assert flatten_control_flow(code, functions=["target"], helper_path=helper) == code


def test_flatten_control_flow_splits_simple_if_from_helper_plan(tmp_path: Path):
    code = "int target(int x){if(x>=0){x+=1;}else{x-=1;}return x;}\n"
    helper = _write_json_helper(tmp_path, _if_plan(code))

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)

    assert "if(x>=0)" in result
    assert "case 1" in result
    assert "case 2" in result


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_handles_sequence_and_return():
    helper = _require_helper()
    code = "int target(int x){x+=1;x*=2;return x;}int main(){return target(2)-6;}\n"

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)
    compiled = _compile_cpp(result, std="-std=c++20")

    assert compiled.returncode == 0, compiled.stderr
    assert "switch(qcf)" in result
    assert "case 0" in result


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_keeps_lambda_and_switch_semantics():
    helper = _require_helper()
    code = (
        "int target(int x){"
        "auto add=[&](int y){return x+y;};"
        "switch(x){case 1:x=add(2);break;case 2:x=add(3);break;default:x=0;break;}"
        "return x;"
        "}"
        "int main(){return target(2)-5;}\n"
    )

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)
    compiled = _compile_cpp(result, std="-std=c++20")

    assert compiled.returncode == 0, compiled.stderr
    assert "switch(x)" in result
    assert "auto add" in result
    assert "switch(qcf)" in result


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_skips_constexpr_function_even_when_configured():
    helper = _require_helper()
    code = (
        "constexpr int target(int x){int y=x;y+=1;return y;}"
        "constexpr int value=target(2);"
        "int main(){return value==3?0:1;}\n"
    )

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)
    compiled = _compile_cpp(result, std="-std=c++20")

    assert compiled.returncode == 0, compiled.stderr
    assert result == code
    assert "switch(qcf)" not in result


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_skips_functions_with_preprocessor_directives():
    helper = _require_helper()
    code = (
        "#include <string>\n"
        "std::string target(){\n"
        "#define LOCAL_TEXT \"ok\"\n"
        "std::string s=LOCAL_TEXT;\n"
        "#if defined(__GNUC__)\n"
        "s += \"g\";\n"
        "#endif\n"
        "return s;\n"
        "}\n"
        "int main(){return target().empty();}\n"
    )

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)
    compiled = _compile_cpp(result, std="-std=c++20")

    assert compiled.returncode == 0, compiled.stderr
    assert result == code
    assert "\n#define LOCAL_TEXT" in result
    assert "\n#if defined(__GNUC__)" in result
    assert "\n#endif" in result
    assert "switch(qcf)" not in result
    assert ";#if" not in result
    assert ";#endif" not in result


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_supports_exact_qualified_match():
    helper = _require_helper()
    code = "namespace N{struct Box{int target(int x){return x+3;}};}int main(){N::Box b;return b.target(1)-4;}\n"

    result = flatten_control_flow(code, functions=["N::Box::target"], helper_path=helper)
    compiled = _compile_cpp(result, std="-std=c++20")

    assert compiled.returncode == 0, compiled.stderr
    assert "switch(qcf)" in result


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_does_not_expand_glob_patterns():
    helper = _require_helper()
    code = "namespace N{int a(){return 1;}int b(){return 2;}}int main(){return N::a()+N::b()-3;}\n"

    result = flatten_control_flow(code, functions=["N::*"], helper_path=helper)

    assert result == code


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_recurses_into_while_body_if_else():
    helper = _require_helper()
    code = (
        "#include <iostream>\n"
        "int target(){int i=0;int s=0;while(i<5){if(i%2){s+=3;}else{s+=1;}++i;}return s;}"
        "int main(){std::cout<<target();return 0;}\n"
    )

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)
    run = _compile_and_run_cpp(result)

    assert run.returncode == 0, run.stderr
    assert run.stdout == "9"
    assert len(re.findall(r"switch\(qcf\w*\)", result)) >= 2
    assert "while(i<5)" in result


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_recurses_into_for_body_if_else():
    helper = _require_helper()
    code = (
        "#include <iostream>\n"
        "int target(){int s=0;for(int i=0;i<6;++i){if(i<3){s+=i;}else{s+=2;}}return s;}"
        "int main(){std::cout<<target();return 0;}\n"
    )

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)
    run = _compile_and_run_cpp(result)

    assert run.returncode == 0, run.stderr
    assert run.stdout == "9"
    assert len(re.findall(r"switch\(qcf\w*\)", result)) >= 2
    assert "for(int i=0;i<6;++i)" in result


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_recurses_into_loop_containing_lambda():
    helper = _require_helper()
    code = (
        "#include <iostream>\n"
        "int target(){int s=0;int i=0;while(i<4){if(i==2){auto add=[&](int x){return x+s;};s+=add(i);}s+=1;++i;}return s;}"
        "int main(){std::cout<<target();return 0;}\n"
    )

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)
    run = _compile_and_run_cpp(result)

    assert run.returncode == 0, run.stderr
    assert run.stdout == "8"
    assert "auto add" in result
    assert "while(i<4)" in result
    assert len(re.findall(r"switch\(qcf\w*\)", result)) >= 2


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_recurses_into_single_statement_if_else():
    helper = _require_helper()
    code = (
        "#include <iostream>\n"
        "struct Box{int v=0;void fill(int x){v=x;}};"
        "int target(bool flag){Box b;for(int i=0;i<1;++i){if(flag)b.fill(7);else b.fill(11);}return b.v;}"
        "int main(){std::cout<<target(true)<<' '<<target(false);return 0;}\n"
    )

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)
    run = _compile_and_run_cpp(result)

    assert run.returncode == 0, run.stderr
    assert run.stdout == "7 11"
    assert len(re.findall(r"switch\(qcf\w*\)", result)) >= 2
    assert "if(flag)b.fill(7);else b.fill(11);" not in result


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_recurses_into_plain_block_without_scope_leak():
    helper = _require_helper()
    code = (
        "#include <iostream>\n"
        "int target(int x){{int y=x;if(y>2){y+=3;}else{y-=3;}x=y;}return x;}"
        "int main(){std::cout<<target(4)<<' '<<target(1);return 0;}\n"
    )

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)
    run = _compile_and_run_cpp(result)

    assert run.returncode == 0, run.stderr
    assert run.stdout == "7 -2"
    assert len(re.findall(r"switch\(qcf\w*\)", result)) >= 2


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_preserves_break_continue_in_loop_body():
    helper = _require_helper()
    code = (
        "#include <iostream>\n"
        "int target(){int s=0;for(int i=0;i<8;++i){if(i==6)break;if(i%2)continue;s+=i;}return s;}"
        "int main(){std::cout<<target();return 0;}\n"
    )

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)
    run = _compile_and_run_cpp(result)

    assert run.returncode == 0, run.stderr
    assert run.stdout == "6"
    assert re.search(r"if\(qcf\w+==1\)break;", result)
    assert re.search(r"if\(qcf\w+==2\)continue;", result)
    assert "if(i==6){qcf" in result
    assert "if(i%2){qcf" in result


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_splits_if_containing_nested_loop_break_continue():
    helper = _require_helper()
    code = (
        "#include <iostream>\n"
        "int target(bool flag){int s=0;if(flag){for(int i=0;i<6;++i){if(i==4)break;if(i%2)continue;s+=i;}}else{s=100;}return s;}"
        "int main(){std::cout<<target(true)<<' '<<target(false);return 0;}\n"
    )

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)
    run = _compile_and_run_cpp(result)

    assert run.returncode == 0, run.stderr
    assert run.stdout == "2 100"
    assert "if(flag){for(" not in result
    assert re.search(r"if\(qcf\w+==1\)break;", result)
    assert re.search(r"if\(qcf\w+==2\)continue;", result)


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_nested_loop_break_targets_inner_loop_only():
    helper = _require_helper()
    code = (
        "#include <iostream>\n"
        "int target(){int s=0;for(int i=0;i<4;++i){for(int j=0;j<4;++j){if(j==2)break;s+=10*i+j;}s+=100;}return s;}"
        "int main(){std::cout<<target();return 0;}\n"
    )

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)
    run = _compile_and_run_cpp(result)

    assert run.returncode == 0, run.stderr
    assert run.stdout == "524"
    assert result.count("switch(qcf") >= 3


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_preserves_switch_case_break_inside_loop_body():
    helper = _require_helper()
    code = (
        "#include <iostream>\n"
        "int target(){int s=0;for(int i=0;i<4;++i){switch(i){case 1:break;default:s+=i;}s+=10;}return s;}"
        "int main(){std::cout<<target();return 0;}\n"
    )

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)
    run = _compile_and_run_cpp(result)

    assert run.returncode == 0, run.stderr
    assert run.stdout == "45"
    assert "switch(i)" in result
    assert "case 1:break;" in result


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_macro_break_keeps_loop_body_atomic():
    helper = _require_helper()
    code = (
        "#include <iostream>\n"
        "#define STOP break\n"
        "int target(){int s=0;for(int i=0;i<5;++i){if(i==3)STOP;s+=i;}return s;}"
        "int main(){std::cout<<target();return 0;}\n"
    )

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)
    run = _compile_and_run_cpp(result)

    assert run.returncode == 0, run.stderr
    assert run.stdout == "3"
    assert "STOP" in result


def test_flatten_control_flow_noops_without_functions():
    code = "int target(){return 1;}\n"

    assert flatten_control_flow(code, functions=[]) == code


def test_flatten_control_flow_verbose_reports_missing_targets(tmp_path: Path, capsys):
    code = "int target(){return 1;}\n"
    helper = _write_json_helper(tmp_path, _make_plan(code))

    result = flatten_control_flow(
        code,
        functions=["target", "missing"],
        helper_path=helper,
        verbose=True,
    )
    captured = capsys.readouterr()

    assert "switch(qcf)" in result
    assert "[flatten_cfg] skip missing: target not found by helper" in captured.err


def test_flatten_control_flow_skips_goto_and_try_catch():
    helper = _require_helper()
    with_goto = "int target(int x){if(x)goto done;return 1;done:return 2;}\n"
    with_try = "int other(){try{return 1;}catch(...){return 2;}}\n"

    assert flatten_control_flow(with_goto, functions=["target"], helper_path=helper) == with_goto
    assert flatten_control_flow(with_try, functions=["other"], helper_path=helper) == with_try


def test_process_uses_config_cli_function_list_and_helper(tmp_path: Path):
    helper = _require_helper()
    source = tmp_path / "main.cpp"
    config = tmp_path / "cppgolf.toml"
    source.write_text(
        "int configured(int x){return x+1;}\n"
        "int cli_added(int x){return x+2;}\n"
        "int main(){return configured(1)+cli_added(1)-5;}\n",
        encoding="utf-8",
    )
    config.write_text(
        "[flatten_cfg]\n"
        "enabled = true\n"
        'functions = ["configured"]\n'
        f'helper = "{helper.as_posix()}"\n',
        encoding="utf-8",
    )

    result, _ = process(
        source,
        [],
        config_path=config,
        flatten_cfg_functions=["cli_added"],
        no_std_ns=True,
        no_typedefs=True,
        no_compress_ws=True,
        rename_symbols=False,
    )

    assert result.count("switch(qcf)") == 2


def test_process_merges_config_and_cli_helper_includes(tmp_path: Path):
    helper = _require_helper()
    source = tmp_path / "main.cpp"
    config = tmp_path / "cppgolf.toml"
    config_include = tmp_path / "config-include"
    cli_include = tmp_path / "cli-include"
    config_include.mkdir()
    cli_include.mkdir()
    (config_include / "from_config.h").write_text("inline int from_config(){return 1;}\n", encoding="utf-8")
    (cli_include / "from_cli.h").write_text("inline int from_cli(){return 2;}\n", encoding="utf-8")
    source.write_text(
        "#include <from_config.h>\n"
        "#include <from_cli.h>\n"
        "int target(){return from_config()+from_cli();}\n",
        encoding="utf-8",
    )
    config.write_text(
        "[flatten_cfg]\n"
        "enabled = true\n"
        'functions = ["target"]\n'
        f'helper = "{helper.as_posix()}"\n'
        f'helper_includes = ["{config_include.as_posix()}"]\n',
        encoding="utf-8",
    )

    result, _ = process(
        source,
        [],
        config_path=config,
        cfg_helper_includes=[cli_include],
        no_merge=True,
        no_std_ns=True,
        no_typedefs=True,
        no_compress_ws=True,
        rename_symbols=False,
    )

    assert "switch(qcf)" in result


def test_cfg_helper_json_smoke():
    helper = _require_helper()
    source = ROOT / "1.cpp"
    if not source.exists():
        pytest.skip("local 1.cpp smoke input is not available")

    proc = subprocess.run(
        [
            str(helper),
            "-function=Suggester::addWord",
            str(source),
            "--",
            "-std=c++20",
            "--target=x86_64-w64-windows-gnu",
            "-isystem",
            "C:/mingw64/lib/gcc/x86_64-w64-mingw32/15.2.0/include/c++",
            "-isystem",
            "C:/mingw64/lib/gcc/x86_64-w64-mingw32/15.2.0/include/c++/x86_64-w64-mingw32",
            "-isystem",
            "C:/mingw64/lib/gcc/x86_64-w64-mingw32/15.2.0/include/c++/backward",
            "-isystem",
            "C:/mingw64/lib/gcc/x86_64-w64-mingw32/15.2.0/include",
            "-isystem",
            "C:/mingw64/x86_64-w64-mingw32/include",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    assert data["version"] == 4
    functions = data["functions"]
    assert functions[0]["qualified_name"] == "Suggester::addWord"
    block_plan = functions[0]["block_plan"]
    assert any(statement.get("kind") == "IfStmt" for statement in block_plan["statements"])
    assert isinstance(block_plan["locals"], list)
    assert isinstance(block_plan, dict)


def test_cfg_helper_marks_qualified_calls_as_linear_and_cross_case_locals():
    helper = _require_helper()
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "sample.cpp"
        code = (
            "#include <cstring>\n"
            "struct S{int a[2];};"
            "S* gp;"
            "void target(S& dst,const S& src){S local;gp=&local;std::memcpy(dst.a,src.a,sizeof(dst.a));dst.a[0]+=gp->a[0];}\n"
        )
        source.write_text(code, encoding="utf-8")
        proc = subprocess.run(
            [str(helper), "-function=target", str(source), "--", "-std=c++20"],
            capture_output=True,
            text=True,
            check=False,
        )

    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    function = data["functions"][0]
    statements = function["statements"]
    memcpy_statement = next(item for item in statements if "memcpy" in code[item["range"]["start"]:item["range"]["end"]])
    assert memcpy_statement["category"] == "linear"
    local = next(item for item in function["locals"] if item["name"] == "local")
    assert local["address_taken"] is True
    assert local["safe_hoist"] is True


def test_cfg_helper_emits_recursive_block_plan_for_if_and_loop():
    helper = _require_helper()
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "sample.cpp"
        code = "int target(int n){int s=0;for(int i=0;i<n;++i){if(i%2)s+=i;else s-=i;}return s;}\n"
        source.write_text(code, encoding="utf-8")
        proc = subprocess.run(
            [str(helper), "-function=target", str(source), "--", "-std=c++20"],
            capture_output=True,
            text=True,
            check=False,
        )

    assert proc.returncode == 0, proc.stderr
    data = json.loads(proc.stdout)
    function = data["functions"][0]
    loop = next(item for item in function["block_plan"]["statements"] if item["category"] == "loop")
    body_block = loop["control"]["body_block"]
    nested_if = next(item for item in body_block["statements"] if item["category"] == "if")
    assert nested_if["control"]["then_block"]["statements"]
    assert nested_if["control"]["else_block"]["statements"]
    assert function["block_plan"]["locals"]


def _collect_transfers_from_block(block: dict) -> list[dict]:
    result: list[dict] = []
    for statement in block.get("statements", []):
        result.extend(statement.get("transfers", []))
        control = statement.get("control", {})
        for key in ("then_block", "else_block", "body_block"):
            nested = control.get(key)
            if isinstance(nested, dict):
                result.extend(_collect_transfers_from_block(nested))
        nested_block = statement.get("block_plan")
        if isinstance(nested_block, dict):
            result.extend(_collect_transfers_from_block(nested_block))
    return result


def test_cfg_helper_marks_break_continue_targets_for_loop_body():
    helper = _require_helper()
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "sample.cpp"
        code = "int target(int n){int s=0;while(n--){if(n==4)break;if(n%2)continue;s+=n;}return s;}\n"
        source.write_text(code, encoding="utf-8")
        proc = subprocess.run(
            [str(helper), "-function=target", str(source), "--", "-std=c++20"],
            capture_output=True,
            text=True,
            check=False,
        )

    assert proc.returncode == 0, proc.stderr
    function = json.loads(proc.stdout)["functions"][0]
    loop = next(item for item in function["block_plan"]["statements"] if item["category"] == "loop")
    loop_id = loop["control_id"]
    body = loop["control"]["body_block"]
    transfers = _collect_transfers_from_block(body)
    assert body["active_loop_control_id"] == loop_id
    assert {item["kind"] for item in transfers} >= {"break", "continue"}
    assert all(item["target_control_id"] == loop_id for item in transfers)
    assert all(item["target_kind"] == "while" for item in transfers)
    assert all(item["safe"] is True for item in transfers)


def test_cfg_helper_distinguishes_switch_break_from_loop_break():
    helper = _require_helper()
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "sample.cpp"
        code = "int target(int n){while(n--){switch(n){case 1:break;default:n+=0;}if(n==0)break;}return n;}\n"
        source.write_text(code, encoding="utf-8")
        proc = subprocess.run(
            [str(helper), "-function=target", str(source), "--", "-std=c++20"],
            capture_output=True,
            text=True,
            check=False,
        )

    assert proc.returncode == 0, proc.stderr
    function = json.loads(proc.stdout)["functions"][0]
    transfers = _collect_transfers_from_block(function["block_plan"])
    assert any(item["kind"] == "break" and item["target_kind"] == "switch" for item in transfers)
    assert any(item["kind"] == "break" and item["target_kind"] == "while" for item in transfers)


def test_cfg_helper_marks_macro_break_as_unsafe():
    helper = _require_helper()
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "sample.cpp"
        code = "#define STOP break\nint target(int n){while(n--){if(n==1)STOP;n+=1;}return n;}\n"
        source.write_text(code, encoding="utf-8")
        proc = subprocess.run(
            [str(helper), "-function=target", str(source), "--", "-std=c++20"],
            capture_output=True,
            text=True,
            check=False,
        )

    assert proc.returncode == 0, proc.stderr
    transfers = _collect_transfers_from_block(json.loads(proc.stdout)["functions"][0]["block_plan"])
    assert any(item["kind"] == "break" and item["safe"] is False for item in transfers)


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_keeps_macro_expression_statements_atomic():
    helper = _require_helper()
    code = (
        "#include <cassert>\n"
        "#define CHECK(x) assert((x) > 0)\n"
        "int target(int x){CHECK(x);x+=1;CHECK(x);return x;}\n"
        "int main(){return target(1)==2?0:1;}\n"
    )

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)
    compiled = _compile_cpp(result)

    assert compiled.returncode == 0, compiled.stderr
    assert "switch(qcf)" in result
    assert result.count("CHECK(x);") == 2
    assert "__assert" not in result
    assert result.count("case ") >= 4


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_hoists_array_decay_locals():
    helper = _require_helper()
    code = (
        "int target(int x){"
        "if(x<0)return 0;"
        "int pv[2];"
        "int* p;"
        "p=pv;"
        "pv[0]=x;"
        "return p[0];"
        "}"
        "int main(){return target(7)==7?0:1;}\n"
    )

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)
    run = _compile_and_run_cpp(result)
    target_text = result[result.index("int target"):result.index("int main")]

    assert run.returncode == 0, run.stderr
    assert "int pv[2];" in target_text
    assert not re.search(r"case \d+:\{\s*int pv\[2\];", target_text)
    assert re.search(r"int pv\[2\];.*switch\(qcf", target_text, re.S)


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_keeps_dependent_array_bound_order():
    helper = _require_helper()
    code = (
        "#include <cstdint>\n"
        "int target(int x){"
        "x+=1;"
        "const std::uint32_t BUF_SIZE=4;"
        "std::uint8_t buf[BUF_SIZE];"
        "std::uint32_t pos=0;"
        "auto write=[&](std::uint8_t b){buf[pos++]=b;};"
        "write(std::uint8_t(x));"
        "return buf[0];"
        "}"
        "int main(){return target(6)==7?0:1;}\n"
    )

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)
    run = _compile_and_run_cpp(result)
    target_text = result[result.index("int target"):result.index("int main")]

    assert run.returncode == 0, run.stderr
    assert target_text.index("BUF_SIZE=4") < target_text.index("buf[BUF_SIZE]")
    assert not re.search(r"case \d+:\{\s*std::uint8_t buf\\[BUF_SIZE\\];", target_text)


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_uses_barrier_for_non_hoistable_cross_case_decl():
    helper = _require_helper()
    code = (
        "#include <tuple>\n"
        "struct Writer{int* p;Writer()=delete;Writer(int* q):p(q){}int get()const{return *p;}};"
        "std::tuple<bool,Writer> probe(int& x){return {true,Writer(&x)};}"
        "int target(int x){"
        "x+=1;"
        "auto tmp=probe(x);"
        "auto ok=std::get<0>(tmp);"
        "auto writer=std::get<1>(tmp);"
        "if(ok)x+=writer.get();"
        "x+=3;"
        "return x;"
        "}"
        "int main(){return target(2)==9?0:1;}\n"
    )

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)
    run = _compile_and_run_cpp(result)
    target_text = result[result.index("int target"):result.index("int main")]

    assert run.returncode == 0, run.stderr
    assert target_text.count("switch(qcf)") >= 2
    assert "auto tmp=probe(x);" in target_text
    assert "auto writer=std::get<1>(tmp);" in target_text
    assert not re.search(r"case \d+:\{\s*auto writer=std::get<1>\(tmp\);", target_text)
    assert re.search(r"auto writer=std::get<1>\(tmp\);.*switch\(qcf", target_text, re.S)


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_keeps_hoisted_decl_after_prelude_dependencies():
    helper = _require_helper()
    code = (
        "#include <fstream>\n"
        "#include <string>\n"
        "bool target(bool flag){"
        "std::string actualFilename;"
        "std::string msg;"
        "if(flag)actualFilename=\"a\";else actualFilename=\"b\";"
        "std::ofstream stream(actualFilename);"
        "bool saved=stream.good();"
        "msg=saved?actualFilename:\"bad\";"
        "return !msg.empty();"
        "}"
        "int main(){return target(true)?0:1;}\n"
    )

    result = flatten_control_flow(code, functions=["target"], helper_path=helper)
    compiled = _compile_cpp(result)
    target_text = result[result.index("bool target"):result.index("int main")]

    assert compiled.returncode == 0, compiled.stderr
    stream_index = target_text.index("std::ofstream stream(actualFilename);")
    assert target_text.index("std::string actualFilename;") < stream_index
    assert target_text.index("std::string msg;") < stream_index
    assert target_text.index('actualFilename="a";') < stream_index
    assert target_text.index('actualFilename="b";') < stream_index


@pytest.mark.skipif(GPP is None, reason="g++ not available")
def test_flatten_control_flow_real_sample_main_gets_nested_cases():
    helper = _require_helper()
    source = ROOT / "1.cpp"
    if not source.exists():
        pytest.skip("local 1.cpp smoke input is not available")

    code = source.read_text(encoding="utf-8")
    result = flatten_control_flow(
        code,
        functions=["main", "Suggester::addWord", "Suggester::getSuggestion"],
        helper_path=helper,
    )
    compiled = _compile_cpp(result, std="-std=c++20")

    assert compiled.returncode == 0, compiled.stderr
    assert len(re.findall(r"switch\(qcf\w*\)", result)) >= 3
    assert result.count("case ") > 5
    assert "vector<Word> getSuggestion" in result


def test_cli_help_lists_flatten_options():
    proc = subprocess.run(
        [sys.executable, "-m", "cppgolf", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "--config" in proc.stdout
    assert "--cfg-helper" in proc.stdout
    assert "--flatten-cfg" in proc.stdout
    assert "--flatten-cfg-function" in proc.stdout
