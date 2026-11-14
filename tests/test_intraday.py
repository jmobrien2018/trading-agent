from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from trading_agent import backtest_intraday as bi
from trading_agent import spreads
from trading_agent.strategies.intraday_momentum import IntradayMomentum
from trading_agent.strategy import Enter, ExitRules, Param, Params, Sizing, Strategy, no_lookahead

NY = ZoneInfo("America/New_York")


def minute_bars(days: dict[date, list[float]], minutes: int = 390) -> pd.DataFrame:
    """Prepared minute bars: for each session, a list of closes (one per minute from 9:30);
    each bar opens at the previous close. Shorter lists make half days."""
    rows, idx = [], []
    prev = None
    for d, closes in days.items():
        for m, close in enumerate(closes[:minutes]):
            ts = datetime.combine(d, time(9, 30), NY) + timedelta(minutes=m)
            open_ = close if prev is None else prev
            rows.append((open_, max(open_, close), min(open_, close), close, 100))
            idx.append(ts.astimezone(ZoneInfo("UTC")))
            prev = close
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"],
                      index=pd.DatetimeIndex(idx, name="ts_utc"))
    return bi.prepare(df)


class AtMinute(Strategy):
    """Test strategy: decide to enter at the close of a given minute every day."""

    name, version, family, style, description, timeframe, warmup_bars = "at_minute", 1, "trend", "event", "t", "1Min", 0
    params = {"x": Param(0, (0,))}

    def __init__(self, minute=10, stop_offset=None, target_offset=None, flat_by=389):
        self.minute, self.stop_offset, self.target_offset = minute, stop_offset, target_offset
        self.flat_by_minute = flat_by

    def signals(self, bars, p):
        return pd.DataFrame({"close": bars["close"], "check": bars["minute"] == self.minute}, index=bars.index)

    def decide(self, sig, position, ctx):
        if position is None:
            c = sig["close"]
            return Enter("test entry",
                         stop=None if self.stop_offset is None else c - self.stop_offset,
                         target=None if self.target_offset is None else c + self.target_offset)
        return None

    def sizing(self, p):
        return Sizing("percent_of_capital", 100)


def cfg(**kw):
    return bi.IntradayConfig(date(2024, 1, 1), date(2024, 12, 31), **{"capital": 10_000, "cost_bp": 0, **kw})


def test_prepare_session_and_minute_across_dst():
    df = minute_bars({date(2024, 3, 8): [100] * 3, date(2024, 3, 11): [100] * 3})  # DST starts 3/10
    assert list(df["minute"]) == [0, 1, 2, 0, 1, 2]
    assert set(df["session"]) == {date(2024, 3, 8), date(2024, 3, 11)}


def test_enters_next_bar_open_and_is_flat_at_close():
    closes = list(np.linspace(100, 139, 390))
    bars = {"AAA": minute_bars({date(2024, 1, 2): closes})}
    r = bi.run(AtMinute(minute=10), Params(x=0), bars, cfg(cost_bp=10))
    (t,) = r.trades
    assert t.entry_price == pytest.approx(closes[10] * 1.001)  # next bar opens at the previous close
    assert t.exit_price == pytest.approx(closes[-1] * 0.999)
    assert t.exit_reason == "flat at end of day"
    assert r.equity.index.tolist() == [date(2024, 1, 2)]


def test_strategy_stop_and_target_levels():
    down = [100.0] * 12 + [95.0] * 378
    bars = {"AAA": minute_bars({date(2024, 1, 2): down})}
    r = bi.run(AtMinute(minute=10, stop_offset=2), Params(x=0), bars, cfg())
    # The drop bar opens at 100 (the previous close), above the 98 stop, so it fills at the stop price.
    assert r.trades[0].exit_reason == "stop" and r.trades[0].exit_price == pytest.approx(98.0)

    gap = [100.0] * 12 + [95.0] * 378
    df = minute_bars({date(2024, 1, 2): gap})
    df.iloc[12, df.columns.get_loc("open")] = 95.0  # this bar opens below the stop: fill at the open
    r = bi.run(AtMinute(minute=10, stop_offset=2), Params(x=0), {"AAA": df}, cfg())
    assert r.trades[0].exit_price == pytest.approx(95.0)

    up = [100.0] * 12 + [103.0] * 378
    bars = {"AAA": minute_bars({date(2024, 1, 2): up})}
    r = bi.run(AtMinute(minute=10, target_offset=2), Params(x=0), bars, cfg())
    # The jump bar opens at 100, below the 102 target, so it fills at the target price.
    assert r.trades[0].exit_reason == "profit target" and r.trades[0].exit_price == pytest.approx(102.0)


