"""Daily backtest engine (Phase 3). Runs any strategy written in the standard format.

Each day, in order (see the plan doc's "Strategy format spec" tab):
1. Fill yesterday's orders at today's open, paying `cost_bp` per side. Sells before buys;
   entries in rank order (lowest first); an entry that doesn't fit in cash is skipped.
2. Check stops and targets against today's high and low. A stop fills at the stop price,
   or at the open if price gapped through it.
3. Mark positions to today's close.
4. Time stops, then the strategy's decision for each symbol, from signals up to today's close.
5. Only with fill="close": those decisions fill at today's close instead of tomorrow's open.

Phase 3 enforces only the per-position cap (a % of current strategy capital); account-wide
limits arrive with the risk engine.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from .strategy import Context, Enter, Exit, ExitRules, Params, Position, Sizing, Strategy, Target

TARGET_BAND = 0.25  # target style: top up or trim only when a position drifts this far from target
TRADING_DAYS = 252
MIN_ORDER_USD = 1.0


OPEN, CLOSE = 0, 3  # positions in a day's (open, high, low, close) tuple


@dataclass(frozen=True)
class BacktestConfig:
    start: date
    end: date
    capital: float = 10_000
    cost_bp: float = 5
    position_cap: float = math.inf  # max dollars per position
    position_cap_pct: float | None = None  # max % of current strategy capital per position
    fill: str = "open"  # "open": next day's open (default) | "close": same day's close

    def __post_init__(self) -> None:
        if self.fill not in ("open", "close"):
            raise ValueError("fill must be 'open' or 'close'")


@dataclass
class Trade:
    symbol: str
    entry_date: date
    entry_price: float  # includes cost
    exit_date: date
    exit_price: float  # includes cost
    qty: float
    entry_reason: str
    exit_reason: str
    bars_held: int

    @property
    def pnl(self) -> float:
        return self.qty * (self.exit_price - self.entry_price)

    @property
    def return_pct(self) -> float:
        return self.exit_price / self.entry_price - 1


@dataclass
class Order:
    symbol: str
    side: str  # "buy" | "sell"
    reason: str
    dollars: float = 0.0  # buys
    qty: float | None = None  # sells; None = whole position
    rank: float | None = None
    atr: float | None = None
    is_entry: bool = True  # False for target-style top-ups


@dataclass
class RunResult:
    strategy: str
    version: int
    params: Params
    config: BacktestConfig
    equity: pd.Series
    exposure: pd.Series
    trades: list[Trade]
    skipped_entries: int
    metrics: dict[str, Any] = field(default_factory=dict)


def run(
    strategy: Strategy,
    p: Params,
    bars: dict[str, pd.DataFrame],
    cfg: BacktestConfig,
    signal_bars: dict[str, pd.DataFrame] | None = None,
) -> RunResult:
    """Backtest one parameter set. `bars`: symbol -> daily OHLC DataFrame indexed by date, for the
    symbols traded. `signal_bars`: extra symbols used only as signal sources (see signal_symbol)."""
    c = cfg.cost_bp / 10_000
    rules: ExitRules = strategy.exits(p)
    sizing: Sizing = strategy.sizing(p)
    symbols = tuple(bars)

    # Signals use data up to the test's end date only, never beyond it.
    bars = {s: df.loc[: cfg.end] for s, df in bars.items()}
    sources = {**{s: df.loc[: cfg.end] for s, df in (signal_bars or {}).items()}, **bars}
    signals = {}
    for s, df in bars.items():
        src = strategy.signal_symbol(s, p)
        if src not in sources:
            raise ValueError(f"{strategy.name}: signals for {s} come from {src}, but there are no bars for {src}")
        # Signal rows line up with the traded symbol's trading days.
        signals[s] = strategy.signals(sources[src], p).reindex(df.index)
    ohlc = {s: {d: (r.open, r.high, r.low, r.close) for d, r in zip(df.index, df.itertuples())} for s, df in bars.items()}
    dates = sorted({d for df in bars.values() for d in df.index if cfg.start <= d <= cfg.end})

    cash = cfg.capital
    positions: dict[str, Position] = {}
    pending: list[Order] = []
    last_close: dict[str, float] = {}
    trades: list[Trade] = []
    equity_points: list[float] = []
    exposure_points: list[float] = []
    skipped = 0

    def close_out(s: str, qty: float, price: float, d: date, reason: str) -> None:
        nonlocal cash
        pos = positions[s]
        cash += qty * price
        trades.append(Trade(s, pos.entry_date, pos.entry_price, d, price, qty, pos.entry_reason, reason, pos.bars_held))
        pos.qty -= qty
        if pos.qty <= 1e-12:
            del positions[s]

    def fill_orders(orders: list[Order], today: dict[str, tuple], d: date, at: int) -> list[Order]:
        """Fill orders at today's open (at=OPEN) or close (at=CLOSE): sells first, then buys
        lowest rank first. Returns orders that couldn't fill (no bar today) for tomorrow."""
        nonlocal cash, skipped
        carry: list[Order] = []
        sells = [o for o in orders if o.side == "sell"]
        buys = sorted((o for o in orders if o.side == "buy"), key=lambda o: math.inf if o.rank is None else o.rank)
        for o in sells:
            if o.symbol not in today or o.symbol not in positions:
                if o.symbol in positions:
                    carry.append(o)  # no bar today (halt or missing data): try again tomorrow
                continue
            pos = positions[o.symbol]
            qty = pos.qty if o.qty is None else min(o.qty, pos.qty)
            close_out(o.symbol, qty, today[o.symbol][at] * (1 - c), d, o.reason)
        for o in buys:
            if o.symbol not in today:
                carry.append(o)
                continue
            dollars = o.dollars
            if dollars > cash + 1e-9:
                if o.is_entry:
                    skipped += 1  # not enough cash: skip, per the spec
                    continue
                dollars = cash
            if dollars < MIN_ORDER_USD:
                continue
            price = today[o.symbol][at] * (1 + c)
            qty = dollars / price  # fractional shares allowed
            cash -= dollars
            if o.symbol in positions:  # target-style top-up: average the entry price
                pos = positions[o.symbol]
                pos.entry_price = (pos.qty * pos.entry_price + qty * price) / (pos.qty + qty)
                pos.qty += qty
            else:
                stop = None
                if rules.stop_pct is not None:
                    stop = price * (1 - rules.stop_pct / 100)
                if rules.stop_atr is not None and o.atr and not math.isnan(o.atr):
                    atr_stop = price - rules.stop_atr * o.atr
                    stop = atr_stop if stop is None else max(stop, atr_stop)
                positions[o.symbol] = Position(
                    qty, price, d, o.reason, stop=stop, highest_close=price, bars_held=0, atr_at_entry=o.atr
                )
        return carry

    def cap(equity: float) -> float:
        pct_cap = equity * cfg.position_cap_pct / 100 if cfg.position_cap_pct is not None else math.inf
        return min(cfg.position_cap, pct_cap)

    for d in dates:
        today = {s: ohlc[s][d] for s in symbols if d in ohlc[s]}

        # 1. Fill yesterday's orders at the open.
        pending = fill_orders(pending, today, d, OPEN)

        # 2. Stops and targets during the day (stop checked first: the cautious assumption).
        for s in [s for s in positions if s in today]:
            pos = positions[s]
            o_, h, l, _ = today[s]
            stop = pos.stop
            if rules.trailing_atr is not None and pos.atr_at_entry:
                trail = pos.highest_close - rules.trailing_atr * pos.atr_at_entry
                stop = trail if stop is None else max(stop, trail)
            if stop is not None and l <= stop:
                fill = o_ if o_ < stop else stop
                close_out(s, pos.qty, fill * (1 - c), d, "stop")
                pending = [o for o in pending if o.symbol != s]
                continue
            if rules.target_pct is not None:
                target = pos.entry_price * (1 + rules.target_pct / 100)
                if h >= target:
                    fill = o_ if o_ > target else target
                    close_out(s, pos.qty, fill * (1 - c), d, "profit target")
                    pending = [o for o in pending if o.symbol != s]

        # 3. Mark to the close.
        for s, bar in today.items():
            last_close[s] = bar[3]
        invested = sum(pos.qty * last_close[s] for s, pos in positions.items())
        equity = cash + invested
        equity_points.append(equity)
        exposure_points.append(invested / equity if equity > 0 else 0.0)
        for s, pos in positions.items():
            if s in today:
                pos.bars_held += 1
                pos.highest_close = max(pos.highest_close, today[s][3])

        # 4. Decide at the close, for tomorrow's open.
        busy = {o.symbol for o in pending}
        for s in symbols:
            if s not in today or s in busy:
                continue
            row = signals[s].loc[d]
            if row.isna().any():
                continue  # still warming up
            pos = positions.get(s)
            if pos is not None and rules.time_stop_bars is not None and pos.bars_held >= rules.time_stop_bars:
                pending.append(Order(s, "sell", f"time stop after {pos.bars_held} bars"))
                continue
            intent = strategy.decide(row, pos, Context(p, s, symbols))
            close = today[s][3]
            if isinstance(intent, Enter) and pos is None:
                dollars = _size(sizing, equity)
                pending.append(Order(s, "buy", intent.reason, dollars=min(dollars, cap(equity)),
                                     rank=intent.rank, atr=row.get("atr")))
            elif isinstance(intent, Exit) and pos is not None and rules.signal:
                pending.append(Order(s, "sell", intent.reason))
            elif isinstance(intent, Target):
                desired = min(max(intent.weight, 0.0) * equity, cap(equity))
                value = pos.qty * close if pos else 0.0
                if desired <= 0 and pos is not None and rules.signal:
                    pending.append(Order(s, "sell", intent.reason))
                elif desired > 0 and pos is None:
                    pending.append(Order(s, "buy", intent.reason, dollars=desired, atr=row.get("atr")))
                elif pos is not None and desired > 0 and abs(value - desired) > TARGET_BAND * desired:
                    if value > desired:
                        pending.append(Order(s, "sell", "trim to target", qty=(value - desired) / close))
                    else:
                        pending.append(Order(s, "buy", "top up to target", dollars=desired - value, is_entry=False))

        # 5. Fill-at-the-close variant: today's decisions fill at today's close (as with a
        #    market-on-close order placed just before the bell), then the day is re-marked.
        if cfg.fill == "close" and pending:
            pending = fill_orders(pending, today, d, CLOSE)
            invested = sum(pos.qty * last_close[s] for s, pos in positions.items())
            equity_points[-1] = cash + invested
            exposure_points[-1] = invested / equity_points[-1] if equity_points[-1] > 0 else 0.0

    # Positions still open at the end are closed at the last close (with cost) for trade stats.
    final = dates[-1] if dates else cfg.end
    for s in list(positions):
        close_out(s, positions[s].qty, last_close[s] * (1 - c), final, "end of test")

    equity_s = pd.Series(equity_points, index=pd.Index(dates, name="date"), dtype=float)
    exposure_s = pd.Series(exposure_points, index=equity_s.index, dtype=float)
    result = RunResult(strategy.name, strategy.version, p, cfg, equity_s, exposure_s, trades, skipped)
    result.metrics = metrics(equity_s, trades, exposure_s, start_value=cfg.capital)
    return result


