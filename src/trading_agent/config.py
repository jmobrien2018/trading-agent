"""Load non-secret settings from config/settings.toml (+ optional local override)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Live trading is deliberately not an accepted mode until the go-live gate (Phase 8).
ALLOWED_MODES = frozenset({"paper"})


class ConfigError(Exception):
    """Settings are missing or invalid."""


@dataclass(frozen=True)
class RiskLimits:
    max_position_usd: float
    max_daily_loss_usd: float
    max_open_positions: int
    max_trades_per_day: int


@dataclass(frozen=True)
class Settings:
    root: Path
    mode: str
    data_dir: Path
    log_dir: Path
    db_file: Path
    broker_name: str
    broker_base_url: str
    risk: RiskLimits


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def _require(raw: dict[str, Any], section: str, key: str) -> Any:
    try:
        return raw[section][key]
    except KeyError:
        raise ConfigError(f"Missing setting [{section}] {key}") from None


def _positive(name: str, value: Any, kind: type) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"[risk] {name} must be a number, got {value!r}")
    if value <= 0:
        raise ConfigError(f"[risk] {name} must be greater than 0, got {value!r}")
    if kind is int and value != int(value):
        raise ConfigError(f"[risk] {name} must be a whole number, got {value!r}")
    return kind(value)


def load_settings(root: Path | None = None) -> Settings:
    """Read config/settings.toml under `root`, overlaid with settings.local.toml if present."""
    root = (root or PROJECT_ROOT).resolve()
    base_file = root / "config" / "settings.toml"
    local_file = root / "config" / "settings.local.toml"

    if not base_file.is_file():
        raise ConfigError(f"Settings file not found: {base_file}")

    with base_file.open("rb") as f:
        raw = tomllib.load(f)
    if local_file.is_file():
        with local_file.open("rb") as f:
            raw = _merge(raw, tomllib.load(f))

    mode = _require(raw, "app", "mode")
    if mode not in ALLOWED_MODES:
        raise ConfigError(
            f"Mode {mode!r} is not allowed. Only {sorted(ALLOWED_MODES)} until the go-live gate is passed."
        )

    def resolve(p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else root / path

    risk = RiskLimits(
        max_position_usd=_positive("max_position_usd", _require(raw, "risk", "max_position_usd"), float),
        max_daily_loss_usd=_positive("max_daily_loss_usd", _require(raw, "risk", "max_daily_loss_usd"), float),
        max_open_positions=_positive("max_open_positions", _require(raw, "risk", "max_open_positions"), int),
        max_trades_per_day=_positive("max_trades_per_day", _require(raw, "risk", "max_trades_per_day"), int),
    )

    return Settings(
        root=root,
        mode=mode,
        data_dir=resolve(_require(raw, "paths", "data_dir")),
        log_dir=resolve(_require(raw, "paths", "log_dir")),
        db_file=resolve(_require(raw, "paths", "db_file")),
        broker_name=_require(raw, "broker", "name"),
        broker_base_url=_require(raw, "broker", "paper_base_url"),
        risk=risk,
    )
