from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from trading_agent import backtest, indicators
from trading_agent.backtest import BacktestConfig
from trading_agent.strategies import REGISTRY
from trading_agent.strategies.rsi2_pullback import RSI2Pullback
from trading_agent.strategies.trend_ma import TrendMA
from trading_agent.strategy import (
    Enter, Exit, ExitRules, Param, Params, Sizing, Strategy, Target, no_lookahead,
)


def make_bars(closes, start=date(2024, 1, 1), opens=None, highs=None, lows=None) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    opens = closes if opens is None else np.asarray(opens, dtype=float)
    highs = np.maximum(opens, closes) if highs is None else np.asarray(highs, dtype=float)
    lows = np.minimum(opens, closes) if lows is None else np.asarray(lows, dtype=float)
    idx = pd.Index([start + timedelta(days=i) for i in range(len(closes))], name="date")
    return pd.DataFrame({"open": opens, "high": highs, "low": lows, "close": closes, "volume": 1000}, index=idx)


def random_walk(n=400, seed=1) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    closes = 100 * np.exp(np.cumsum(rng.normal(0.0005, 0.015, n)))
    opens = closes * (1 + rng.normal(0, 0.003, n))
    return make_bars(closes, opens=opens, highs=np.maximum(opens, closes) * 1.005, lows=np.minimum(opens, closes) * 0.995)


class Scripted(Strategy):
    """Test strategy: enter on given day numbers, exit on others."""

    name, version, family, style, description, warmup_bars = "scripted", 1, "dip-buying", "event", "test", 1
    params = {"x": Param(0, (0,))}

    def __init__(self, enter_days=(), exit_days=(), rules=ExitRules(), ranks=None):
        self.enter_days, self.exit_days, self.rules, self.ranks = set(enter_days), set(exit_days), rules, ranks or {}

    def signals(self, bars, p):
        return pd.DataFrame({"day": np.arange(len(bars), dtype=float), "atr": 1.0}, index=bars.index)

    def decide(self, sig, position, ctx):
        day = int(sig["day"])
        if position is None and day in self.enter_days:
            return Enter("scripted entry", rank=self.ranks.get(ctx.symbol))
        if position is not None and day in self.exit_days:
            return Exit("scripted exit")
        return None

    def exits(self, p):
        return self.rules

    def sizing(self, p):
        return Sizing("percent_of_capital", 20)


def cfg(bars, **kw) -> BacktestConfig:
    idx = next(iter(bars.values())).index
    return BacktestConfig(start=idx[0], end=idx[-1], **{"capital": 10_000, "cost_bp": 0, "position_cap": 1e9, **kw})


# --- indicators and the format ------------------------------------------------------------------


def test_indicators_basic_values():
    s = pd.Series([1.0, 2, 3, 4, 5])
    assert indicators.sma(s, 3).tolist()[2:] == [2.0, 3.0, 4.0]
    assert indicators.sma(s, 3).isna().sum() == 2
    assert indicators.rsi(s, 2).iloc[-1] == 100.0  # only gains
    falling = pd.Series([5.0, 4, 3, 2, 1])
    assert indicators.rsi(falling, 2).iloc[-1] == pytest.approx(0.0)
    bars = make_bars([10, 10, 10, 10], highs=[11] * 4, lows=[9] * 4)
    assert indicators.atr(bars, 2).iloc[-1] == pytest.approx(2.0)


def test_param_grids_match_spec():
    assert len(TrendMA.param_grid()) == 3
    assert len(RSI2Pullback.param_grid()) == 8  # 2 x 2 x 2, as listed in the spec
    assert set(REGISTRY) == {"trend_ma", "rsi2_pullback", "trend_ma_underlying", "intraday_momentum", "orb",
                             "gap_fill", "gap_go", "vwap_trend"}


@pytest.mark.parametrize("cls", [TrendMA, RSI2Pullback])
def test_real_strategies_do_not_look_ahead(cls):
    report = no_lookahead(cls(), random_walk(), cls.defaults())
    assert report.ok, report.problems


def test_lookahead_check_catches_a_leak():
    class Leaky(TrendMA):
        def signals(self, bars, p):
            sig = super().signals(bars, p)
            sig["average"] = bars["close"].shift(-1)  # tomorrow's close: cheating
            return sig

    assert not no_lookahead(Leaky(), random_walk(), Params(ma_days=50)).ok


# --- the engine ---------------------------------------------------------------------------------


def test_entry_and_exit_fill_at_next_open_with_costs():
    bars = {"AAA": make_bars(closes=[100] * 6, opens=[100, 100, 110, 110, 120, 120])}
    strat = Scripted(enter_days={1}, exit_days={3})
    r = backtest.run(strat, Params(x=0), bars, cfg(bars, cost_bp=10))
    (t,) = r.trades
    assert t.entry_date == date(2024, 1, 3) and t.entry_price == pytest.approx(110 * 1.001)
    assert t.exit_date == date(2024, 1, 5) and t.exit_price == pytest.approx(120 * 0.999)
    assert t.qty * t.entry_price == pytest.approx(2_000)  # 20% of $10,000
    assert t.exit_reason == "scripted exit"


def test_stop_fills_at_stop_or_at_gapped_open():
    rules = ExitRules(stop_pct=10)
    touch = {"AAA": make_bars([100, 100, 100, 100], opens=[100, 100, 100, 95], lows=[100, 100, 100, 85])}
    r = backtest.run(Scripted(enter_days={1}, rules=rules), Params(x=0), touch, cfg(touch))
    assert r.trades[0].exit_reason == "stop" and r.trades[0].exit_price == pytest.approx(90)

    gap = {"AAA": make_bars([100, 100, 100, 80], opens=[100, 100, 100, 80], lows=[100, 100, 100, 79])}
    r = backtest.run(Scripted(enter_days={1}, rules=rules), Params(x=0), gap, cfg(gap))
    assert r.trades[0].exit_price == pytest.approx(80)  # gapped through: open, not the stop


