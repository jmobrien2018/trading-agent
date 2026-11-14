"""Measure real trading costs: sample historical bid/ask spreads at fixed times of day.

A market order pays about half the spread relative to the mid price, so the half-spread in basis
points is the floor for a backtest's cost per side. Slippage (price moving before the fill) comes
on top and isn't measured here. Sampling, not every quote: a fixed set of past sessions and
times of day keeps it to a few hundred API calls and makes reruns comparable.
"""

from __future__ import annotations

import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from .alpaca import AlpacaClient
from .data_quality import NEW_YORK

SAMPLE_TIMES = (time(9, 31), time(9, 45), time(10, 0), time(11, 0), time(12, 30),
                time(14, 0), time(15, 0), time(15, 30), time(15, 50))
WINDOW = timedelta(seconds=60)
BUCKETS = {"open (9:31-10:00)": (time(9, 31), time(10, 0)),
           "midday (11:00-15:00)": (time(11, 0), time(15, 0)),
           "close (15:30-15:50)": (time(15, 30), time(15, 50))}


@dataclass(frozen=True)
class SpreadSample:
    symbol: str
    day: date
    at: time
    half_spread_bp: float  # median over the quotes in a 60-second window
    quotes: int


def half_spread_bp(bid: float, ask: float) -> float:
    mid = (bid + ask) / 2
    return (ask - bid) / 2 / mid * 10_000


def pick_sessions(dates: Sequence[date], n: int, seed: int = 7) -> list[date]:
    """A fixed random sample of trading days, so reruns measure the same days."""
    dates = sorted(dates)
    if len(dates) <= n:
        return dates
    return sorted(random.Random(seed).sample(dates, n))


def sample(client: AlpacaClient, symbols: Sequence[str], days: Sequence[date], feed: str = "sip",
           times: Sequence[time] = SAMPLE_TIMES) -> list[SpreadSample]:
    out = []
    for symbol in symbols:
        for day in days:
            for at in times:
                start = datetime.combine(day, at, NEW_YORK)
                quotes = client.get_quotes(symbol, start, start + WINDOW, feed=feed)
                if quotes:
                    values = [half_spread_bp(b, a) for _, b, a in quotes]
                    out.append(SpreadSample(symbol, day, at, statistics.median(values), len(quotes)))
    return out


def summarize(samples: Sequence[SpreadSample]) -> dict[str, dict[str, dict[str, float]]]:
    """symbol -> bucket ("all" plus time-of-day buckets) -> {median, p90, n} of half-spread bp."""
    out: dict[str, dict[str, dict[str, float]]] = {}
    for symbol in sorted({s.symbol for s in samples}):
        mine = [s for s in samples if s.symbol == symbol]
        groups = {"all": mine}
        for name, (lo, hi) in BUCKETS.items():
            groups[name] = [s for s in mine if lo <= s.at <= hi]
        out[symbol] = {}
        for name, group in groups.items():
            values = sorted(s.half_spread_bp for s in group)
            if values:
                p90 = values[min(len(values) - 1, int(round(0.9 * (len(values) - 1))))]
                out[symbol][name] = {"median": statistics.median(values), "p90": p90, "n": len(values)}
    return out
