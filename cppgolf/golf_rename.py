"""
golf_rename.py — Pass 5: 符号名压缩（tree-sitter AST 驱动）
"""
import re
import itertools

from tree_sitter import Language, Parser
import tree_sitter_cpp as tscpp

_DECLARATOR_CONTAINERS = frozenset({
    'init_declarator', 'pointer_declarator', 'reference_declarator',
    'array_declarator', 'abstract_pointer_declarator',
    'abstract_reference_declarator', 'abstract_array_declarator',
})
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
    # common macros / built-ins that must not be shadowed
    'NULL', 'TRUE', 'FALSE', 'EOF', 'stdin', 'stdout', 'stderr',
})


def _gen_short_names():
    for length in itertools.count(1):
        for combo in itertools.product('abcdefghijklmnopqrstuvwxyz', repeat=length):
            yield ''.join(combo)


def _extract_declarator_id(node, want_field: bool):
    target_type = 'field_identifier' if want_field else 'identifier'
    if node.type == target_type:
        return node
    if node.type in _DECLARATOR_CONTAINERS:
        for ch in node.children:
            if ch.type in ('*', '**', '&', '&&', '=', '[', ']',
                           'const', 'volatile', 'restrict',
                           '__cdecl', '__stdcall', '__fastcall', '__thiscall',
                           'abstract_pointer_declarator',
                           'abstract_reference_declarator'):
                continue
            result = _extract_declarator_id(ch, want_field)
            if result:
                return result
    return None


