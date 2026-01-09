from __future__ import annotations

import sqlite3
from datetime import date, time, timedelta
from decimal import Decimal
from pathlib import Path

from models import TimeEntry, Config


DB_PATH = Path(__file__).parent / "data" / "timesheet.db"


def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist."""
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS time_entries (
            date TEXT PRIMARY KEY,
            day_of_week TEXT NOT NULL,
            clock_in TEXT,
            lunch_minutes INTEGER,
            clock_out TEXT,
            adjustment_minutes INTEGER,
            adjust_type TEXT,
            comment TEXT
        );

        CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_entries_date ON time_entries(date);
    """)
    conn.commit()
    conn.close()


def _parse_time(val: str | None) -> time | None:
    if not val:
        return None
    parts = val.split(":")
    return time(int(parts[0]), int(parts[1]))


def _format_time(t: time | None) -> str | None:
    if not t:
        return None
    return t.strftime("%H:%M")


def _row_to_entry(row: sqlite3.Row) -> TimeEntry:
    lunch = timedelta(minutes=row["lunch_minutes"]) if row["lunch_minutes"] else None
    adj = timedelta(minutes=row["adjustment_minutes"]) if row["adjustment_minutes"] else None

    return TimeEntry(
        date=date.fromisoformat(row["date"]),
        day_of_week=row["day_of_week"],
        clock_in=_parse_time(row["clock_in"]),
        lunch_duration=lunch,
        clock_out=_parse_time(row["clock_out"]),
        adjustment=adj,
        adjust_type=row["adjust_type"],
        comment=row["comment"],
    )


def save_entry(entry: TimeEntry):
    """Insert or update a time entry."""
    conn = get_connection()
    lunch_mins = int(entry.lunch_duration.total_seconds() // 60) if entry.lunch_duration else None
    adj_mins = int(entry.adjustment.total_seconds() // 60) if entry.adjustment else None

    conn.execute("""
        INSERT OR REPLACE INTO time_entries
        (date, day_of_week, clock_in, lunch_minutes, clock_out, adjustment_minutes, adjust_type, comment)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        entry.date.isoformat(),
        entry.day_of_week,
        _format_time(entry.clock_in),
        lunch_mins,
        _format_time(entry.clock_out),
        adj_mins,
        entry.adjust_type,
        entry.comment,
    ))
    conn.commit()
    conn.close()


def get_entry(d: date) -> TimeEntry | None:
    """Get a single entry by date."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM time_entries WHERE date = ?",
        (d.isoformat(),)
    ).fetchone()
    conn.close()

    if row:
        return _row_to_entry(row)
    return None


def get_entries_range(start: date, end: date) -> list[TimeEntry]:
    """Get entries between two dates (inclusive)."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM time_entries WHERE date >= ? AND date <= ? ORDER BY date",
        (start.isoformat(), end.isoformat())
    ).fetchall()
    conn.close()

    return [_row_to_entry(row) for row in rows]


def get_month_entries(year: int, month: int) -> list[TimeEntry]:
    """Get all entries for a calendar month."""
    from calendar import monthrange
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    return get_entries_range(start, end)


def get_config() -> Config:
    """Load config from database."""
    conn = get_connection()
    rows = conn.execute("SELECT key, value FROM config").fetchall()
    conn.close()

    config = Config()
    for row in rows:
        if row["key"] == "hourly_rate":
            config.hourly_rate = Decimal(row["value"])
        elif row["key"] == "currency":
            config.currency = row["value"]
        elif row["key"] == "standard_day_hours":
            config.standard_day_hours = Decimal(row["value"])

    return config


def save_config(config: Config):
    """Save config to database."""
    conn = get_connection()
    conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                 ("hourly_rate", str(config.hourly_rate)))
    conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                 ("currency", config.currency))
    conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                 ("standard_day_hours", str(config.standard_day_hours)))
    conn.commit()
    conn.close()


def get_uk_holidays(year: int) -> dict[date, str]:
    """Get UK bank holidays for a given year."""
    import holidays
    uk_holidays = holidays.UK(years=year)
    return {d: name for d, name in uk_holidays.items()}


def get_working_days(start: date, end: date) -> list[date]:
    """Get list of working days (weekdays minus UK holidays) in a date range."""
    uk_holidays = get_uk_holidays(start.year)
    if start.year != end.year:
        uk_holidays.update(get_uk_holidays(end.year))

    working_days = []
    current = start
    while current <= end:
        # Monday=0 to Friday=4 are weekdays
        if current.weekday() < 5 and current not in uk_holidays:
            working_days.append(current)
        current += timedelta(days=1)

    return working_days


def get_holidays_in_range(start: date, end: date) -> dict[date, str]:
    """Get UK holidays that fall on weekdays in a date range."""
    uk_holidays = get_uk_holidays(start.year)
    if start.year != end.year:
        uk_holidays.update(get_uk_holidays(end.year))

    return {d: name for d, name in uk_holidays.items()
            if start <= d <= end and d.weekday() < 5}


def populate_holidays(year: int, month: int, standard_hours: Decimal) -> int:
    """Pre-populate holiday entries for a month. Returns count of entries created."""
    from calendar import monthrange
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])

    holidays_in_month = get_holidays_in_range(start, end)
    count = 0

    for holiday_date, holiday_name in holidays_in_month.items():
        existing = get_entry(holiday_date)
        # Only create if no entry exists or entry has no data
        if not existing or (not existing.clock_in and not existing.adjustment):
            entry = TimeEntry(
                date=holiday_date,
                day_of_week=holiday_date.strftime("%a"),
                adjustment=timedelta(hours=float(standard_hours)),
                adjust_type="P",
                comment=holiday_name,
            )
            save_entry(entry)
            count += 1

    return count