def test_half_day_and_no_overnight_orders():
    bars = {"AAA": minute_bars({date(2024, 1, 2): [100.0] * 210, date(2024, 1, 3): [100.0] * 390})}
    # Minute 209 is the half day's last bar: that decision can't fill and isn't carried into
    # the next morning. The only trade is the full day's own minute-209 decision.
    r = bi.run(AtMinute(minute=209), Params(x=0), bars, cfg())
    (t,) = r.trades
    assert t.entry_date == date(2024, 1, 3) and t.bars_held == 180  # bought at 210, out at 389
    r = bi.run(AtMinute(minute=100), Params(x=0), bars, cfg())
    assert [t.exit_reason for t in r.trades] == ["flat at end of day"] * 2


def test_symbols_get_equal_slices():
    flat = [100.0] * 390
    bars = {"AAA": minute_bars({date(2024, 1, 2): flat}), "BBB": minute_bars({date(2024, 1, 2): flat})}
    r = bi.run(AtMinute(minute=10), Params(x=0), bars, cfg())
    assert all(t.qty * t.entry_price == pytest.approx(5_000) for t in r.trades)
    assert r.equity.iloc[-1] == pytest.approx(10_000)
    assert set(r.by_symbol) == {"AAA", "BBB"}


def momentum_days(first_half_up: bool) -> dict[date, list[float]]:
    day1 = [100.0] * 390
    move = 101.0 if first_half_up else 99.0
    day2 = [move] * 30 + [move] * 330 + list(np.linspace(move, move + 1, 30))  # rises into the close
    return {date(2024, 1, 2): day1, date(2024, 1, 3): day2}


def test_intraday_momentum_buys_last_half_hour_after_up_morning():
    bars = {"SPY": minute_bars(momentum_days(True))}
    r = bi.run(IntradayMomentum(), IntradayMomentum.defaults(), bars, cfg())
    (t,) = r.trades
    assert t.entry_date == date(2024, 1, 3) and "first half hour +1.00%" in t.entry_reason
    assert t.bars_held == 30  # 15:30 through 15:59


def test_intraday_momentum_stays_out_after_down_morning_or_small_move():
    bars = {"SPY": minute_bars(momentum_days(False))}
    assert bi.run(IntradayMomentum(), IntradayMomentum.defaults(), bars, cfg()).trades == []
    bars = {"SPY": minute_bars(momentum_days(True))}
    big_only = Params(min_move_bp=150, entry_minute=360)  # needs a 1.5% morning
    assert bi.run(IntradayMomentum(), big_only, bars, cfg()).trades == []


def test_intraday_momentum_does_not_look_ahead():
    rng = np.random.default_rng(3)
    days = {date(2024, 1, 2) + timedelta(days=i): list(100 + np.cumsum(rng.normal(0, 0.05, 390)))
            for i in range(6)}
    report = no_lookahead(IntradayMomentum(), minute_bars(days), IntradayMomentum.defaults())
    assert report.ok, report.problems


def orb_day(range_closes: list[float], rest: list[float]) -> list[float]:
    return range_closes + rest + [rest[-1]] * (390 - len(range_closes) - len(rest))


def test_orb_buys_up_range_with_stop_at_range_low_and_target():
    from trading_agent.strategies.orb import OpeningRangeBreakout

    # Bars open at the previous close; minute 0 opens at 100. Range closes up at 101; its low is 99.5.
    day = orb_day([99.5, 100.2, 100.5, 100.8, 101.0], [101.2, 101.5])
    bars = {"SPY": minute_bars({date(2024, 1, 2): [100.0], date(2024, 1, 3): day})}
    bars["SPY"].iloc[1, bars["SPY"].columns.get_loc("open")] = 100.0  # day 2 opens at 100
    r = bi.run(OpeningRangeBreakout(), OpeningRangeBreakout.defaults(), bars, cfg())
    (t,) = r.trades
    assert t.entry_price == pytest.approx(101.0)  # minute 5 opens at the range's last close
    assert "stop 99.50" in t.entry_reason
    assert t.exit_reason == "flat at end of day" and t.exit_price == pytest.approx(101.5)


