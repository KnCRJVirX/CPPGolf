from __future__ import annotations

from pathlib import Path

import pytest

from cppgolf.config import load_config


def test_load_config_reads_flatten_cfg(tmp_path: Path):
    config_path = tmp_path / "cppgolf.toml"
    config_path.write_text(
        '[flatten_cfg]\n'
        'enabled = true\n'
        'functions = ["evaluate", "Stockfish::Tablebases::probe"]\n'
        'exclude = ["main"]\n',
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.flatten_cfg.enabled is True
    assert config.flatten_cfg.functions == ["evaluate", "Stockfish::Tablebases::probe"]
    assert config.flatten_cfg.exclude == ["main"]
    assert config.flatten_cfg.helper is None
    assert config.flatten_cfg.helper_includes == []


def test_load_config_reads_flatten_cfg_helper(tmp_path: Path):
    config_path = tmp_path / "cppgolf.toml"
    config_path.write_text(
        '[flatten_cfg]\n'
        'enabled = true\n'
        'helper = "build/cfg-helper/cppgolf-cfg-helper.exe"\n',
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.flatten_cfg.helper == Path("build/cfg-helper/cppgolf-cfg-helper.exe")


def test_load_config_reads_flatten_cfg_helper_includes(tmp_path: Path):
    config_path = tmp_path / "cppgolf.toml"
    config_path.write_text(
        '[flatten_cfg]\n'
        'helper_includes = ["C:/msys64/usr/include", "C:/msys64/usr/lib/gcc/x/include"]\n',
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.flatten_cfg.helper_includes == [
        Path("C:/msys64/usr/include"),
        Path("C:/msys64/usr/lib/gcc/x/include"),
    ]


def test_load_config_reads_flower(tmp_path: Path):
    config_path = tmp_path / "cppgolf.toml"
    config_path.write_text(
        '[flower]\n'
        'enabled = true\n'
        'dead_code = true\n'
        'declarations = false\n'
        'functions = ["target", "N::Box::run"]\n'
        'exclude = ["main"]\n'
        'seed = 7\n'
        'dead_blocks_per_function = 2\n'
        'declaration_count = 5\n',
        encoding="utf-8",
    )

    config = load_config(config_path)

    assert config.flower.enabled is True
    assert config.flower.dead_code is True
    assert config.flower.declarations is False
    assert config.flower.functions == ["target", "N::Box::run"]
    assert config.flower.exclude == ["main"]
    assert config.flower.seed == 7
    assert config.flower.dead_blocks_per_function == 2
    assert config.flower.declaration_count == 5


def test_load_config_rejects_non_list_functions(tmp_path: Path):
    config_path = tmp_path / "cppgolf.toml"
    config_path.write_text('[flatten_cfg]\nfunctions = "evaluate"\n', encoding="utf-8")

    with pytest.raises(ValueError, match="flatten_cfg.functions"):
        load_config(config_path)


def test_load_config_rejects_invalid_flower_count(tmp_path: Path):
    config_path = tmp_path / "cppgolf.toml"
    config_path.write_text("[flower]\ndeclaration_count = -1\n", encoding="utf-8")

    with pytest.raises(ValueError, match="flower.declaration_count"):
        load_config(config_path)
