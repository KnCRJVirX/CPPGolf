"""
golf_rename.py — Pass 5: 符号名压缩（libclang AST 驱动）

依赖: pip install libclang
"""
import re
import itertools
import tempfile
import os
import sys as _sys
import struct as _struct

_MIN_RENAME_LEN = 2

# C/C++ 保留关键字，生成短名时不得使用
_CXX_KEYWORDS = frozenset({
    # C keywords
    'auto', 'break', 'case', 'char', 'const', 'continue', 'default',
    'do', 'double', 'else', 'enum', 'extern', 'float', 'for', 'goto',
    'if', 'inline', 'int', 'long', 'register', 'restrict', 'return',
    'short', 'signed', 'sizeof', 'static', 'struct', 'switch', 'typedef',
    'union', 'unsigned', 'void', 'volatile', 'while',
    # C++ keywords
    'alignas', 'alignof', 'and', 'and_eq', 'asm', 'bitand', 'bitor',
    'bool', 'catch', 'class', 'compl', 'concept', 'consteval', 'constexpr',
    'constinit', 'co_await', 'co_return', 'co_yield', 'decltype', 'delete',
    'explicit', 'export', 'false', 'friend', 'mutable', 'namespace',
    'new', 'noexcept', 'not', 'not_eq', 'nullptr', 'operator', 'or',
    'or_eq', 'private', 'protected', 'public', 'requires', 'static_assert',
    'static_cast', 'dynamic_cast', 'reinterpret_cast', 'const_cast',
    'template', 'this', 'thread_local', 'throw', 'true', 'try', 'typeid',
    'typename', 'using', 'virtual', 'wchar_t', 'xor', 'xor_eq',
    # 常用宏 / 内置名
    'NULL', 'TRUE', 'FALSE', 'EOF', 'stdin', 'stdout', 'stderr',
})


def _gen_short_names():
    for length in itertools.count(1):
        for combo in itertools.product('abcdefghijklmnopqrstuvwxyz', repeat=length):
            yield ''.join(combo)


def _make_platform_args() -> list:
    """返回当前平台所需的 libclang 预处理宏参数列表。

    - Windows: 注入 _WIN32/WIN32/_WIN64/WIN64，以及 _HAS_STD_BYTE=0（避免
      MSVC STL std::byte 与 Windows 头文件中全局 byte typedef 冲突）和
      WIN32_LEAN_AND_MEAN（减少头文件噪音）。
    - Linux / macOS: 注入对应平台宏。
    """
    args: list = []
    if _sys.platform == 'win32' or os.name == 'nt':
        args += ['-D_WIN32', '-DWIN32']
        if _struct.calcsize('P') == 8:
            args += ['-D_WIN64', '-DWIN64']
        args += ['-D_HAS_STD_BYTE=0', '-DWIN32_LEAN_AND_MEAN']
    elif _sys.platform.startswith('linux'):
        args += ['-D__linux__', '-D__unix__', '-DLINUX']
    elif _sys.platform == 'darwin':
        args += ['-D__APPLE__', '-D__unix__', '-D__MACH__']
    return args


def _is_user_file(cursor, tmppath: str) -> bool:
    """判断 cursor 的定义位置是否属于用户临时文件（即待处理的源码），
    而非系统/第三方头文件。用于过滤系统结构体字段等不应被重命名的符号。"""
    loc = cursor.location
    if not loc.file:
        return False
    try:
        return os.path.samefile(loc.file.name, tmppath)
    except OSError:
        return False