def test_orb_stop_hit_and_down_range_skipped():
    from trading_agent.strategies.orb import OpeningRangeBreakout

    stopped = orb_day([100.0, 100.5, 100.2, 100.6, 101.0], [100.5, 99.0])
    bars = {"SPY": minute_bars({date(2024, 1, 3): stopped})}
    r = bi.run(OpeningRangeBreakout(), OpeningRangeBreakout.defaults(), bars, cfg())
    (t,) = r.trades
    assert t.exit_reason == "stop" and t.exit_price == pytest.approx(100.0)  # range low (first bar opens at 100)

    down = orb_day([99.8, 99.5, 99.2, 99.0, 98.8], [99.5, 100.0])
    bars = {"SPY": minute_bars({date(2024, 1, 3): down})}
    assert bi.run(OpeningRangeBreakout(), OpeningRangeBreakout.defaults(), bars, cfg()).trades == []


def test_orb_target_and_no_target_variant():
    from trading_agent.strategies.orb import OpeningRangeBreakout

    # The first bar opens and closes at 100.1 (the range low); the range closes at 100.5.
    # Risk 0.4, so the 10R target is 100.5 + 4.0 = 104.5.
    day = orb_day([100.1, 100.2, 100.3, 100.4, 100.5], list(np.linspace(100.6, 107, 50)))
    bars = {"SPY": minute_bars({date(2024, 1, 3): day})}
    r = bi.run(OpeningRangeBreakout(), OpeningRangeBreakout.defaults(), bars, cfg())
    assert r.trades[0].exit_reason == "profit target" and r.trades[0].exit_price == pytest.approx(104.5)
    r = bi.run(OpeningRangeBreakout(), Params(range_minutes=5, target_r=None), bars, cfg())
    assert r.trades[0].exit_reason == "flat at end of day" and r.trades[0].exit_price == pytest.approx(107.0)


def test_orb_does_not_look_ahead():
    from trading_agent.strategies.orb import OpeningRangeBreakout

    rng = np.random.default_rng(5)
    days = {date(2024, 1, 2) + timedelta(days=i): list(100 + np.cumsum(rng.normal(0, 0.05, 390)))
            for i in range(6)}
    for p in OpeningRangeBreakout.param_grid():
        assert no_lookahead(OpeningRangeBreakout(), minute_bars(days), p).ok


def gap_bars(day2_closes: list[float], day2_open: float) -> dict[str, pd.DataFrame]:
    """Day 1 ends at 100; day 2 opens at `day2_open` (a gap) and then follows `day2_closes`."""
    df = minute_bars({date(2024, 1, 2): [100.0] * 390, date(2024, 1, 3): day2_closes})
    i = 390  # first bar of day 2
    df.iloc[i, df.columns.get_loc("open")] = day2_open
    df.iloc[i, df.columns.get_loc("high")] = max(day2_open, day2_closes[0])
    df.iloc[i, df.columns.get_loc("low")] = min(day2_open, day2_closes[0])
    return {"SPY": df}


def test_gap_fill_buys_gap_down_and_sells_when_it_fills():
    from trading_agent.strategies.gaps import GapFill

    day2 = [99.0] * 20 + list(np.linspace(99.0, 100.5, 40)) + [100.5] * 330
    r = bi.run(GapFill(), Params(gap_pct=0.5), gap_bars(day2, 99.0), cfg())
    (t,) = r.trades
    assert t.entry_price == pytest.approx(99.0)  # 9:31 open
    assert t.exit_reason == "profit target" and t.exit_price == pytest.approx(100.0)  # yesterday's close
    # With gap_pct=1.0 a 0.9% gap down (open 99.1) is too small to trade.
    assert bi.run(GapFill(), Params(gap_pct=1.0), gap_bars(day2, 99.1), cfg()).trades == []


