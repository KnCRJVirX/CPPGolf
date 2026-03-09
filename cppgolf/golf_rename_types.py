"""
golf_rename_types.py — 给长类型名（struct/class/enum/union）生成 typedef 短名并重命名引用。

策略：
  1. libclang 解析，找用户文件中所有用户自定义类型声明（名称长度 >= MIN_TYPE_LEN）。
  2. 为每个类型分配一个大写短名（A, B, ... AA, AB, ...），与变量重命名的小写短名不冲突。
  3. 在每个类型定义的 `}` 之后插入 `typedef OrigName ShortName;`。
  4. 只重命名 TYPE_REF / TEMPLATE_REF token，**不触碰** `struct X {` 定义行、
     前向声明 `struct X;`、继承列表中隐式出现的名字（这些 token 的 cursor 是 STRUCT_DECL /
     CLASS_DECL，而非 TYPE_REF）。
"""
from __future__ import annotations

import itertools
import os
import re
import sys
import tempfile
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass  # clang.cindex 仅在运行时导入

# 类型名最短重命名长度
_MIN_TYPE_LEN = 3

# 短名生成器：A, B, ..., Z, AA, AB, ... （大写，与变量小写短名互不干扰）
def _gen_type_short_names():
    for length in itertools.count(1):
        for combo in itertools.product('ABCDEFGHIJKLMNOPQRSTUVWXYZ', repeat=length):
            yield ''.join(combo)


# 不能用作类型短名的标识符
_CXX_KEYWORDS: frozenset[str] = frozenset({
    'if', 'else', 'for', 'while', 'do', 'switch', 'case', 'break',
    'continue', 'return', 'goto', 'try', 'catch', 'throw', 'new', 'delete',
    'class', 'struct', 'union', 'enum', 'namespace', 'template', 'typename',
    'public', 'private', 'protected', 'virtual', 'inline', 'static',
    'extern', 'const', 'volatile', 'mutable', 'friend', 'explicit',
    'operator', 'sizeof', 'alignof', 'decltype', 'typedef', 'using',
    'bool', 'char', 'short', 'int', 'long', 'float', 'double', 'void',
    'auto', 'true', 'false', 'nullptr', 'this',
    # Windows macros
    'BOOL', 'VOID', 'DWORD', 'WORD', 'BYTE', 'HANDLE',
    'TRUE', 'FALSE', 'NULL', 'LONG', 'LONGLONG', 'ULONGLONG',
    'ULONG',
})

# 用于标记不应重命名的位置的哨兵 USR
_SKIP_SENTINEL = '\x00skip\x00'


