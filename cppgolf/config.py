"""Configuration file support for cppgolf."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class FlattenCfgConfig:
    enabled: bool = False
    functions: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    helper: Path | None = None
    helper_includes: list[Path] = field(default_factory=list)


@dataclass(frozen=True)
class FlowerConfig:
    enabled: bool = False
    dead_code: bool = True
    declarations: bool = True
    functions: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    seed: int = 1
    dead_blocks_per_function: int = 1
    declaration_count: int = 24


@dataclass(frozen=True)
class CppGolfConfig:
    flatten_cfg: FlattenCfgConfig = field(default_factory=FlattenCfgConfig)
    flower: FlowerConfig = field(default_factory=FlowerConfig)


def load_config(path: Path | None) -> CppGolfConfig:
    """Load a cppgolf TOML config file."""
    if path is None:
        return CppGolfConfig()

    data = _load_toml(path)
    flatten_data = data.get("flatten_cfg", {})
    if not isinstance(flatten_data, dict):
        raise ValueError("[flatten_cfg] must be a table")
    flower_data = data.get("flower", {})
    if not isinstance(flower_data, dict):
        raise ValueError("[flower] must be a table")

    return CppGolfConfig(
        flatten_cfg=FlattenCfgConfig(
            enabled=bool(flatten_data.get("enabled", False)),
            functions=_as_str_list(flatten_data.get("functions", []), "flatten_cfg.functions"),
            exclude=_as_str_list(flatten_data.get("exclude", []), "flatten_cfg.exclude"),
            helper=_as_optional_path(flatten_data.get("helper"), "flatten_cfg.helper"),
            helper_includes=_as_path_list(flatten_data.get("helper_includes", []), "flatten_cfg.helper_includes"),
        ),
        flower=FlowerConfig(
            enabled=bool(flower_data.get("enabled", False)),
            dead_code=bool(flower_data.get("dead_code", True)),
            declarations=bool(flower_data.get("declarations", True)),
            functions=_as_str_list(flower_data.get("functions", []), "flower.functions"),
            exclude=_as_str_list(flower_data.get("exclude", []), "flower.exclude"),
            seed=_as_int(flower_data.get("seed", 1), "flower.seed", minimum=0),
            dead_blocks_per_function=_as_int(
                flower_data.get("dead_blocks_per_function", 1),
                "flower.dead_blocks_per_function",
                minimum=0,
            ),
            declaration_count=_as_int(flower_data.get("declaration_count", 24), "flower.declaration_count", minimum=0),
        ),
    )


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        import tomllib
    except ModuleNotFoundError:  # pragma: no cover - exercised only on Python 3.10
        import tomli as tomllib  # type: ignore[no-redef]

    with path.open("rb") as handle:
        loaded = tomllib.load(handle)
    if not isinstance(loaded, dict):
        raise ValueError("config root must be a TOML table")
    return loaded


def _as_str_list(value: Any, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{field_name} must be a list of strings")
        if item:
            result.append(item)
    return result


def _as_optional_path(value: Any, field_name: str) -> Path | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a string")
    if not value:
        return None
    return Path(value)


def _as_path_list(value: Any, field_name: str) -> list[Path]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field_name} must be a list of strings")
    result: list[Path] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{field_name} must be a list of strings")
        if item:
            result.append(Path(item))
    return result


def _as_int(value: Any, field_name: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")
    if value < minimum:
        raise ValueError(f"{field_name} must be >= {minimum}")
    return value
