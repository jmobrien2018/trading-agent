"""All strategies, by name. Add new strategy classes here."""

from __future__ import annotations

from ..strategy import Strategy
from .rsi2_pullback import RSI2Pullback
from .trend_ma import TrendMA
from .trend_ma_underlying import TrendMAUnderlying

REGISTRY: dict[str, type[Strategy]] = {cls.name: cls for cls in (TrendMA, RSI2Pullback, TrendMAUnderlying)}
