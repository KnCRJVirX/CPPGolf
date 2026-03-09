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
from typing import TYPE_CHECKING

import clang.cindex as ci

# clang_getSpecializedCursorTemplate：函数模板实例化 cursor → 模板声明 cursor
# 用于将 F@funcname<...> 实例化 USR 归并到 FT@>... 模板声明 USR
try:
    _clang_getSpecializedCursorTemplate = ci.conf.lib.clang_getSpecializedCursorTemplate # type: ignore
    _clang_getSpecializedCursorTemplate.restype = ci.Cursor
    _clang_getSpecializedCursorTemplate.argtypes = [ci.Cursor]
except AttributeError:
    _clang_getSpecializedCursorTemplate = None

if TYPE_CHECKING:
    import clang.cindex as _ci  # 仅用于类型标注，运行时不导入

_MIN_RENAME_LEN = 2

# 函数/方法名的最小重命名长度（高于变量）：
# libclang 对模板内非依存的无限定方法调用无法解析，短名如 at/to/is 会漏掉调用点。
# 设为 4 可排除多数 standard-compatible 短名，避免模板场景下的局部不一致。
_MIN_METHOD_RENAME_LEN = 4

# 不得被重命名的函数名（程序入口 / 系统回调，重命名会导致链接错误）
_PROTECTED_NAMES = frozenset({
    'main', 'WinMain', 'wWinMain', 'DllMain', 'wmain',
})

# token_candidates 中用于标记"此 token 属于 virtual 方法参数的引用，应跳过所有回退策略"的哨兵 USR。
# 哨兵 USR 不在 rename_map 中，因此不会被应用替换；
# 其不为 None，使得 merge_token_candidates 的策略2/3判断（usr is None）时跳过此 token，
# 防止策略3（最近 DECL_REF 推断）跨函数边界把 virtual 参数的使用处错误归并到无关 USR。
_VIRT_PARM_SENTINEL = '\x00virt_parm\x00'

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


def _group_virt_usrs(virt_usrs: set, decl_map: dict) -> dict:
    """按方法名将 virtual USR 分组，返回 {non_rep_usr → rep_usr} 的重映射表。

    libclang 为基类和各派生类的同名虚函数分配不同 USR，若直接重命名会产生不同短名，
    导致 override 关系断裂。此函数将同名虚函数的所有 USR 合并到同一代表 USR
    （首次出现的，即 offset 最小的那个），使全链统一映射到同一短名。
    """
    by_name: dict = {}  # name → list of (usr, offset)
    for usr in virt_usrs:
        if usr in decl_map:
            name, off, _ = decl_map[usr]
            by_name.setdefault(name, []).append((usr, off))
    remap: dict = {}
    for name, entries in by_name.items():
        # offset 最小的作为代表（通常为基类/抽象类中的首次声明）
        rep_usr = min(entries, key=lambda e: e[1])[0]
        for usr, _ in entries:
            if usr != rep_usr:
                remap[usr] = rep_usr
    return remap


def _group_overload_usrs(func_usrs: set, decl_map: dict) -> dict:
    """按 (函数名, 父作用域USR) 将同名重载函数的 USR 合并到同一代表 USR。

    C++ 允许同名函数重载，libclang 对每个重载分配独立 USR，
    若直接重命名会产生不同短名，导致调用处出现歧义或未声明错误。
    此函数将同一作用域内同名函数的所有 USR 合并到 offset 最小的代表 USR。
    func_usrs 元素为 (usr, parent_usr) 二元组。
    """
    by_scope_name: dict = {}  # (name, parent_usr) → list of (usr, offset)
    for usr, parent_usr in func_usrs:
        if usr in decl_map:
            name, off, _ = decl_map[usr]
            key = (name, parent_usr)
            by_scope_name.setdefault(key, []).append((usr, off))
    remap: dict = {}
    for (name, parent_usr), entries in by_scope_name.items():
        if len(entries) <= 1:
            continue  # 无重载，无需合并
        # offset 最小的作为代表（通常为类中首个重载声明）
        rep_usr = min(entries, key=lambda e: e[1])[0]
        for usr, _ in entries:
            if usr != rep_usr:
                remap[usr] = rep_usr
    return remap


def _is_user_file(cursor: '_ci.Cursor', tmppath: str) -> bool:
    """判断 cursor 的定义位置是否属于用户临时文件（即待处理的源码），
    而非系统/第三方头文件。用于过滤系统结构体字段等不应被重命名的符号。"""
    loc = cursor.location
    if not loc.file:
        return False
    try:
        return os.path.samefile(loc.file.name, tmppath)
    except OSError:
        return False