def test_time_stop():
    bars = {"AAA": make_bars([100] * 10)}
    r = backtest.run(Scripted(enter_days={0}, rules=ExitRules(time_stop_bars=3)), Params(x=0), bars, cfg(bars))
    (t,) = r.trades
    assert t.exit_reason.startswith("time stop") and t.bars_held == 3
    assert (t.exit_date - t.entry_date).days == 3


def test_short_cash_fills_lowest_rank_first_and_skips_rest():
    syms = [f"S{i}" for i in range(7)]
    bars = {s: make_bars([100] * 5) for s in syms}
    ranks = {s: float(10 - i) for i, s in enumerate(syms)}  # S6 has the lowest rank (deepest dip)
    r = backtest.run(Scripted(enter_days={0}, ranks=ranks), Params(x=0), bars, cfg(bars))
    bought = {t.symbol for t in r.trades}
    assert bought == {"S6", "S5", "S4", "S3", "S2"}  # 5 x 20% uses all the cash
    assert r.skipped_entries == 2


def test_position_cap_limits_size():
    bars = {"AAA": make_bars([100] * 5)}
    r = backtest.run(Scripted(enter_days={0}, exit_days={2}), Params(x=0), bars, cfg(bars, position_cap=500))
    assert r.trades[0].qty * r.trades[0].entry_price == pytest.approx(500)


def test_position_cap_as_percent_of_current_capital():
    bars = {"AAA": make_bars([100] * 5)}
    r = backtest.run(Scripted(enter_days={0}, exit_days={2}), Params(x=0), bars, cfg(bars, position_cap_pct=10))
    assert r.trades[0].qty * r.trades[0].entry_price == pytest.approx(1_000)  # 20% sizing capped at 10%


def test_fill_at_close_uses_same_day_close():
    bars = {"AAA": make_bars(closes=[100, 105, 110, 120, 130], opens=[100, 101, 108, 115, 125])}
    r = backtest.run(Scripted(enter_days={1}, exit_days={3}), Params(x=0), bars, cfg(bars, fill="close"))
    (t,) = r.trades
    assert t.entry_date == date(2024, 1, 2) and t.entry_price == pytest.approx(105)
    assert t.exit_date == date(2024, 1, 4) and t.exit_price == pytest.approx(120)
    # Equity on the entry day is marked after the close fill: cash moved into the position.
    assert r.exposure.iloc[1] == pytest.approx(0.2)
    with pytest.raises(ValueError):
        BacktestConfig(date(2024, 1, 1), date(2024, 2, 1), fill="noon")


def test_target_strategy_enters_once_and_exits_without_churn():
    up = list(np.linspace(100, 150, 60))
    down = list(np.linspace(149, 90, 30))
    bars = {"AAA": make_bars(up + down)}
    r = backtest.run(TrendMA(), Params(ma_days=10), bars, cfg(bars))
    entries = {t.entry_date for t in r.trades}
    assert len(entries) == 1  # bought once; no daily rebalancing trades
    assert r.trades[-1].exit_reason.startswith("close") and "below" in r.trades[-1].exit_reason


def test_leveraged_etf_follows_its_index_trend():
    from trading_agent.strategies.trend_ma_underlying import TrendMAUnderlying

    # QQQ climbs steadily; TQQQ (made up) dips below its own average late on.
    qqq = make_bars(list(np.linspace(100, 160, 80)))
    tqqq = make_bars(list(np.linspace(100, 200, 60)) + list(np.linspace(190, 150, 20)))
    c = cfg({"TQQQ": tqqq}, position_cap_pct=100)

    own = backtest.run(TrendMA(), Params(ma_days=10), {"TQQQ": tqqq}, c)
    via_index = backtest.run(TrendMAUnderlying(), Params(ma_days=10), {"TQQQ": tqqq}, c, signal_bars={"QQQ": qqq})
    assert any("below" in t.exit_reason for t in own.trades)  # own signal sold on the dip
    assert all(t.exit_reason == "end of test" for t in via_index.trades)  # index still trending: held

    with pytest.raises(ValueError, match="come from QQQ"):
        backtest.run(TrendMAUnderlying(), Params(ma_days=10), {"TQQQ": tqqq}, c)


def test_metrics_measure_from_money_put_in():
    eq = pd.Series([9_900.0, 11_000, 8_800, 12_000], index=[date(2024, 1, d) for d in (1, 2, 3, 4)])
    m = backtest.metrics(eq, start_value=10_000)
    assert m["total_return"] == pytest.approx(0.2)
    assert m["max_drawdown"] == pytest.approx(8_800 / 11_000 - 1)


def test_buy_and_hold_counts_entry_cost():
    bars = {"A": make_bars([100, 110]), "B": make_bars([100, 90])}
    c = cfg(bars, cost_bp=10)
    eq = backtest.buy_and_hold(bars, c)
    assert backtest.metrics(eq, start_value=c.capital)["total_return"] == pytest.approx(1 / 1.001 - 1)


def test_first_tradable_date_waits_for_every_symbol():
    bars = {"A": make_bars([1] * 10), "B": make_bars([1] * 8, start=date(2024, 1, 3))}
    assert backtest.first_tradable_date(bars, 5) == date(2024, 1, 7)
