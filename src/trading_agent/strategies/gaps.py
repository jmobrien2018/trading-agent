"""Gap strategies, long-only (Research: plan doc, "Day deep research" tab, section 4).

- gap_fill: after a gap DOWN of at least `gap_pct`, buy right after the open and sell when price
  gets back to yesterday's close (the gap "fills"), or at the close.
- gap_go: after a gap UP of at least `gap_pct`, if the first `confirm_minutes` close above the
  open without falling back to yesterday's close, buy; stop at that early range's low; out at the close.

The engine fills at the next minute's open, so "right after the open" means 9:31.
"""

from __future__ import annotations

import pandas as pd

from ..strategy import Context, Enter, ExitRules, Param, Params, Position, Sizing, Strategy


def _gap_columns(bars: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """(yesterday's final close, today's 9:30 open), carried through each session. Causal."""
    session, minute = bars["session"], bars["minute"]
    prev_close = session.map(bars.groupby("session")["close"].last().shift(1)).astype(float)
    day_open = bars["open"].where(minute == 0).groupby(session).ffill()
    return prev_close, day_open


class GapFill(Strategy):
    name = "gap_fill"
    version = 1
    family = "dip-buying"
    style = "event"
    description = "After a gap down, buy at the open; sell when the gap fills (yesterday's close) or at the close."
    timeframe = "1Min"
    warmup_bars = 1
    flat_by_minute = 389
    params = {"gap_pct": Param(default=0.5, test=(0.3, 0.5, 1.0))}

    def signals(self, bars: pd.DataFrame, p: Params) -> pd.DataFrame:
        prev_close, day_open = _gap_columns(bars)
        return pd.DataFrame(
            {"prev_close": prev_close, "day_open": day_open, "check": bars["minute"] == 0},
            index=bars.index,
        )

    def decide(self, sig, position: Position | None, ctx: Context) -> Enter | None:
        gap = sig["day_open"] / sig["prev_close"] - 1
        if position is None and gap <= -ctx.p.gap_pct / 100:
            return Enter(f"gap down {gap * 100:.2f}%; target yesterday's close {sig['prev_close']:.2f}",
                         target=float(sig["prev_close"]))
        return None

    def exits(self, p: Params) -> ExitRules:
        return ExitRules(signal=True)

    def sizing(self, p: Params) -> Sizing:
        return Sizing("percent_of_capital", 100)


class GapGo(Strategy):
    name = "gap_go"
    version = 1
    family = "trend"
    style = "event"
    description = "After a gap up that holds for the first minutes, buy; stop at their low; out at the close."
    timeframe = "1Min"
    warmup_bars = 1
    flat_by_minute = 389
    params = {
        "gap_pct": Param(default=0.5, test=(0.3, 0.5, 1.0)),
        "confirm_minutes": Param(default=15, test=(5, 15)),
    }

    def signals(self, bars: pd.DataFrame, p: Params) -> pd.DataFrame:
        prev_close, day_open = _gap_columns(bars)
        minute = bars["minute"]
        early_low = bars["low"].where(minute < p.confirm_minutes).groupby(bars["session"]).cummin()
        return pd.DataFrame(
            {
                "close": bars["close"],
                "prev_close": prev_close,
                "day_open": day_open,
                "early_low": early_low,
                "check": minute == p.confirm_minutes - 1,
            },
            index=bars.index,
        )

    def decide(self, sig, position: Position | None, ctx: Context) -> Enter | None:
        gap = sig["day_open"] / sig["prev_close"] - 1
        holding = sig["close"] > sig["day_open"] and sig["early_low"] > sig["prev_close"]
        if position is None and gap >= ctx.p.gap_pct / 100 and holding and sig["close"] > sig["early_low"]:
            return Enter(f"gap up {gap * 100:.2f}% held {ctx.p.confirm_minutes} min; stop {sig['early_low']:.2f}",
                         stop=float(sig["early_low"]))
        return None

    def exits(self, p: Params) -> ExitRules:
        return ExitRules(signal=True)

    def sizing(self, p: Params) -> Sizing:
        return Sizing("percent_of_capital", 100)