def _walk_ast(
    cursor,
    tmppath: str,
    src_bytes: bytes,
    decl_map: dict,
    replacements: list,
    decl_kinds: frozenset,
    ref_kinds: frozenset,
) -> None:
    """递归遍历 AST，收集需要重命名的符号声明位（decl_map）和所有引用位（replacements）。

    参数：
        decl_kinds  — VAR_DECL / FIELD_DECL / PARM_DECL 等声明节点类型集合
        ref_kinds   — MEMBER_REF_EXPR / DECL_REF_EXPR / MEMBER_REF 等引用节点类型集合
        decl_map    — USR → (orig_name, first_decl_offset, name_byte_len)，原地追加
        replacements — (offset, byte_len, usr) 三元组列表，原地追加
    """
    if cursor.kind.is_invalid():
        return
    if _is_user_file(cursor, tmppath):
        kind = cursor.kind
        if kind in decl_kinds:
            name = cursor.spelling
            if len(name) >= _MIN_RENAME_LEN:
                usr = cursor.get_usr()
                if usr:
                    off = cursor.location.offset
                    blen = len(name.encode('utf-8'))
                    # 跳过 offset 与源码不匹配的（宏展开内参数等）
                    if src_bytes[off:off + blen] == name.encode('utf-8'):
                        if usr not in decl_map:
                            decl_map[usr] = (name, off, blen)
                        replacements.append((off, blen, usr))
        elif kind in ref_kinds:
            ref = cursor.referenced
            if ref and ref.kind in decl_kinds:
                usr = ref.get_usr()
                name = cursor.spelling
                if usr and len(name) >= _MIN_RENAME_LEN:
                    off = cursor.location.offset
                    blen = len(name.encode('utf-8'))
                    # 跳过 offset 与源码不匹配的（宏展开内引用等）
                    if src_bytes[off:off + blen] == name.encode('utf-8'):
                        replacements.append((off, blen, usr))
    for child in cursor.get_children():
        _walk_ast(child, tmppath, src_bytes, decl_map, replacements, decl_kinds, ref_kinds)


def _scan_tokens(
    tu,
    tmppath: str,
    src_bytes: bytes,
    decl_map: dict,
    decl_kinds: frozenset,
    ref_kinds: frozenset,
    ci,
) -> list:
    """Token 扫描补全 pass：修正 AST walk 因宏展开 offset 错位而漏掉的符号位置。

    libclang 的 AST cursor 对宏参数的 location.offset 指向宏调用起始而非参数本身，
    导致 offset 校验失败、符号未进入 decl_map / replacements。
    token.location 是真实文本位置，此 pass 在 rename_map 建好前先收集候选。

    参数：
        decl_map — 可能被原地追加（补入宏内漏掉的 DECL）
        ci       — clang.cindex 模块（用于访问 CursorKind.DECL_STMT 等）

    返回：
        token_candidates 列表，元素为
        (offset: int, byte_len: int, tok_name: str, usr: str|None, is_member_access: bool)
        - usr=None 表示本 token 暂未匹配到已知 USR，留给后续策略2/3处理
        - is_member_access=True 表示该 token 前紧跟 . 或 ->，策略2/3 须跳过
    """
    token_candidates: list = []
    prev_tok_spelling = ''
    for token in tu.get_tokens(extent=tu.cursor.extent):
        if token.kind.name != 'IDENTIFIER':
            prev_tok_spelling = token.spelling   # 跟踪 . 和 -> 等标点符号
            continue
        loc = token.location
        if not loc.file:
            prev_tok_spelling = token.spelling
            continue
        try:
            if not os.path.samefile(loc.file.name, tmppath):
                prev_tok_spelling = token.spelling
                continue
        except OSError:
            prev_tok_spelling = token.spelling
            continue
        off = loc.offset
        tok_name = token.spelling
        # 判断是否是成员访问（前面紧跟 . 或 ->），用于限制名字回退策略的误用范围
        is_member_access = prev_tok_spelling in ('.', '->')
        prev_tok_spelling = tok_name
        if len(tok_name) < _MIN_RENAME_LEN:
            continue
        blen = len(tok_name.encode('utf-8'))
        if src_bytes[off:off + blen] != tok_name.encode('utf-8'):
            continue
        cur = token.cursor
        usr = None
        # 策略1：cursor 精确匹配（无宏展开偏移问题时走这里）
        if cur.kind in decl_kinds and cur.spelling == tok_name:
            usr = cur.get_usr()
            # AST walk 因 offset 校验失败而漏掉的 DECL，在此补入 decl_map
            if usr and usr not in decl_map:
                decl_map[usr] = (tok_name, off, blen)
        # 策略1.5：cursor 为 DECL_STMT（宏内变量声明常见），向下找 VAR_DECL 子节点
        elif cur.kind == ci.CursorKind.DECL_STMT:
            for child in cur.get_children():
                if child.kind in decl_kinds and child.spelling == tok_name:
                    usr = child.get_usr()
                    if usr and usr not in decl_map:
                        decl_map[usr] = (tok_name, off, blen)
                    break
        elif cur.kind in ref_kinds:
            ref = cur.referenced
            if ref and ref.kind in decl_kinds and cur.spelling == tok_name:
                ref_usr = ref.get_usr()
                if ref_usr:
                    # 仅当被引用 DECL 位于用户文件时才补入 decl_map，
                    # 避免把系统结构体字段（如 sockaddr_in6::sin6_family）纳入重命名
                    if ref_usr not in decl_map and _is_user_file(ref, tmppath):
                        decl_name = ref.spelling or tok_name
                        if len(decl_name) >= _MIN_RENAME_LEN:
                            decl_map[ref_usr] = (decl_name, off, blen)
                    # usr 只在 decl_map 中存在时才设置，
                    # 防止系统字段 USR 进入 token_candidates 后被错误匹配
                    if ref_usr in decl_map:
                        usr = ref_usr
        token_candidates.append((off, blen, tok_name, usr, is_member_access))
    return token_candidates