def golf_rename_types(
    code: str,
    lang: str = 'c++',
    extra_args: list[str] | None = None,
    verbose: bool = False,
) -> str:
    """为长类型名添加 typedef 短名，并重命名代码中所有 TYPE_REF 引用。"""
    try:
        import clang.cindex as ci  # type: ignore
    except ImportError:
        print('[golf_rename_types] 未找到 libclang，跳过类型重命名。', file=sys.stderr)
        return code

    src_bytes = code.encode('utf-8')

    args: list[str] = list(extra_args or [])
    args += ['-x', 'c++' if lang == 'c++' else 'c',
             '-std=c++17', '-w',
             '-D_WIN32', '-DWIN32', '-D_WIN64', '-DWIN64',
             '-D_HAS_STD_BYTE=0', '-DWIN32_LEAN_AND_MEAN']

    suffix = '.cpp' if lang == 'c++' else '.c'
    fd, tmppath = tempfile.mkstemp(suffix=suffix)
    try:
        os.write(fd, src_bytes)
        os.close(fd)

        index = ci.Index.create()
        tu = index.parse(
            tmppath, args=args,
            options=(ci.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
                     | ci.TranslationUnit.PARSE_INCOMPLETE),
        )

        # ── 1. 收集用户文件中的类型声明 ─────────────────────────────────────
        _TYPE_DECL_KINDS = frozenset({
            ci.CursorKind.STRUCT_DECL,       # type: ignore[attr-defined]
            ci.CursorKind.CLASS_DECL,        # type: ignore[attr-defined]
            ci.CursorKind.CLASS_TEMPLATE,    # type: ignore[attr-defined]
            ci.CursorKind.ENUM_DECL,         # type: ignore[attr-defined]
        })

        # usr → (name, def_start_byte, def_end_byte)
        type_decls: dict[str, tuple[str, int, int]] = {}

        def _collect(cursor: ci.Cursor) -> None:  # type: ignore[name-defined]
            loc = cursor.location
            if loc.file:
                try:
                    in_user_file = os.path.samefile(loc.file.name, tmppath)
                except OSError:
                    in_user_file = False

                if in_user_file and cursor.kind in _TYPE_DECL_KINDS:
                    name: str = cursor.spelling
                    usr: str = cursor.get_usr() or ''
                    if (name
                            and len(name) >= _MIN_TYPE_LEN
                            and usr
                            and cursor.is_definition()):
                        try:
                            start_off = cursor.extent.start.offset
                            end_off = cursor.extent.end.offset
                            if usr not in type_decls:
                                type_decls[usr] = (name, start_off, end_off)
                        except Exception:
                            pass

            for child in cursor.get_children():
                _collect(child)

        _collect(tu.cursor)

        if not type_decls:
            return code

        # ── 2. 分配短名 ──────────────────────────────────────────────────────
        all_existing: set[str] = set(re.findall(r'\b[A-Za-z_]\w*\b', code))
        occupied: set[str] = all_existing | _CXX_KEYWORDS

        # 按定义位置排序，靠前的定义优先分配最短名
        sorted_usrs = sorted(type_decls.keys(), key=lambda u: type_decls[u][1])

        rename_map: dict[str, str] = {}  # usr → short_name
        gen = _gen_type_short_names()
        for usr in sorted_usrs:
            orig = type_decls[usr][0]
            short = next(gen)
            while short in occupied or short == orig:
                short = next(gen)
            rename_map[usr] = short
            occupied.add(short)
            if verbose:
                print(f'[golf_rename_types] {orig} → {short}', file=sys.stderr)

        # ── 3. 扫描所有 TOKEN，收集 TYPE_REF / TEMPLATE_REF 引用位置 ─────────
        _TYPE_REF_KINDS = frozenset({
            ci.CursorKind.TYPE_REF,         # type: ignore[attr-defined]
        })
        # 也处理 TEMPLATE_REF（模板类名引用，如 vector<MyClass>）
        try:
            _TMPL_REF = ci.CursorKind.TEMPLATE_REF  # type: ignore[attr-defined]
            _TYPE_REF_KINDS = _TYPE_REF_KINDS | frozenset({_TMPL_REF})
        except AttributeError:
            pass

        # 预建「USR → (start_off, end_off)」表，用于自身范围排除
        _self_extents: dict[str, tuple[int, int]] = {
            u: (v[1], v[2]) for u, v in type_decls.items()
        }

        # replacements: list of (offset, byte_len, new_name)
        replacements: list[tuple[int, int, str]] = []

        for token in tu.get_tokens(extent=tu.cursor.extent):
            if token.kind.name != 'IDENTIFIER':
                continue
            loc = token.location
            if not loc.file:
                continue
            try:
                if not os.path.samefile(loc.file.name, tmppath):
                    continue
            except OSError:
                continue

            off: int = loc.offset
            tok_name: str = token.spelling
            blen = len(tok_name.encode('utf-8'))
            if src_bytes[off:off + blen] != tok_name.encode('utf-8'):
                continue

            cur = token.cursor
            if cur.kind not in _TYPE_REF_KINDS:
                # 重要：跳过 STRUCT_DECL / CLASS_DECL / CLASS_TEMPLATE / ENUM_DECL token，
                # 即 `struct X {`、`class X :`、`struct X;` 中的 X — 保持原定义名不变
                continue

            # 解析引用目标 USR
            ref = cur.referenced
            if ref is None:
                continue
            ref_usr = ref.get_usr() or ''
            if not ref_usr:
                continue

            # 只处理我们要重命名的类型
            if ref_usr not in rename_map:
                continue

            # 跳过类型自身 extent 内的 TYPE_REF（如方法返回类型 `Stub& fn()`）。
            # typedef 插在定义之后，类体内自引用时短名尚未定义，保留原名即可。
            self_start, self_end = _self_extents.get(ref_usr, (-1, -1))
            if self_start <= off <= self_end:
                continue

            short = rename_map[ref_usr]
            replacements.append((off, blen, short))

        if not replacements and not rename_map:
            return code

        # ── 4. 构建插入表（typedef 声明，紧跟定义 `}` 之后的第一个 `;`）──
        # inserts: list of (byte_offset_after_semicolon, text_to_insert)
        inserts: list[tuple[int, str]] = []
        for usr, (orig_name, _start_off, end_off) in type_decls.items():
            if usr not in rename_map:
                continue
            short = rename_map[usr]
            # 从 end_off 向后找第一个 ';'，插入到 ';' 之后
            pos = end_off
            while pos < len(src_bytes) and src_bytes[pos:pos + 1] not in (b';', b'}'):
                pos += 1
            if pos < len(src_bytes):
                insert_pos = pos + 1  # after ';' or '}'
            else:
                insert_pos = len(src_bytes)
            typedef_text = f'\ntypedef {orig_name} {short};'
            inserts.append((insert_pos, typedef_text))

        # ── 5. 合并所有编辑操作，按偏移从大到小逐个应用（不影响前面的偏移）──
        # 将 replacements 和 inserts 合并，统一以 byte offset 排序后从右往左处理
        # replacements → (off, blen, new_text, False)  False=替换
        # inserts      → (off, 0,    new_text, True)   True=纯插入
        all_ops: list[tuple[int, int, str, bool]] = []
        for off, blen, new_name in replacements:
            all_ops.append((off, blen, new_name, False))
        for insert_off, text in inserts:
            all_ops.append((insert_off, 0, text, True))

        all_ops.sort(key=lambda x: (-x[0], x[3]))  # 从大到小；同 offset 时先插入后替换

        result_bytes = bytearray(src_bytes)
        for off, blen, new_text, is_insert in all_ops:
            new_enc = new_text.encode('utf-8')
            if is_insert:
                result_bytes[off:off] = new_enc
            else:
                result_bytes[off:off + blen] = new_enc

        result = result_bytes.decode('utf-8', errors='replace')

        # ── 6. 修正 using NS::OrigName; 声明 ─────────────────────────────────
        # libclang 中 `using A::B;` 的 B token cursor kind 是 USING_DECLARATION，
        # 不在 _TYPE_REF_KINDS 内，因此步骤5的 token 扫描不会覆盖它。
        # 用 regex 后处理：将 using ...::OrigName; 替换为 using ...::ShortName;
        orig_to_short: dict[str, str] = {
            type_decls[u][0]: rename_map[u] for u in rename_map if u in type_decls
        }
        def _replace_using(m: 're.Match') -> str:
            prefix = m.group(1)   # "using A::B::"
            name   = m.group(2)   # "OrigName"
            tail   = m.group(3)   # ";"
            return prefix + orig_to_short.get(name, name) + tail

        result = re.sub(
            r'(\busing\s+(?:\w+::)+)([A-Za-z_]\w*)([ \t]*;)',
            _replace_using,
            result,
        )

        return result

    finally:
        try:
            os.unlink(tmppath)
        except OSError:
            pass
