"""Technical indicators. Every one is causal: the value on a day uses only that day and earlier,
so a strategy built from them can't see the future. The backtester checks this (no_lookahead)."""

from __future__ import annotations

import pandas as pd


def sma(series: pd.Series, n: int) -> pd.Series:
    """Simple moving average; NaN until n values exist."""
    return series.rolling(n, min_periods=n).mean()


def rsi(series: pd.Series, n: int) -> pd.Series:
    """Wilder's RSI (0-100), the standard one used for 2-day RSI rules."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    avg_loss = loss.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = avg_gain / avg_loss
    out = 100 - 100 / (1 + rs)
    # No losses in the window: RSI is 100 (rs is infinite); no movement at all: undefined.
    out = out.where(avg_loss != 0, 100.0)
    return out.where(avg_gain.notna())


def atr(bars: pd.DataFrame, n: int = 14) -> pd.Series:
    """Wilder's average true range: typical daily price range, including overnight gaps."""
    prev_close = bars["close"].shift()
    true_range = pd.concat(
        [bars["high"] - bars["low"], (bars["high"] - prev_close).abs(), (bars["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
