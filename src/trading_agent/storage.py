"""Local SQLite storage. Phase 0 holds the audit log; market data tables arrive in Phase 2.

The audit log is append-only: triggers reject UPDATE and DELETE so the record of every
signal, proposal, decision and order can't be rewritten after the fact.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

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
"""


def connect(db_file: Path) -> sqlite3.Connection:
    """Open (creating if needed) the database and apply the schema."""
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    with conn:
        conn.executescript(_SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    return conn


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
