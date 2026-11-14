"""Opening range breakout (Zarattini & Aziz, 2023), long-only.

Take the direction of the first `range_minutes` of the session. If it closed up (above the
9:30 open), buy at the next bar's open with a stop at the range's low and a target of
`target_r` times the risk; otherwise stay in cash (we don't short). Out at the close.
Research: plan doc, "Day deep research" tab, section 1.
"""

from __future__ import annotations

import pandas as pd

from ..strategy import Context, Enter, ExitRules, Param, Params, Position, Sizing, Strategy


class OpeningRangeBreakout(Strategy):
    name = "orb"
    version = 1
    family = "trend"
    style = "event"
    description = "If the opening range closes up, buy; stop at its low, target 10x the risk, out at the close."
    timeframe = "1Min"
    warmup_bars = 0
    flat_by_minute = 389
    params = {
        "range_minutes": Param(default=5, test=(5, 15, 30)),
        "target_r": Param(default=10, test=(10, None)),  # None = no target, hold to the close
    }

    def signals(self, bars: pd.DataFrame, p: Params) -> pd.DataFrame:
        session, minute = bars["session"], bars["minute"]
        in_range = minute < p.range_minutes
        # Running values within the opening range; at its last bar they cover the whole range.
        range_open = bars["open"].where(minute == 0).groupby(session).ffill()
        range_low = bars["low"].where(in_range).groupby(session).cummin()
        return pd.DataFrame(
            {
                "close": bars["close"],
                "range_open": range_open,
                "range_low": range_low,
                "check": minute == p.range_minutes - 1,
            },
            index=bars.index,
        )

    def decide(self, sig, position: Position | None, ctx: Context) -> Enter | None:
        close, open_, low = sig["close"], sig["range_open"], sig["range_low"]
        risk = close - low
        if position is None and close > open_ and risk > 0:
            target = close + ctx.p.target_r * risk if ctx.p.target_r is not None else None
            return Enter(
                f"{ctx.p.range_minutes}-min range up {(close / open_ - 1) * 100:+.2f}%; stop {low:.2f}",
                stop=float(low),
                target=None if target is None else float(target),
            )
        return None

    def exits(self, p: Params) -> ExitRules:
        return ExitRules(signal=True)  # stop and target come with the entry; out at the close

    def sizing(self, p: Params) -> Sizing:
        return Sizing("percent_of_capital", 100)