class _RenameCtx:
    """封装一次重命名所需的全部状态与子方法。"""

    def __init__(self, src_bytes, tree):
        self.src = src_bytes
        self.tree = tree
        # 类型上下文
        self.user_struct_names: set = set()
        self.struct_field_types: dict = {}
        self.var_type_map: dict = {}
        self.typedef_map: dict = {}

    # ── 工具 ────────────────────────────────────────────────────────────
    def name_of(self, node) -> str:
        return self.src[node.start_byte:node.end_byte].decode('utf-8')

    def _get_primary_type_name(self, node) -> str | None:
        result = None
        for ch in node.children:
            if ch.type in ('type_identifier', 'primitive_type'):
                result = self.name_of(ch)
            elif ch.type == 'qualified_identifier':
                for sub in reversed(ch.children):
                    if sub.type in ('identifier', 'type_identifier'):
                        result = self.name_of(sub); break
            elif ch.type == 'ERROR':
                # tree-sitter 遇到宏（如 F_BEGIN）时会把真正的类型包进 ERROR 节点
                for sub in ch.children:
                    if sub.type in ('type_identifier', 'identifier'):
                        result = self.name_of(sub); break
        return result

    def _is_qid_name(self, node) -> bool:
        par = node.parent
        if not par or par.type != 'qualified_identifier':
            return False
        for ch in reversed(par.children):
            if ch.type != '::':
                return ch == node
        return False

    def _get_qid_scope_class(self, qid_node) -> str | None:
        for ch in qid_node.children:
            if ch.type == '::':
                break
            if ch.type in ('identifier', 'type_identifier', 'namespace_identifier'):
                return self.name_of(ch)
            elif ch.type == 'qualified_identifier':
                for sub in reversed(ch.children):
                    if sub.type in ('identifier', 'type_identifier', 'namespace_identifier'):
                        return self.name_of(sub)
                break
        return None

    # ── 步骤 0：构建类型上下文 ───────────────────────────────────────────
    def build_type_context(self):
        self._walk_types(self.tree.root_node)
        for alias, real in self.typedef_map.items():
            if real in self.user_struct_names:
                self.user_struct_names.add(alias)
                if real in self.struct_field_types and alias not in self.struct_field_types:
                    self.struct_field_types[alias] = self.struct_field_types[real]

    def _walk_types(self, node):
        nt = node.type
        if nt == 'type_definition':
            inner = None
            for ch in node.children:
                if ch.type in ('struct_specifier', 'class_specifier', 'union_specifier'):
                    for sub in ch.children:
                        if sub.type == 'type_identifier':
                            inner = self.name_of(sub); break
                    break
            if inner:
                for ch in node.children:
                    if ch.type == 'type_identifier' and self.name_of(ch) != inner:
                        self.typedef_map[self.name_of(ch)] = inner
                    elif ch.type in _DECLARATOR_CONTAINERS:
                        id_node = _extract_declarator_id(ch, False)
                        if id_node:
                            self.typedef_map[self.name_of(id_node)] = inner
        if nt in ('struct_specifier', 'class_specifier', 'union_specifier'):
            struct_name = None
            for ch in node.children:
                if ch.type == 'type_identifier':
                    struct_name = self.name_of(ch); break
            if struct_name and any(c.type == 'field_declaration_list' for c in node.children):
                self.user_struct_names.add(struct_name)
                fmap = self.struct_field_types.setdefault(struct_name, {})
                for ch in node.children:
                    if ch.type == 'field_declaration_list':
                        for fd in ch.children:
                            if fd.type != 'field_declaration':
                                continue
                            ftype = self._get_primary_type_name(fd)
                            for fc in fd.children:
                                if fc.type == 'field_identifier':
                                    fmap[self.name_of(fc)] = ftype
                                elif fc.type in _DECLARATOR_CONTAINERS or fc.type == 'init_declarator':
                                    id_node = _extract_declarator_id(fc, True)
                                    if id_node:
                                        fmap[self.name_of(id_node)] = ftype
                        break
        if nt in ('declaration', 'parameter_declaration'):
            vtype = self._get_primary_type_name(node)
            if vtype:
                for ch in node.children:
                    if ch.type == 'identifier':
                        self.var_type_map.setdefault(self.name_of(ch), vtype)
                    elif ch.type in _DECLARATOR_CONTAINERS or ch.type == 'init_declarator':
                        id_node = _extract_declarator_id(ch, False)
                        if id_node:
                            self.var_type_map.setdefault(self.name_of(id_node), vtype)
        for ch in node.children:
            self._walk_types(ch)

    # ── cast 类型提取 ────────────────────────────────────────────────────
    def _extract_cast_target_type(self, node) -> str | None:
        if node.type == 'call_expression':
            fn = node.children[0] if node.children else None
            if fn and fn.type == 'template_function':
                fn_name = None
                for ch in fn.children:
                    if ch.type == 'identifier':
                        fn_name = self.name_of(ch); break
                if fn_name in ('reinterpret_cast', 'static_cast', 'dynamic_cast', 'const_cast'):
                    for ch in fn.children:
                        if ch.type == 'template_argument_list':
                            for sub in ch.children:
                                if sub.type == 'type_descriptor':
                                    return self._get_primary_type_name(sub)
        if node.type == 'cast_expression':
            for ch in node.children:
                if ch.type == 'type_descriptor':
                    return self._get_primary_type_name(ch)
        if node.type in ('reinterpret_cast_expression', 'static_cast_expression',
                         'dynamic_cast_expression', 'const_cast_expression'):
            for ch in node.children:
                if ch.type == 'type_descriptor':
                    return self._get_primary_type_name(ch)
        return None

    def _extract_init_cast_type(self, decl_node, var_name) -> str | None:
        for ch in decl_node.children:
            if ch.type == 'init_declarator':
                id_nd = _extract_declarator_id(ch, False)
                if not id_nd or self.name_of(id_nd) != var_name:
                    continue
                for sub in ch.children:
                    t = self._extract_cast_target_type(sub)
                    if t:
                        return t
        return None

    # ── 作用域感知的变量类型查找 ─────────────────────────────────────────
    def _lookup_var_type_in_scope(self, identifier_node) -> str | None:
        var_name = self.name_of(identifier_node)
        node = identifier_node.parent
        while node is not None:
            if node.type == 'parameter_list':
                for param in node.children:
                    if param.type == 'parameter_declaration':
                        vtype = self._get_primary_type_name(param)
                        if vtype:
                            for ch in param.children:
                                if ch.type == 'identifier' and self.name_of(ch) == var_name:
                                    return vtype
                                elif ch.type in _DECLARATOR_CONTAINERS:
                                    id_nd = _extract_declarator_id(ch, False)
                                    if id_nd and self.name_of(id_nd) == var_name:
                                        return vtype
            if node.type in ('compound_statement', 'translation_unit',
                             'namespace_definition', 'function_definition'):
                for child in node.children:
                    if child.type == 'declaration':
                        vtype = self._get_primary_type_name(child)
                        matched = False
                        for ch in child.children:
                            if ch.type == 'identifier' and self.name_of(ch) == var_name:
                                matched = True; break
                            elif ch.type in _DECLARATOR_CONTAINERS or ch.type == 'init_declarator':
                                id_nd = _extract_declarator_id(ch, False)
                                if id_nd and self.name_of(id_nd) == var_name:
                                    matched = True; break
                        if matched:
                            if vtype:
                                return vtype
                            return self._extract_init_cast_type(child, var_name)
                # function_definition：参数列表不在祖先链上，需主动下探
                if node.type == 'function_definition':
                    for child in node.children:
                        if child.type in ('function_declarator', 'pointer_declarator',
                                          'reference_declarator'):
                            for sub in child.children:
                                if sub.type == 'parameter_list':
                                    for param in sub.children:
                                        if param.type != 'parameter_declaration':
                                            continue
                                        vtype = self._get_primary_type_name(param)
                                        if not vtype:
                                            continue
                                        for ch in param.children:
                                            if ch.type == 'identifier' and self.name_of(ch) == var_name:
                                                return vtype
                                            elif ch.type in _DECLARATOR_CONTAINERS:
                                                id_nd = _extract_declarator_id(ch, False)
                                                if id_nd and self.name_of(id_nd) == var_name:
                                                    return vtype
            # for-range loop 变量：for (Type var : range)
            if node.type == 'for_range_loop':
                loop_type = self._get_primary_type_name(node)  # 直接子节点里找 type_identifier
                found_first = False
                for ch in node.children:
                    if ch.type in (':', 'compound_statement'):
                        break
                    if ch.is_named and not found_first:
                        found_first = True   # 跳过类型说明符节点本身
                        continue
                    if ch.type == 'identifier' and self.name_of(ch) == var_name:
                        return loop_type
                    elif ch.type in _DECLARATOR_CONTAINERS:
                        id_nd = _extract_declarator_id(ch, False)
                        if id_nd and self.name_of(id_nd) == var_name:
                            return loop_type
            # 类/结构体成员字段（方法内访问 this->field 或其他成员变量）
            if node.type in ('struct_specifier', 'class_specifier', 'union_specifier'):
                for ch in node.children:
                    if ch.type == 'field_declaration_list':
                        for fd in ch.children:
                            if fd.type != 'field_declaration':
                                continue
                            vtype = self._get_primary_type_name(fd)
                            for fc in fd.children:
                                if fc.type == 'field_identifier' and self.name_of(fc) == var_name:
                                    return vtype
                                elif fc.type in _DECLARATOR_CONTAINERS or fc.type == 'init_declarator':
                                    id_nd = _extract_declarator_id(fc, True)
                                    if id_nd and self.name_of(id_nd) == var_name:
                                        return vtype
                        break
            node = node.parent
        return self.var_type_map.get(var_name)

    # ── 字段访问对象类型推断 ─────────────────────────────────────────────
    def _enclosing_class(self, node) -> str | None:
        """向上找最近的 class/struct/union 定义，返回其名字。"""
        n = node.parent
        while n is not None:
            if n.type in ('struct_specifier', 'class_specifier', 'union_specifier'):
                for ch in n.children:
                    if ch.type == 'type_identifier':
                        return self.name_of(ch)
            n = n.parent
        return None

    def _resolve_field_object_type(self, field_expr_node) -> str | None:
        if not field_expr_node.children:
            return None
        value_node = field_expr_node.children[0]
        vt = value_node.type
        td = self.typedef_map
        if vt == 'this':
            cls = self._enclosing_class(field_expr_node)
            return td.get(cls, cls) if cls else None
        if vt == 'identifier':
            t = self._lookup_var_type_in_scope(value_node)
            return td.get(t, t)
        elif vt == 'field_expression':
            parent_type = self._resolve_field_object_type(value_node)
            if parent_type and parent_type in self.struct_field_types:
                for ch in value_node.children:
                    if ch.type == 'field_identifier':
                        ft = self.struct_field_types[parent_type].get(self.name_of(ch))
                        return td.get(ft, ft) if ft else None
            return None
        elif vt == 'pointer_expression':
            for ch in value_node.children:
                if ch.type == 'identifier':
                    t = self._lookup_var_type_in_scope(ch)
                    return td.get(t, t)
        elif vt == 'subscript_expression':
            arr = value_node.children[0] if value_node.children else None
            if arr is None:
                return None
            if arr.type == 'identifier':
                t = self._lookup_var_type_in_scope(arr)
                return td.get(t, t) if t else None
            elif arr.type == 'field_expression':
                return self._resolve_field_object_type(arr)
        return None

    # ── 步骤 1：收集声明位节点 ────────────────────────────────────────────
    def collect_decl_nodes(self):
        local_decl: list = []
        member_decl: list = []

        def walk(node):
            nt = node.type
            if nt == 'declaration':
                for ch in node.children:
                    if ch.type == 'identifier':
                        local_decl.append(ch)
                    elif ch.type in _DECLARATOR_CONTAINERS or ch.type == 'init_declarator':
                        id_node = _extract_declarator_id(ch, False)
                        if id_node: local_decl.append(id_node)
                    elif ch.type == 'function_declarator':
                        decl_type = self._get_primary_type_name(node)
                        if decl_type and decl_type in self.user_struct_names:
                            for sub in ch.children:
                                if sub.type == 'identifier':
                                    local_decl.append(sub); break
            elif nt == 'parameter_declaration':
                for ch in node.children:
                    if ch.type == 'identifier':
                        local_decl.append(ch)
                    elif ch.type in _DECLARATOR_CONTAINERS:
                        id_node = _extract_declarator_id(ch, False)
                        if id_node: local_decl.append(id_node)
            elif nt == 'for_range_loop':
                found_type = False
                for ch in node.children:
                    if ch.type in (':', 'compound_statement'): break
                    if ch.is_named and not found_type:
                        found_type = True; continue
                    if ch.type == 'identifier':
                        local_decl.append(ch); break
                    elif ch.type in _DECLARATOR_CONTAINERS:
                        id_node = _extract_declarator_id(ch, False)
                        if id_node: local_decl.append(id_node)
                        break
            elif nt == 'field_declaration':
                for ch in node.children:
                    if ch.type == 'field_identifier':
                        member_decl.append(ch)
                    elif ch.type in _DECLARATOR_CONTAINERS or ch.type == 'init_declarator':
                        id_node = _extract_declarator_id(ch, True)
                        if id_node: member_decl.append(id_node)
            if nt == 'function_declarator':
                for ch in node.children:
                    if ch.type != 'identifier': walk(ch)
            else:
                for ch in node.children: walk(ch)

        walk(self.tree.root_node)
        return local_decl, member_decl

    # ── 步骤 3：统计频率 ──────────────────────────────────────────────────
    def count_freq(self, local_names, member_names) -> dict:
        freq: dict = {}
        def walk(node):
            if node.type == 'identifier':
                n = self.name_of(node)
                if n in local_names:
                    freq[n] = freq.get(n, 0) + 1
                elif n in member_names and self._is_qid_name(node):
                    scope_cls = self._get_qid_scope_class(node.parent)
                    real_cls = self.typedef_map.get(scope_cls, scope_cls) if scope_cls else None
                    if real_cls and real_cls in self.user_struct_names:
                        freq[n] = freq.get(n, 0) + 1
            elif node.type == 'field_identifier':
                n = self.name_of(node)
                if n in member_names: freq[n] = freq.get(n, 0) + 1
            elif node.type == 'type_identifier':
                n = self.name_of(node)
                if n in local_names:
                    par = node.parent
                    if (par and par.type == 'parameter_declaration'
                            and par.parent and par.parent.type == 'parameter_list'
                            and par.parent.parent and par.parent.parent.type == 'function_declarator'
                            and par.parent.parent.parent
                            and par.parent.parent.parent.type == 'declaration'):
                        freq[n] = freq.get(n, 0) + 1
            for ch in node.children: walk(ch)
        walk(self.tree.root_node)
        return freq

    # ── 步骤 5：收集替换位置 ──────────────────────────────────────────────
    def build_replacements(self, rename_map, local_names, member_names):
        replacements: list = []
        class_stack: list = []

        def walk(node):
            entered = False
            nt = node.type
            if nt in ('struct_specifier', 'class_specifier', 'union_specifier'):
                for ch in node.children:
                    if ch.type == 'type_identifier':
                        class_stack.append(self.name_of(ch)); entered = True; break

            if nt == 'identifier':
                n = self.name_of(node)
                if n in rename_map and n in local_names:
                    replacements.append((node.start_byte, node.end_byte, rename_map[n].encode()))
                elif n in rename_map and n in member_names and class_stack:
                    replacements.append((node.start_byte, node.end_byte, rename_map[n].encode()))
                elif n in rename_map and n in member_names and self._is_qid_name(node):
                    scope_cls = self._get_qid_scope_class(node.parent)
                    real_cls = self.typedef_map.get(scope_cls, scope_cls) if scope_cls else None
                    if real_cls and real_cls in self.user_struct_names:
                        replacements.append((node.start_byte, node.end_byte, rename_map[n].encode()))
            elif nt == 'type_identifier':
                n = self.name_of(node)
                if n in rename_map and n in local_names:
                    par = node.parent
                    if (par and par.type == 'parameter_declaration'
                            and par.parent and par.parent.type == 'parameter_list'
                            and par.parent.parent and par.parent.parent.type == 'function_declarator'
                            and par.parent.parent.parent
                            and par.parent.parent.parent.type == 'declaration'):
                        decl_type = self._get_primary_type_name(par.parent.parent.parent)
                        if decl_type and decl_type in self.user_struct_names:
                            replacements.append((node.start_byte, node.end_byte, rename_map[n].encode()))
            elif nt == 'field_identifier':
                n = self.name_of(node)
                if n in rename_map and n in member_names:
                    parent = node.parent
                    if parent and parent.type == 'field_expression':
                        obj_type = self._resolve_field_object_type(parent)
                        if obj_type and obj_type in self.user_struct_names:
                            replacements.append((node.start_byte, node.end_byte, rename_map[n].encode()))
                    else:
                        replacements.append((node.start_byte, node.end_byte, rename_map[n].encode()))

            for ch in node.children: walk(ch)
            if entered: class_stack.pop()

        walk(self.tree.root_node)
        return replacements

    # ── 步骤 6：应用替换 ──────────────────────────────────────────────────
    def apply(self, replacements) -> str:
        replacements.sort(key=lambda x: x[0], reverse=True)
        buf = bytearray(self.src)
        for start, end, new in replacements:
            buf[start:end] = new
        return buf.decode('utf-8')


