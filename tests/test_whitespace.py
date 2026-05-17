from __future__ import annotations

from cppgolf.whitespace import compress_whitespace


def test_compress_whitespace_keeps_space_between_template_close_and_equals():
    code = "template<typename T, std::enable_if_t<sizeof(T)==4, bool> = true> int f();\n"
    result = compress_whitespace(code)
    assert "bool> =" in result
    assert "bool>=true" not in result
    assert "bool> =true" in result or "bool> = true" in result


def test_compress_whitespace_keeps_space_between_shift_close_and_equals():
    code = (
        "template<typename T, "
        "std::enable_if_t<std::is_same_v<T, std::array<std::array<int, 2>, 3>>, int> = 0>"
        " int f();\n"
    )
    result = compress_whitespace(code)
    assert "int> =" in result
    assert ">>=" not in result


def test_compress_whitespace_still_compresses_code_after_preprocessor_with_quotes():
    code = (
        "#define MESSAGE \"it's fine\"\n"
        "#define COMMENT /* don't parse quotes here */\n"
        "static size_t f(int a,\n"
        "                int b) {\n"
        "    return a + b;\n"
        "}\n"
    )
    result = compress_whitespace(code)
    assert '#define MESSAGE "it\'s fine"' in result
    assert "static size_t f(int a,int b){return a+b;}" in result
