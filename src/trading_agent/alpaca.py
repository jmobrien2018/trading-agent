"""Thin client for Alpaca's REST APIs: paper trading account + market data. Read-only for now.

Paper only: the client refuses any trading URL that isn't Alpaca's paper endpoint.
No order methods exist yet; orders arrive in Phase 6, behind the risk engine (Phase 4).
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import date, datetime, time as dtime, timezone
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx

from .config import PAPER_TRADING_HOST, Settings
from .credentials import Secret, get_secret
from .models import TIMEFRAMES, Bar, Session, Snapshot

log = logging.getLogger(__name__)

NEW_YORK = ZoneInfo("America/New_York")
MAX_PAGE_SIZE = 10_000
_EXTRA_DIGITS = re.compile(r"(\.\d{6})\d+")


class AlpacaError(Exception):
    """An Alpaca request failed in a way the caller should report."""


def _utc_iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    # Alpaca sends up to nanoseconds ("...:05.123456789Z"); Python keeps microseconds.
    text = _EXTRA_DIGITS.sub(r"\1", value.replace("Z", "+00:00"))
    ts = datetime.fromisoformat(text)
    return (ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


def _num(value: Any, kind: type) -> Any:
    return None if value is None else kind(value)


def _parse_hhmm(value: str) -> dtime:
    value = value.replace(":", "")
    return dtime(int(value[:2]), int(value[2:4]))


class AlpacaClient:
    def __init__(
        self,
        key_id: Secret,
        secret_key: Secret,
        trading_url: str,
        data_url: str,
        *,
        transport: httpx.BaseTransport | None = None,
        max_retries: int = 5,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 30.0,
    ) -> None:
        if urlparse(trading_url).hostname != PAPER_TRADING_HOST:
            raise AlpacaError(f"Refusing non-paper trading URL {trading_url!r}")
        headers = {
            "APCA-API-KEY-ID": key_id.reveal(),
            "APCA-API-SECRET-KEY": secret_key.reveal(),
            "Accept": "application/json",
        }
        self._trading = httpx.Client(base_url=trading_url, headers=headers, timeout=timeout, transport=transport)
        self._data = httpx.Client(base_url=data_url, headers=headers, timeout=timeout, transport=transport)
        self._max_retries = max_retries
        self._sleep = sleep

    # -- plumbing -----------------------------------------------------------------------------

    def close(self) -> None:
        self._trading.close()
        self._data.close()

    def __enter__(self) -> AlpacaClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _get(self, client: httpx.Client, path: str, params: dict[str, Any] | None = None) -> Any:
        last = "no response"
        for attempt in range(self._max_retries + 1):
            try:
                resp = client.get(path, params=params)
            except httpx.TransportError as e:
                last = type(e).__name__
                if attempt == self._max_retries:
                    break
                self._sleep(min(2**attempt, 30))
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                last = f"HTTP {resp.status_code}"
                if attempt == self._max_retries:
                    break
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.isdigit() else min(2**attempt, 30)
                log.warning("Alpaca %s on %s; retrying in %.0fs", last, path, wait)
                self._sleep(wait)
                continue

            if resp.status_code in (401, 403):
                raise AlpacaError(
                    f"Alpaca rejected the request to {path} (HTTP {resp.status_code}). "
                    "Check the paper keys with `trading-agent secrets status`, and that the feed is on your plan."
                )
            if resp.is_error:
                raise AlpacaError(f"GET {path} failed: HTTP {resp.status_code} {resp.text[:200]}")
            return resp.json()
        raise AlpacaError(f"GET {path} still failing after {self._max_retries} retries ({last})")

    # -- trading API (read-only) -------------------------------------------------------------

    def get_account(self) -> dict[str, Any]:
        return self._get(self._trading, "/v2/account")

    def get_clock(self) -> dict[str, Any]:
        """{'is_open': bool, 'timestamp', 'next_open', 'next_close'}"""
        return self._get(self._trading, "/v2/clock")

    def get_calendar(self, start: date, end: date) -> list[Session]:
        """Regular trading sessions between two dates, inclusive."""
        rows = self._get(self._trading, "/v2/calendar", {"start": start.isoformat(), "end": end.isoformat()})
        sessions = []
        for row in rows:
            day = date.fromisoformat(row["date"])
            open_ny = datetime.combine(day, _parse_hhmm(row["open"]), NEW_YORK)
            close_ny = datetime.combine(day, _parse_hhmm(row["close"]), NEW_YORK)
            sessions.append(Session(day, open_ny.astimezone(timezone.utc), close_ny.astimezone(timezone.utc)))
        return sessions

    # -- market data API ---------------------------------------------------------------------

    def get_bars(
        self,
        symbols: Sequence[str],
        timeframe: str,
        start: datetime,
        end: datetime,
        *,
        feed: str = "iex",
        adjustment: str = "split",
    ) -> Iterator[Bar]:
        """Yield historical bars for many symbols, following pagination."""
        if timeframe not in TIMEFRAMES:
            raise ValueError(f"timeframe must be one of {TIMEFRAMES}")
        if not symbols:
            return
        params: dict[str, Any] = {
            "symbols": ",".join(symbols),
            "timeframe": timeframe,
            "start": _utc_iso(start),
            "end": _utc_iso(end),
            "limit": MAX_PAGE_SIZE,
            "feed": feed,
            "adjustment": adjustment,
            "sort": "asc",
        }
        while True:
            body = self._get(self._data, "/v2/stocks/bars", params)
            for symbol, rows in (body.get("bars") or {}).items():
                for r in rows:
                    yield Bar(
                        symbol=symbol,
                        timeframe=timeframe,
                        ts_utc=_parse_ts(r["t"]),
                        open=float(r["o"]),
                        high=float(r["h"]),
                        low=float(r["l"]),
                        close=float(r["c"]),
                        volume=int(r["v"]),
                        trade_count=int(r["n"]) if r.get("n") is not None else None,
                        vwap=float(r["vw"]) if r.get("vw") is not None else None,
                    )
            token = body.get("next_page_token")
            if not token:
                return
            params["page_token"] = token

    def get_snapshots(self, symbols: Sequence[str], *, feed: str = "iex") -> list[Snapshot]:
        """Latest quote, trade and previous close per symbol. Symbols with no data are skipped."""
        if not symbols:
            return []
        body = self._get(self._data, "/v2/stocks/snapshots", {"symbols": ",".join(symbols), "feed": feed})
        body = body.get("snapshots", body)  # tolerate either response shape
        out = []
        for symbol in symbols:
            snap = body.get(symbol) or {}
            quote = snap.get("latestQuote") or {}
            trade = snap.get("latestTrade") or {}
            prev = snap.get("prevDailyBar") or {}
            quote_ts = _parse_ts(quote.get("t")) or _parse_ts(trade.get("t"))
            if quote_ts is None:
                continue
            out.append(
                Snapshot(
                    symbol=symbol,
                    quote_ts_utc=quote_ts,
                    bid=_num(quote.get("bp"), float),
                    ask=_num(quote.get("ap"), float),
                    bid_size=_num(quote.get("bs"), int),
                    ask_size=_num(quote.get("as"), int),
                    last_price=_num(trade.get("p"), float),
                    last_trade_utc=_parse_ts(trade.get("t")),
                    prev_close=_num(prev.get("c"), float),
                )
            )
        return out


def client_from_settings(settings: Settings, **kwargs: Any) -> AlpacaClient:
    """Build a client from settings + paper keys in the keychain/env. Raises MissingSecretError."""
    return AlpacaClient(
        get_secret("ALPACA_PAPER_API_KEY"),
        get_secret("ALPACA_PAPER_SECRET_KEY"),
        settings.broker_base_url,
        settings.data_base_url,
        **kwargs,
    )
