"""whitespace.py — token 级空白压缩（字符串/预处理行感知）"""
import re

_IDENT_END   = re.compile(r'[A-Za-z0-9_]$')
_IDENT_START = re.compile(r'^[A-Za-z0-9_]')

# 完整预处理行（含续行 \）
# 注意：用 (?:[^\n\\]|\\.)* 代替 [^\n]* ，防止行末的 \ 被贪婪吞掉
# 导致 (?:\\\n...) 无法匹配续行
_PP_LINE_RE = re.compile(r'[ \t]*#(?:[^\n\\]|\\.)*(?:\\\n(?:[^\n\\]|\\.)*)*')

# token 正则
_TOKENIZE_RE = re.compile(
    r'(\x01[^\x01]+\x01)'                      # \x01PP...\x01
    r'|(\x02[^\x02]+\x02)'                      # \x02S...\x02
    r'|(0[xX][0-9A-Fa-f]+[uUlL]*'              # 十六进制
    r'|0[bB][01]+[uUlL]*'                       # 二进制
    r'|\d[\d.]*(?:[eE][+-]?\d+)?[uUlLfF]*'     # 整数/浮点
    r'|\.[\d]+(?:[eE][+-]?\d+)?[fF]?)'         # .开头浮点
    r'|([A-Za-z_]\w*)'                          # 标识符
    r'|(>>=|<<=|->|\.\.\.|::'                   # 多字符运算符
    r'|[+\-*/%&|^]=|==|!=|<=|>=|<<|>>|\+\+|\-\-|&&|\|\|'
    r'|[~!%^&*()\-+=\[\]{}|;:,.<>?/])'
    r'|(\n[ \t]*)'                              # 换行
    r'|([ \t]+)',                               # 水平空白
)


def _needs_space(a: str, b: str) -> bool:
    '''判断两个token之间是否需要空格'''
    if not a or not b:
        return False
    if _IDENT_END.search(a) and _IDENT_START.match(b):
        return True
    if a[-1] in '+-' and b[0] == a[-1]:
        return True
    return False


def _tokenize(code: str) -> list:
    tokens = []
    for pp_ph, str_ph, num, ident, op, nl, sp in _TOKENIZE_RE.findall(code):
        if   pp_ph:  tokens.append(('lit', pp_ph))      # 预处理行占位符（\x01PP...\x01）
        elif str_ph: tokens.append(('lit', str_ph))     # 字符串字面占位符（\x02S...\x02）
        elif num:    tokens.append(('num', num))        # 整数字面量
        elif ident:  tokens.append(('id',  ident))      # 标识符
        elif op:     tokens.append(('op',  op))         # 运算符
        elif nl:     tokens.append(('nl',  '\n'))       # 换行符
        elif sp:     tokens.append(('sp',  ' '))        # 空白
    return tokens


def _extract_strings(src: str) -> tuple[str, list]:
    """提取字符串/字符字面量，替换为 \\x02S{n}\\x02 占位符。"""
    str_lits: list = []
    result = []
    i = 0
    n = len(src)
    while i < n:
        raw_m = re.match(r'R"([^()\\ \t\n]*)\(', src[i:])
        if raw_m:
            delim = raw_m.group(1)
            end_marker = ')' + delim + '"'
            end_idx = src.find(end_marker, i + raw_m.end())
            if end_idx == -1:
                result.append(src[i:]); break
            end_idx += len(end_marker)
            idx = len(str_lits); str_lits.append(src[i:end_idx])
            result.append(f'\x02S{idx}\x02'); i = end_idx; continue
        if src[i] == '"':
            j = i + 1
            while j < n:
                if src[j] == '\\': j += 2
                elif src[j] == '"': j += 1; break
                else: j += 1
            idx = len(str_lits); str_lits.append(src[i:j])
            result.append(f'\x02S{idx}\x02'); i = j; continue
        if src[i] == "'":
            j = i + 1
            while j < n:
                if src[j] == '\\': j += 2
                elif src[j] == "'": j += 1; break
                else: j += 1
            idx = len(str_lits); str_lits.append(src[i:j])
            result.append(f'\x02S{idx}\x02'); i = j; continue
        result.append(src[i]); i += 1
    return ''.join(result), str_lits


def compress_whitespace(code: str) -> str:
    """
    1. 提取字符串字面量 → \\x02S{n}\\x02
    2. 提取预处理行    → \\x01PP{n}\\x01
    3. token 级空白最小化
    4. 还原 PP 行 / 字符串字面量
    """
    code_no_str, str_lits = _extract_strings(code)

    pp_lines: list = []

    def replace_pp(m):
        idx = len(pp_lines)
        pp_lines.append(m.group(0).strip())
        return f'\x01PP{idx}\x01'

    code_no_pp = _PP_LINE_RE.sub(replace_pp, code_no_str)

    tokens = _tokenize(code_no_pp)
    out: list = []
    prev_val = ''
    pending_space = False
    for kind, val in tokens:
        if kind in ('nl', 'sp'):
            pending_space = True
        else:
            if pending_space and _needs_space(prev_val, val):
                out.append(' ')
            pending_space = False
            out.append(val)
            prev_val = val

    code_min = ''.join(out)
    code_min = re.sub(r'\x01PP(\d+)\x01',
                      lambda m: '\n' + pp_lines[int(m.group(1))] + '\n',
                      code_min)
    code_min = re.sub(r'\x02S(\d+)\x02',
                      lambda m: str_lits[int(m.group(1))],
                      code_min)
    code_min = re.sub(r'\n{2,}', '\n', code_min)
    return code_min.strip()
