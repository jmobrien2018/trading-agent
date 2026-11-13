from datetime import date, timedelta

import pytest

from fake_alpaca import DATA, PAPER, FakeAlpaca, utc
from trading_agent import pipeline, storage
from trading_agent.alpaca import AlpacaClient
from trading_agent.config import load_settings
from trading_agent.credentials import Secret

# Friday 2026-11-13, 14:00 in New York: market open. Sessions Tue 10th to Fri 13th.
NOW = utc(2026, 11, 13, 19, 0)
OPENS = {d: utc(2026, 11, d, 14, 30) for d in (10, 11, 12, 13)}


def snapshot(price: float) -> dict:
    return {
        "latestQuote": {"t": "2026-11-13T18:59:58.123456789Z", "bp": price - 0.01, "ap": price + 0.01, "bs": 3, "as": 5},
        "latestTrade": {"t": "2026-11-13T18:59:59.5Z", "p": price, "s": 100},
        "prevDailyBar": {"c": price - 1},
    }


@pytest.fixture
def settings(project_root):
    (project_root / "config" / "settings.local.toml").write_text(
        '[watchlist]\nsymbols = ["SPY", "AAPL"]\n[history]\nminute_days = 3\ndaily_years = 1\n'
    )
    return load_settings(project_root)


@pytest.fixture
def fake():
    f = FakeAlpaca()
    f.is_open = True
    f.calendar = [{"date": f"2026-11-{d}", "open": "09:30", "close": "16:00"} for d in (10, 11, 12, 13)]
    for d in (10, 11, 12):
        f.add_minutes("SPY", OPENS[d], 390)
        # AAPL: 30 minutes missing on the 12th -> 92% coverage, below the 95% minimum.
        f.add_minutes("AAPL", OPENS[d], 390, skip=set(range(60, 90)) if d == 12 else set())
    f.add_minutes("SPY", OPENS[13], 270)  # up to 18:59 UTC: fresh
    f.add_minutes("AAPL", OPENS[13], 241)  # up to 18:30 UTC: 30 minutes stale
    for sym in ("SPY", "AAPL"):
        f.add_days(sym, [utc(2026, 11, d, 5) for d in (10, 11, 12, 13)])
        f.snapshots[sym] = snapshot(100.0)
    return f


@pytest.fixture
def conn(settings):
    c = storage.connect(settings.db_file)
    yield c
    c.close()


def client(fake: FakeAlpaca) -> AlpacaClient:
    return AlpacaClient(Secret("test-key"), Secret("s"), PAPER, DATA, transport=fake.transport, sleep=lambda s: None)


def test_first_refresh_backfills_history(fake, conn, settings):
    with client(fake) as c:
        result = pipeline.refresh(c, conn, settings, now=NOW)

    assert result.market_open
    assert result.backfilled == {"1Min": ["SPY", "AAPL"], "1Day": ["SPY", "AAPL"]}
    assert result.written["1Min"] == {"SPY": 390 * 3 + 270, "AAPL": 390 * 3 - 30 + 241}
    assert result.written["1Day"] == {"SPY": 4, "AAPL": 4}

    # Backfill starts at New York midnight, [history] days back.
    assert fake.bar_requests("1Min")[0]["start"] == "2026-11-10T05:00:00Z"
    assert fake.bar_requests("1Day")[0]["start"] == "2025-11-13T05:00:00Z"

    assert result.snapshots == 2
    assert result.checked_session == date(2026, 11, 12)
    assert len(result.warnings) == 2
    assert any(w.startswith("AAPL: 2026-11-12 1Min coverage 92.3%") for w in result.warnings)
    assert any(w.startswith("AAPL: stale data") and "30 min old" in w for w in result.warnings)


def test_second_refresh_is_incremental(fake, conn, settings):
    with client(fake) as c:
        pipeline.refresh(c, conn, settings, now=NOW)
        fake.add_minutes("SPY", OPENS[13] + timedelta(minutes=270), 5)
        fake.add_minutes("AAPL", OPENS[13] + timedelta(minutes=241), 34)
        fake.requests.clear()
        result = pipeline.refresh(c, conn, settings, now=NOW + timedelta(minutes=5))

    assert result.backfilled == {}
    # One request for both symbols, from the stalest newest bar (AAPL 18:30) minus 15 minutes.
    minute_reqs = fake.bar_requests("1Min")
    assert len(minute_reqs) == 1 and minute_reqs[0]["start"] == "2026-11-13T18:15:00Z"
    assert fake.bar_requests("1Day")[0]["start"] == "2026-11-08T05:00:00Z"  # newest daily bar minus 5 days
    assert result.checked_session is None  # the 12th was already checked
    assert result.warnings == []  # AAPL caught up
    assert storage.latest_bar_time(conn, "AAPL", "1Min", "iex") == utc(2026, 11, 13, 19, 4)

    # Overlapping bars were overwritten, not duplicated.
    n = conn.execute("SELECT COUNT(*) FROM bars WHERE symbol='SPY' AND timeframe='1Min'").fetchone()[0]
    assert n == 390 * 3 + 275


