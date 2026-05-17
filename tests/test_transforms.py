from __future__ import annotations

from cppgolf.transforms import (
    golf_define_shortcuts,
    golf_endl_to_newline,
    golf_remove_inline,
    golf_remove_main_return,
    golf_std_namespace,
    golf_typedefs,
)


def test_unsigned_long_long_is_not_partially_rewritten_to_unsigned_ll():
    code = """#include <bits/stdc++.h>
unsigned long long a=1;
unsigned long long b=2;
"""
    result = golf_typedefs(code)
    assert "typedef unsigned long long ull;" in result
    assert "unsigned ll" not in result
    assert "typedef long long ll;" not in result
    assert "ull a=1;" in result
    assert "ull b=2;" in result


def test_unsigned_long_long_and_long_long_can_coexist():
    code = """#include <bits/stdc++.h>
unsigned long long a=1;
long long b=2;
unsigned long long c=3;
long long d=4;
"""
    result = golf_typedefs(code)
    assert "typedef unsigned long long ull;" in result
    assert "typedef long long ll;" in result
    assert "ull a=1;" in result
    assert "ll b=2;" in result
    assert "ull c=3;" in result
    assert "ll d=4;" in result
    assert "unsigned ll" not in result


def test_vector_long_long_still_can_chain_into_vll():
    code = """#include <vector>
vector<long long> a;
vector<long long> b;
"""
    result = golf_typedefs(code)
    assert "typedef long long ll;" in result
    assert "typedef vector<ll> vll;" in result
    assert "vll a;" in result
    assert "vll b;" in result


def test_existing_typedef_still_rewrites_long_long_uses():
    code = """#include <bits/stdc++.h>
typedef long long ll;
long long a;
long long b;
"""
    result = golf_typedefs(code)
    assert result.count("typedef long long ll;") == 1
    assert "ll a;" in result
    assert "ll b;" in result


def test_std_namespace_does_not_touch_string_or_raw_string_literals():
    code = '#include <iostream>\nint main(){auto a="std::vector";auto b=R"(std::cout << endl)";std::cout<<a;}\n'
    result = golf_std_namespace(code)
    assert '"std::vector"' in result
    assert 'R"(std::cout << endl)"' in result
    assert "using namespace std;" in result
    assert "cout<<a;" in result


def test_std_namespace_skips_unsafe_std_patterns():
    code = (
        "#include <mutex>\n"
        "#include <string>\n"
        "using std::string;\n"
        "struct std::hash<MyType> {};\n"
        "std::unique_lock<std::mutex> lock(mutex);\n"
    )
    result = golf_std_namespace(code)
    assert result == code


def test_typedefs_do_not_touch_literals():
    code = 'const char* a="long long"; const char* b=R"(vector<long long>)"; long long x; long long y;'
    result = golf_typedefs(code)
    assert '"long long"' in result
    assert 'R"(vector<long long>)"' in result
    assert "ll x;" in result
    assert "ll y;" in result


def test_remove_inline_does_not_touch_literals():
    code = 'const char* a="inline"; const char* b=R"(inline value)"; inline int f(){return 1;}'
    result = golf_remove_inline(code)
    assert '"inline"' in result
    assert 'R"(inline value)"' in result
    assert "inline int f" not in result
    assert "int f(){return 1;}" in result


def test_remove_inline_does_not_break_preprocessor_lines():
    code = "#define INLINE_KEYWORD inline\ninline int f(){return 1;}\n"
    result = golf_remove_inline(code)
    assert "#define INLINE_KEYWORD inline" in result
    assert "int f(){return 1;}" in result


def test_endl_to_newline_does_not_touch_literals():
    code = 'const char* a="endl"; const char* b=R"(std::endl)"; std::cout<<std::endl;'
    result = golf_endl_to_newline(code)
    assert '"endl"' in result
    assert 'R"(std::endl)"' in result
    assert '<<std::endl' not in result
    assert '<<"\\n";' in result


def test_endl_to_newline_does_not_touch_sync_endl_like_identifiers():
    code = 'sync_cout << value << sync_endl;\nstd::cout << std::endl;\n'
    result = golf_endl_to_newline(code)
    assert "sync_endl" in result
    assert 'sync_"\\n"' not in result
    assert 'std::cout <<"\\n";' in result or 'std::cout << "\\n";' in result or 'std::cout<<"\\n";' in result


def test_define_shortcuts_does_not_touch_literals():
    code = (
        'const char* a="cout cout cout cout cout";'
        'const char* b=R"(cin cout)";'
        'cout<<1;cout<<2;cout<<3;cout<<4;cout<<5;'
    )
    result = golf_define_shortcuts(code)
    assert '#define co cout' in result
    assert '"cout cout cout cout cout"' in result
    assert 'R"(cin cout)"' in result
    assert 'co<<1;' in result


def test_remove_main_return_only_removes_top_level_trailing_return():
    code = """int main(){
    auto f=[](){return 0;};
    if(false){return 0;}
    return 0;
}"""
    result = golf_remove_main_return(code)
    assert "auto f=[](){return 0;};" in result
    assert "if(false){return 0;}" in result
    assert result.count("return 0;") == 2


def test_remove_main_return_keeps_nested_local_type_returns():
    code = """int main(){
    struct Runner{
        int operator()() const { return 0; }
    };
    return 0;
}"""
    result = golf_remove_main_return(code)
    assert "int operator()() const { return 0; }" in result
    assert result.count("return 0;") == 1
