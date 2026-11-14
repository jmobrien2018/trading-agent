"""Trend following: hold each symbol while its close is above its N-day average; otherwise cash.

Research: plan doc, "Swing deep research" tab, section 1.
"""

from __future__ import annotations

import pandas as pd

from ..indicators import sma
from ..strategy import Context, ExitRules, Param, Params, Position, Sizing, Strategy, Target


class TrendMA(Strategy):
    name = "trend_ma"
    version = 1
    family = "trend"
    style = "target"
    description = "Hold each symbol while its close is above its N-day average; otherwise cash."
    timeframe = "1Day"
    warmup_bars = 200
    params = {
        "ma_days": Param(default=200, test=(100, 150, 200)),
    }

    def signals(self, bars: pd.DataFrame, p: Params) -> pd.DataFrame:
        average = sma(bars["close"], p.ma_days)
        return pd.DataFrame(
            {"close": bars["close"], "average": average, "in_trend": bars["close"] > average},
            index=bars.index,
        ).where(average.notna())

    def decide(self, sig: pd.Series, position: Position | None, ctx: Context) -> Target:
        in_trend = bool(sig["in_trend"])
        weight = 1 / len(ctx.symbols) if in_trend else 0.0
        side = "above" if in_trend else "below"
        return Target(weight, f"close {sig['close']:.2f} {side} {ctx.p.ma_days}-day average {sig['average']:.2f}")

    def exits(self, p: Params) -> ExitRules:
        return ExitRules(signal=True)

    def sizing(self, p: Params) -> Sizing:
        return Sizing("target_weight")
