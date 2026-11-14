"""VWAP trend (Zarattini & Aziz), long-only: hold while price is above the session's VWAP.

VWAP is the day's volume-weighted average of each bar's (high + low + close) / 3, reset every
morning. On each decision bar (every minute, or every `bar_minutes`), price is "above" if that
bar closes above VWAP. With `confirm` > 1 the side only changes after that many decision bars
in a row agree, which cuts the trade count. Long when above, cash when below; flat at the close.
Research: plan doc, "Day deep research" tab, section 3.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ..strategy import Context, Enter, Exit, ExitRules, Param, Params, Position, Sizing, Strategy


def session_vwap(bars: pd.DataFrame) -> pd.Series:
    typical = (bars["high"] + bars["low"] + bars["close"]) / 3
    volume = bars["volume"].astype(float)
    session = bars["session"]
    return (typical * volume).groupby(session).cumsum() / volume.groupby(session).cumsum()


class VWAPTrend(Strategy):
    name = "vwap_trend"
    version = 1
    family = "trend"
    style = "event"
    description = "Hold while price closes above the session VWAP; cash when below; flat at the close."
    timeframe = "1Min"
    warmup_bars = 0
    flat_by_minute = 389
    params = {
        "bar_minutes": Param(default=1, test=(1, 5)),
        "confirm": Param(default=1, test=(1, 3)),
    }

    def signals(self, bars: pd.DataFrame, p: Params) -> pd.DataFrame:
        vwap = session_vwap(bars)
        minute = bars["minute"]
        decision = (minute % p.bar_minutes) == (p.bar_minutes - 1)
        d = bars[decision]
        above = (d["close"] > vwap[decision]).astype(float)
        # Confirmed side: 1 after `confirm` decision bars above in a row, 0 after that many below,
        # otherwise unchanged from the previous decision bar (within the day).
        count = above.groupby(d["session"]).transform(lambda s: s.rolling(p.confirm).sum())
        state = pd.Series(np.where(count == p.confirm, 1.0, np.where(count == 0, 0.0, np.nan)), index=d.index)
        state = state.groupby(d["session"]).ffill()
        previous = state.groupby(d["session"]).shift(1)
        changed = state.notna() & (state != previous)
        return pd.DataFrame(
            {
                "vwap": vwap,
                "state": state.reindex(bars.index),
                # The decision only changes when the confirmed side changes, so only ask then.
                "check": changed.reindex(bars.index, fill_value=False).astype(bool),
            },
            index=bars.index,
        )

    def decide(self, sig, position: Position | None, ctx: Context) -> Enter | Exit | None:
        if position is None and sig["state"] == 1.0:
            return Enter(f"closed above VWAP {sig['vwap']:.2f}")
        if position is not None and sig["state"] == 0.0:
            return Exit(f"closed below VWAP {sig['vwap']:.2f}")
        return None

    def exits(self, p: Params) -> ExitRules:
        return ExitRules(signal=True)

    def sizing(self, p: Params) -> Sizing:
        return Sizing("percent_of_capital", 100)
