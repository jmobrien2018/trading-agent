"""Trend following where a leveraged ETF takes its signal from the index it tracks.

Theory: TQQQ is 3x QQQ, reset daily. Its own price swings 3x as much and loses ground in choppy
markets (volatility decay), so it crosses its moving average more often than the real trend does.
Using QQQ's trend to decide when to hold TQQQ should mean fewer false signals and results that
depend less on the average's length. Other symbols use their own signal, as in trend_ma.
"""

from __future__ import annotations

from ..strategy import Params
from .trend_ma import TrendMA

# Leveraged ETF -> the index ETF whose trend it should follow.
UNDERLYING = {"TQQQ": "QQQ", "QLD": "QQQ", "UPRO": "SPY", "SSO": "SPY"}


class TrendMAUnderlying(TrendMA):
    name = "trend_ma_underlying"
    version = 1
    description = "Like trend_ma, but leveraged ETFs follow their index's trend (TQQQ trades on QQQ's)."

    def signal_symbol(self, symbol: str, p: Params) -> str:
        return UNDERLYING.get(symbol, symbol)
