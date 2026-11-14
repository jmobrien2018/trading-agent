"""Intraday (1-minute) backtest engine for day strategies. Positions never stay overnight.

Each symbol trades its own fixed slice of the capital (capital / number of symbols) and the
slices are added up, so symbols never compete for cash. Day strategies hold at most one
position per symbol, so this matches how they run.

For each minute bar of a regular session, in order:
1. Fill the order decided at the previous bar's close, at this bar's open, paying `cost_bp`.
   Orders never carry over to the next session.
2. Check the stop and target against this bar's high and low (stop first: the cautious case).
   A stop fills at the stop price, or at the open if price gapped through it.
3. At the strategy's flat-by bar, or the session's last bar (half days), close at the bar's close.
4. Otherwise ask the strategy to decide, on bars where its `check` signal column is True
   (or every bar if it has none), using signals up to this bar's close.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from .backtest import Trade, metrics
from .data_quality import NEW_YORK
from .strategy import Context, Enter, Exit, Params, Position, Strategy, Target


@dataclass(frozen=True)
class IntradayConfig:
    start: date
    end: date
    capital: float = 10_000
    cost_bp: float = 1.0
    position_cap_pct: float = 100.0  # of the symbol's slice
    fill: str = "next bar"  # orders fill at the next minute's open (recorded with saved runs)


@dataclass
class IntradayResult:
    strategy: str
    version: int
    params: Params
    config: IntradayConfig
    equity: pd.Series  # end-of-day total equity, indexed by session date
    exposure: pd.Series  # share of minutes with a position, per day (averaged across symbols)
    trades: list[Trade]
    by_symbol: dict[str, dict[str, Any]] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)


def prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Add `session` (New York date) and `minute` (minutes after 9:30) columns to minute bars
    indexed by UTC timestamp. Strategies' signals and the engine both rely on them."""
    ny = df.index.tz_convert(NEW_YORK)
    out = df.copy()
    out["session"] = ny.date
    out["minute"] = (ny.hour * 60 + ny.minute - 570).astype(int)
    return out


def run(strategy: Strategy, p: Params, bars: dict[str, pd.DataFrame], cfg: IntradayConfig) -> IntradayResult:
    """`bars`: symbol -> prepared 1-minute bars (see prepare). Signals use bars up to cfg.end only."""
    if strategy.style != "event":
        raise ValueError("intraday engine supports event-style strategies (Enter / Exit)")
    symbols = tuple(bars)
    slice_capital = cfg.capital / len(symbols)
    trades: list[Trade] = []
    daily: dict[str, pd.Series] = {}
    exposure: dict[str, pd.Series] = {}
    by_symbol: dict[str, dict[str, Any]] = {}

    for s in symbols:
        df = bars[s]
        df = df[df["session"] <= cfg.end]
        sig = strategy.signals(df, p)
        keep = (df["session"] >= cfg.start).to_numpy()
        eq, ex, tr = _run_symbol(strategy, p, s, symbols, df[keep], sig[keep], slice_capital, cfg)
        daily[s], exposure[s] = eq, ex
        trades += tr
        by_symbol[s] = metrics(eq, tr, ex, start_value=slice_capital)

    equity = pd.DataFrame(daily).ffill().fillna(slice_capital).sum(axis=1)
    expo = pd.DataFrame(exposure).mean(axis=1)
    result = IntradayResult(strategy.name, strategy.version, p, cfg, equity, expo, trades, by_symbol)
    result.metrics = metrics(equity, trades, expo, start_value=cfg.capital)
    return result


