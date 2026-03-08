"""transforms.py — 语义级高尔夫变换（std::、typedef、endl、inline、braces、shortcuts）"""
import re


def golf_std_namespace(code: str) -> str:
    """无条件在最后一个顶层 #include 之后插入 using namespace std;，并删除所有 std:: 前缀。

    - 先移除代码中已有的 using namespace std;（避免重复）。
    - 只扫描 #if/#ifdef/#ifndef 条件块之外（深度=0）的 #include 行作为候选插入点，
      避免把 #ifdef _WIN32 / #include <windows.h> 误认为"最后一个 include"。
    - 若无顶层 #include 则退回全局最后一个 #include；若仍无则插到文件开头。
    - 移除所有 std:: 前缀。
    """
    # 移除已有的 using namespace std;（可能在任意位置）
    code = re.sub(r'[ \t]*using\s+namespace\s+std\s*;\n?', '', code)

    # 扫描所有预处理行，跟踪 #if 深度，只记录深度=0 的 #include 末尾位置
    pp_re = re.compile(r'^[ \t]*#[ \t]*(\w+).*$', re.MULTILINE)
    depth = 0
    top_level_include_ends: list[int] = []
    all_include_ends: list[int] = []
    for m in pp_re.finditer(code):
        directive = m.group(1).lower()
        if directive in ('if', 'ifdef', 'ifndef'):
            depth += 1
        elif directive == 'endif':
            depth = max(0, depth - 1)
        elif directive == 'include':
            all_include_ends.append(m.end())
            if depth == 0:
                top_level_include_ends.append(m.end())

    insert_at = (top_level_include_ends[-1] if top_level_include_ends
                 else all_include_ends[-1] if all_include_ends
                 else 0)
    code = code[:insert_at] + '\nusing namespace std;' + code[insert_at:]

    # 删除所有 std:: 前缀
    return re.sub(r'\bstd::', '', code)


def golf_typedefs(code: str) -> str:
    """对高频长类型名添加 typedef 缩写（出现 ≥2 次时触发）。"""
    replacements = [
        (r'\blong long\b',          'll',   'typedef long long ll;'),
        (r'\bunsigned long long\b', 'ull',  'typedef unsigned long long ull;'),
        (r'\blong double\b',        'ld',   'typedef long double ld;'),
        (r'\bvector<int>\b',        'vi',   'typedef vector<int> vi;'),
        (r'\bvector<ll>\b',         'vll',  'typedef vector<ll> vll;'),
        (r'\bpair<int,int>\b',      'pii',  'typedef pair<int,int> pii;'),
        (r'\bpair<ll,ll>\b',        'pll',  'typedef pair<ll,ll> pll;'),
    ]
    defines_to_add = []
    for pattern, short, defline in replacements:
        # 提取缩写名（typedef ... short;）
        macro = defline.rstrip(';').split()[-1]
        # 匹配已有的 typedef 或 #define 形式
        existing_re = re.compile(
            r'^[ \t]*(?:'
            r'typedef\b[^\n]+\b' + re.escape(macro) + r'\s*;'
            r'|#[ \t]*define[ \t]+' + re.escape(macro) + r'\b[^\n]*'
            r')[ \t]*\n?',
            re.MULTILINE,
        )
        existing = existing_re.search(code)
        if existing:
            # 已有定义：从原位删掉，稍后统一插到顶部
            code = code[:existing.start()] + code[existing.end():]
            defines_to_add.append(defline)
        elif len(re.findall(pattern, code)) >= 2:
            defines_to_add.append(defline)
            code = re.sub(pattern, short, code)
    if defines_to_add:
        # 插入点：文件顶部 include 块末尾（仅顶层 #include，不计 #ifdef 内的）
        pp_re2 = re.compile(r'^[ \t]*#[ \t]*(\w+).*$', re.MULTILINE)
        depth2 = 0
        top_inc_ends: list[int] = []
        all_inc_ends: list[int] = []
        for m in pp_re2.finditer(code):
            d = m.group(1).lower()
            if d in ('if', 'ifdef', 'ifndef'):
                depth2 += 1
            elif d == 'endif':
                depth2 = max(0, depth2 - 1)
            elif d == 'include':
                all_inc_ends.append(m.end())
                if depth2 == 0:
                    top_inc_ends.append(m.end())
        last = (top_inc_ends[-1] if top_inc_ends
                else all_inc_ends[-1] if all_inc_ends
                else 0)
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


