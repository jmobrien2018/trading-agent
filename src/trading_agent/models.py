"""Plain data types shared by the broker client, storage and data-quality checks."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

TIMEFRAMES = ("1Min", "5Min", "15Min", "1Hour", "1Day")


@dataclass(frozen=True)
class Bar:
    symbol: str
    timeframe: str
    ts_utc: datetime  # bar start time, timezone-aware UTC
    open: float
    high: float
    low: float
    close: float
    volume: int
    trade_count: int | None = None
    vwap: float | None = None


@dataclass(frozen=True)
class Session:
    """One trading day's regular session (09:30-16:00 New York, earlier on half days)."""

    date: date
    open_utc: datetime
    close_utc: datetime
