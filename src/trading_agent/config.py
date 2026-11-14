"""Load non-secret settings from config/settings.toml (+ optional local override)."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .models import TIMEFRAMES

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SYMBOL_RE = re.compile(r"[A-Z][A-Z0-9.]{0,9}")

# Live trading is deliberately not an accepted mode until the go-live gate (Phase 8).
ALLOWED_MODES = frozenset({"paper"})
PAPER_TRADING_HOST = "paper-api.alpaca.markets"
DATA_FEEDS = frozenset({"iex", "sip"})
ADJUSTMENTS = frozenset({"raw", "split", "dividend", "all"})


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
    data_base_url: str
    data_feed: str  # 1-minute (intraday) bars and quotes
    daily_feed: str  # daily bars
    data_adjustment: str  # 1-minute bars
    daily_adjustment: str  # daily bars
    watchlist: tuple[str, ...]
    history_daily_years: int
    history_minute_days: int
    refresh_timeframes: tuple[str, ...]
    min_coverage: float
    stale_minutes: int
    backtest_capital: float
    backtest_costs_bp: tuple[float, ...]
    holdout_start: date
    backtest_position_cap_pct: float
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


def _positive(name: str, value: Any, kind: type, section: str = "risk") -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"[{section}] {name} must be a number, got {value!r}")
    if value <= 0:
        raise ConfigError(f"[{section}] {name} must be greater than 0, got {value!r}")
    if kind is int and value != int(value):
        raise ConfigError(f"[{section}] {name} must be a whole number, got {value!r}")
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

    broker_base_url = _require(raw, "broker", "paper_base_url")
    if urlparse(broker_base_url).hostname != PAPER_TRADING_HOST:
        raise ConfigError(f"[broker] paper_base_url must point at {PAPER_TRADING_HOST}, got {broker_base_url!r}")

    feed = _require(raw, "data", "feed")
    if feed not in DATA_FEEDS:
        raise ConfigError(f"[data] feed must be one of {sorted(DATA_FEEDS)}, got {feed!r}")
    daily_feed = _require(raw, "data", "daily_feed")
    if daily_feed not in DATA_FEEDS:
        raise ConfigError(f"[data] daily_feed must be one of {sorted(DATA_FEEDS)}, got {daily_feed!r}")
    adjustment = _require(raw, "data", "adjustment")
    if adjustment not in ADJUSTMENTS:
        raise ConfigError(f"[data] adjustment must be one of {sorted(ADJUSTMENTS)}, got {adjustment!r}")
    daily_adjustment = _require(raw, "data", "daily_adjustment")
    if daily_adjustment not in ADJUSTMENTS:
        raise ConfigError(f"[data] daily_adjustment must be one of {sorted(ADJUSTMENTS)}, got {daily_adjustment!r}")

    timeframes = _require(raw, "refresh", "timeframes")
    if (
        isinstance(timeframes, str)
        or not isinstance(timeframes, list)
        or not timeframes
        or any(tf not in TIMEFRAMES for tf in timeframes)
    ):
        raise ConfigError(f"[refresh] timeframes must be a non-empty list from {list(TIMEFRAMES)}, got {timeframes!r}")
    min_coverage = _positive("min_coverage", _require(raw, "refresh", "min_coverage"), float, "refresh")
    if min_coverage > 1:
        raise ConfigError(f"[refresh] min_coverage is a fraction from 0 to 1, got {min_coverage!r}")

    costs = _require(raw, "backtest", "costs_bp")
    if isinstance(costs, str) or not isinstance(costs, list) or not costs or any(
        isinstance(c, bool) or not isinstance(c, (int, float)) or c < 0 for c in costs
    ):
        raise ConfigError(f"[backtest] costs_bp must be a non-empty list of numbers >= 0, got {costs!r}")
    holdout = _require(raw, "backtest", "holdout_start")
    try:
        holdout_start = holdout if isinstance(holdout, date) else date.fromisoformat(str(holdout))
    except ValueError:
        raise ConfigError(f"[backtest] holdout_start must be a date like 2025-11-14, got {holdout!r}") from None

    return Settings(
        root=root,
        mode=mode,
        data_dir=resolve(_require(raw, "paths", "data_dir")),
        log_dir=resolve(_require(raw, "paths", "log_dir")),
        db_file=resolve(_require(raw, "paths", "db_file")),
        broker_name=_require(raw, "broker", "name"),
        broker_base_url=broker_base_url,
        data_base_url=_require(raw, "broker", "data_base_url"),
        data_feed=feed,
        daily_feed=daily_feed,
        data_adjustment=adjustment,
        daily_adjustment=daily_adjustment,
        watchlist=parse_symbols(_require(raw, "watchlist", "symbols")),
        history_daily_years=_positive("daily_years", _require(raw, "history", "daily_years"), int, "history"),
        history_minute_days=_positive("minute_days", _require(raw, "history", "minute_days"), int, "history"),
        refresh_timeframes=tuple(dict.fromkeys(timeframes)),
        min_coverage=min_coverage,
        stale_minutes=_positive("stale_minutes", _require(raw, "refresh", "stale_minutes"), int, "refresh"),
        backtest_capital=_positive("capital", _require(raw, "backtest", "capital"), float, "backtest"),
        backtest_costs_bp=tuple(float(c) for c in costs),
        holdout_start=holdout_start,
        backtest_position_cap_pct=_cap_pct(_require(raw, "backtest", "position_cap_pct")),
        risk=risk,
    )


def _cap_pct(value: Any) -> float:
    pct = _positive("position_cap_pct", value, float, "backtest")
    if pct > 100:
        raise ConfigError(f"[backtest] position_cap_pct is a percent from 0 to 100, got {value!r}")
    return pct


def parse_symbols(value: Any) -> tuple[str, ...]:
    """Validate and normalise a list of ticker symbols (uppercase, de-duplicated, order kept)."""
    if isinstance(value, str) or not isinstance(value, (list, tuple)) or not value:
        raise ConfigError(f"Symbols must be a non-empty list, got {value!r}")
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not _SYMBOL_RE.fullmatch(item.strip().upper()):
            raise ConfigError(f"Invalid symbol {item!r}")
        sym = item.strip().upper()
        if sym not in out:
            out.append(sym)
    return tuple(out)
