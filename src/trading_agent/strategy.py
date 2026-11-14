"""The standard strategy format (Phase 3 step 2; spec in the plan doc's "Strategy format spec" tab).

A strategy is one Python file with a class that sets the header fields and implements:
- signals(bars, p): indicator columns computed from past bars only (one row per bar)
- decide(sig, position, ctx): today's trade intent, or None
- exits(p) / sizing(p): choices from the shared exit and sizing menus

Strategies never place orders or enforce limits. The engine fills orders and runs exits;
the risk engine (Phase 4) caps sizes. Rule or test-value changes mean a new `version`.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any, ClassVar, Literal

import pandas as pd


@dataclass(frozen=True)
class Param:
    """A parameter: its default and the exact values to test, fixed before any test runs."""

    default: Any
    test: tuple[Any, ...]

    def __post_init__(self) -> None:
        if not self.test:
            raise ValueError("test values must not be empty")


class Params(dict):
    """Parameter values for one run; read as attributes (p.ma_days) or keys."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None

    def label(self) -> str:
        return ", ".join(f"{k}={v}" for k, v in self.items())


# --- what decide() returns: trade intents ---------------------------------------------------


@dataclass(frozen=True)
class Enter:
    """Event style: open a long position at the next open. When there's more to buy than cash,
    the engine fills the lowest `rank` first (e.g. the deepest dip); unranked entries go last."""

    reason: str
    rank: float | None = None
    # Optional price levels set by the strategy itself (e.g. an opening-range low). Used by the
    # intraday engine; when given they take precedence over the ExitRules stop/target.
    stop: float | None = None
    target: float | None = None


@dataclass(frozen=True)
class Exit:
    """Event style: close the position at the next open."""

    reason: str


@dataclass(frozen=True)
class Target:
    """Target style: hold this fraction (0-1) of the strategy's capital in the symbol."""

    weight: float
    reason: str


Intent = Enter | Exit | Target


# --- shared exit and sizing menus --------------------------------------------------------------


@dataclass(frozen=True)
class ExitRules:
    """Exits the engine runs. Whichever triggers first wins. None = not used."""

    signal: bool = True  # the strategy's own Exit / Target(0)
    stop_pct: float | None = None  # stop this % below entry
    stop_atr: float | None = None  # stop this many ATRs below entry
    trailing_atr: float | None = None  # stop this many ATRs below the highest close since entry
    target_pct: float | None = None  # take profit this % above entry
    time_stop_bars: int | None = None  # exit after this many bars held


@dataclass(frozen=True)
class Sizing:
    """One sizing method per strategy. The engine/risk engine may shrink it, never grow it."""

    method: Literal["fixed_dollars", "percent_of_capital", "target_weight"]
    amount: float = 0.0  # dollars for fixed_dollars; percent (0-100) for percent_of_capital


@dataclass(frozen=True)
class Context:
    """What decide() may know besides today's signals."""

    p: Params
    symbol: str
    symbols: tuple[str, ...]


@dataclass
class Position:
    """An open position, as the engine tracks it."""

    qty: float
    entry_price: float
    entry_date: Any
    entry_reason: str
    stop: float | None = None
    highest_close: float = 0.0
    bars_held: int = 0
    atr_at_entry: float | None = None


# --- the base class ----------------------------------------------------------------------------


class Strategy:
    name: ClassVar[str]
    version: ClassVar[int]
    family: ClassVar[Literal["trend", "dip-buying", "rotation"]]
    style: ClassVar[Literal["event", "target"]]
    description: ClassVar[str]
    timeframe: ClassVar[str] = "1Day"
    warmup_bars: ClassVar[int]
    params: ClassVar[dict[str, Param]] = {}
    # Intraday ("1Min") strategies: close any position at the close of this bar, counted in
    # minutes after 9:30 New York time (389 = the 15:59 bar, i.e. the session's last minute).
    flat_by_minute: ClassVar[int] = 389

    def signals(self, bars: pd.DataFrame, p: Params) -> pd.DataFrame:
        """Indicator columns, same index as bars. Must use only past and current rows."""
        raise NotImplementedError

    def decide(self, sig: pd.Series, position: Position | None, ctx: Context) -> Intent | None:
        raise NotImplementedError

    def exits(self, p: Params) -> ExitRules:
        return ExitRules()

    def sizing(self, p: Params) -> Sizing:
        raise NotImplementedError

    def signal_symbol(self, symbol: str, p: Params) -> str:
        """Whose bars the signals for `symbol` come from. Default: its own. A leveraged ETF can
        take its signals from the index it tracks (TQQQ trades on QQQ's trend)."""
        return symbol

    # helpers

    @classmethod
    def defaults(cls) -> Params:
        return Params({k: v.default for k, v in cls.params.items()})

    @classmethod
    def param_grid(cls) -> list[Params]:
        """Every combination of test values, in a stable order."""
        names = list(cls.params)
        combos = itertools.product(*(cls.params[n].test for n in names))
        return [Params(zip(names, values)) for values in combos]

    @classmethod
    def label(cls) -> str:
        return f"{cls.name} v{cls.version}"


@dataclass(frozen=True)
class LookaheadReport:
    ok: bool
    problems: list[str] = field(default_factory=list)


def no_lookahead(strategy: Strategy, bars: pd.DataFrame, p: Params, cuts: int = 5) -> LookaheadReport:
    """Check signals are causal: computing them on the first k bars must give the same values
    for those bars as computing them on all bars. Any difference means the future leaked in."""
    full = strategy.signals(bars, p)
    problems = []
    n = len(bars)
    for k in sorted({int(n * f) for f in (0.2, 0.4, 0.6, 0.8, 1.0)[:cuts] if int(n * f) > 0}):
        part = strategy.signals(bars.iloc[:k], p)
        a, b = part.iloc[-1], full.iloc[k - 1]
        for col in full.columns:
            x, y = a[col], b[col]
            if pd.isna(x) and pd.isna(y):
                continue
            if isinstance(x, float) or isinstance(y, float):
                same = abs(float(x) - float(y)) <= 1e-9 * max(1.0, abs(float(y)))
            else:
                same = x == y
            if not same:
                problems.append(f"{col} at bar {k - 1}: {x!r} with {k} bars vs {y!r} with all bars")
    return LookaheadReport(not problems, problems)
