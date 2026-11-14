"""Intraday momentum (Gao, Han, Li & Zhou, 2018): the first half hour predicts the last half hour.

If the return from yesterday's close to 10:00 is positive (above `min_move_bp`), buy at 15:30
and sell at the close. Long-only, so negative mornings mean cash.
Research: plan doc, "Day deep research" tab, section 2.
"""

from __future__ import annotations

import pandas as pd

from ..strategy import Context, Enter, ExitRules, Param, Params, Position, Sizing, Strategy

FIRST_HALF_HOUR_LAST_MINUTE = 29  # the 9:59 bar closes the first half hour


class IntradayMomentum(Strategy):
    name = "intraday_momentum"
    version = 1
    family = "trend"
    style = "event"
    description = "If the market is up from yesterday's close to 10:00, hold from 15:30 to the close."
    timeframe = "1Min"
    warmup_bars = 1
    flat_by_minute = 389  # sell at the 15:59 bar's close
    params = {
        "min_move_bp": Param(default=0, test=(0, 25, 50)),  # first-half-hour move needed, in bp
        "entry_minute": Param(default=360, test=(360,)),  # 15:30
    }

    def signals(self, bars: pd.DataFrame, p: Params) -> pd.DataFrame:
        session, minute = bars["session"], bars["minute"]
        prev_close = bars.groupby("session")["close"].last().shift(1)  # yesterday's final close
        prev = session.map(prev_close).astype(float)
        # Price at 10:00: the last close at or before the 9:59 bar, carried through the day.
        first = bars["close"].where(minute <= FIRST_HALF_HOUR_LAST_MINUTE).groupby(session).ffill()
        return pd.DataFrame(
            {"first_half_return": first / prev - 1, "check": minute == p.entry_minute - 1},
            index=bars.index,
        )

    def decide(self, sig, position: Position | None, ctx: Context) -> Enter | None:
        r = sig["first_half_return"]
        if position is None and r > ctx.p.min_move_bp / 10_000:
            return Enter(f"first half hour {r * 100:+.2f}% from yesterday's close")
        return None

    def exits(self, p: Params) -> ExitRules:
        return ExitRules(signal=True)  # no stops: out at the close

    def sizing(self, p: Params) -> Sizing:
        return Sizing("percent_of_capital", 100)
