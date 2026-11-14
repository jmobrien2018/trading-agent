"""Phase 2 data pipeline: backfill, incremental refresh, live snapshots and quality checks.

`refresh()` is safe to run as often as you like (the scheduler runs it every few minutes):
- A symbol seen for the first time is backfilled ([history] in settings).
- After that only new bars are fetched, starting a little before the newest stored bar so
  late corrections are picked up. Re-fetched bars overwrite, never duplicate.
- While the market is open it also saves a quote snapshot per symbol and flags symbols
  whose newest minute bar is stale.
- Once per completed session it checks that day's minute coverage.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from collections import defaultdict
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path

from . import data_quality, storage
from .alpaca import AlpacaClient
from .config import Settings
from .data_quality import NEW_YORK

log = logging.getLogger(__name__)

# Free plan: SIP data from the last 15 minutes is off limits; stay a minute clear of the edge.
SIP_DELAY = timedelta(minutes=16)
UPSERT_BATCH = 5_000
# How far before the newest stored bar a refresh starts, to pick up late corrections
# (and, for daily bars, the still-forming bar of the current day).
OVERLAP = {"1Day": timedelta(days=5)}
DEFAULT_OVERLAP = timedelta(minutes=15)
LOCK_STALE_AFTER = timedelta(minutes=30)
QUALITY_META_KEY = "quality_checked_session"


@dataclass
class RefreshResult:
    market_open: bool
    written: dict[str, dict[str, int]] = field(default_factory=dict)  # timeframe -> symbol -> bars
    backfilled: dict[str, list[str]] = field(default_factory=dict)  # timeframe -> symbols seen for the first time
    full_repull: list[str] = field(default_factory=list)  # timeframes re-pulled in full (dividend adjustment)
    snapshots: int = 0
    checked_session: date | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def total_bars(self) -> int:
        return sum(sum(v.values()) for v in self.written.values())


def ny_midnight_utc(day: date) -> datetime:
    return datetime.combine(day, dtime(0), NEW_YORK).astimezone(timezone.utc)


def pull_window(now: datetime, days: int, feed: str) -> tuple[datetime, datetime]:
    """Start at New York midnight `days` days ago, so every session in the window is pulled whole
    and lines up with `data report --days N`. End now (SIP: 16 minutes ago, the free-plan limit)."""
    today = now.astimezone(NEW_YORK).date()
    end = now - SIP_DELAY if feed == "sip" else now
    return ny_midnight_utc(today - timedelta(days=days)), end


def feed_for(timeframe: str, settings: Settings) -> str:
    """Daily bars come from [data] daily_feed; intraday bars and quotes from [data] feed."""
    return settings.daily_feed if timeframe == "1Day" else settings.data_feed


def adjustment_for(timeframe: str, settings: Settings) -> str:
    """Daily bars use [data] daily_adjustment; intraday bars use [data] adjustment."""
    return settings.daily_adjustment if timeframe == "1Day" else settings.data_adjustment


def history_start(now: datetime, timeframe: str, settings: Settings) -> datetime:
    days = settings.history_daily_years * 365 if timeframe == "1Day" else settings.history_minute_days
    return pull_window(now, days, settings.data_feed)[0]


def ingest(
    client: AlpacaClient,
    conn: sqlite3.Connection,
    symbols: Sequence[str],
    timeframe: str,
    start: datetime,
    end: datetime,
    *,
    feed: str,
    adjustment: str,
) -> tuple[dict[str, int], dict[str, datetime]]:
    """Fetch bars and store them in batches. Returns (bars per symbol, newest bar per symbol)."""
    counts: dict[str, int] = defaultdict(int)
    newest: dict[str, datetime] = {}
    batch = []
    for bar in client.get_bars(symbols, timeframe, start, end, feed=feed, adjustment=adjustment):
        batch.append(bar)
        counts[bar.symbol] += 1
        if bar.symbol not in newest or bar.ts_utc > newest[bar.symbol]:
            newest[bar.symbol] = bar.ts_utc
        if len(batch) >= UPSERT_BATCH:
            storage.upsert_bars(conn, batch, feed)
            batch.clear()
    storage.upsert_bars(conn, batch, feed)
    return dict(counts), newest


def refresh(
    client: AlpacaClient,
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    symbols: Sequence[str] | None = None,
    now: datetime | None = None,
) -> RefreshResult:
    now = now or datetime.now(timezone.utc)
    symbols = tuple(symbols or settings.watchlist)
    feed = settings.data_feed

    result = RefreshResult(market_open=bool(client.get_clock().get("is_open")))

    for tf in settings.refresh_timeframes:
        tf_feed = feed_for(tf, settings)
        tf_adj = adjustment_for(tf, settings)
        end = now - SIP_DELAY if tf_feed == "sip" else now
        depth = history_start(now, tf, settings)
        # Dividend-adjusted prices are re-scaled by every new dividend, so once per New York day
        # re-pull the whole history instead of only new bars (about 3 requests for daily bars).
        full_key = f"full_repull:{tf_feed}:{tf}:{tf_adj}"
        today = now.astimezone(NEW_YORK).date().isoformat()
        full_repull = tf_adj in ("dividend", "all") and storage.get_meta(conn, full_key) != today
        new: list[str] = []
        known: dict[str, datetime] = {}
        for s in symbols:
            done = _backfill_record(conn, s, tf, tf_feed)
            # Backfill once per symbol/timeframe/feed, even if a manual pull already stored some
            # recent bars; again if [history] was made deeper or the price adjustment changed.
            if done is None or depth < done[0] or done[2] != tf_adj:
                new.append(s)
            elif full_repull:
                known[s] = depth + OVERLAP.get(tf, DEFAULT_OVERLAP)  # whole history, see above
            else:
                # Newest stored bar; for a symbol whose backfill returned nothing (bad ticker,
                # no trades on this feed), the time of that backfill.
                known[s] = storage.latest_bar_time(conn, s, tf, tf_feed) or done[1]

        groups: list[tuple[datetime, list[str]]] = []
        if new:
            groups.append((depth, new))
            result.backfilled[tf] = new
        if known:
            # One request for every known symbol, from the stalest one. Re-fetching a few
            # bars for the others is harmless: stored bars are overwritten, not duplicated.
            groups.append((min(known.values()) - OVERLAP.get(tf, DEFAULT_OVERLAP), list(known)))

        written: dict[str, int] = {}
        for start, group in groups:
            if start >= end:
                continue
            counts, _ = ingest(client, conn, group, tf, start, end, feed=tf_feed, adjustment=tf_adj)
            written.update(counts)
        for s in new:
            storage.set_meta(
                conn, _backfill_key(s, tf, tf_feed), f"{storage.format_ts(depth)}|{storage.format_ts(end)}|{tf_adj}"
            )
        if tf_adj in ("dividend", "all"):
            storage.set_meta(conn, full_key, today)
            if full_repull and not new:
                result.full_repull.append(tf)
        result.written[tf] = written

    if result.market_open:
        snaps = client.get_snapshots(symbols, feed=feed)
        result.snapshots = storage.insert_snapshots(conn, snaps, feed)
        if "1Min" in settings.refresh_timeframes:
            result.warnings += _stale_warnings(conn, symbols, feed, now, settings.stale_minutes)

    if "1Min" in settings.refresh_timeframes:
        result.checked_session, coverage_warnings = _check_last_session(client, conn, settings, symbols, now)
        result.warnings += coverage_warnings

    for w in result.warnings:
        log.warning(w)
    log.info("refresh: %d bars written, %d snapshots, market %s",
             result.total_bars, result.snapshots, "open" if result.market_open else "closed")
    return result


def _backfill_key(symbol: str, timeframe: str, feed: str) -> str:
    return f"backfill:{feed}:{timeframe}:{symbol}"


def _backfill_record(
    conn: sqlite3.Connection, symbol: str, timeframe: str, feed: str
) -> tuple[datetime, datetime, str] | None:
    """(oldest time backfilled from, time it ran up to, price adjustment), or None if never.
    Records written before adjustments were tracked were all "split"."""
    value = storage.get_meta(conn, _backfill_key(symbol, timeframe, feed))
    if not value:
        return None
    start, end, *rest = value.split("|")
    return storage.parse_ts(start), storage.parse_ts(end), (rest[0] if rest else "split")


def _stale_warnings(
    conn: sqlite3.Connection, symbols: Sequence[str], feed: str, now: datetime, stale_minutes: int
) -> list[str]:
    out = []
    for s in symbols:
        last = storage.latest_bar_time(conn, s, "1Min", feed)
        age = data_quality.age_minutes(last, now)
        if age is None or age > stale_minutes:
            shown = "no bars" if age is None else f"newest 1Min bar is {age:.0f} min old"
            out.append(f"{s}: stale data while market is open ({shown}, limit {stale_minutes} min)")
    return out


def _check_last_session(
    client: AlpacaClient, conn: sqlite3.Connection, settings: Settings, symbols: Sequence[str], now: datetime
) -> tuple[date | None, list[str]]:
    """Check minute coverage of the most recent completed session, once per session."""
    today = now.astimezone(NEW_YORK).date()
    sessions = data_quality.completed_sessions(client.get_calendar(today - timedelta(days=10), today), now)
    if not sessions:
        return None, []
    session = sessions[-1]
    if storage.get_meta(conn, QUALITY_META_KEY) == session.date.isoformat():
        return None, []

    warnings = []
    day_start = ny_midnight_utc(session.date)
    for s in symbols:
        times = storage.bar_times(conn, s, "1Min", settings.data_feed, day_start, day_start + timedelta(days=1))
        day = data_quality.minute_coverage(session, times)
        if day.coverage < settings.min_coverage:
            warnings.append(
                f"{s}: {session.date} 1Min coverage {day.coverage:.1%} is below {settings.min_coverage:.0%} "
                f"({day.present}/{day.expected} minutes, longest gap {day.max_gap_min} min)"
            )
    storage.set_meta(conn, QUALITY_META_KEY, session.date.isoformat())
    return session.date, warnings


@contextmanager
def refresh_lock(path: Path, now: float | None = None) -> Iterator[bool]:
    """Exclusive lock file so two refreshes never run at once. Yields False if already held.
    A lock older than 30 minutes is treated as left over from a crashed run and taken over."""
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time() if now is None else now
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age = now - path.stat().st_mtime
        except FileNotFoundError:
            age = None  # released between our two checks; try once more below
        if age is not None and age < LOCK_STALE_AFTER.total_seconds():
            yield False
            return
        path.unlink(missing_ok=True)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            yield False
            return
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        yield True
    finally:
        path.unlink(missing_ok=True)