def _run_symbol(
    strategy: Strategy, p: Params, symbol: str, symbols: tuple[str, ...], df: pd.DataFrame, sig: pd.DataFrame,
    capital: float, cfg: IntradayConfig,
) -> tuple[pd.Series, pd.Series, list[Trade]]:
    c = cfg.cost_bp / 10_000
    rules = strategy.exits(p)
    sizing = strategy.sizing(p)
    o, h, l, cl = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close"))
    session = df["session"].to_numpy()
    minute = df["minute"].to_numpy()
    n = len(df)
    last_of_session = np.ones(n, dtype=bool)
    if n > 1:
        last_of_session[:-1] = session[1:] != session[:-1]
    flat = (minute >= strategy.flat_by_minute) | last_of_session

    cols = list(sig.columns)
    arrays = {k: sig[k].to_numpy() for k in cols}
    check = arrays["check"].astype(bool) if "check" in arrays else np.ones(n, dtype=bool)
    ctx = Context(p, symbol, symbols)

    cash = capital
    pos: Position | None = None
    target_px: float | None = None
    pending: tuple[str, Any] | None = None  # ("buy", Enter) | ("sell", reason)
    trades: list[Trade] = []
    day_equity: dict[date, float] = {}
    day_minutes: dict[date, list[int]] = {}

    def close(i: int, price: float, reason: str) -> None:
        nonlocal cash, pos, target_px
        cash += pos.qty * price
        trades.append(Trade(symbol, session[i], pos.entry_price, session[i], price, pos.qty, pos.entry_reason,
                            reason, pos.bars_held))
        pos, target_px = None, None

    for i in range(n):
        if i > 0 and session[i] != session[i - 1]:
            pending = None  # orders never carry into a new session

        # 1. Fill the previous bar's order at this bar's open.
        if pending is not None and not flat[i]:
            side, what = pending
            if side == "buy" and pos is None:
                price = o[i] * (1 + c)
                equity = cash
                dollars = min(_size(sizing, equity), equity * cfg.position_cap_pct / 100, cash)
                if dollars >= 1.0:
                    qty = dollars / price
                    cash -= dollars
                    stop = what.stop
                    if stop is None and rules.stop_pct is not None:
                        stop = price * (1 - rules.stop_pct / 100)
                    target_px = what.target
                    if target_px is None and rules.target_pct is not None:
                        target_px = price * (1 + rules.target_pct / 100)
                    pos = Position(qty, price, session[i], what.reason, stop=stop, highest_close=price, bars_held=0)
            elif side == "sell" and pos is not None:
                close(i, o[i] * (1 - c), what)
        pending = None

        # 2. Stop, then target, during the bar.
        if pos is not None:
            if pos.stop is not None and l[i] <= pos.stop:
                close(i, min(o[i], pos.stop) * (1 - c), "stop")
            elif target_px is not None and h[i] >= target_px:
                close(i, max(o[i], target_px) * (1 - c), "profit target")

        # 3. Flat by the strategy's time or the session's last bar.
        if pos is not None:
            pos.bars_held += 1
            day_minutes.setdefault(session[i], [0, 0])[0] += 1
            if flat[i]:
                close(i, cl[i] * (1 - c), "flat at end of day")
        day_minutes.setdefault(session[i], [0, 0])[1] += 1

        # 4. Decide at this bar's close, for the next bar's open.
        if not flat[i] and check[i]:
            row = {k: arrays[k][i] for k in cols}
            if not any(isinstance(v, float) and math.isnan(v) for v in row.values()):
                if pos is not None and rules.time_stop_bars is not None and pos.bars_held >= rules.time_stop_bars:
                    pending = ("sell", f"time stop after {pos.bars_held} bars")
                else:
                    intent = strategy.decide(row, pos, ctx)
                    if isinstance(intent, Enter) and pos is None:
                        pending = ("buy", intent)
                    elif isinstance(intent, Exit) and pos is not None and rules.signal:
                        pending = ("sell", intent.reason)
                    elif isinstance(intent, Target):
                        raise ValueError("intraday engine supports event-style intents only")

        if last_of_session[i]:
            day_equity[session[i]] = cash  # always flat at the end of a session

    equity = pd.Series(day_equity, dtype=float)
    equity.index.name = "date"
    exposure = pd.Series({d: (v[0] / v[1] if v[1] else 0.0) for d, v in day_minutes.items()}, dtype=float)
    return equity, exposure.reindex(equity.index).fillna(0.0), trades


def _size(sizing, equity: float) -> float:
    if sizing.method == "fixed_dollars":
        return sizing.amount
    if sizing.method == "percent_of_capital":
        return equity * sizing.amount / 100
    raise ValueError(f"sizing {sizing.method!r} isn't supported intraday")
