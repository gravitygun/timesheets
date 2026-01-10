"""Utility functions for timesheet calculations."""

from __future__ import annotations

from datetime import date, timedelta
from calendar import monthrange


def get_week_start(d: date) -> date:
    """Get the Saturday that starts the week containing date d."""
    # Saturday = 5 in weekday()
    days_since_saturday = (d.weekday() + 2) % 7
    return d - timedelta(days=days_since_saturday)


def get_weeks_in_month(year: int, month: int) -> list[tuple[date, date]]:
    """Get list of (week_start, week_end) tuples that overlap with the month."""
    first_day = date(year, month, 1)
    last_day = date(year, month, monthrange(year, month)[1])

    weeks = []
    week_start = get_week_start(first_day)

    while week_start <= last_day:
        week_end = week_start + timedelta(days=6)
        weeks.append((week_start, week_end))
        week_start = week_start + timedelta(days=7)

    return weeks


ADJUST_TYPES = [
    ("", "None"),
    ("P", "P - Public Holiday"),
    ("L", "L - Leave"),
    ("S", "S - Sick"),
    ("T", "T - Training"),
]
