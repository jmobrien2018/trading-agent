"""2-day RSI mean reversion (Connors): buy short, sharp dips in an uptrend; sell on the bounce.

Research: plan doc, "Swing deep research" tab, section 3.
"""

from __future__ import annotations

import pandas as pd

from ..indicators import atr, rsi, sma
from ..strategy import Context, Enter, Exit, ExitRules, Param, Params, Position, Sizing, Strategy


class RSI2Pullback(Strategy):
    name = "rsi2_pullback"
    version = 1
    family = "dip-buying"
    style = "event"
    description = "Buy short, sharp dips in an uptrend; sell on the bounce."
    timeframe = "1Day"
    warmup_bars = 200
    params = {
        "trend_days": Param(default=200, test=(200,)),
        "rsi_entry": Param(default=5, test=(5, 10)),
        "exit_days": Param(default=5, test=(5,)),
        "time_stop": Param(default=10, test=(5, 10)),
        "stop_atr": Param(default=None, test=(None, 3.0)),
    }

    def signals(self, bars: pd.DataFrame, p: Params) -> pd.DataFrame:
        close = bars["close"]
        trend = sma(close, p.trend_days)
        exit_avg = sma(close, p.exit_days)
        sig = pd.DataFrame(
            {
                "close": close,
                "uptrend": close > trend,
                "rsi2": rsi(close, 2),
                "bounced": close > exit_avg,
                "atr": atr(bars, 14),
            },
            index=bars.index,
        )
        return sig.where(trend.notna())

    def decide(self, sig: pd.Series, position: Position | None, ctx: Context) -> Enter | Exit | None:
        if position is None and bool(sig["uptrend"]) and sig["rsi2"] < ctx.p.rsi_entry:
            return Enter(
                f"2-day RSI {sig['rsi2']:.1f} below {ctx.p.rsi_entry}, close above {ctx.p.trend_days}-day average",
                rank=float(sig["rsi2"]),  # deepest dip first when cash is short
            )
        if position is not None and bool(sig["bounced"]):
            return Exit(f"close above {ctx.p.exit_days}-day average")
        return None

    def exits(self, p: Params) -> ExitRules:
        return ExitRules(signal=True, time_stop_bars=p.time_stop, stop_atr=p.stop_atr)

    def sizing(self, p: Params) -> Sizing:
        return Sizing("percent_of_capital", 20)