def test_new_symbol_gets_its_own_backfill(fake, conn, settings, project_root):
    with client(fake) as c:
        pipeline.refresh(c, conn, settings, now=NOW)
        fake.add_minutes("MSFT", OPENS[12], 390)
        fake.requests.clear()
        result = pipeline.refresh(c, conn, settings, symbols=["SPY", "AAPL", "MSFT"], now=NOW)

    assert result.backfilled["1Min"] == ["MSFT"]
    starts = sorted(r["start"] for r in fake.bar_requests("1Min"))
    assert starts == ["2026-11-10T05:00:00Z", "2026-11-13T18:15:00Z"]
    assert result.written["1Min"]["MSFT"] == 390


def test_symbol_with_no_data_is_not_backfilled_every_run(fake, conn, settings):
    with client(fake) as c:
        first = pipeline.refresh(c, conn, settings, symbols=["SPY", "ZZZZ"], now=NOW)
        fake.requests.clear()
        second = pipeline.refresh(c, conn, settings, symbols=["SPY", "ZZZZ"], now=NOW + timedelta(minutes=5))
    assert "ZZZZ" in first.backfilled["1Min"]
    assert second.backfilled == {}
    assert all(r["start"] >= "2026-11-13" for r in fake.bar_requests("1Min"))  # no 3-day re-pull


def test_partial_manual_pull_still_gets_full_backfill_once(fake, conn, settings):
    """Regression: bars from an earlier `data pull` made refresh skip the history backfill."""
    with client(fake) as c:
        storage.upsert_bars(conn, list(c.get_bars(["SPY"], "1Min", OPENS[13], NOW)), "iex")
        fake.requests.clear()
        first = pipeline.refresh(c, conn, settings, now=NOW)
        fake.requests.clear()
        second = pipeline.refresh(c, conn, settings, now=NOW + timedelta(minutes=5))
    assert "SPY" in first.backfilled["1Min"]
    assert first.written["1Min"]["SPY"] == 390 * 3 + 270
    assert second.backfilled == {}


def test_deeper_history_setting_triggers_another_backfill(fake, conn, settings, project_root):
    with client(fake) as c:
        pipeline.refresh(c, conn, settings, now=NOW)
        (project_root / "config" / "settings.local.toml").write_text(
            '[watchlist]\nsymbols = ["SPY", "AAPL"]\n[history]\nminute_days = 30\ndaily_years = 1\n'
        )
        deeper = load_settings(project_root)
        fake.requests.clear()
        result = pipeline.refresh(c, conn, deeper, now=NOW + timedelta(minutes=5))
    assert result.backfilled == {"1Min": ["SPY", "AAPL"]}
    assert fake.bar_requests("1Min")[0]["start"] == "2026-10-14T04:00:00Z"  # 30 days back, EDT midnight


def test_market_closed_skips_snapshots_and_staleness(fake, conn, settings):
    fake.is_open = False
    with client(fake) as c:
        result = pipeline.refresh(c, conn, settings, now=NOW)
    assert result.snapshots == 0
    assert not any("stale" in w for w in result.warnings)
    assert not any(r.url.path == "/v2/stocks/snapshots" for r in fake.requests)


def test_sip_feed_stays_clear_of_free_plan_delay(fake, conn, project_root):
    (project_root / "config" / "settings.local.toml").write_text(
        '[watchlist]\nsymbols = ["SPY"]\n[data]\nfeed = "sip"\n[refresh]\ntimeframes = ["1Min"]\n'
    )
    settings = load_settings(project_root)
    fake.is_open = False
    with client(fake) as c:
        pipeline.refresh(c, conn, settings, now=NOW)
    req = fake.bar_requests("1Min")[0]
    assert req["feed"] == "sip" and req["end"] == "2026-11-13T18:44:00Z"


def test_lock_blocks_second_runner_and_recovers_stale_lock(tmp_path):
    lock = tmp_path / "refresh.lock"
    with pipeline.refresh_lock(lock) as first:
        assert first and lock.exists()
        with pipeline.refresh_lock(lock) as second:
            assert second is False
        assert lock.exists()  # the blocked runner must not remove the holder's lock
    assert not lock.exists()

    lock.write_text("12345")  # left behind by a crashed run
    later = lock.stat().st_mtime + pipeline.LOCK_STALE_AFTER.total_seconds() + 1
    with pipeline.refresh_lock(lock, now=later) as taken:
        assert taken
    assert not lock.exists()