def test_gap_go_needs_the_gap_to_hold():
    from trading_agent.strategies.gaps import GapGo

    held = [101.2] * 15 + list(np.linspace(101.2, 102, 50)) + [102.0] * 325
    r = bi.run(GapGo(), Params(gap_pct=0.5, confirm_minutes=15), gap_bars(held, 101.0), cfg())
    (t,) = r.trades
    assert t.exit_reason == "flat at end of day" and t.exit_price == pytest.approx(102.0)
    assert "stop 101.00" in t.entry_reason  # early range low = the open

    faded = [100.8] * 10 + [99.8] * 5 + [100.5] * 375  # dips below yesterday's close (100)
    assert bi.run(GapGo(), Params(gap_pct=0.5, confirm_minutes=15), gap_bars(faded, 101.0), cfg()).trades == []


@pytest.mark.parametrize("name", ["gap_fill", "gap_go"])
def test_gap_strategies_do_not_look_ahead(name):
    from trading_agent.strategies import REGISTRY

    cls = REGISTRY[name]
    rng = np.random.default_rng(11)
    days = {date(2024, 1, 2) + timedelta(days=i): list(100 + np.cumsum(rng.normal(0, 0.08, 390)))
            for i in range(6)}
    for p in cls.param_grid():
        assert no_lookahead(cls(), minute_bars(days), p).ok


def vwap_day() -> dict[str, pd.DataFrame]:
    # Rises for an hour (above VWAP), then falls well below it, then stays flat.
    closes = list(np.linspace(100, 101, 60)) + list(np.linspace(101, 99, 140)) + [99.0] * 190
    return {"QQQ": minute_bars({date(2024, 1, 3): closes})}


def test_vwap_holds_above_and_exits_on_close_below():
    from trading_agent.strategies.vwap_trend import VWAPTrend

    bars = vwap_day()
    r = bi.run(VWAPTrend(), Params(bar_minutes=1, confirm=1), bars, cfg())
    (t,) = r.trades
    closes = bars["QQQ"]["close"].to_numpy()
    assert t.entry_price == pytest.approx(closes[1])  # minute 1 closed above VWAP -> buy at minute 2's open
    assert t.exit_reason.startswith("closed below VWAP")
    assert 60 < t.bars_held < 200  # sold during the fall, well before the close


def test_vwap_confirmation_delays_entry():
    from trading_agent.strategies.vwap_trend import VWAPTrend

    bars = vwap_day()
    closes = bars["QQQ"]["close"].to_numpy()
    r = bi.run(VWAPTrend(), Params(bar_minutes=1, confirm=3), bars, cfg())
    assert r.trades[0].entry_price == pytest.approx(closes[3])  # needs minutes 1-3 above
    r5 = bi.run(VWAPTrend(), Params(bar_minutes=5, confirm=1), bars, cfg())
    assert r5.trades[0].entry_price == pytest.approx(closes[4])  # first 5-minute bar closes at minute 4


def test_vwap_does_not_look_ahead():
    from trading_agent.strategies.vwap_trend import VWAPTrend

    rng = np.random.default_rng(13)
    days = {date(2024, 1, 2) + timedelta(days=i): list(100 + np.cumsum(rng.normal(0, 0.05, 390)))
            for i in range(6)}
    for p in VWAPTrend.param_grid():
        assert no_lookahead(VWAPTrend(), minute_bars(days), p).ok


def test_spread_math_and_summary():
    assert spreads.half_spread_bp(99.99, 100.01) == pytest.approx(1.0)
    samples = [spreads.SpreadSample("SPY", date(2024, 1, 2), t, bp, 10)
               for t, bp in [(time(9, 31), 0.3), (time(12, 30), 0.1), (time(15, 50), 0.2)]]
    s = spreads.summarize(samples)["SPY"]
    assert s["all"]["median"] == pytest.approx(0.2)
    assert s["open (9:31-10:00)"]["median"] == pytest.approx(0.3)
    assert spreads.pick_sessions([date(2024, 1, d) for d in range(1, 31)], 5) == \
        spreads.pick_sessions([date(2024, 1, d) for d in range(1, 31)], 5)  # repeatable
