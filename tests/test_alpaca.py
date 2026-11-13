from datetime import date

import pytest

from fake_alpaca import DATA, PAPER, FakeAlpaca, utc
from trading_agent.alpaca import AlpacaClient, AlpacaError
from trading_agent.credentials import Secret


def make_client(fake: FakeAlpaca, key: str = "test-key", **kw) -> AlpacaClient:
    sleeps: list[float] = []
    client = AlpacaClient(Secret(key), Secret("test-secret"), PAPER, DATA, transport=fake.transport,
                          sleep=sleeps.append, **kw)
    client.sleeps = sleeps
    return client


def test_refuses_live_trading_url():
    with pytest.raises(AlpacaError, match="non-paper"):
        AlpacaClient(Secret("k"), Secret("s"), "https://api.alpaca.markets", DATA)


def test_keys_sent_as_headers_and_account_read():
    fake = FakeAlpaca()
    with make_client(fake) as c:
        assert c.get_account()["status"] == "ACTIVE"
    req = fake.requests[0]
    assert req.headers["APCA-API-KEY-ID"] == "test-key"
    assert req.headers["APCA-API-SECRET-KEY"] == "test-secret"
    assert req.url.host == "paper-api.alpaca.markets"


def test_bad_keys_give_clear_error_without_leaking_them():
    fake = FakeAlpaca()
    with make_client(fake, key="wrong-key") as c, pytest.raises(AlpacaError) as exc:
        c.get_account()
    assert "secrets status" in str(exc.value)
    assert "wrong-key" not in str(exc.value)


def test_retries_rate_limit_then_succeeds():
    fake = FakeAlpaca()
    fake.fail_next = [429, 503]
    with make_client(fake) as c:
        assert c.get_account()["status"] == "ACTIVE"
        assert c.sleeps == [1.0, 1.0]  # honoured Retry-After


def test_gives_up_after_max_retries():
    fake = FakeAlpaca()
    fake.fail_next = [429] * 10
    with make_client(fake, max_retries=2) as c, pytest.raises(AlpacaError, match="after 2 retries"):
        c.get_account()


def test_calendar_converts_new_york_hours_to_utc():
    fake = FakeAlpaca()
    fake.calendar = [
        {"date": "2026-07-02", "open": "09:30", "close": "16:00"},  # summer: UTC-4
        {"date": "2026-11-27", "open": "09:30", "close": "13:00"},  # day after Thanksgiving, half day, UTC-5
    ]
    with make_client(fake) as c:
        summer, half = c.get_calendar(date(2026, 7, 1), date(2026, 11, 30))
    assert summer.open_utc == utc(2026, 7, 2, 13, 30)
    assert summer.close_utc == utc(2026, 7, 2, 20, 0)
    assert half.open_utc == utc(2026, 11, 27, 14, 30)
    assert half.close_utc == utc(2026, 11, 27, 18, 0)


def test_bars_follow_pagination_across_symbols():
    fake = FakeAlpaca()
    fake.page_size = 7
    fake.add_minutes("AAPL", utc(2026, 11, 10, 14, 30), 10)
    fake.add_minutes("SPY", utc(2026, 11, 10, 14, 30), 10)
    with make_client(fake) as c:
        bars = list(c.get_bars(["AAPL", "SPY"], "1Min", utc(2026, 11, 10), utc(2026, 11, 11)))
    assert len(bars) == 20
    assert {b.symbol for b in bars} == {"AAPL", "SPY"}
    assert len(fake.requests) == 3  # 7 + 7 + 6
    first = bars[0]
    assert first.ts_utc == utc(2026, 11, 10, 14, 30) and first.volume == 100 and first.vwap == 1.2
    q = fake.requests[0].url.params
    assert q["feed"] == "iex" and q["timeframe"] == "1Min" and q["start"] == "2026-11-10T00:00:00Z"


def test_bars_reject_naive_timestamps():
    from datetime import datetime

    with make_client(FakeAlpaca()) as c, pytest.raises(ValueError, match="timezone-aware"):
        list(c.get_bars(["AAPL"], "1Min", datetime(2026, 11, 10), utc(2026, 11, 11)))
