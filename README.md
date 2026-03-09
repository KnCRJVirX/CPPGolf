# CPPGolf

C++ 多文件合并 + 代码高尔夫（压缩）工具，专为竞技编程场景设计。

## 安装

```bash
pip install cppgolf
```

## CLI 用法

```bash
cppgolf solution.cpp                       # 输出到 stdout
cppgolf solution.cpp -o golf.cpp           # 输出到文件
cppgolf solution.cpp -I include/ -o out.cpp
cppgolf solution.cpp --no-rename           # 不压缩符号
```

### 选项

| 选项 | 说明 |
|------|------|
| `-o FILE` | 输出文件（默认 stdout） |
| `-I DIR` | 追加 include 搜索目录（可多次） |
| `--no-merge` | 跳过 `#include "..."` 内联 |
| `--no-strip-comments` | 保留注释 |
| `--no-compress-ws` | 保留空白格式 |
| `--no-std-ns` | 不添加 `using namespace std` |
| `--no-typedefs` | 不添加 `ll`/`ld` 等类型宏 |
| `--no-rename` | 不对变量/成员名进行压缩 |
| `--keep-main-return` | 保留 `return 0;` |
| `--keep-endl` | 保留 `endl` |
| `--keep-inline` | 保留 `inline` 关键字 |
| `--aggressive` | 去除单语句 if/for/while 花括号 |
| `--shortcuts` | 高频 cout/cin → `#define` 缩写 |
| `--rename-function` | 压缩函数名到短名 |
| `--rename-type` | 压缩类型名到短名 |
| `--stats` | 显示压缩率统计 |

## Python API

```python
from cppgolf import process
from pathlib import Path

result = process(
    Path("solution.cpp"),
    include_dirs=[],          # 额外的 #include 搜索目录
    rename_symbols=True,
)
print(result)
```

也可单独使用各 pass：

```python
from cppgolf import strip_comments, compress_whitespace, golf_rename_symbols

code = open("a.cpp").read()
code = strip_comments(code)
code = golf_rename_symbols(code)
code = compress_whitespace(code)
```

## 功能说明

- **合并**：递归内联 `#include "..."` 本地头文件，去除 include guard / `#pragma once`，系统头去重
- **去注释**：状态机感知字符串，支持 `//`、`/* */`、原始字符串 `R"(...)"` 
- **语义压缩**：`std::` 消除、`long long→ll` 宏、`endl→"\n"`、去 `return 0;`、去 `inline`
- **空白压缩**：token 级最小化，代码压为单行，预处理行保留换行
- **符号重命名**：libclang 驱动，重命名用户自定义变量/参数/成员名，可选重命名函数名
