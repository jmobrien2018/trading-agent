from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from trading_agent import walkforward as wf


def returns(daily: dict[int, float], start=date(2018, 1, 1), end=date(2021, 12, 31)) -> pd.Series:
    """Business-day returns: a constant per year, with a little noise so Sharpe is defined."""
    rng = np.random.default_rng(1)
    days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
    days = [d for d in days if d.weekday() < 5]
    return pd.Series([daily[d.year] + rng.normal(0, 0.0001) for d in days], index=days)


def test_picks_best_so_far_and_tests_on_next_year_only():
    a = returns({2018: 0.002, 2019: 0.002, 2020: -0.001, 2021: -0.001})  # good early, bad later
    b = returns({2018: 0.000, 2019: 0.000, 2020: 0.003, 2021: 0.003})  # bad early, good later
    result = wf.walk_forward({"A": a, "B": b}, first_test_year=2020)

    # 2020 is chosen from 2018-2019 only (A looked best), even though B wins in 2020.
    y2020, y2021 = result.years
    assert (y2020.year, y2020.chosen, y2020.best_in_hindsight) == (2020, "A", "B")
    assert y2020.test_return < 0
    # By 2021 the growing window (2018-2020) still favors A on Sharpe; the out-of-sample result
    # is what that procedure really earns, not B's hindsight return.
    assert y2021.chosen in {"A", "B"}
    assert result.metrics["total_return"] == pytest.approx(
        (1 + a[[d.year == 2020 for d in a.index]]).prod() * (1 + (a if y2021.chosen == "A" else b)
                                                                  [[d.year == 2021 for d in a.index]]).prod() - 1)
    assert set(result.candidates_same_years) == {"A", "B"}
    assert result.final_choice in {"A", "B"}


def test_daily_returns_count_the_first_day_from_capital():
    eq = pd.Series([10_100.0, 9_999.0], index=[date(2024, 1, 2), date(2024, 1, 3)])
    r = wf.daily_returns(eq, 10_000)
    assert r.iloc[0] == pytest.approx(0.01) and r.iloc[1] == pytest.approx(9_999 / 10_100 - 1)


def test_needs_a_test_year():
    a = returns({2018: 0.001, 2019: 0.001, 2020: 0.001, 2021: 0.001})
    with pytest.raises(ValueError, match="no test years"):
        wf.walk_forward({"A": a}, first_test_year=2030)