def walk_ast(
    cursor: '_ci.Cursor',
    tmppath: str,
    src_bytes: bytes,
    decl_map: dict,
    replacements: list,
    decl_kinds: frozenset,
    ref_kinds: frozenset,
    member_usrs: set | None = None,
    virt_usrs: set | None = None,
    func_usrs: set | None = None,
    func_ranges: list | None = None,
) -> None:
    """递归遍历 AST，收集需要重命名的符号声明位（decl_map）和所有引用位（replacements）。

    参数：
        decl_kinds  — VAR_DECL / FIELD_DECL / PARM_DECL 等声明节点类型集合
        ref_kinds   — MEMBER_REF_EXPR / DECL_REF_EXPR / MEMBER_REF 等引用节点类型集合
        decl_map    — USR → (orig_name, first_decl_offset, name_byte_len)，原地追加
        replacements — (offset, byte_len, usr) 三元组列表，原地追加
        member_usrs — 字段 + 成员函数的 USR 集合，供 scan_tokens 兜底策略使用
        func_ranges — (start_off, end_off, func_usr) 三元组列表，收集函数体字节范围，
                      供 merge_token_candidates 策略3限制只在同函数内查找使用
        virt_usrs   — virtual CXX_METHOD 的 USR 集合，供后续按名分组合并使用
        func_usrs   — (usr, parent_usr) 集合，记录所有函数/方法 USR 及其父作用域，供重载分组合并使用
    """
    if cursor.kind.is_invalid():
        return
    if _is_user_file(cursor, tmppath):
        kind = cursor.kind
        # 始终收集函数/方法的字节范围（无论 rename_functions 是否为 True），
        # 供 merge_token_candidates 策略3限制只在同函数体内查找候选，防止跨函数边界错误归并。
        # 注意：此处独立于 decl_kinds，即使函数不在重命名列表中也要记录范围。
        if (func_ranges is not None
                and kind.name in ('CXX_METHOD', 'FUNCTION_DECL', 'FUNCTION_TEMPLATE')):
            try:
                ext = cursor.extent
                func_start = ext.start.offset
                func_end = ext.end.offset
                if func_end > func_start:
                    # 只记录有函数体的声明（函数体内 func_end 会包含 '}'）
                    func_ranges.append((func_start, func_end, cursor.get_usr() or ''))
            except Exception:
                pass
        if kind in decl_kinds:
            # 跳过 virtual 方法（含 override）的参数：
            # virtual 方法参数名常被宏体硬编码依赖
            # 不同 override 对同名参数各自分配不同短名，宏展开时会产生"未声明"错误。
            # 统一不重命名 virtual 方法参数，保持宏约定不被破坏。
            if kind.name == 'PARM_DECL':  # type: ignore
                parent: ci.Cursor = cursor.semantic_parent
                if parent is not None and parent.is_virtual_method():
                    # 不加入 decl_map，但将声明位置加入 replacements。
                    # replacements 中有该 offset 后，merge_token_candidates 的 ast_seen
                    # 会跳过声明处的 token，防止策略2将无关同名符号误匹配到此 PARM_DECL。
                    # apply_replacements 会因 USR 不在 rename_map 中而过滤掉该条目，
                    # 声明处和函数体里的该名字均保持原名，确保一致性。
                    _vp_name = cursor.spelling
                    _vp_usr = cursor.get_usr()
                    if _vp_usr and _vp_name:
                        _vp_off = cursor.location.offset
                        _vp_blen = len(_vp_name.encode('utf-8'))
                        if src_bytes[_vp_off:_vp_off + _vp_blen] == _vp_name.encode('utf-8'):
                            replacements.append((_vp_off, _vp_blen, _vp_usr))
                    return  # 不递归子节点（PARM_DECL 无实质子节点）
            name = cursor.spelling
            # 函数/方法名用更高的最小长度阈值，避免模板内无限定调用无法解析
            _is_func_kind = kind.name in ('CXX_METHOD', 'FUNCTION_DECL', 'FUNCTION_TEMPLATE')
            min_len = _MIN_METHOD_RENAME_LEN if _is_func_kind else _MIN_RENAME_LEN
            # 排除 operator 重载（通过运算符语法调用，重命名后隐式调用失效）
            if (len(name) >= min_len
                    and name not in _PROTECTED_NAMES
                    and not name.startswith('operator')):
                usr = cursor.get_usr()
                if usr:
                    off = cursor.location.offset
                    blen = len(name.encode('utf-8'))
                    # 跳过 offset 与源码不匹配的（宏展开内参数等）
                    if src_bytes[off:off + blen] == name.encode('utf-8'):
                        if usr not in decl_map:
                            decl_map[usr] = (name, off, blen)
                        # 记录字段/成员函数 USR，供 scan_tokens 成员访问兜底使用
                        # FIELD_DECL：字段；CXX_METHOD/FUNCTION_DECL/FUNCTION_TEMPLATE：成员函数
                        if member_usrs is not None and kind.name in (
                            'FIELD_DECL', 'CXX_METHOD', 'FUNCTION_DECL', 'FUNCTION_TEMPLATE'
                        ):
                            member_usrs.add(usr)
                        # 记录 virtual 方法 USR，供后续名字分组合并使用
                        if virt_usrs is not None and cursor.is_virtual_method():
                            virt_usrs.add(usr)
                        # 记录函数/方法 USR 及其父作用域，供后续重载分组合并使用
                        if func_usrs is not None and kind.name in (
                            'CXX_METHOD', 'FUNCTION_DECL', 'FUNCTION_TEMPLATE'
                        ):
                            sp = cursor.semantic_parent
                            parent_usr = sp.get_usr() if sp else ''
                            func_usrs.add((usr, parent_usr))
                        replacements.append((off, blen, usr))
        elif kind in ref_kinds:
            ref = cursor.referenced
            if ref and ref.kind in decl_kinds:
                name = cursor.spelling
                if name and len(name) >= _MIN_RENAME_LEN:
                    usr = ref.get_usr()
                    if usr:
                        # 函数模板实例化 USR（含 '<'）归并到模板声明 USR，
                        # 与 scan_tokens 的 ref_kinds 分支保持一致。
                        # 注意：walk_ast 是 DFS，访问引用时声明可能尚未入 decl_map，
                        # 因此不检查 tmpl_usr in decl_map（apply_replacements 会过滤无效 USR）。
                        if ('<' in usr
                                and _clang_getSpecializedCursorTemplate is not None
                                and ref.kind.name in ('CXX_METHOD', 'FUNCTION_DECL', 'FUNCTION_TEMPLATE')):
                            try:
                                tmpl_cur = _clang_getSpecializedCursorTemplate(ref)
                                if tmpl_cur and not tmpl_cur.kind.is_invalid():
                                    tmpl_usr = tmpl_cur.get_usr()
                                    if tmpl_usr:
                                        usr = tmpl_usr
                            except Exception:
                                pass
                        off = cursor.location.offset
                        blen = len(name.encode('utf-8'))
                        # 跳过 offset 与源码不匹配的（宏展开内引用等）
                        if src_bytes[off:off + blen] == name.encode('utf-8'):
                            replacements.append((off, blen, usr))
    for child in cursor.get_children():
        walk_ast(child, tmppath, src_bytes, decl_map, replacements, decl_kinds, ref_kinds, member_usrs, virt_usrs, func_usrs, func_ranges)


