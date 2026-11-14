"""All strategies, by name. Add new strategy classes here."""

from __future__ import annotations

from ..strategy import Strategy
from .gaps import GapFill, GapGo
from .intraday_momentum import IntradayMomentum
from .orb import OpeningRangeBreakout
from .rsi2_pullback import RSI2Pullback
from .trend_ma import TrendMA
from .trend_ma_underlying import TrendMAUnderlying
from .vwap_trend import VWAPTrend

REGISTRY: dict[str, type[Strategy]] = {
    cls.name: cls
    for cls in (TrendMA, RSI2Pullback, TrendMAUnderlying, IntradayMomentum, OpeningRangeBreakout, GapFill, GapGo,
                VWAPTrend)
}
