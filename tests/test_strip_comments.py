from __future__ import annotations

from cppgolf import strip_comments


def test_strip_comments_preserves_literals_and_removes_comments():
    code = (
        'const char* a="// not comment";\n'
        "const char c='\\'';\n"
        'const char* raw=R"tag(/* still literal */ // literal)tag";\n'
        "int x = 1; // remove me\n"
        "/* multi\nline\ncomment */\n"
        "int y = 2;\n"
    )

    result = strip_comments(code)

    assert '"// not comment"' in result
    assert "const char c='\\'';" in result
    assert 'R"tag(/* still literal */ // literal)tag"' in result
    assert "remove me" not in result
    assert "multi" not in result
    assert "comment */" not in result
    assert "int x = 1;" in result
    assert "int y = 2;" in result
    assert result.count("\n") == code.count("\n")


def test_strip_comments_handles_line_comment_continuation():
    code = "int a = 0; // first line\\\nsecond line\nint b = 1;\n"

    result = strip_comments(code)

    assert "first line" not in result
    assert "second line" not in result
    assert "int b = 1;" in result


def test_strip_comments_preserves_preprocessor_lines_verbatim():
    code = (
        "#if defined(FOO) && defined(BAR) /* keep comment */ \\\n"
        " || defined(BAZ)\n"
        '#include "header.h" // keep comment too\n'
        "#endif\n"
    )

    result = strip_comments(code)

    assert result == code


def test_strip_comments_preserves_complex_quoted_literals():
    code = (
        'const char* a = "func(\\"/*not comment*/\\", [1,2,{3}]) // still literal";\n'
        'const char* b = "line one\\\nline two with // and /* */ and ([{}])"; // remove me\n'
        'const char* c = "left" "/* also literal */" "(right)";\n'
        "int value = 42; /* real block comment */\n"
    )

    result = strip_comments(code)

    assert '"func(\\"/*not comment*/\\", [1,2,{3}]) // still literal"' in result
    assert '"line one\\\nline two with // and /* */ and ([{}])"' in result
    assert '"left" "/* also literal */" "(right)"' in result
    assert "remove me" not in result
    assert "real block comment" not in result
    assert "int value = 42;" in result


def test_strip_comments_preserves_complex_raw_strings():
    code = (
        'auto raw = R"tag(\n'
        'json = {"text": "/* literal */", "seq": [1, 2, (3), {4}]};\n'
        '// still literal in raw string\n'
        '/* block literal in raw string */\n'
        ')tag"; // real comment\n'
        "int done = 1;\n"
    )

    result = strip_comments(code)

    assert 'R"tag(\njson = {"text": "/* literal */", "seq": [1, 2, (3), {4}]};\n// still literal in raw string\n/* block literal in raw string */\n)tag"' in result
    assert "real comment" not in result
    assert "int done = 1;" in result


def test_strip_comments_preserves_prefixed_literals_and_characters():
    code = (
        'auto a = u8"// prefix string ([{}])";\n'
        'auto b = LR"delim(quoted "text" /* still literal */ // raw literal)delim";\n'
        "char slash = '/';\n"
        "char quote = '\\''; // trailing comment\n"
    )

    result = strip_comments(code)

    assert 'u8"// prefix string ([{}])"' in result
    assert 'LR"delim(quoted "text" /* still literal */ // raw literal)delim"' in result
    assert "char slash = '/';" in result
    assert "char quote = '\\'';" in result
    assert "trailing comment" not in result