def _size(sizing: Sizing, equity: float) -> float:
    if sizing.method == "fixed_dollars":
        return sizing.amount
    if sizing.method == "percent_of_capital":
        return equity * sizing.amount / 100
    raise ValueError(f"sizing {sizing.method!r} is only for target-style strategies")


def buy_and_hold(bars: dict[str, pd.DataFrame], cfg: BacktestConfig) -> pd.Series:
    """Benchmark: split capital equally across the symbols at the first open, hold to the end."""
    frames = {s: df.loc[cfg.start : cfg.end] for s, df in bars.items()}
    frames = {s: df for s, df in frames.items() if not df.empty}
    first = min(df.index[0] for df in frames.values())
    starters = {s: df for s, df in frames.items() if df.index[0] == first}
    each = cfg.capital / len(starters)
    c = cfg.cost_bp / 10_000
    closes = pd.DataFrame({s: df["close"] for s, df in starters.items()}).ffill()
    qty = {s: each / (df["open"].iloc[0] * (1 + c)) for s, df in starters.items()}
    return (closes * pd.Series(qty)).sum(axis=1)


def metrics(
    equity: pd.Series,
    trades: list[Trade] | None = None,
    exposure: pd.Series | None = None,
    start_value: float | None = None,
) -> dict[str, Any]:
    """Performance figures. `start_value` = the money put in (default: the first day's value),
    so returns and drawdowns count costs and moves on the very first day."""
    if equity.empty:
        return {}
    start_v = float(equity.iloc[0]) if start_value is None else float(start_value)
    end_v = float(equity.iloc[-1])
    days = (equity.index[-1] - equity.index[0]).days or 1
    years = days / 365.25
    total = end_v / start_v - 1 if start_v else 0.0
    daily = pd.concat([pd.Series([start_v]), equity.reset_index(drop=True)]).pct_change().dropna()
    sd = float(daily.std())
    peak = equity.cummax().clip(lower=start_v)
    out: dict[str, Any] = {
        "start": equity.index[0].isoformat(),
        "end": equity.index[-1].isoformat(),
        "total_return": total,
        "cagr": (end_v / start_v) ** (1 / years) - 1 if start_v > 0 and end_v > 0 else float("nan"),
        "max_drawdown": float((equity / peak - 1).min()),
        "sharpe": float(daily.mean() / sd * np.sqrt(TRADING_DAYS)) if sd > 0 else float("nan"),
    }
    if exposure is not None:
        out["exposure"] = float(exposure.mean())
    if trades is not None:
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]
        gross_win = sum(t.pnl for t in wins)
        gross_loss = -sum(t.pnl for t in losses)
        out.update(
            trades=len(trades),
            win_rate=len(wins) / len(trades) if trades else float("nan"),
            avg_win=float(np.mean([t.return_pct for t in wins])) if wins else float("nan"),
            avg_loss=float(np.mean([t.return_pct for t in losses])) if losses else float("nan"),
            profit_factor=gross_win / gross_loss if gross_loss > 0 else float("nan"),
            avg_bars_held=float(np.mean([t.bars_held for t in trades])) if trades else float("nan"),
        )
    return out


def first_tradable_date(bars: dict[str, pd.DataFrame], warmup_bars: int) -> date:
    """The first date on which every symbol has enough history for the strategy's warm-up."""
    return max(df.index[min(warmup_bars, len(df)) - 1] for df in bars.values())
