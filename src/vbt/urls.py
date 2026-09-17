"""Visa Bulletin URL construction and month arithmetic.

The one trap here: the path segment is the FISCAL year, not the calendar
year. October, November and December bulletins live under the *next* year's
folder, so the November 2025 bulletin is served from .../2026/.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

BASE = "https://travel.state.gov/content/travel/en/legal/visa-law0/visa-bulletin"
INDEX_URL = f"{BASE}.html"

_MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]

# October 2015 is the first bulletin with the two-chart (Final Action /
# Dates for Filing) structure this tracker depends on.
FIRST_TWO_CHART_MONTH = (2015, 10)


def fiscal_year(year: int, month: int) -> int:
    """US federal fiscal year for a bulletin month. FY starts 1 October."""
    return year + 1 if month >= 10 else year


def bulletin_url(year: int, month: int) -> str:
    if not 1 <= month <= 12:
        raise ValueError(f"month out of range: {month}")
    return (
        f"{BASE}/{fiscal_year(year, month)}/"
        f"visa-bulletin-for-{_MONTH_NAMES[month - 1]}-{year}.html"
    )


def month_key(year: int, month: int) -> str:
    return f"{year:04d}-{month:02d}"


def parse_month_key(key: str) -> tuple[int, int]:
    y, m = key.split("-")
    return int(y), int(m)


def next_month(year: int, month: int) -> tuple[int, int]:
    return (year + 1, 1) if month == 12 else (year, month + 1)


def prev_month(year: int, month: int) -> tuple[int, int]:
    return (year - 1, 12) if month == 1 else (year, month - 1)


def iter_months(start: tuple[int, int], end: tuple[int, int]):
    """Yield (year, month) inclusive from start to end."""
    y, m = start
    while (y, m) <= end:
        yield y, m
        y, m = next_month(y, m)


def months_to_backfill(today: date | None = None) -> list[tuple[int, int]]:
    """Every bulletin month the two-chart era has produced so far.

    The bulletin for month N is published during month N-1, so the newest
    month that can exist today is next month.
    """
    today = today or date.today()
    end = next_month(today.year, today.month)
    return list(iter_months(FIRST_TWO_CHART_MONTH, end))


@dataclass(frozen=True)
class BulletinMonth:
    year: int
    month: int

    @property
    def key(self) -> str:
        return month_key(self.year, self.month)

    @property
    def url(self) -> str:
        return bulletin_url(self.year, self.month)

    @property
    def fiscal_year(self) -> int:
        return fiscal_year(self.year, self.month)
