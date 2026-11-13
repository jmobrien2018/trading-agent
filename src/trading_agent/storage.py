"""Local SQLite storage: the audit log and price bars.

The audit log is append-only: triggers reject UPDATE and DELETE so the record of every
signal, proposal, decision and order can't be rewritten after the fact.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import Bar, Snapshot

SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts_utc  TEXT NOT NULL,
    kind    TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_kind_ts ON audit_log (kind, ts_utc);

CREATE TRIGGER IF NOT EXISTS audit_log_no_update
BEFORE UPDATE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

CREATE TRIGGER IF NOT EXISTS audit_log_no_delete
BEFORE DELETE ON audit_log
BEGIN
    SELECT RAISE(ABORT, 'audit_log is append-only');
END;

-- Price bars. ts_utc = bar start, 'YYYY-MM-DDTHH:MM:SSZ' so text order = time order.
-- Re-pulling the same window overwrites (adjusted prices can change after splits).
CREATE TABLE IF NOT EXISTS bars (
    symbol      TEXT    NOT NULL,
    timeframe   TEXT    NOT NULL,
    feed        TEXT    NOT NULL,
    ts_utc      TEXT    NOT NULL,
    open        REAL    NOT NULL,
    high        REAL    NOT NULL,
    low         REAL    NOT NULL,
    close       REAL    NOT NULL,
    volume      INTEGER NOT NULL,
    trade_count INTEGER,
    vwap        REAL,
    PRIMARY KEY (symbol, timeframe, feed, ts_utc)
) WITHOUT ROWID;

-- Latest quote/trade snapshots, saved while the market is open. One row per new quote.
CREATE TABLE IF NOT EXISTS quotes (
    symbol         TEXT NOT NULL,
    feed           TEXT NOT NULL,
    quote_ts_utc   TEXT NOT NULL,
    bid            REAL,
    ask            REAL,
    bid_size       INTEGER,
    ask_size       INTEGER,
    last_price     REAL,
    last_trade_utc TEXT,
    prev_close     REAL,
    fetched_utc    TEXT NOT NULL,
    PRIMARY KEY (symbol, feed, quote_ts_utc)
) WITHOUT ROWID;
"""

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"
_TS_FMT_US = "%Y-%m-%dT%H:%M:%S.%fZ"


def _ts(dt: datetime) -> str:
    if dt.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return dt.astimezone(timezone.utc).strftime(_TS_FMT)


def _ts_us(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        raise ValueError("timestamps must be timezone-aware")
    return dt.astimezone(timezone.utc).strftime(_TS_FMT_US)


def _parse(text: str) -> datetime:
    return datetime.strptime(text, _TS_FMT).replace(tzinfo=timezone.utc)


# Public names for other modules storing timestamps in the same text format.
format_ts = _ts
parse_ts = _parse


def connect(db_file: Path) -> sqlite3.Connection:
    """Open (creating if needed) the database and apply the schema."""
    db_file.parent.mkdir(parents=True, exist_ok=True)
    # Generous timeout: a scheduled refresh and a manual command may touch the database at once.
    conn = sqlite3.connect(db_file, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    with conn:
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(SCHEMA_VERSION),),
        )
    return conn


def upsert_bars(conn: sqlite3.Connection, bars: Iterable[Bar], feed: str) -> int:
    """Insert or replace bars; returns how many rows were written."""
    rows = [
        (b.symbol, b.timeframe, feed, _ts(b.ts_utc), b.open, b.high, b.low, b.close, b.volume, b.trade_count, b.vwap)
        for b in bars
    ]
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO bars "
            "(symbol, timeframe, feed, ts_utc, open, high, low, close, volume, trade_count, vwap) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    return len(rows)


def bar_times(
    conn: sqlite3.Connection, symbol: str, timeframe: str, feed: str, start: datetime, end: datetime
) -> list[datetime]:
    """Start times of stored bars in [start, end), ascending."""
    rows = conn.execute(
        "SELECT ts_utc FROM bars WHERE symbol = ? AND timeframe = ? AND feed = ? AND ts_utc >= ? AND ts_utc < ? "
        "ORDER BY ts_utc",
        (symbol, timeframe, feed, _ts(start), _ts(end)),
    ).fetchall()
    return [_parse(r[0]) for r in rows]


def stored_feeds(conn: sqlite3.Connection, timeframe: str) -> list[str]:
    return [r[0] for r in conn.execute("SELECT DISTINCT feed FROM bars WHERE timeframe = ? ORDER BY feed", (timeframe,))]


def latest_bar_time(conn: sqlite3.Connection, symbol: str, timeframe: str, feed: str) -> datetime | None:
    row = conn.execute(
        "SELECT MAX(ts_utc) FROM bars WHERE symbol = ? AND timeframe = ? AND feed = ?", (symbol, timeframe, feed)
    ).fetchone()
    return _parse(row[0]) if row and row[0] else None


def bar_inventory(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Bar count and first/last bar time per feed, timeframe and symbol."""
    rows = conn.execute(
        "SELECT feed, timeframe, symbol, COUNT(*) AS n, MIN(ts_utc) AS first, MAX(ts_utc) AS last "
        "FROM bars GROUP BY feed, timeframe, symbol ORDER BY feed, timeframe, symbol"
    ).fetchall()
    return [
        {"feed": r["feed"], "timeframe": r["timeframe"], "symbol": r["symbol"], "bars": r["n"],
         "first": _parse(r["first"]), "last": _parse(r["last"])}
        for r in rows
    ]


def insert_snapshots(conn: sqlite3.Connection, snapshots: Iterable[Snapshot], feed: str) -> int:
    """Save snapshots; one already stored for the same quote time is overwritten."""
    fetched = _ts_us(datetime.now(timezone.utc))
    rows = [
        (s.symbol, feed, _ts_us(s.quote_ts_utc), s.bid, s.ask, s.bid_size, s.ask_size, s.last_price,
         _ts_us(s.last_trade_utc), s.prev_close, fetched)
        for s in snapshots
    ]
    with conn:
        conn.executemany(
            "INSERT OR REPLACE INTO quotes (symbol, feed, quote_ts_utc, bid, ask, bid_size, ask_size, "
            "last_price, last_trade_utc, prev_close, fetched_utc) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
    return len(rows)


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def record_event(conn: sqlite3.Connection, kind: str, payload: dict[str, Any] | None = None) -> int:
    """Append one event to the audit log and return its id."""
    if not kind:
        raise ValueError("kind is required")
    ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
    body = json.dumps(payload or {}, sort_keys=True, default=str)
    with conn:
        cur = conn.execute(
            "INSERT INTO audit_log (ts_utc, kind, payload) VALUES (?, ?, ?)",
            (ts, kind, body),
        )
    return int(cur.lastrowid)


def recent_events(conn: sqlite3.Connection, kind: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
    """Newest first."""
    if kind:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE kind = ? ORDER BY id DESC LIMIT ?", (kind, limit)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [
        {"id": r["id"], "ts_utc": r["ts_utc"], "kind": r["kind"], "payload": json.loads(r["payload"])}
        for r in rows
    ]
