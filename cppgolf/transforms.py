"""transforms.py — 语义级高尔夫变换（std::、typedef、endl、inline、braces、shortcuts）"""
import re


def golf_std_namespace(code: str) -> str:
    """若代码有 std:: 则添加 using namespace std; 并删除所有 std:: 前缀。"""
    has_using = bool(re.search(r'\busing\s+namespace\s+std\s*;', code))
    if not re.search(r'\bstd::', code):
        return code
    if not has_using:
        lines = code.split('\n')
        insert_at = len(lines)
        for idx, line in enumerate(lines):
            s = line.strip()
            if s and not s.startswith('#'):
                insert_at = idx; break
        lines.insert(insert_at, 'using namespace std;')
        code = '\n'.join(lines)
    return re.sub(r'\bstd::', '', code)


def golf_typedefs(code: str) -> str:
    """对高频长类型名添加 #define 缩写（出现 ≥2 次时触发）。"""
    replacements = [
        (r'\blong long\b',          'll',   '#define ll long long'),
        (r'\bunsigned long long\b', 'ull',  '#define ull unsigned long long'),
        (r'\blong double\b',        'ld',   '#define ld long double'),
        (r'\bvector<int>\b',        'vi',   '#define vi vector<int>'),
        (r'\bvector<ll>\b',         'vll',  '#define vll vector<ll>'),
        (r'\bpair<int,int>\b',      'pii',  '#define pii pair<int,int>'),
        (r'\bpair<ll,ll>\b',        'pll',  '#define pll pair<ll,ll>'),
    ]
    defines_to_add = []
    for pattern, short, defline in replacements:
        macro = defline.split()[1]
        if re.search(r'\b' + re.escape(macro) + r'\b', code):
            continue
        if len(re.findall(pattern, code)) >= 2:
            defines_to_add.append(defline)
            code = re.sub(pattern, short, code)
    if defines_to_add:
        last = max(
            (m.end() for m in re.finditer(r'^#(?:include|define)\b.*$', code, re.MULTILINE)),
            default=0,
        )
        code = code[:last] + '\n' + '\n'.join(defines_to_add) + '\n' + code[last:]
    return code


def golf_remove_main_return(code: str) -> str:
    """移除 main 末尾的 return 0;（C++ 标准允许省略）。"""
    return re.sub(
        r'(int\s+main\s*\([^)]*\)\s*\{.*?)(\s*return\s+0\s*;\s*)(\})',
        lambda m: m.group(1) + '\n' + m.group(3),
        code, flags=re.DOTALL,
    )


def golf_endl_to_newline(code: str) -> str:
    r"""将 endl 替换为 '\n'（避免 flush，且更短）。"""
    nl_str = r'"\n"'
    code = re.sub(r'<<\s*endl\b', lambda _: '<< ' + nl_str, code)
    code = re.sub(r'\bendl\b(?=\s*[;,)])', lambda _: nl_str, code)
    return code


def golf_remove_inline(code: str) -> str:
    """移除 inline，保留 inline static（C++17 内联静态成员变量）。"""
    return re.sub(r'\binline\s+(?!static\b)', '', code)


def golf_braces_single_stmt(code: str) -> str:
    """（激进）移除单条语句 if/for/while 的花括号。"""
    return re.compile(
        r'\b(if|for|while)\s*(\([^)]*\))\s*\{\s*([^{};]*;)\s*\}',
        re.DOTALL,
    ).sub(r'\1\2\3', code)


def golf_define_shortcuts(code: str) -> str:
    """高频（≥5次）cout/cin 生成 #define 缩写。"""
    shortcuts = [
        (r'\bcout\b', 'co', '#define co cout'),
        (r'\bcin\b',  'ci', '#define ci cin'),
    ]
    defines_to_add = []
    for pattern, short, defline in shortcuts:
        if re.search(re.escape(defline), code):
            continue
        if len(re.findall(pattern, code)) >= 5:
            defines_to_add.append(defline)
            code = re.sub(pattern, short, code)
    if defines_to_add:
        last = max(
            (m.end() for m in re.finditer(r'^#(?:include|define)\b.*$', code, re.MULTILINE)),
            default=0,
        )
        code = code[:last] + '\n' + '\n'.join(defines_to_add) + '\n' + code[last:]
    return code