def golf_windows_lean(code: str) -> str:
    """当代码包含任意 Windows SDK 入口头文件时，自动在其前面插入两个防冲突宏定义。

    1. WIN32_LEAN_AND_MEAN —— 阻止 winscard.h → wtypes.h → rpcndr.h 链。
    2. _HAS_STD_BYTE 0    —— 禁用 <cstddef> 的 std::byte，彻底消除与
       rpcndr.h / winternl.h 等路径引入的全局 byte typedef 的歧义。

    覆盖的 Windows SDK 入口头：windows.h, winsock2.h, winsock.h,
    winternl.h, ws2tcpip.h（及大小写变体）。
    若代码中已有对应宏则跳过对应部分，但两者独立判断。
    """
    # 匹配常见 Windows SDK 入口头（任意大小写变体）
    _WIN_HDR_RE = re.compile(
        r'[ \t]*#[ \t]*include[ \t]*<'
        r'(?:[Ww]indows|[Ww]in[Ss]ock2?|[Ww]internl|[Ww]s2tcpip)'
        r'\.h>'
    )
    win_m = _WIN_HDR_RE.search(code)
    if not win_m:
        return code
    insert_pos = win_m.start()

    inject = ''
    if 'WIN32_LEAN_AND_MEAN' not in code:
        inject += '#ifndef WIN32_LEAN_AND_MEAN\n#define WIN32_LEAN_AND_MEAN\n#endif\n'
    if '_HAS_STD_BYTE' not in code:
        inject += '#ifndef _HAS_STD_BYTE\n#define _HAS_STD_BYTE 0\n#endif\n'

    if not inject:
        return code
    return code[:insert_pos] + inject + code[insert_pos:]


def golf_braces_single_stmt(code: str) -> str:
    """（激进）移除单条语句 if/for/while 的花括号。

    使用括号计数扫描，支持任意深度嵌套，不依赖正则深度限制。
    """
    def _match_bracket(s: str, pos: int, open_ch: str, close_ch: str) -> int:
        """从 pos（open_ch 处）扫描，返回对应 close_ch 之后的位置。"""
        depth = 0
        i = pos
        n = len(s)
        while i < n:
            c = s[i]
            if c == open_ch:
                depth += 1
            elif c == close_ch:
                depth -= 1
                if depth == 0:
                    return i + 1
            i += 1
        return n  # 未找到匹配（不合法输入）

    kw_re = re.compile(r'\b(if|for|while)\s*')
    result: list[str] = []
    i = 0
    n = len(code)
    while i < n:
        m = kw_re.search(code, i)
        if not m:
            result.append(code[i:])
            break

        result.append(code[i:m.start()])
        kw = m.group(1)
        pos = m.end()  # 紧接关键字+空白之后

        # 必须紧跟 '('
        if pos >= n or code[pos] != '(':
            result.append(m.group(0))
            i = m.end()
            continue

        # 找条件括号的匹配 ')'
        cond_end = _match_bracket(code, pos, '(', ')')
        cond = code[pos:cond_end]

        # 跳过空白
        k = cond_end
        while k < n and code[k] in ' \t\n':
            k += 1

        # 必须紧跟 '{'
        if k >= n or code[k] != '{':
            result.append(m.group(0))
            i = m.end()
            continue

        # 找函数体括号的匹配 '}'
        body_end = _match_bracket(code, k, '{', '}')
        body = code[k + 1:body_end - 1].strip()

        # 只处理：函数体内无嵌套花括号、且恰好是单条分号结尾语句
        if '{' not in body and '}' not in body and body.count(';') == 1 and body.endswith(';'):
            result.append(f'{kw}{cond}{body}')
            i = body_end
        else:
            result.append(m.group(0))
            i = m.end()

    return ''.join(result)


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
