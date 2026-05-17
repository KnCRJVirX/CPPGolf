from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_import_cppgolf_package():
    module = importlib.import_module("cppgolf")
    assert module.__version__ == "0.1.10"
    assert callable(module.process)


def test_python_m_cppgolf_help():
    proc = subprocess.run(
        [sys.executable, "-m", "cppgolf", "--help"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "cppgolf" in proc.stdout
    assert "--no-rename" in proc.stdout
    assert "--define" in proc.stdout
    assert "--inject-define" in proc.stdout
    assert "--merge-only" in proc.stdout


def test_python_m_cppgolf_without_rename(tmp_path: Path):
    sample = tmp_path / "sample.cpp"
    sample.write_text(
        '#include <iostream>\nint main(){std::cout<<"hi"<<std::endl;return 0;}\n',
        encoding="utf-8",
    )

    proc = subprocess.run(
        [sys.executable, "-m", "cppgolf", str(sample), "--no-rename"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "using namespace std;" in proc.stdout
    assert 'cout<<"hi"' in proc.stdout
    assert '<<"\\n";' in proc.stdout


def test_python_m_cppgolf_merge_only(tmp_path: Path):
    header = tmp_path / "sum.h"
    source = tmp_path / "sample.cpp"
    header.write_text("#pragma once\ninline int add(int a,int b){return a+b;}\n", encoding="utf-8")
    source.write_text(
        '#include <iostream>\n#include "sum.h"\n// keep comment\nint main(){std::cout<<add(1,2)<<std::endl;return 0;}\n',
        encoding="utf-8",
    )

    proc = subprocess.run(
        [sys.executable, "-m", "cppgolf", str(source), "-I", str(tmp_path), "--merge-only"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "using namespace std;" not in proc.stdout
    assert "// keep comment" in proc.stdout
    assert "inline int add" in proc.stdout
    assert "std::cout<<add(1,2)<<std::endl" in proc.stdout


def test_python_m_cppgolf_inject_define(tmp_path: Path):
    source = tmp_path / "sample.cpp"
    source.write_text("int main(){return 0;}\n", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "cppgolf", str(source), "-DDEBUG=1", "--inject-define", "--merge-only"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("#define DEBUG 1\n")