def _build_rename_map(
    decl_map: dict,
    replacements: list,
    code: str,
) -> tuple:
    """根据声明表和引用频次生成 USR→短名 映射，并构建名字单义查找表。

    参数：
        decl_map     — USR → (orig_name, first_decl_offset, name_byte_len)
        replacements — (offset, byte_len, usr) 列表，用于统计出现频次
        code         — 原始源码字符串，用于提取已有标识符（避免短名冲突）

    返回：
        (rename_map, name_to_usr)
        rename_map  — USR → short_name（高频 USR 优先分配最短名）
        name_to_usr — orig_name → USR，仅包含该名字在 decl_map 中唯一对应一个
                      USR 的情况（供 token 扫描策略2的名字单义回退使用）
    """
    # 统计每个 USR 的引用频次，高频符号优先分配最短名
    freq: dict = {}
    for _, _, usr in replacements:
        freq[usr] = freq.get(usr, 0) + 1

    sorted_usrs = sorted(decl_map.keys(), key=lambda u: -freq.get(u, 0))

    # 生成短名，跳过已存在的标识符和 C++ 关键字
    all_existing = set(re.findall(r'\b[A-Za-z_]\w*\b', code))
    occupied = all_existing | _CXX_KEYWORDS
    rename_map: dict = {}
    gen = _gen_short_names()
    for usr in sorted_usrs:
        orig = decl_map[usr][0]
        short = next(gen)
        while short in occupied or short == orig:
            short = next(gen)
        rename_map[usr] = short
        occupied.add(short)

    # 构建旧名 → USR 的单义查找表（仅唯一映射才加入，防止多义时误匹配）
    name_counts: dict = {}
    for u, (oname, _, _) in decl_map.items():
        if u in rename_map:
            name_counts[oname] = name_counts.get(oname, 0) + 1
    name_to_usr: dict = {}
    for u, (oname, _, _) in decl_map.items():
        if u in rename_map and name_counts.get(oname, 0) == 1:
            name_to_usr[oname] = u

    return rename_map, name_to_usr


def _merge_token_candidates(
    token_candidates: list,
    replacements: list,
    rename_map: dict,
    name_to_usr: dict,
) -> None:
    """将 token 候选列表合并进 replacements，应用策略2/3补全 AST walk 漏掉的位置。

    策略说明（is_member_access=True 时策略2/3均跳过）：
        策略1/1.5  cursor 精确匹配，token_candidates 中 usr 已设置，直接使用。
        策略2      名字单义回退：tok_name 在 name_to_usr 中唯一对应一个 USR。
                   用于 AST cursor 指向错误（宏参数常见）但名字无歧义的情况。
        策略3      最近 DECL_REF 推断：找同名且已知 USR 的 token 中距离最近的。
                   用于策略1/2均失败、但代码局部性强的情况。

    参数：
        token_candidates — _scan_tokens 返回的候选列表（只读）
        replacements     — 原地追加新的 (offset, byte_len, usr) 三元组
        rename_map       — USR → short_name，用于过滤无效 USR
        name_to_usr      — orig_name → USR（仅单义映射），供策略2使用
    """
    # 建立名字 → [(offset, usr)] 的索引，供策略3（最近 DECL_REF 推断）使用
    ref_by_name: dict = {}
    for off, _blen, tok_name, usr, _ma in token_candidates:
        if usr is not None:
            ref_by_name.setdefault(tok_name, []).append((off, usr))

    ast_seen = {off for off, _, _ in replacements}
    for off, blen, tok_name, usr, is_member_access in token_candidates:
        if off in ast_seen:
            continue  # AST walk 已覆盖，跳过
        if usr is None and not is_member_access:
            # 策略2：名字单义回退（非成员访问）
            usr = name_to_usr.get(tok_name)
        if usr is None and not is_member_access:
            # 策略3：最近 DECL_REF 推断（非成员访问）
            candidates_for_name = ref_by_name.get(tok_name, [])
            if candidates_for_name:
                nearest_usr = min(candidates_for_name, key=lambda x: abs(x[0] - off))[1]
                usr = nearest_usr
        if usr and usr in rename_map:
            replacements.append((off, blen, usr))
            ast_seen.add(off)


