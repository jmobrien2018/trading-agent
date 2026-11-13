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
