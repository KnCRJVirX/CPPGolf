# CPPGolf

`cppgolf` is a C/C++ multi-file merge and code-golf/minifier tool.

## Install

```bash
pip install cppgolf
```

`libclang`-backed features such as symbol renaming, type renaming, and static deduplication are optional at runtime. The base CLI and text transforms work without `clang.cindex`.

## CLI

```bash
cppgolf solution.cpp
cppgolf solution.cpp -o golf.cpp
cppgolf solution.cpp --merge-only -I include/
cppgolf solution.cpp -DDEBUG=1 --inject-define --merge-only
cppgolf solution.cpp -I include/ --rename-functions --stats
cppgolf solution.cpp -DBOTZONE_PIKAFISH_STANDALONE=1 -DZSTD_DISABLE_ASM
cppgolf solution.cpp --no-rename
cppgolf solution.cpp --config cppgolf.toml --flatten-cfg
```

### Common options

| Option | Meaning |
| --- | --- |
| `-o FILE` | Write output to a file instead of stdout. |
| `-I DIR` | Add an include search directory. Can be repeated. |
| `-D MACRO[=VALUE]` | Pass a macro definition to libclang-backed optional passes. Can be repeated. |
| `--inject-define` | Write `-D` macro definitions into the generated source code. |
| `--merge-only` | Only merge files. Skip all later transforms and compression passes. |
| `--no-merge` | Skip inlining `#include "..."`. |
| `--no-strip-comments` | Keep comments. |
| `--no-compress-ws` | Keep whitespace formatting. |
| `--no-std-ns` | Do not add `using namespace std;`. |
| `--no-typedefs` | Do not add shortcuts such as `ll` / `ld`. |
| `--no-rename` | Disable symbol renaming. |
| `--no-win-lean` | Do not inject Windows header conflict guards. |
| `--keep-main-return` | Keep the trailing `return 0;` in `main`. |
| `--keep-endl` | Keep `endl`. |
| `--keep-inline` | Keep `inline`. |
| `--dedup-statics` | Deduplicate merged `static` definitions with libclang. |
| `--aggressive` | Remove braces from single-statement `if` / `for` / `while`. |
| `--shortcuts` | Insert `#define` shortcuts for frequent `cout` / `cin`. |
| `--rename-functions` | Also rename user-defined functions and methods. |
| `--rename-type` | Add typedef aliases for long user-defined type names. |
| `--config PATH` | Read cppgolf options from a TOML config file. |
| `--cfg-helper PATH` | Explicitly use the required `cppgolf-cfg-helper` executable for CFG flattening. |
| `--cfg-helper-include PATH` | Add a system/cross-platform header directory used only by the CFG helper parser. Can be repeated. |
| `--flatten-cfg` | Flatten configured function bodies into a switch-based state machine. |
| `--flatten-cfg-function NAME` | Add an exact function name for control-flow flattening. |
| `--flower` | Insert both dead-code and declaration flowers. |
| `--flower-dead-code` | Insert only complex always-false dead-code blocks in function bodies. |
| `--flower-decls` | Insert only inert global/namespace/class declarations. |
| `--flower-function NAME` | Limit dead-code flower insertion to an exact function name. Can be repeated. |
| `--flower-seed N` | Set the deterministic flower seed. |
| `--flower-dead-blocks N` | Maximum dead-code flower blocks per function. |
| `--flower-decl-count N` | Maximum inert declarations to insert. |
| `--stats` | Print size reduction statistics to stderr. |

### Configuration

Control-flow flattening is an obfuscation pass and can increase code size. It is disabled by default and only touches configured functions.

```toml
[flatten_cfg]
enabled = true
helper = "build/cfg-helper/cppgolf-cfg-helper.exe"
helper_includes = ["C:/msys64/usr/include"]
functions = [
  "evaluate",
  "Stockfish::Search::Worker::search",
  "Stockfish::Tablebases::probe",
]
exclude = ["main"]
```

Flower insertion is also disabled by default. It uses the same `cppgolf-cfg-helper` parser as CFG flattening to find safe insertion points.

```toml
[flower]
enabled = true
dead_code = true
declarations = true
functions = ["target", "N::Box::run"]
exclude = ["main"]
seed = 1
dead_blocks_per_function = 1
declaration_count = 24
```

When `--flatten-cfg`, `[flatten_cfg].enabled`, `--flower`, or `[flower].enabled` is active, `cppgolf-cfg-helper` is required. `cppgolf` searches for it in this order: explicit `--cfg-helper` / Python API path, TOML `flatten_cfg.helper`, `CPPGOLF_CFG_HELPER`, `build/cfg-helper/cppgolf-cfg-helper(.exe)` under the current working directory, the same path under the package repository root, then `PATH`.

For cross-platform flattening, pass target headers to the helper parser with `--cfg-helper-include` or `flatten_cfg.helper_includes`. When an MSYS `/usr/include` directory such as `C:/msys64/usr/include` is provided, cppgolf automatically uses the matching MSYS GCC include bundle and `x86_64-pc-msys` target for helper parsing.

Build the helper with normal LLVM/Clang CMake package discovery:

```bash
cmake -S tools/cfg-helper -B build/cfg-helper -G Ninja \
  -DCMAKE_BUILD_TYPE=Release \
  -DLLVM_DIR=/path/to/llvm/lib/cmake/llvm \
  -DClang_DIR=/path/to/llvm/lib/cmake/clang
cmake --build build/cfg-helper --config Release
```

If LLVM is installed in a standard prefix, `CMAKE_PREFIX_PATH=/path/to/llvm` or `llvm-config --cmakedir` can also be used to locate the packages.

## Python API

```python
from pathlib import Path

from cppgolf import process

result, merged_size = process(
    Path("solution.cpp"),
    include_dirs=[],
    defines=["BOTZONE_PIKAFISH_STANDALONE=1"],
    inject_defines=False,
    merge_only=False,
    rename_symbols=False,
    flatten_cfg=False,
    flatten_cfg_functions=[],
    flower=False,
    flower_seed=1,
    cfg_helper_path=None,
)

print(merged_size)
print(result)
```

Individual passes can also be used directly:

```python
from cppgolf import compress_whitespace, strip_comments
from cppgolf.transforms import golf_typedefs

code = Path("a.cpp").read_text()
code = strip_comments(code)
code = golf_typedefs(code)
code = compress_whitespace(code)
```

## Features

- Merge local `#include "..."` files recursively and deduplicate top-level system headers.
- Strip comments while preserving strings, character literals, and raw strings.
- Apply safe default text transforms such as `std::` removal, typedef shortcuts, `endl` replacement, `inline` removal, and trailing `return 0;` removal in `main`.
- Compress whitespace at token granularity while preserving preprocessor line structure.
- Optionally rename symbols and user-defined types with libclang.
- Optionally flatten configured function bodies with the required Clang CFG helper.
