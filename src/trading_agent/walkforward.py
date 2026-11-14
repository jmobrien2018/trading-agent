"""Walk-forward test: does "pick the best setting so far" keep working on years it hasn't seen?

Each candidate (a strategy at one parameter set) is backtested once over the whole in-sample
period, giving its daily returns. Then for every test year Y, the candidate with the best Sharpe
ratio over all days before Y (an anchored, growing window) is chosen, and only its returns in Y
are kept. Stitching those years together gives an out-of-sample result for the whole procedure.

Switching candidates at a year boundary reuses each candidate's continuous returns rather than
re-entering positions; for day strategies (flat every night) that's exact, for swing strategies
a close approximation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

import numpy as np
import pandas as pd

from .backtest import TRADING_DAYS, metrics


@dataclass
class YearChoice:
    year: int
    chosen: str
    train_sharpe: float
    test_return: float
    best_in_hindsight: str
    best_in_hindsight_return: float


@dataclass
class WalkForwardResult:
    years: list[YearChoice]
    equity: pd.Series  # stitched out-of-sample equity
    metrics: dict
    candidates_same_years: dict[str, dict] = field(default_factory=dict)
    final_choice: str = ""  # trained on every in-sample day: the pick for the hold-out year


def daily_returns(equity: pd.Series, start_value: float) -> pd.Series:
    """Daily returns from an equity curve, the first day measured from the money put in."""
    values = np.concatenate([[start_value], equity.to_numpy(dtype=float)])
    return pd.Series(values[1:] / values[:-1] - 1, index=equity.index)


def sharpe(returns: pd.Series) -> float:
    sd = float(returns.std())
    return float(returns.mean() / sd * math.sqrt(TRADING_DAYS)) if sd > 0 and len(returns) > 1 else float("-inf")


def walk_forward(candidates: dict[str, pd.Series], first_test_year: int, capital: float = 10_000) -> WalkForwardResult:
    """`candidates`: label -> daily returns (indexed by date) over the same in-sample period."""
    frame = pd.DataFrame(candidates).fillna(0.0).sort_index()
    years = sorted({d.year for d in frame.index})
    test_years = [y for y in years if y >= first_test_year]
    if not test_years:
        raise ValueError("no test years: the in-sample period is too short for this first_test_year")

    choices, pieces = [], []
    for y in test_years:
        train = frame[[d < date(y, 1, 1) for d in frame.index]]
        test = frame[[d.year == y for d in frame.index]]
        scores = {c: sharpe(train[c]) for c in frame.columns}
        chosen = max(frame.columns, key=lambda c: scores[c])  # ties: first listed
        year_returns = (1 + test).prod() - 1
        hindsight = year_returns.idxmax()
        choices.append(YearChoice(y, chosen, scores[chosen], float(year_returns[chosen]),
                                  hindsight, float(year_returns[hindsight])))
        pieces.append(test[chosen])

    oos = pd.concat(pieces)
    equity = capital * (1 + oos).cumprod()
    tested = frame[[d.year >= first_test_year for d in frame.index]]
    same_years = {c: metrics(capital * (1 + tested[c]).cumprod(), start_value=capital) for c in frame.columns}
    final = max(frame.columns, key=lambda c: sharpe(frame[c]))
    return WalkForwardResult(choices, equity, metrics(equity, start_value=capital), same_years, final)
