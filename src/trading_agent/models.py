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
class Snapshot:
    """Latest quote and trade for one symbol. On the IEX feed the quote is IEX's own
    best bid/ask, not the national best (NBBO), so spreads can look wider than they are."""

    symbol: str
    quote_ts_utc: datetime  # time of the quote, or of the last trade if there is no quote
    bid: float | None
    ask: float | None
    bid_size: int | None
    ask_size: int | None
    last_price: float | None
    last_trade_utc: datetime | None
    prev_close: float | None

    @property
    def spread_bps(self) -> float | None:
        if not self.bid or not self.ask or self.ask < self.bid:
            return None
        mid = (self.bid + self.ask) / 2
        return (self.ask - self.bid) / mid * 10_000


@dataclass(frozen=True)
class Session:
    """One trading day's regular session (09:30-16:00 New York, earlier on half days)."""

    date: date
    open_utc: datetime
    close_utc: datetime