def scan_tokens(
    tu: ci.TranslationUnit,
    tmppath: str,
    src_bytes: bytes,
    decl_map: dict,
    decl_kinds: frozenset,
    ref_kinds: frozenset,
    ci,
    member_usrs: set | None = None,
) -> list[tuple[int, int, str, str | None, bool, bool]]:
    """Token 扫描补全 pass：修正 AST walk 因宏展开 offset 错位而漏掉的符号位置。

    libclang 的 AST cursor 对宏参数的 location.offset 指向宏调用起始而非参数本身，
    导致 offset 校验失败、符号未进入 decl_map / replacements。
    token.location 是真实文本位置，此 pass 在 rename_map 建好前先收集候选。

    参数：
        decl_map — 可能被原地追加（补入宏内漏掉的 DECL）
        ci       — clang.cindex 模块（用于访问 CursorKind.DECL_STMT 等）

    返回：
        token_candidates 列表，元素为
        (offset: int, byte_len: int, tok_name: str, usr: str|None, is_member_access: bool,
         is_in_macro_body: bool)
        - usr=None 表示本 token 暂未匹配到已知 USR，留给后续策略2/3处理
        - is_member_access=True 表示该 token 前紧跟 . 或 ->，策略2/3 须跳过
        - is_in_macro_body=True 表示该 token 位于 #define 定义体中，策略3须跳过
    """
    token_candidates: list[tuple[int, int, str, str | None, bool, bool]] = []
    prev_tok_spelling = ''
    for token in tu.get_tokens(extent=tu.cursor.extent):
        if token.kind.name != 'IDENTIFIER':
            prev_tok_spelling = token.spelling   # 跟踪 . 和 -> 等标点符号
            continue
        
        # token所在的文件
        loc: ci.SourceLocation = token.location
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
        tok_name: str = token.spelling
        # 判断是否是成员访问（前面紧跟 . 或 ->），用于限制名字回退策略的误用范围
        is_member_access: bool = prev_tok_spelling in ('.', '->')
        prev_tok_spelling = tok_name
        if len(tok_name) < _MIN_RENAME_LEN:
            continue
        blen = len(tok_name.encode('utf-8'))
        # 判断偏移量和长度是否正确
        if src_bytes[off:off + blen] != tok_name.encode('utf-8'):
            continue
        cur = token.cursor
        # #include <header> 中的 header identifier：
        #   - `include` 关键字本身的 cursor.kind == INCLUSION_DIRECTIVE
        #   - `iostream`/`fstream` 等 header 名的 cursor.kind == INVALID_FILE
        # 两者均不应进入候选，用 is_invalid() 统一过滤（也防止策略2/3误匹配）
        if cur.kind.is_invalid() or cur.kind == ci.CursorKind.INCLUSION_DIRECTIVE:  # type: ignore
            continue
        # 检测当前 token 是否位于 #define 定义体中（cursor 指向 MACRO_DEFINITION）。
        # 宏定义体内的标识符如 xbuf 等是"约定名"而非 C++ 符号引用，
        # 策略3（最近 DECL_REF 推断）不适用，但策略2/2b 在名字唯一时仍可安全重命名。
        is_in_macro_body: bool = (cur.kind.name == 'MACRO_DEFINITION')
        usr: str | None = None
        # 策略1：cursor 精确匹配（无宏展开偏移问题时走这里）
        # 附加条件：cur.location.offset == off（cursor 必须确实指向本 token 所在位置）
        # 若 cursor 位于别处（如宏参数导致 libclang 将 cursor 指向另一函数的同名参数），
        # 说明 cursor 信息不可信，跳过本策略，防止错 USR 污染 decl_map。
        _is_func_cur = cur.kind.name in ('CXX_METHOD', 'FUNCTION_DECL', 'FUNCTION_TEMPLATE')
        _min_len_cur = _MIN_METHOD_RENAME_LEN if _is_func_cur else _MIN_RENAME_LEN
        # 与 walk_ast 保持一致：virtual 方法的 PARM_DECL 不加入 decl_map
        _is_virt_parm_cur = (cur.kind.name == 'PARM_DECL'
                             and cur.semantic_parent is not None
                             and cur.semantic_parent.is_virtual_method())
        if (not _is_virt_parm_cur
                and cur.kind in decl_kinds and cur.spelling == tok_name
                and len(tok_name) >= _min_len_cur
                and tok_name not in _PROTECTED_NAMES
                and not tok_name.startswith('operator')
                and cur.location.offset == off):  # cursor 必须指向本 token
            usr = cur.get_usr()
            # AST walk 因 offset 校验失败而漏掉的 DECL，在此补入 decl_map
            if usr and usr not in decl_map:
                decl_map[usr] = (tok_name, off, blen)
        # 策略1.5：cursor 为 DECL_STMT（宏内变量声明常见），向下找 VAR_DECL 子节点
        elif cur.kind == ci.CursorKind.DECL_STMT:
            for child in cur.get_children():
                if (child.kind in decl_kinds and child.spelling == tok_name
                        and tok_name not in _PROTECTED_NAMES
                        and not tok_name.startswith('operator')):
                    usr = child.get_usr()
                    if usr and usr not in decl_map:
                        decl_map[usr] = (tok_name, off, blen)
                    break
        elif cur.kind in ref_kinds:
            ref = cur.referenced
            if ref and ref.kind in decl_kinds and cur.spelling == tok_name:
                ref_usr = ref.get_usr()
                if ref_usr:
                    # 与 walk_ast 保持一致：检查是否为 virtual 方法的 PARM_DECL 引用。
                    # virtual 方法参数不重命名，设哨兵 USR 阻断策略2/3，
                    # 防止策略3（最近 DECL_REF 推断）跨函数边界归并到无关 USR。
                    _is_virt_parm_ref = (ref.kind.name == 'PARM_DECL'
                                         and ref.semantic_parent is not None
                                         and ref.semantic_parent.is_virtual_method())
                    if _is_virt_parm_ref:
                        usr = _VIRT_PARM_SENTINEL
                    else:
                        # 若 ref_usr 不在 decl_map，且 ref 是函数模板实例化，
                        # 尝试通过模板特化 API 找到模板声明 USR。
                        # 函数模板实例化（USR 含 '<'，如 F@setArg<#&$...>）libclang 会给出
                        # 独立于模板声明（FT@>1#TsetArg...）的 USR，导致 decl_map 出现两份条目。
                        # clang_getSpecializedCursorTemplate 可从实例化 cursor 走到模板声明 cursor。
                        # 保护条件：只对 FUNCTION_TEMPLATE/CXX_METHOD 类型且 USR 含 '<'（实例化特征）才调用
                        if (ref_usr not in decl_map
                                and '<' in ref_usr
                                and _clang_getSpecializedCursorTemplate is not None
                                and ref.kind.name in ('CXX_METHOD', 'FUNCTION_DECL', 'FUNCTION_TEMPLATE')):
                            try:
                                tmpl_cur = _clang_getSpecializedCursorTemplate(ref)
                                if tmpl_cur and not tmpl_cur.kind.is_invalid():
                                    tmpl_usr = tmpl_cur.get_usr()
                                    if tmpl_usr and tmpl_usr in decl_map:
                                        ref_usr = tmpl_usr
                            except Exception:
                                pass
                        # 仅当被引用 DECL 位于用户文件时才补入 decl_map，
                        # 避免把系统结构体字段（如 sockaddr_in6::sin6_family）纳入重命名
                        if ref_usr not in decl_map and _is_user_file(ref, tmppath):
                            decl_name = ref.spelling or tok_name
                            # 与 walk_ast 保持一致：排除受保护名字、operator 重载
                            # 对函数/方法应用更高的最小长度阈值
                            _is_func_ref = ref.kind.name in ('CXX_METHOD', 'FUNCTION_DECL', 'FUNCTION_TEMPLATE')
                            _min_len_ref = _MIN_METHOD_RENAME_LEN if _is_func_ref else _MIN_RENAME_LEN
                            if (len(decl_name) >= _min_len_ref
                                    and decl_name not in _PROTECTED_NAMES
                                    and not decl_name.startswith('operator')):
                                decl_map[ref_usr] = (decl_name, off, blen)
                        # usr 只在 decl_map 中存在时才设置，
                        # 防止系统字段 USR 进入 token_candidates 后被错误匹配
                        if ref_usr in decl_map:
                            usr = ref_usr
        # 兜底：is_member_access（obj->field/method 或 obj.field/method）且 usr 仍未解析时，
        # 限定在 member_usrs（字段 + 成员函数 USR 集合）中按名字唯一匹配，
        # 排除同名局部变量（VAR_DECL，不在 member_usrs 中）的歧义。
        # 覆盖两类 libclang 无法正常返回引用信息的情况：
        #   1. 模板类未实例化时，MEMBER_REF_EXPR 的 spelling='' 且 referenced=None
        #   2. 宏定义体中，token cursor 为 MACRO_DEFINITION 等，spelling 与 tok_name 不符
        # 唯一性约束：匹配到的符号 USR 有且仅有一个时才替换，防止同名字段/方法歧义。
        if usr is None and is_member_access and member_usrs:
            name_matches = [
                u for u, (n, _, _) in decl_map.items()
                if n == tok_name and u in member_usrs
            ]
            if len(name_matches) == 1:
                usr = name_matches[0]
        token_candidates.append((off, blen, tok_name, usr, is_member_access, is_in_macro_body))
    return token_candidates


