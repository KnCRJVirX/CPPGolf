"""static_dedup.py — 用 libclang 对合并后代码中 static 函数/变量重复定义去重。

策略
----
1. 用 libclang（PARSE_INCOMPLETE）解析已合并代码，遍历 AST 收集 static 定义节点。
2. 按名字分组（C 不支持重载，同名 static 必然冲突）。
3. 对每组：第一个定义保留原名；后续定义：
   - 体哈希相同 → 删除（替换为等量换行符）。
   - 体哈希不同 → 冲突，重命名为 name__F{stem}，并在该定义所在**文件**的字符范围内
     同步替换所有调用点（使用 file_ranges 参数确定文件边界）。
     若未提供 file_ranges，则回退到"下一个同名定义起始 - 1"作为领地终点。
4. **迭代执行**：libclang 在首次 redefinition 报错后可能仅能看到前两个重复节点，
   每轮迭代处理当前可见的部分，循环直至收敛。
   使用文件边界领地可确保每文件最多需 1 轮迭代即可完全处理，避免无限循环。
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import tempfile


def _body_hash(src: bytes, start: int, end: int) -> str:
    text = src[start:end + 1].decode('utf-8', 'replace')
    body = re.sub(r'\s+', ' ', text).strip().encode()
    return hashlib.md5(body, usedforsecurity=False).hexdigest()


def _build_b2c(src_bytes: bytes) -> list[int]:
    """构建字节偏移 → str 字符偏移映射表。"""
    mapping = [0] * (len(src_bytes) + 1)
    ti = 0
    i = 0
    while i < len(src_bytes):
        b = src_bytes[i]
        step = 1 if b < 0x80 else (2 if b < 0xE0 else (3 if b < 0xF0 else 4))
        for j in range(step):
            if i + j < len(src_bytes):
                mapping[i + j] = ti
        i += step
        ti += 1
    mapping[len(src_bytes)] = ti
    return mapping


def _find_file_range(
    def_char: int,
    file_ranges: list[tuple[int, int, str]],
) -> tuple[int, int, str] | None:
    """返回 def_char 所属的文件范围 (start_char, end_char, stem)，未找到则 None。"""
    for start, end, stem in file_ranges:
        if start <= def_char < end:
            return (start, end, stem)
    return None


def _extract_fwd_decl(def_bytes: bytes, orig_name: str, new_name: str) -> str:
    """从函数定义字节中提取签名，构造 static 前向声明。"""
    depth = 0
    i = 0
    n = len(def_bytes)
    while i < n:
        c = def_bytes[i:i+1]
        if c == b'{' and depth == 0:
            sig = def_bytes[:i].decode('utf-8', 'replace').strip()
            sig = re.sub(r'\b' + re.escape(orig_name) + r'\b', new_name, sig)
            if not re.search(r'\bstatic\b', sig):
                sig = 'static ' + sig
            return sig + ';'
        elif c == b'(':
            depth += 1
        elif c == b')':
            depth -= 1
        i += 1
    return ''


def _collect_static_defs(
    src_bytes: bytes,
    tmppath: str,
    args: list[str],
) -> 'dict[str, list[tuple[int, int, str]]]':
    """用 libclang 解析临时文件，收集所有 static 定义节点。

    返回 by_name[name] = [(start_byte, end_byte, body_hash), ...]
    """
    import clang.cindex as ci  # noqa: PLC0415

    _STATIC = getattr(getattr(ci, 'StorageClass', None), 'STATIC', None)
    _DEF_KINDS: frozenset = frozenset({ci.CursorKind.FUNCTION_DECL,  # type: ignore
                                       ci.CursorKind.VAR_DECL})      # type: ignore
    _meth = getattr(ci.CursorKind, 'CXX_METHOD', None)
    if _meth:
        _DEF_KINDS = _DEF_KINDS | frozenset({_meth})

    index = ci.Index.create()
    tu = index.parse(
        tmppath, args=args,
        options=(ci.TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD
                 | ci.TranslationUnit.PARSE_INCOMPLETE),
    )

    by_name: dict[str, list[tuple[int, int, str]]] = {}

    def walk(cursor: 'ci.Cursor') -> None:
        loc = cursor.location
        if loc.file and loc.file.name == tmppath:
            if (cursor.kind in _DEF_KINDS
                    and cursor.is_definition()
                    and (_STATIC is None or cursor.storage_class == _STATIC)):
                try:
                    ext = cursor.extent
                    s, e = ext.start.offset, ext.end.offset
                    if e > s:
                        bh   = _body_hash(src_bytes, s, e)
                        name = cursor.spelling
                        by_name.setdefault(name, []).append((s, e, bh))
                except Exception:
                    pass
            for child in cursor.get_children():
                walk(child)
        else:
            for child in cursor.get_children():
                walk(child)

    walk(tu.cursor)
    return by_name


def _build_ops(
    by_name: 'dict[str, list[tuple[int, int, str]]]',
    src_bytes: bytes,
    src_len: int,
    b2c: list[int],
    file_ranges: 'list[tuple[int, int, str]] | None',
    verbose: bool,
) -> 'tuple[list, dict, int, int]':
    """根据 AST 结果构建操作列表和前向声明插入表。

    返回 (ops, insertions, removed, renamed)
    ops 元素: (s, e, kind, data)
      kind='blank'       → s/e 为字节偏移，data=None
      kind='rename_char' → s/e 为字符偏移（inclusive），data=(old, new)
      kind='rename'      → s/e 为字节偏移，data=(old, new)
    insertions[territory_start_char] = [(new_name, fwd_decl_text), ...]
    """
    ops: list[tuple[int, int, str, object]] = []
    insertions: dict[int, list[tuple[str, str]]] = {}
    removed = renamed = 0

    for name, occs in by_name.items():
        if len(occs) <= 1:
            continue
        occs.sort(key=lambda x: x[0])
        first_hash = occs[0][2]

        for idx, (s, e, bh) in enumerate(occs[1:], start=1):
            s_char = b2c[s]

            if bh == first_hash:
                ops.append((s, e, 'blank', None))
                removed += 1
                if verbose:
                    ln = src_bytes[:s].count(b'\n') + 1
                    print(f'[static_dedup] 删除重复：`{name}` 第 {ln} 行',
                          file=sys.stderr)
            else:
                fr = (_find_file_range(s_char, file_ranges)
                      if file_ranges else None)
                if fr is not None:
                    t_start_char, t_end_char, stem = fr
                    new_name = f'{name}__{stem}'
                    ops.append((t_start_char, t_end_char - 1,
                                'rename_char', (name, new_name)))
                    fwd = _extract_fwd_decl(src_bytes[s:e + 1], name, new_name)
                    if fwd:
                        insertions.setdefault(t_start_char, []).append(
                            (new_name, fwd))
                else:
                    next_start = (occs[idx + 1][0]
                                  if idx + 1 < len(occs) else src_len)
                    ln = src_bytes[:s].count(b'\n') + 1
                    new_name = f'{name}__L{ln}'
                    territory_end_byte = next_start - 1
                    if territory_end_byte >= s:
                        ops.append((s, territory_end_byte, 'rename', (name, new_name)))
                        fwd = _extract_fwd_decl(src_bytes[s:e + 1], name, new_name)
                        if fwd:
                            insertions.setdefault(b2c[s], []).append(
                                (new_name, fwd))

                renamed += 1
                if verbose:
                    ln2 = src_bytes[:s].count(b'\n') + 1
                    print(f'[static_dedup] 重命名冲突：`{name}` → `{new_name}` '
                          f'第 {ln2} 行', file=sys.stderr)

    return ops, insertions, removed, renamed


def _apply_ops(
    code: str,
    ops: 'list[tuple[int, int, str, object]]',
    insertions: 'dict[int, list[tuple[str, str]]]',
    b2c: list[int],
    src_len: int,
) -> str:
    """将 blank / rename 操作和前向声明插入应用到代码字符串，返回新代码。"""
    from collections import defaultdict  # noqa: PLC0415

    # ── 阶段 1：blank（保持字符总长度，非换行字符用空格替换）────────────────
    blank_ops_char: list[tuple[int, int]] = []
    rename_ops: list[tuple[int, int, str, str]] = []

    for s_or_char, e_or_char, kind, data in ops:
        if kind == 'blank':
            sc = b2c[s_or_char]
            ec = b2c[min(e_or_char, src_len - 1)] + 1
            blank_ops_char.append((sc, ec))
        elif kind == 'rename_char':
            sc, ec = s_or_char, e_or_char
            rename_ops.append((sc, ec, data[0], data[1]))  # type: ignore[index]
        else:  # 'rename'（字节偏移）
            sc = b2c[s_or_char]
            ec = b2c[min(e_or_char, src_len - 1)]
            rename_ops.append((sc, ec, data[0], data[1]))  # type: ignore[index]

    blank_ops_char.sort()
    merged_blanks: list[list[int]] = []
    for sc, ec in blank_ops_char:
        if merged_blanks and sc <= merged_blanks[-1][1]:
            merged_blanks[-1][1] = max(ec, merged_blanks[-1][1])
        else:
            merged_blanks.append([sc, ec])

    parts: list[str] = []
    prev = 0
    for sc, ec in merged_blanks:
        ec = min(ec, len(code))
        parts.append(code[prev:sc])
        parts.append(''.join('\n' if c == '\n' else ' ' for c in code[sc:ec]))
        prev = ec
    parts.append(code[prev:])
    code_after_blanks = ''.join(parts)

    # ── 阶段 2：rename，按领地分组从右到左应用 ───────────────────────────────
    territory_renames: dict[tuple[int, int], list[tuple[str, str]]] = defaultdict(list)
    for sc, ec, old, new in rename_ops:
        territory_renames[(sc, ec)].append((old, new))

    all_territories: set[tuple[int, int]] = set(territory_renames.keys())
    for t_sc in insertions:
        if not any(sc2 == t_sc for sc2, _ in territory_renames):
            all_territories.add((t_sc, t_sc))

    result = code_after_blanks
    for (sc, ec) in sorted(all_territories, key=lambda x: -x[0]):
        ec_excl = min(ec + 1, len(result)) if ec > sc else sc
        seg = result[sc:ec_excl]
        for old, new in territory_renames.get((sc, ec), []):
            seg = re.sub(r'\b' + re.escape(old) + r'\b', new, seg)
        fwd_lines: list[str] = []
        seen_fwd: set[str] = set()
        for new_nm, fwd_text in insertions.get(sc, []):
            if new_nm not in seen_fwd:
                fwd_lines.append(fwd_text)
                seen_fwd.add(new_nm)
        prefix = ('\n'.join(fwd_lines) + '\n') if fwd_lines else ''
        result = result[:sc] + prefix + seg + result[ec_excl:]

    return result


def _single_pass(
    code: str,
    lang: str,
    args: list[str],
    verbose: bool,
    file_ranges: list[tuple[int, int, str]] | None,
) -> tuple[str, int, int]:
    """执行一轮 AST 扫描 + 去重/重命名，返回 (新代码, 删除数, 重命名数)。"""
    src_bytes = code.encode('utf-8')
    src_len   = len(src_bytes)
    b2c       = _build_b2c(src_bytes)

    suffix = '.c' if lang == 'c' else '.cpp'
    fd, tmppath = tempfile.mkstemp(suffix=suffix)
    try:
        os.write(fd, src_bytes)
        os.close(fd)

        by_name = _collect_static_defs(src_bytes, tmppath, args)
        ops, insertions, removed, renamed = _build_ops(
            by_name, src_bytes, src_len, b2c, file_ranges, verbose
        )

        if not ops and not insertions:
            return code, 0, 0

        return _apply_ops(code, ops, insertions, b2c, src_len), removed, renamed

    finally:
        try:
            os.unlink(tmppath)
        except OSError:
            pass


def deduplicate_static_defs(
    code: str,
    lang: str = 'c++',
    extra_args: list[str] | None = None,
    verbose: bool = False,
    max_iterations: int = 20,
    file_ranges: list[tuple[int, int, str]] | None = None,
) -> str:
    """
    迭代去重直至收敛。

    Parameters
    ----------
    code        : 已合并的源代码文本
    lang        : 'c' 或 'c++'
    extra_args  : 额外 libclang 参数
    verbose     : 打印处理详情
    max_iterations : 最大迭代次数
    file_ranges : [(start_char, end_char, file_stem)] 每个输入文件在合并文本中的字符范围。
                  提供此参数时，冲突重命名的领地范围 = 该文件的全部内容，
                  从根本上解决"领地过大"导致的无限迭代问题。
    """
    try:
        import clang.cindex as ci  # noqa: PLC0415, F401
    except ImportError:
        print('[static_dedup] 警告：找不到 libclang，跳过去重', file=sys.stderr)
        return code

    suffix   = '.c' if lang == 'c' else '.cpp'
    std_flag = '-std=c17' if lang == 'c' else '-std=c++17'
    args = [std_flag, '-w', '-fno-spell-checking']
    if extra_args:
        args.extend(extra_args)

    total_removed = total_renamed = 0
    current = code
    # file_ranges 需要随每轮代码变化而更新（因为rename可能改变字符数）
    # 但 file_ranges 是基于原始合并代码的偏移，rename 后范围会偏移
    # 这里做一个近似：每轮迭代重新计算 file_ranges 的偏移调整
    # 简化处理：只在第一轮使用精确 file_ranges，后续轮迭代已经基本收敛
    current_file_ranges = file_ranges

    for iteration in range(max_iterations):
        new_code, removed, renamed = _single_pass(
            current, lang, args, verbose, current_file_ranges
        )
        total_removed += removed
        total_renamed += renamed
        if removed == 0 and renamed == 0:
            break  # 已收敛
        current = new_code
        # 第二轮起 file_ranges 近似使用 None（回退到行号领地），
        # 但此时大部分冲突已被第一轮处理，剩余的是之前被 libclang 遮蔽的
        if current_file_ranges is not None:
            current_file_ranges = None  # 后续轮用行号策略兜底
        if verbose:
            print(f'[static_dedup] 第 {iteration + 1} 轮：'
                  f'删除 {removed} 处，重命名 {renamed} 处', file=sys.stderr)

    if total_removed + total_renamed > 0:
        print(f'[static_dedup] 完成（{iteration + 1} 轮）：'
              f'共删除 {total_removed} 处，重命名 {total_renamed} 处',
              file=sys.stderr)

    return current
