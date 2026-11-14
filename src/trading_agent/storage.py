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

from zoneinfo import ZoneInfo

import pandas as pd

from .models import Bar, Snapshot

_NEW_YORK = ZoneInfo("America/New_York")

SCHEMA_VERSION = 5

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

-- Backtest results (Phase 3). One row per strategy x parameter set x cost level.
CREATE TABLE IF NOT EXISTS backtest_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_utc TEXT NOT NULL,
    batch       TEXT NOT NULL,  -- groups the runs from one `backtest run` command
    strategy    TEXT NOT NULL,
    version     INTEGER NOT NULL,
    params      TEXT NOT NULL,  -- JSON
    cost_bp     REAL NOT NULL,
    period      TEXT NOT NULL,  -- in-sample | holdout
    fill        TEXT NOT NULL DEFAULT 'open',  -- open (next day's open) | close (same day's close)
    start_date  TEXT NOT NULL,
    end_date    TEXT NOT NULL,
    capital     REAL NOT NULL,
    symbols     TEXT NOT NULL,  -- JSON
    metrics     TEXT NOT NULL   -- JSON
);
CREATE INDEX IF NOT EXISTS idx_backtest_runs_strategy ON backtest_runs (strategy, version, period);

CREATE TABLE IF NOT EXISTS backtest_trades (
    run_id       INTEGER NOT NULL REFERENCES backtest_runs (id),
    symbol       TEXT NOT NULL,
    entry_date   TEXT NOT NULL,
    entry_price  REAL NOT NULL,
    exit_date    TEXT NOT NULL,
    exit_price   REAL NOT NULL,
    qty          REAL NOT NULL,
    pnl          REAL NOT NULL,
    return_pct   REAL NOT NULL,
    bars_held    INTEGER NOT NULL,
    entry_reason TEXT NOT NULL,
    exit_reason  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_backtest_trades_run ON backtest_trades (run_id);
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
        # Schema 5: backtest_runs gained `fill`; add it to databases created before that.
        columns = {r[1] for r in conn.execute("PRAGMA table_info(backtest_runs)")}
        if "fill" not in columns:
            conn.execute("ALTER TABLE backtest_runs ADD COLUMN fill TEXT NOT NULL DEFAULT 'open'")
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


def load_daily_bars(conn: sqlite3.Connection, symbol: str, feed: str) -> pd.DataFrame:
    """Daily bars as a DataFrame indexed by New York trading date (open, high, low, close, volume)."""
    rows = conn.execute(
        "SELECT ts_utc, open, high, low, close, volume FROM bars "
        "WHERE symbol = ? AND timeframe = '1Day' AND feed = ? ORDER BY ts_utc",
        (symbol, feed),
    ).fetchall()
    df = pd.DataFrame(rows, columns=["ts_utc", "open", "high", "low", "close", "volume"])
    # Daily bars are stamped at New York midnight; that date is the trading day.
    df.index = pd.Index([_parse(t).astimezone(_NEW_YORK).date() for t in df.pop("ts_utc")], name="date")
    return df


def load_minute_bars(conn: sqlite3.Connection, symbol: str, feed: str) -> pd.DataFrame:
    """1-minute bars as a DataFrame indexed by UTC timestamp (open, high, low, close, volume)."""
    df = pd.read_sql_query(
        "SELECT ts_utc, open, high, low, close, volume FROM bars "
        "WHERE symbol = ? AND timeframe = '1Min' AND feed = ? ORDER BY ts_utc",
        conn,
        params=(symbol, feed),
    )
    df.index = pd.DatetimeIndex(pd.to_datetime(df.pop("ts_utc"), format=_TS_FMT, utc=True), name="ts_utc")
    return df


def save_backtest(conn: sqlite3.Connection, batch: str, period: str, result: Any, symbols: list[str]) -> int:
    """Store one backtest run (a backtest.RunResult) and its trades; returns the run id."""
    cfg = result.config
    with conn:
        cur = conn.execute(
            "INSERT INTO backtest_runs (created_utc, batch, strategy, version, params, cost_bp, period, fill, "
            "start_date, end_date, capital, symbols, metrics) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                _ts(datetime.now(timezone.utc)), batch, result.strategy, result.version,
                json.dumps(dict(result.params), default=str), cfg.cost_bp, period, cfg.fill,
                cfg.start.isoformat(), cfg.end.isoformat(), cfg.capital,
                json.dumps(list(symbols)),
                json.dumps(result.metrics, default=str),
            ),
        )
        run_id = int(cur.lastrowid)
        conn.executemany(
            "INSERT INTO backtest_trades (run_id, symbol, entry_date, entry_price, exit_date, exit_price, qty, "
            "pnl, return_pct, bars_held, entry_reason, exit_reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (run_id, t.symbol, t.entry_date.isoformat(), t.entry_price, t.exit_date.isoformat(), t.exit_price,
                 t.qty, t.pnl, t.return_pct, t.bars_held, t.entry_reason, t.exit_reason)
                for t in result.trades
            ],
        )
    return run_id


def backtest_trades(conn: sqlite3.Connection, run_id: int) -> pd.DataFrame:
    return pd.read_sql_query(
        "SELECT symbol, entry_date, entry_price, exit_date, exit_price, qty, pnl, return_pct, bars_held, "
        "entry_reason, exit_reason FROM backtest_trades WHERE run_id = ? ORDER BY entry_date, symbol",
        conn,
        params=(run_id,),
    )


def get_backtest_run(conn: sqlite3.Connection, run_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM backtest_runs WHERE id = ?", (run_id,)).fetchone()
    if row is None:
        return None
    out = dict(row)
    for k in ("params", "symbols", "metrics"):
        out[k] = json.loads(out[k])
    return out


def count_backtest_runs(conn: sqlite3.Connection, strategy: str, version: int, period: str) -> int:
    row = conn.execute(
        "SELECT COUNT(DISTINCT batch) FROM backtest_runs WHERE strategy = ? AND version = ? AND period = ?",
        (strategy, version, period),
    ).fetchone()
    return int(row[0])


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