def build_rename_map(
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


def merge_token_candidates(
    token_candidates: list,
    replacements: list,
    rename_map: dict,
    name_to_usr: dict,
    member_usrs: set | None = None,
    func_ranges: list | None = None,
    decl_map: dict | None = None,
) -> None:
    """将 token 候选列表合并进 replacements，应用策略2/3补全 AST walk 漏掉的位置。

    策略说明：
        策略1/1.5  cursor 精确匹配，token_candidates 中 usr 已设置，直接使用。
        策略2      名字单义回退（非成员访问）：tok_name 在 name_to_usr 中唯一对应一个 USR。
        策略2b     成员访问单义回退：同策略2，但额外确认 USR 属于 member_usrs（成员函数/字段）。
                   宏定义体（is_in_macro_body=True）也适用策略2b。
        策略3      最近 DECL_REF 推断（非成员访问，且非宏定义体）：
                   宏定义体内跳过，防止误匹配附近不相关的 VAR_DECL。
                   若提供了 func_ranges，则只在同函数体内查找，彻底防止跨函数边界污染。

    参数：
        token_candidates — scan_tokens 返回的候选列表（只读），元素为 6-tuple
        replacements     — 原地追加新的 (offset, byte_len, usr) 三元组
        rename_map       — USR → short_name，用于过滤无效 USR
        name_to_usr      — orig_name → USR（仅单义映射），供策略2/2b使用
        member_usrs      — 字段 + 成员函数 USR 集合，供策略2b使用
        func_ranges      — (start_off, end_off, func_usr) 列表（已排序），
                           供策略3查找候选时限制在同一函数体内使用
    """
    import bisect

    # 预处理 func_ranges：按 start_off 排序，供策略3二分查找当前 token 所在函数范围
    # func_ranges 中可能有嵌套（如局部函数/lambda），取最窄的包含区间
    _sorted_ranges: list | None = None
    _range_starts: list | None = None
    if func_ranges:
        _sorted_ranges = sorted(func_ranges, key=lambda x: (x[0], -(x[1] - x[0])))
        _range_starts = [r[0] for r in _sorted_ranges]

    def _get_func_range(off: int):
        """返回 off 所在的最窄函数范围 (start_off, end_off, func_usr)，若不在任何函数中则返回 None。"""
        if _sorted_ranges is None:
            return None
        # 找最后一个 start_off <= off 的区间
        idx = bisect.bisect_right(_range_starts, off) - 1 # type: ignore
        # 向前搜索：可能有多个区间 start_off <= off，取最窄的包含 off 的那个
        best = None
        best_size = float('inf')
        i = idx
        while i >= 0:
            r_start, r_end, r_usr = _sorted_ranges[i]
            if r_start > off:
                i -= 1
                continue
            if r_end >= off:
                size = r_end - r_start
                if size < best_size:
                    best_size = size
                    best = (r_start, r_end, r_usr)
            # 如果 r_start 已经比 best_start 还小很多，可以停止
            if best is not None and r_start < best[0] - best_size:
                break
            i -= 1
        return best

    # 建立名字 → [(offset, usr)] 的索引，供策略3（最近 DECL_REF 推断）使用
    ref_by_name: dict = {}
    for off, _blen, tok_name, usr, _ma, _imb in token_candidates:
        if usr is not None:
            ref_by_name.setdefault(tok_name, []).append((off, usr))

    ast_seen = {off for off, _, _ in replacements}
    for off, blen, tok_name, usr, is_member_access, is_in_macro_body in token_candidates:
        if off in ast_seen:
            continue  # AST walk 已覆盖，跳过
        if usr is None and not is_member_access:
            # 策略2：名字单义回退（非成员访问）
            usr = name_to_usr.get(tok_name)
        if usr is None and is_member_access and member_usrs:
            # 策略2b：成员访问单义回退
            # 当 name_to_usr 中有唯一 USR 且该 USR 属于 member_usrs 时安全重命名。
            # 典型场景：setArg/getArg 经重载分组后 decl_map 唯一，
            # 宏体内 (xBufRef).setArg(...) 无法通过策略1关联，但可通过此策略匹配。
            potential_usr = name_to_usr.get(tok_name)
            if potential_usr and potential_usr in member_usrs:
                usr = potential_usr
        if usr is None and not is_member_access and not is_in_macro_body and member_usrs and decl_map is not None:
            # 策略2c：FIELD_DECL 类作用域匹配（非成员访问、非宏定义体）
            # 宏调用里直接传递成员变量名（不带 this->）时，cursor 通常失效，策略1/2均无效。
            # 先通过函数范围推断当前 token 所在的类（取 func_usr 的类前缀），
            # 再匹配该类中同名的 FIELD_DECL。若全局唯一也安全匹配。
            field_matches = [
                u for u, (n, _, _) in decl_map.items()
                if n == tok_name and u in member_usrs
                and u in rename_map  # 仅匹配需要重命名的符号
            ]
            if len(field_matches) == 1:
                usr = field_matches[0]
            elif len(field_matches) > 1 and _sorted_ranges is not None:
                # 多个同名 FIELD_DECL：通过 func_usr 推断当前类，仅匹配该类成员
                func_info = _get_func_range(off)
                if func_info is not None:
                    _, _, cur_func_usr = func_info
                    # func_usr 格式: c:@S@ClassName@F@method... 或 c:@N@ns@S@ClassName@F@method...
                    # 提取 @F@ 之前的类前缀（FIELD_DECL 的 USR 是 class_prefix + @FI@fieldName）
                    at_f = cur_func_usr.find('@F@')
                    if at_f < 0:
                        at_f = cur_func_usr.find('@FT@')
                    if at_f >= 0:
                        class_prefix = cur_func_usr[:at_f]
                        class_matches = [u for u in field_matches if u.startswith(class_prefix + '@')]
                        if len(class_matches) == 1:
                            usr = class_matches[0]
        if usr is None and not is_member_access and not is_in_macro_body:
            # 策略3：最近 DECL_REF 推断（非成员访问）
            # 为什么需要策略3：宏调用内参数 token 的 libclang cursor 有时指向别处（展开偏移问题），
            # 导致策略1无法匹配。若 token 在 name_to_usr 中不唯一，策略2也无法处理。
            # 策略3查找最近的同名 token（有 USR）来推断当前 token 的 USR。
            #
            # 优先用函数范围限制：只在当前 token 所在的函数体内查找候选，
            # 彻底防止跨函数边界的错误归并。
            # 若函数范围不可用（func_ranges=None），则回退到 500 字节距离限制。
            candidates_for_name = ref_by_name.get(tok_name, [])
            if _sorted_ranges is not None:
                # 基于函数范围过滤候选
                func_range = _get_func_range(off)
                if func_range is not None:
                    f_start, f_end, _ = func_range
                    scoped = [(c_off, c_usr) for c_off, c_usr in candidates_for_name
                              if f_start <= c_off <= f_end and c_off != off]
                else:
                    # 不在任何函数体内（如全局变量初始化），不使用策略3
                    scoped = []
            else:
                # 回退：前后各 500 字节距离限制
                _STGY3_RANGE = 500
                scoped = [(c_off, c_usr) for c_off, c_usr in candidates_for_name
                          if abs(c_off - off) <= _STGY3_RANGE and c_off != off]
            if scoped:
                # 取最近的候选（优先前向，次后向）
                prev = [(c_off, c_usr) for c_off, c_usr in scoped if c_off < off]
                if prev:
                    usr = min(prev, key=lambda x: off - x[0])[1]
                else:
                    usr = min(scoped, key=lambda x: x[0] - off)[1]
        if usr and usr in rename_map:
            replacements.append((off, blen, usr))
            ast_seen.add(off)


def apply_replacements(
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


def golf_rename_symbols(code: str, rename_functions: bool = False, verbose: bool = False) -> str:
    """使用 libclang 对 C++ 代码做符号名压缩。

    重命名范围：局部变量、函数参数、结构体/类字段（仅用户代码中定义的）。
    当 rename_functions=True 时额外重命名用户定义的自由函数和成员函数。
    不重命名：类/结构体名、宏名、构造/析构函数名、标准库 / 系统头文件中的符号。
    """
    src_bytes = code.encode('utf-8')

    # 把代码写到临时文件，必须用二进制写，避免 Windows 上 \n→\r\n 导致 offset 错位
    with tempfile.NamedTemporaryFile(suffix='.cpp', mode='wb', delete=False) as f:
        f.write(src_bytes)
        tmppath = f.name

    try:
        index = ci.Index.create()

        # 用clang解析临时文件
        tu: ci.TranslationUnit = index.parse(
            tmppath,
            args=['-std=c++23', '-w', '-fno-spell-checking'] + _make_platform_args(),
            options=(
                ci.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD |
                ci.TranslationUnit.PARSE_INCOMPLETE
            ),
        )

        # 声明节点类型：这些节点是需要被重命名的符号定义位
        _DECL_KINDS = frozenset({
            ci.CursorKind.VAR_DECL,     # type: ignore
            ci.CursorKind.FIELD_DECL,   # type: ignore
            ci.CursorKind.PARM_DECL,    # type: ignore
            *(
                [
                    ci.CursorKind.FUNCTION_DECL,      # type: ignore  自由函数
                    ci.CursorKind.CXX_METHOD,          # type: ignore  成员函数
                    ci.CursorKind.FUNCTION_TEMPLATE,   # type: ignore  函数模板
                ]
                if rename_functions else []
            ),
        })
        # 引用节点类型：这些节点是已声明符号的使用位
        _REF_KINDS = frozenset({
            ci.CursorKind.MEMBER_REF_EXPR,  # type: ignore  # obj.field / obj->field
            ci.CursorKind.MEMBER_REF,       # type: ignore  # 构造函数初始化列表 : field(...)
            ci.CursorKind.DECL_REF_EXPR,    # type: ignore  # 局部变量、参数引用
        })

        # USR → (orig_name, first_decl_offset, name_byte_len)
        decl_map: dict = {}
        # (offset, byte_len, usr) 三元组，记录所有声明位和引用位
        replacements: list = []
        # FIELD_DECL 的 USR 集合，供 scan_tokens 成员访问兜底使用
        member_usrs: set = set()
        # virtual CXX_METHOD 的 USR 集合，用于按名字分组合并（使所有 override 同名）
        virt_usrs: set = set()
        # (usr, parent_usr) 集合，用于按 (名字, 父作用域) 分组合并同名重载函数
        func_usrs: set = set()
        # (start_off, end_off, func_usr) 列表，记录每个函数/方法的字节范围，
        # 供 merge_token_candidates 策略3限制只在同函数体内查找候选
        func_ranges: list = []

        # AST 遍历：收集所有用户文件中的声明和引用
        walk_ast(tu.cursor, tmppath, src_bytes, decl_map, replacements, _DECL_KINDS, _REF_KINDS, member_usrs, virt_usrs, func_usrs, func_ranges)

        # Token 扫描：补全 AST walk 因宏展开 offset 错位而漏掉的符号位置
        # 必须在虚函数合并前运行，以便 scan_tokens 仍能通过原始 USR 查到 decl_map 条目
        token_candidates = scan_tokens(tu, tmppath, src_bytes, decl_map, _DECL_KINDS, _REF_KINDS, ci, member_usrs)

        # 虚函数后处理：将同名 virtual USR 合并到同一代表 USR，
        # 确保基类/派生类声明及所有调用点都被映射到相同短名
        virt_remap: dict = {}
        if virt_usrs:
            virt_remap = _group_virt_usrs(virt_usrs, decl_map)
            if virt_remap:
                # 从 decl_map 中删除非代表 USR 的条目
                for old_usr in virt_remap:
                    decl_map.pop(old_usr, None)
                    member_usrs.discard(old_usr)
                # 将 replacements 中非代表 USR 全部替换为代表 USR
                replacements = [
                    (off, blen, virt_remap.get(usr, usr))
                    for off, blen, usr in replacements
                ]
                # 将 token_candidates 中非代表 USR 全部替换为代表 USR
                token_candidates = [
                    (off, blen, tok_name, virt_remap.get(usr, usr) if usr is not None else None, is_ma, imb)
                    for off, blen, tok_name, usr, is_ma, imb in token_candidates
                ]

        # 重载函数后处理：将同作用域内同名函数的所有 USR 合并到同一代表 USR，
        # 确保各重载声明及所有调用点都被映射到相同短名
        if rename_functions and func_usrs:
            overload_remap = _group_overload_usrs(func_usrs, decl_map)
            if overload_remap:
                for old_usr in overload_remap:
                    decl_map.pop(old_usr, None)
                    member_usrs.discard(old_usr)
                replacements = [
                    (off, blen, overload_remap.get(usr, usr))
                    for off, blen, usr in replacements
                ]
                token_candidates = [
                    (off, blen, tok_name, overload_remap.get(usr, usr) if usr is not None else None, is_ma, imb)
                    for off, blen, tok_name, usr, is_ma, imb in token_candidates
                ]

        if not decl_map:
            return code

        # 生成 USR→短名 映射，以及名字单义查找表（供策略2使用）
        rename_map, name_to_usr = build_rename_map(decl_map, replacements, code)

        if verbose:
            for usr, short in sorted(rename_map.items(), key=lambda kv: decl_map[kv[0]][1]):
                orig = decl_map[usr][0]
                print(f'[golf_rename] {orig} → {short}', file=_sys.stderr)

        # 后处理 name_to_usr：若某个名字在 token_candidates 里出现过 _VIRT_PARM_SENTINEL，
        # 说明该名字在 virtual 方法里也被用作参数（不重命名），不能通过策略2单义推断——
        # 否则 virtual 方法体内宏调用里的同名引用会被错误映射到其他函数的同名参数 USR。
        _virt_parm_names = {
            tok_name
            for _, _, tok_name, usr, _, _ in token_candidates
            if usr == _VIRT_PARM_SENTINEL
        }
        for vp_name in _virt_parm_names:
            name_to_usr.pop(vp_name, None)

        # 合并 token 候选：策略1已设 usr，策略2/3补全宏参数等漏掉的位置
        merge_token_candidates(token_candidates, replacements, rename_map, name_to_usr, member_usrs, func_ranges, decl_map)

        # 应用所有重命名替换（从后向前，保持 offset 正确）
        return apply_replacements(src_bytes, replacements, rename_map)

    # 删掉临时文件
    finally:
        try:
            os.unlink(tmppath)
        except OSError:
            pass