def _apply_replacements(
    src_bytes: bytes,
    replacements: list,
    rename_map: dict,
) -> str:
    """将所有重命名替换应用到源码字节串，返回替换后的字符串。

    处理步骤：
        1. 过滤掉 USR 不在 rename_map 中的记录（系统符号等）。
        2. 按 offset 降序排列并去重，确保从后向前替换不影响前面的 offset。
        3. 逐条把旧名字节替换为新短名字节。
    """
    valid = [
        (off, blen, usr)
        for off, blen, usr in replacements
        if usr in rename_map
    ]
    seen: set = set()
    deduped: list = []
    for off, blen, usr in sorted(valid, key=lambda x: -x[0]):
        if off not in seen:
            seen.add(off)
            deduped.append((off, blen, usr))

    result = bytearray(src_bytes)
    for off, blen, usr in deduped:
        result[off:off + blen] = rename_map[usr].encode('utf-8')
    return result.decode('utf-8')


def golf_rename_symbols(code: str) -> str:
    """使用 libclang 对 C++ 代码做符号名压缩。

    重命名范围：局部变量、函数参数、结构体/类字段（仅用户代码中定义的）。
    不重命名：函数名、类型名、宏名、标准库 / 系统头文件中的符号。
    """
    try:
        import clang.cindex as ci
    except ImportError:
        raise RuntimeError("需要 libclang: pip install libclang")

    src_bytes = code.encode('utf-8')

    # 必须用二进制写，避免 Windows 上 \n→\r\n 导致 offset 错位
    with tempfile.NamedTemporaryFile(suffix='.cpp', mode='wb', delete=False) as f:
        f.write(src_bytes)
        tmppath = f.name

    try:
        index = ci.Index.create()

        tu = index.parse(
            tmppath,
            args=['-std=c++23', '-w', '-fno-spell-checking'] + _make_platform_args(),
            options=(
                ci.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD |
                ci.TranslationUnit.PARSE_INCOMPLETE
            ),
        )

        # 声明节点类型：这些节点是需要被重命名的符号定义位
        _DECL_KINDS = frozenset({
            ci.CursorKind.VAR_DECL,
            ci.CursorKind.FIELD_DECL,
            ci.CursorKind.PARM_DECL,
        })
        # 引用节点类型：这些节点是已声明符号的使用位
        _REF_KINDS = frozenset({
            ci.CursorKind.MEMBER_REF_EXPR,  # obj.field / obj->field
            ci.CursorKind.MEMBER_REF,       # 构造函数初始化列表 : field(...)
            ci.CursorKind.DECL_REF_EXPR,    # 局部变量、参数引用
        })

        # USR → (orig_name, first_decl_offset, name_byte_len)
        decl_map: dict = {}
        # (offset, byte_len, usr) 三元组，记录所有声明位和引用位
        replacements: list = []

        # AST 遍历：收集所有用户文件中的声明和引用
        _walk_ast(tu.cursor, tmppath, src_bytes, decl_map, replacements, _DECL_KINDS, _REF_KINDS)

        # Token 扫描：补全 AST walk 因宏展开 offset 错位而漏掉的符号位置
        token_candidates = _scan_tokens(
            tu, tmppath, src_bytes, decl_map, _DECL_KINDS, _REF_KINDS, ci
        )

        if not decl_map:
            return code

        # 生成 USR→短名 映射，以及名字单义查找表（供策略2使用）
        rename_map, name_to_usr = _build_rename_map(decl_map, replacements, code)

        # 合并 token 候选：策略1已设 usr，策略2/3补全宏参数等漏掉的位置
        _merge_token_candidates(token_candidates, replacements, rename_map, name_to_usr)

        # 应用所有重命名替换（从后向前，保持 offset 正确）
        return _apply_replacements(src_bytes, replacements, rename_map)

    finally:
        try:
            os.unlink(tmppath)
        except OSError:
            pass
