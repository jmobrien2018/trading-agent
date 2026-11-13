"""Data-quality checks for pulled bars: coverage, gaps and freshness.

Minute bars: during each regular session (09:30-16:00 NY, shorter on half days) count how
many of the expected one-minute bars exist and find the longest run of missing minutes.
A minute with no trades on the chosen feed produces no bar, so thin coverage on the free
IEX feed shows up here.

Daily bars: one bar is expected per trading session.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .models import Session

NEW_YORK = ZoneInfo("America/New_York")
ONE_MINUTE = timedelta(minutes=1)
REPORT_TIMEFRAMES = ("1Min", "1Day")


@dataclass(frozen=True)
class DayCoverage:
    date: date
    expected: int
    present: int
    max_gap_min: int  # longest run of consecutive missing minutes (0 = complete)

    @property
    def coverage(self) -> float:
        return self.present / self.expected if self.expected else 1.0


@dataclass(frozen=True)
class CoverageSummary:
    symbol: str
    feed: str
    timeframe: str
    sessions: int
    expected: int
    present: int
    max_gap_min: int
    worst_day: date | None
    missing_days: tuple[date, ...]
    last_bar_utc: datetime | None

    @property
    def coverage(self) -> float:
        return self.present / self.expected if self.expected else 1.0


def minute_coverage(session: Session, times: Sequence[datetime]) -> DayCoverage:
    """Coverage of 1-minute bars inside one regular session. `times` = bar start times (UTC)."""
    expected = int((session.close_utc - session.open_utc) / ONE_MINUTE)
    inside = sorted({t for t in times if session.open_utc <= t < session.close_utc})

    # Fence-post walk: one minute before the open, each bar, then the close.
    points = [session.open_utc - ONE_MINUTE, *inside, session.close_utc]
    max_gap = max(int((b - a) / ONE_MINUTE) - 1 for a, b in zip(points, points[1:]))
    return DayCoverage(session.date, expected, len(inside), max_gap)


def summarize_minutes(
    symbol: str, feed: str, sessions: Sequence[Session], times: Sequence[datetime]
) -> CoverageSummary:
    days = [minute_coverage(s, times) for s in sessions]
    worst = min(days, key=lambda d: d.coverage, default=None)
    return CoverageSummary(
        symbol=symbol,
        feed=feed,
        timeframe="1Min",
        sessions=len(days),
        expected=sum(d.expected for d in days),
        present=sum(d.present for d in days),
        max_gap_min=max((d.max_gap_min for d in days), default=0),
        worst_day=worst.date if worst and worst.present < worst.expected else None,
        missing_days=tuple(d.date for d in days if d.present == 0),
        last_bar_utc=max(times, default=None),
    )


def summarize_days(
    symbol: str, feed: str, sessions: Sequence[Session], times: Sequence[datetime]
) -> CoverageSummary:
    """Daily bars are stamped at New York midnight; match them to session dates."""
    have = {t.astimezone(NEW_YORK).date() for t in times}
    missing = tuple(s.date for s in sessions if s.date not in have)
    return CoverageSummary(
        symbol=symbol,
        feed=feed,
        timeframe="1Day",
        sessions=len(sessions),
        expected=len(sessions),
        present=len(sessions) - len(missing),
        max_gap_min=0,
        worst_day=missing[0] if missing else None,
        missing_days=missing,
        last_bar_utc=max(times, default=None),
    )


def completed_sessions(sessions: Sequence[Session], now: datetime | None = None) -> list[Session]:
    """Drop today's session if it hasn't closed, so a half-finished day doesn't read as gaps."""
    now = now or datetime.now(timezone.utc)
    return [s for s in sessions if s.close_utc <= now]


def age_minutes(ts: datetime | None, now: datetime | None = None) -> float | None:
    if ts is None:
        return None
    now = now or datetime.now(timezone.utc)
    return (now - ts).total_seconds() / 60