# ─────────────────────────────────────────────────────────────────────────────
# 公开入口
# ─────────────────────────────────────────────────────────────────────────────
def golf_rename_symbols(code: str) -> str:
    _lang = Language(tscpp.language())

    src_bytes = code.encode('utf-8')
    parser = Parser(_lang)
    tree = parser.parse(src_bytes)

    ctx = _RenameCtx(src_bytes, tree)
    ctx.build_type_context()

    local_decl, member_decl = ctx.collect_decl_nodes()
    name_of = ctx.name_of

    local_names  = {name_of(n) for n in local_decl  if len(name_of(n)) >= _MIN_RENAME_LEN}
    member_names = {name_of(n) for n in member_decl if len(name_of(n)) >= _MIN_RENAME_LEN}
    if not local_names and not member_names:
        return code

    all_targets = local_names | member_names
    freq = ctx.count_freq(local_names, member_names)

    # 步骤 4：生成重命名映射
    all_existing = set(re.findall(r'\b[A-Za-z_]\w*\b', code))
    occupied = all_existing | _CXX_KEYWORDS
    rename_map: dict = {}
    gen = _gen_short_names()
    for original in sorted(all_targets, key=lambda x: -freq.get(x, 0)):
        short = next(gen)
        while short in occupied or short == original:
            short = next(gen)
        rename_map[original] = short
        occupied.add(short)

    replacements = ctx.build_replacements(rename_map, local_names, member_names)
    if not replacements:
        return code
    return ctx.apply(replacements)
