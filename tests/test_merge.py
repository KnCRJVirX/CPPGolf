from __future__ import annotations

from pathlib import Path

from cppgolf.merge import build_macro_table, merge_files


def test_strip_include_guard_handles_trailing_code_after_guard(tmp_path: Path):
    guarded = tmp_path / "guarded.h"
    trailing = tmp_path / "after.h"

    guarded.write_text(
        "#ifndef GUARDED_H\n#define GUARDED_H\nint guarded_value();\n#endif\n#include \"after.h\"\n",
        encoding="utf-8",
    )
    trailing.write_text("int after_value();\n", encoding="utf-8")
    stripped = build_macro_table  # silence linter reuse import section
    from cppgolf.merge import strip_include_guard

    result = strip_include_guard(guarded.read_text(encoding="utf-8"))

    assert "#ifndef GUARDED_H" not in result
    assert "#endif" not in result
    assert 'include "after.h"' in result


def test_merge_preserves_include_guards_so_reincludes_can_expose_extra_sections(tmp_path: Path):
    header = tmp_path / "guarded.h"
    source = tmp_path / "main.cpp"

    header.write_text(
        "#ifndef GUARDED_H\n#define GUARDED_H\nint base();\n#endif\n"
        "#if defined(ENABLE_EXTRA) && !defined(GUARDED_EXTRA)\n#define GUARDED_EXTRA\nint extra();\n#endif\n",
        encoding="utf-8",
    )
    source.write_text(
        '#include "guarded.h"\n#define ENABLE_EXTRA 1\n#include "guarded.h"\n',
        encoding="utf-8",
    )

    merged = merge_files(source, [tmp_path], set(), [], {})

    assert merged.count('#include "guarded.h"') == 0
    assert merged.count("int base();") == 1
    assert "int extra();" in merged


def test_merge_resolves_defined_macros_from_cli_and_skips_inactive_include(tmp_path: Path):
    config = tmp_path / "config.h"
    magics = tmp_path / "magics.h"
    source = tmp_path / "main.cpp"

    config.write_text(
        "#if defined(BOTZONE_PIKAFISH_STANDALONE)\n#define USE_PEXT 1\n#endif\n",
        encoding="utf-8",
    )
    magics.write_text("int magics_table();\n", encoding="utf-8")
    source.write_text(
        '#include "config.h"\n#ifndef USE_PEXT\n#include "magics.h"\n#endif\nint ready();\n',
        encoding="utf-8",
    )

    merged = merge_files(
        source,
        [tmp_path],
        set(),
        [],
        build_macro_table(["BOTZONE_PIKAFISH_STANDALONE=1"]),
    )

    assert "#include \"magics.h\"" not in merged
    assert "int magics_table();" not in merged
    assert "#define USE_PEXT 1" in merged
    assert "int ready();" in merged


def test_merge_inlines_active_conditional_local_include(tmp_path: Path):
    source = tmp_path / "main.cpp"
    nested = tmp_path / "bitstream.h"

    nested.write_text("int nested_header();\n", encoding="utf-8")
    source.write_text(
        "#define FSE_STATIC_LINKING_ONLY 1\n"
        "#if defined(FSE_STATIC_LINKING_ONLY) && !defined(FSE_H_FSE_STATIC_LINKING_ONLY)\n"
        "#define FSE_H_FSE_STATIC_LINKING_ONLY\n"
        "#include \"bitstream.h\"\n"
        "#endif\n",
        encoding="utf-8",
    )

    merged = merge_files(source, [tmp_path], set(), [], {})

    assert "#include \"bitstream.h\"" not in merged
    assert "int nested_header();" in merged


def test_merge_can_preserve_resolved_conditional_blocks(tmp_path: Path):
    source = tmp_path / "main.cpp"
    source.write_text(
        "#if defined(ENABLE_FEATURE)\n"
        "int selected_branch();\n"
        "#else\n"
        "int pruned_branch();\n"
        "#endif\n",
        encoding="utf-8",
    )

    merged = merge_files(
        source,
        [tmp_path],
        set(),
        [],
        build_macro_table(["ENABLE_FEATURE=1"]),
        preserve_conditionals=True,
    )

    assert "#if defined(ENABLE_FEATURE)" in merged
    assert "#else" in merged
    assert "#endif" in merged
    assert "int selected_branch();" in merged
    assert "int pruned_branch();" in merged


def test_merge_keep_conditionals_still_inlines_local_includes(tmp_path: Path):
    header = tmp_path / "nested.h"
    source = tmp_path / "main.cpp"

    header.write_text("int nested_header();\n", encoding="utf-8")
    source.write_text(
        "#if defined(ENABLE_FEATURE)\n"
        '#include "nested.h"\n'
        "#else\n"
        "int fallback_branch();\n"
        "#endif\n",
        encoding="utf-8",
    )

    merged = merge_files(
        source,
        [tmp_path],
        set(),
        [],
        build_macro_table(["ENABLE_FEATURE=1"]),
        preserve_conditionals=True,
    )

    assert "#if defined(ENABLE_FEATURE)" in merged
    assert "int nested_header();" in merged
    assert '#include "nested.h"' not in merged
    assert "int fallback_branch();" in merged


def test_merge_respects_pragma_once_across_multiple_includes(tmp_path: Path):
    header = tmp_path / "shared.h"
    a = tmp_path / "a.cpp"
    b = tmp_path / "b.cpp"
    root = tmp_path / "main.cpp"

    header.write_text("#pragma once\nstruct SharedType {};\n", encoding="utf-8")
    a.write_text('#include "shared.h"\nint a();\n', encoding="utf-8")
    b.write_text('#include "shared.h"\nint b();\n', encoding="utf-8")
    root.write_text('#include "a.cpp"\n#include "b.cpp"\n', encoding="utf-8")

    merged = merge_files(root, [tmp_path], set(), [], {})

    assert merged.count("struct SharedType {};") == 1
    assert "int a();" in merged
    assert "int b();" in merged
