import sqlite3

import pytest

from trading_agent import storage


@pytest.fixture
def conn(tmp_path):
    c = storage.connect(tmp_path / "nested" / "db.sqlite3")
    yield c
    c.close()


def test_record_and_read_back(conn):
    first = storage.record_event(conn, "signal", {"symbol": "AAPL", "side": "buy"})
    second = storage.record_event(conn, "proposal", {"symbol": "AAPL"})
    assert second > first

    events = storage.recent_events(conn)
    assert [e["kind"] for e in events] == ["proposal", "signal"]  # newest first
    assert events[1]["payload"] == {"symbol": "AAPL", "side": "buy"}

    assert [e["id"] for e in storage.recent_events(conn, kind="signal")] == [first]


def test_audit_log_is_append_only(conn):
    event_id = storage.record_event(conn, "order", {"qty": 1})
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        with conn:
            conn.execute("UPDATE audit_log SET payload = '{}' WHERE id = ?", (event_id,))
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        with conn:
            conn.execute("DELETE FROM audit_log WHERE id = ?", (event_id,))
    assert storage.recent_events(conn)[0]["payload"] == {"qty": 1}


def test_bars_upsert_is_idempotent_and_queryable(conn):
    from datetime import datetime, timedelta, timezone

    from trading_agent.models import Bar

    t0 = datetime(2026, 11, 10, 14, 30, tzinfo=timezone.utc)
    bars = [Bar("AAPL", "1Min", t0 + timedelta(minutes=i), 1, 2, 0.5, 1.5, 100) for i in range(3)]
    assert storage.upsert_bars(conn, bars, "iex") == 3
    storage.upsert_bars(conn, bars, "iex")  # same window again: no duplicates
    storage.upsert_bars(conn, bars[:1], "sip")  # other feed kept separately

    times = storage.bar_times(conn, "AAPL", "1Min", "iex", t0, t0 + timedelta(minutes=2))
    assert times == [t0, t0 + timedelta(minutes=1)]  # end is exclusive
    assert conn.execute("SELECT COUNT(*) FROM bars").fetchone()[0] == 4
    assert storage.stored_feeds(conn, "1Min") == ["iex", "sip"]


def test_old_database_gains_fill_column(tmp_path):
    db = tmp_path / "old.sqlite3"
    old = sqlite3.connect(db)
    old.execute("CREATE TABLE backtest_runs (id INTEGER PRIMARY KEY, created_utc TEXT NOT NULL, batch TEXT NOT NULL, "
                "strategy TEXT NOT NULL, version INTEGER NOT NULL, params TEXT NOT NULL, cost_bp REAL NOT NULL, "
                "period TEXT NOT NULL, start_date TEXT NOT NULL, end_date TEXT NOT NULL, capital REAL NOT NULL, "
                "symbols TEXT NOT NULL, metrics TEXT NOT NULL)")
    old.execute("INSERT INTO backtest_runs VALUES (1, 't', 'b', 's', 1, '{}', 5, 'in-sample', 'a', 'b', 1, '[]', '{}')")
    old.commit()
    old.close()
    conn = storage.connect(db)
    try:
        assert conn.execute("SELECT fill FROM backtest_runs WHERE id = 1").fetchone()[0] == "open"
    finally:
        conn.close()


def test_reconnect_keeps_schema_and_data(tmp_path):
    db = tmp_path / "db.sqlite3"
    c1 = storage.connect(db)
    storage.record_event(c1, "startup_check")
    c1.close()
    c2 = storage.connect(db)
    try:
        assert len(storage.recent_events(c2)) == 1
        version = c2.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]
        assert version == str(storage.SCHEMA_VERSION)
    finally:
        c2.close()
