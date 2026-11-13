from datetime import date, timedelta

from fake_alpaca import utc
from trading_agent import data_quality as dq
from trading_agent.models import Session

OPEN = utc(2026, 11, 10, 14, 30)
SESSION = Session(date(2026, 11, 10), OPEN, utc(2026, 11, 10, 21, 0))  # 390 minutes


def minutes(*idx: int):
    return [OPEN + timedelta(minutes=i) for i in idx]


def test_full_session_is_complete():
    day = dq.minute_coverage(SESSION, minutes(*range(390)))
    assert (day.expected, day.present, day.max_gap_min) == (390, 390, 0)
    assert day.coverage == 1.0


def test_gaps_counted_including_edges():
    present = [i for i in range(390) if i not in {0, 1, 100, 101, 102, 389}]
    day = dq.minute_coverage(SESSION, minutes(*present))
    assert day.present == 384
    assert day.max_gap_min == 3  # minutes 100-102


def test_bars_outside_regular_hours_ignored():
    pre_market = [OPEN - timedelta(minutes=5)]
    after_hours = [SESSION.close_utc]  # the close itself starts an after-hours bar
    day = dq.minute_coverage(SESSION, pre_market + minutes(0, 1) + after_hours)
    assert day.present == 2


def test_empty_day_is_one_big_gap():
    day = dq.minute_coverage(SESSION, [])
    assert (day.present, day.max_gap_min) == (0, 390)


def test_minute_summary_across_days():
    day2 = Session(date(2026, 11, 11), OPEN + timedelta(days=1), SESSION.close_utc + timedelta(days=1))
    times = minutes(*range(390)) + [day2.open_utc + timedelta(minutes=i) for i in range(0, 390, 2)]
    s = dq.summarize_minutes("AAPL", "iex", [SESSION, day2], times)
    assert (s.expected, s.present) == (780, 585)
    assert s.max_gap_min == 1
    assert s.worst_day == date(2026, 11, 11)
    assert s.missing_days == ()
    assert s.last_bar_utc == day2.open_utc + timedelta(minutes=388)


def test_daily_summary_matches_new_york_dates():
    day2 = Session(date(2026, 11, 11), OPEN + timedelta(days=1), SESSION.close_utc + timedelta(days=1))
    # Alpaca stamps daily bars at NY midnight = 05:00 UTC in November.
    s = dq.summarize_days("SPY", "iex", [SESSION, day2], [utc(2026, 11, 10, 5)])
    assert (s.present, s.expected) == (1, 2)
    assert s.missing_days == (date(2026, 11, 11),)


def test_unfinished_session_dropped():
    assert dq.completed_sessions([SESSION], now=SESSION.close_utc - timedelta(minutes=1)) == []
    assert dq.completed_sessions([SESSION], now=SESSION.close_utc) == [SESSION]
