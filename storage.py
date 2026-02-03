from __future__ import annotations

import os
import sqlite3
from datetime import date, time, timedelta
from decimal import Decimal
from pathlib import Path

from models import Config, Ticket, TicketAllocation, TimeEntry


def _get_db_path() -> Path:
    """Get database path from environment variable or default location."""
    if env_path := os.environ.get("TIMESHEET_DB"):
        return Path(env_path)
    return Path(__file__).parent / "data" / "timesheet.db"


DB_PATH = _get_db_path()


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

        CREATE TABLE IF NOT EXISTS tickets (
            id TEXT PRIMARY KEY,
            description TEXT NOT NULL,
            archived INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS ticket_allocations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticket_id TEXT NOT NULL,
            date TEXT NOT NULL,
            hours TEXT NOT NULL,
            entered_on_client INTEGER DEFAULT 0,
            FOREIGN KEY (ticket_id) REFERENCES tickets(id),
            UNIQUE(ticket_id, date)
        );

        CREATE INDEX IF NOT EXISTS idx_entries_date ON time_entries(date);
        CREATE INDEX IF NOT EXISTS idx_allocations_date ON ticket_allocations(date);
        CREATE INDEX IF NOT EXISTS idx_allocations_ticket ON ticket_allocations(ticket_id);
    """)

    # Migration: Add entered_on_client column if it doesn't exist
    cursor = conn.execute("PRAGMA table_info(ticket_allocations)")
    columns = [row[1] for row in cursor.fetchall()]
    if "entered_on_client" not in columns:
        conn.execute(
            "ALTER TABLE ticket_allocations ADD COLUMN entered_on_client INTEGER DEFAULT 0"
        )
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
        elif row["key"] == "vat_rate":
            config.vat_rate = Decimal(row["value"])

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
    conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                 ("vat_rate", str(config.vat_rate)))
    conn.commit()
    conn.close()


def get_uk_holidays(year: int) -> dict[date, str]:
    """Get England bank holidays for a given year."""
    import holidays
    uk_holidays = holidays.UK(years=year, subdiv='ENG')  # type: ignore[attr-defined]
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


# --- Ticket Functions ---


def _row_to_ticket(row: sqlite3.Row) -> Ticket:
    """Convert a database row to a Ticket object."""
    return Ticket(
        id=row["id"],
        description=row["description"],
        archived=bool(row["archived"]),
        created_at=date.fromisoformat(row["created_at"]) if row["created_at"] else None,
    )


def save_ticket(ticket: Ticket) -> None:
    """Insert or update a ticket."""
    conn = get_connection()
    created_at = ticket.created_at or date.today()
    conn.execute(
        """
        INSERT OR REPLACE INTO tickets (id, description, archived, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (ticket.id, ticket.description, int(ticket.archived), created_at.isoformat()),
    )
    conn.commit()
    conn.close()


def get_ticket(ticket_id: str) -> Ticket | None:
    """Get a single ticket by ID."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM tickets WHERE id = ?", (ticket_id,)
    ).fetchone()
    conn.close()
    return _row_to_ticket(row) if row else None


def get_all_tickets(include_archived: bool = False) -> list[Ticket]:
    """Get all tickets, optionally including archived ones."""
    conn = get_connection()
    if include_archived:
        rows = conn.execute(
            "SELECT * FROM tickets ORDER BY id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE archived = 0 ORDER BY id"
        ).fetchall()
    conn.close()
    return [_row_to_ticket(row) for row in rows]


def search_tickets(query: str, include_archived: bool = False) -> list[Ticket]:
    """Search tickets by ID or description."""
    conn = get_connection()
    pattern = f"%{query}%"
    if include_archived:
        rows = conn.execute(
            """
            SELECT * FROM tickets
            WHERE id LIKE ? OR description LIKE ?
            ORDER BY id
            """,
            (pattern, pattern),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM tickets
            WHERE (id LIKE ? OR description LIKE ?) AND archived = 0
            ORDER BY id
            """,
            (pattern, pattern),
        ).fetchall()
    conn.close()
    return [_row_to_ticket(row) for row in rows]


def delete_ticket(ticket_id: str) -> bool:
    """Delete a ticket. Returns False if ticket has allocations."""
    if not can_delete_ticket(ticket_id):
        return False
    conn = get_connection()
    conn.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))
    conn.commit()
    conn.close()
    return True


def can_delete_ticket(ticket_id: str) -> bool:
    """Check if a ticket can be deleted (has no allocations)."""
    conn = get_connection()
    row = conn.execute(
        "SELECT COUNT(*) as count FROM ticket_allocations WHERE ticket_id = ?",
        (ticket_id,),
    ).fetchone()
    conn.close()
    return row["count"] == 0


def archive_ticket(ticket_id: str) -> None:
    """Archive a ticket."""
    conn = get_connection()
    conn.execute("UPDATE tickets SET archived = 1 WHERE id = ?", (ticket_id,))
    conn.commit()
    conn.close()


def unarchive_ticket(ticket_id: str) -> None:
    """Unarchive a ticket."""
    conn = get_connection()
    conn.execute("UPDATE tickets SET archived = 0 WHERE id = ?", (ticket_id,))
    conn.commit()
    conn.close()


# --- Ticket Allocation Functions ---


def _row_to_allocation(row: sqlite3.Row) -> TicketAllocation:
    """Convert a database row to a TicketAllocation object."""
    return TicketAllocation(
        ticket_id=row["ticket_id"],
        date=date.fromisoformat(row["date"]),
        hours=Decimal(row["hours"]),
        entered_on_client=bool(row["entered_on_client"]) if row["entered_on_client"] else False,
    )


def save_allocation(allocation: TicketAllocation) -> None:
    """Insert or update a ticket allocation."""
    conn = get_connection()
    conn.execute(
        """
        INSERT OR REPLACE INTO ticket_allocations (ticket_id, date, hours, entered_on_client)
        VALUES (?, ?, ?, ?)
        """,
        (
            allocation.ticket_id,
            allocation.date.isoformat(),
            str(allocation.hours),
            int(allocation.entered_on_client),
        ),
    )
    conn.commit()
    conn.close()


def get_allocations_for_date(d: date) -> list[TicketAllocation]:
    """Get all allocations for a specific date."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM ticket_allocations WHERE date = ? ORDER BY ticket_id",
        (d.isoformat(),),
    ).fetchall()
    conn.close()
    return [_row_to_allocation(row) for row in rows]


def get_allocations_for_month(year: int, month: int) -> list[TicketAllocation]:
    """Get all allocations for a calendar month."""
    from calendar import monthrange
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT * FROM ticket_allocations
        WHERE date >= ? AND date <= ?
        ORDER BY date, ticket_id
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    conn.close()
    return [_row_to_allocation(row) for row in rows]


def delete_allocation(ticket_id: str, d: date) -> None:
    """Delete a specific allocation."""
    conn = get_connection()
    conn.execute(
        "DELETE FROM ticket_allocations WHERE ticket_id = ? AND date = ?",
        (ticket_id, d.isoformat()),
    )
    conn.commit()
    conn.close()


def get_total_allocated_hours(d: date) -> Decimal:
    """Get the total hours allocated for a specific date."""
    conn = get_connection()
    row = conn.execute(
        "SELECT COALESCE(SUM(CAST(hours AS REAL)), 0) as total FROM ticket_allocations WHERE date = ?",
        (d.isoformat(),),
    ).fetchone()
    conn.close()
    return Decimal(str(row["total"])).quantize(Decimal("0.01"))
