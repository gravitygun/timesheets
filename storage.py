from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import date, time, timedelta
from decimal import ROUND_CEILING, Decimal
from pathlib import Path

from models import (
    Config,
    Deliverable,
    MonthlyBilling,
    Ticket,
    TicketAllocation,
    TimeEntry,
    WorkPackage,
)


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
    # WAL mode lets the HTTP API read concurrently while the TUI is open.
    conn.execute("PRAGMA journal_mode=WAL")
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

        CREATE TABLE IF NOT EXISTS work_packages (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS deliverables (
            id TEXT PRIMARY KEY,
            work_package_id TEXT NOT NULL,
            title TEXT NOT NULL,
            active INTEGER DEFAULT 1,
            FOREIGN KEY (work_package_id) REFERENCES work_packages(id)
        );

        CREATE TABLE IF NOT EXISTS monthly_point_budgets (
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            max_points INTEGER NOT NULL,
            PRIMARY KEY (year, month)
        );

        CREATE TABLE IF NOT EXISTS monthly_billing (
            year INTEGER NOT NULL,
            month INTEGER NOT NULL,
            finalised INTEGER DEFAULT 0,
            PRIMARY KEY (year, month)
        );

        CREATE INDEX IF NOT EXISTS idx_entries_date ON time_entries(date);
        CREATE INDEX IF NOT EXISTS idx_allocations_date ON ticket_allocations(date);
        CREATE INDEX IF NOT EXISTS idx_allocations_ticket ON ticket_allocations(ticket_id);
        CREATE INDEX IF NOT EXISTS idx_deliverables_wp ON deliverables(work_package_id);
    """)

    # Migration: Add entered_on_client column if it doesn't exist
    cursor = conn.execute("PRAGMA table_info(ticket_allocations)")
    columns = [row[1] for row in cursor.fetchall()]
    if "entered_on_client" not in columns:
        conn.execute(
            "ALTER TABLE ticket_allocations ADD COLUMN entered_on_client INTEGER DEFAULT 0"
        )

    # Migration: Add description column if it doesn't exist
    if "description" not in columns:
        conn.execute(
            "ALTER TABLE ticket_allocations ADD COLUMN description TEXT"
        )

    # Migration: Add points_entered column to tickets if it doesn't exist
    cursor = conn.execute("PRAGMA table_info(tickets)")
    ticket_columns = [row[1] for row in cursor.fetchall()]
    if "points_entered" not in ticket_columns:
        conn.execute(
            "ALTER TABLE tickets ADD COLUMN points_entered INTEGER DEFAULT 0"
        )

    # Migration: Add deliverable_id column to tickets if it doesn't exist
    if "deliverable_id" not in ticket_columns:
        conn.execute(
            "ALTER TABLE tickets ADD COLUMN deliverable_id TEXT"
        )

    # Migration: Add billed flag and audit columns
    if "billed" not in ticket_columns:
        conn.execute(
            "ALTER TABLE tickets ADD COLUMN billed INTEGER DEFAULT 0"
        )
    if "billed_year" not in ticket_columns:
        conn.execute(
            "ALTER TABLE tickets ADD COLUMN billed_year INTEGER"
        )
    if "billed_month" not in ticket_columns:
        conn.execute(
            "ALTER TABLE tickets ADD COLUMN billed_month INTEGER"
        )

    # Seed points_start_date config if not yet set
    row = conn.execute(
        "SELECT value FROM config WHERE key = 'points_start_date'"
    ).fetchone()
    if not row:
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?)",
            ("points_start_date", "2026-03-01"),
        )

    # Seed contract config if not yet set
    for key, value in [
        ("contract_start", "2026-04-01"),
        ("contract_end", "2027-03-31"),
        ("annual_max_points", "960"),
    ]:
        existing = conn.execute(
            "SELECT value FROM config WHERE key = ?", (key,)
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO config (key, value) VALUES (?, ?)",
                (key, value),
            )

    # Update point_rate to 200 if still at old default of 210
    rate_row = conn.execute(
        "SELECT value FROM config WHERE key = 'point_rate'"
    ).fetchone()
    if rate_row and rate_row["value"] == "210":
        conn.execute(
            "UPDATE config SET value = '200' WHERE key = 'point_rate'"
        )

    # Update annual_max_points from 825 to 960 (contract amendment)
    annual_row = conn.execute(
        "SELECT value FROM config WHERE key = 'annual_max_points'"
    ).fetchone()
    if annual_row and annual_row["value"] == "825":
        conn.execute(
            "UPDATE config SET value = '960' WHERE key = 'annual_max_points'"
        )

    # Seed work packages and deliverables if empty
    wp_count = conn.execute("SELECT COUNT(*) as c FROM work_packages").fetchone()
    if wp_count["c"] == 0:
        _seed_work_packages_and_deliverables(conn)
    else:
        # Migration: add missing WPs and inactive deliverables to existing DBs
        _backfill_inactive_deliverables(conn)

    # Seed monthly point budgets if empty
    budget_count = conn.execute(
        "SELECT COUNT(*) as c FROM monthly_point_budgets"
    ).fetchone()
    if budget_count["c"] == 0:
        _seed_monthly_point_budgets(conn)
    else:
        # Migration: bump existing monthly budgets to new contract values
        _update_monthly_point_budgets(conn)

    conn.commit()
    conn.close()


def _seed_work_packages_and_deliverables(conn: sqlite3.Connection) -> None:
    """Seed initial work packages and deliverables."""
    work_packages = [
        ("WP2", "Design and Build Service"),
        ("WP2a", "Solution Development Service"),
        ("WP2b", "Design Service"),
        ("WP2c", "Build Service"),
        ("WP2d", "Assurance Service"),
        ("WP4", "Service Improvement"),
        ("WP5", "Support and Sustain Service"),
        ("WP5a", "Transform Service"),
    ]
    for wp_id, title in work_packages:
        conn.execute(
            "INSERT INTO work_packages (id, title) VALUES (?, ?)",
            (wp_id, title),
        )

    # (id, work_package_id, title, active)
    deliverables = [
        # WP2 – Design and Build Service (inactive)
        ("WP2-D1", "WP2", "Solution Options paper", 0),
        ("WP2-D2", "WP2", "Updated Architecture Decision Log", 0),
        ("WP2-D3", "WP2", "Platform Specification produced for required system", 0),
        ("WP2-D4", "WP2", "Update Workbook", 0),
        # WP2a – Solution Development Service (active)
        ("WP2a-D1", "WP2a", "Solution Options paper", 1),
        ("WP2a-D2", "WP2a", "Update Workbook task", 1),
        # WP2b – Design Service (active)
        ("WP2b-D1", "WP2b", "Design Architecture Pack", 1),
        ("WP2b-D2", "WP2b", "Platform Specification", 1),
        ("WP2b-D3", "WP2b", "Update Workbook task", 1),
        # WP2c – Build Service (inactive)
        ("WP2c-D1", "WP2c", "Build scripts and Automation playbook developed/enhanced", 0),
        ("WP2c-D2", "WP2c", "Build Sheets and build test report", 0),
        ("WP2c-D3", "WP2c", "Deployed Analytics platforms and Query Systems", 0),
        ("WP2c-D4", "WP2c", "Update Workbook task", 0),
        # WP2d – Assurance Service (active)
        ("WP2d-D1", "WP2d", "Document methodology, tooling, and approach", 1),
        ("WP2d-D2", "WP2d", "Coding Standards Review", 1),
        ("WP2d-D3", "WP2d", "Code Review Reports", 1),
        # WP4 – Service Improvement (inactive)
        ("WP4-D1", "WP4", "Efficiency Log", 0),
        # WP5 – Support and Sustain Service (active)
        ("WP5-D1", "WP5", "Monthly Governance Report", 1),
        ("WP5-D2", "WP5", "Quarterly Summary Report", 1),
        ("WP5-D3", "WP5", "Annual Summary Report", 1),
        ("WP5-D4", "WP5", "Incident Management Outputs", 1),
        # WP5a – Transform Service (active)
        ("WP5a-D1", "WP5a", "Deployed Code Changes", 1),
        ("WP5a-D2", "WP5a", "Change Management Process Completed", 1),
        ("WP5a-D3", "WP5a", "Technical Documentation Updated", 1),
    ]
    for del_id, wp_id, title, active in deliverables:
        conn.execute(
            "INSERT INTO deliverables (id, work_package_id, title, active) "
            "VALUES (?, ?, ?, ?)",
            (del_id, wp_id, title, active),
        )


def _backfill_inactive_deliverables(conn: sqlite3.Connection) -> None:
    """Add missing work packages and inactive deliverables to existing databases."""
    missing_wps = [
        ("WP2", "Design and Build Service"),
        ("WP2c", "Build Service"),
        ("WP4", "Service Improvement"),
    ]
    for wp_id, title in missing_wps:
        existing = conn.execute(
            "SELECT id FROM work_packages WHERE id = ?", (wp_id,)
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO work_packages (id, title) VALUES (?, ?)",
                (wp_id, title),
            )

    missing_dels = [
        ("WP2-D1", "WP2", "Solution Options paper", 0),
        ("WP2-D2", "WP2", "Updated Architecture Decision Log", 0),
        ("WP2-D3", "WP2", "Platform Specification produced for required system", 0),
        ("WP2-D4", "WP2", "Update Workbook", 0),
        ("WP2c-D1", "WP2c", "Build scripts and Automation playbook developed/enhanced", 0),
        ("WP2c-D2", "WP2c", "Build Sheets and build test report", 0),
        ("WP2c-D3", "WP2c", "Deployed Analytics platforms and Query Systems", 0),
        ("WP2c-D4", "WP2c", "Update Workbook task", 0),
        ("WP4-D1", "WP4", "Efficiency Log", 0),
    ]
    for del_id, wp_id, title, active in missing_dels:
        existing = conn.execute(
            "SELECT id FROM deliverables WHERE id = ?", (del_id,)
        ).fetchone()
        if not existing:
            conn.execute(
                "INSERT INTO deliverables (id, work_package_id, title, active) "
                "VALUES (?, ?, ?, ?)",
                (del_id, wp_id, title, active),
            )


def _seed_monthly_point_budgets(conn: sqlite3.Connection) -> None:
    """Seed monthly point budgets from the contract fee schedule."""
    budgets = [
        (2026, 4, 80), (2026, 5, 80), (2026, 6, 80),
        (2026, 7, 80), (2026, 8, 80), (2026, 9, 80),
        (2026, 10, 80), (2026, 11, 80), (2026, 12, 80),
        (2027, 1, 80), (2027, 2, 80), (2027, 3, 80),
    ]
    for year, month, points in budgets:
        conn.execute(
            "INSERT INTO monthly_point_budgets (year, month, max_points) "
            "VALUES (?, ?, ?)",
            (year, month, points),
        )


def _update_monthly_point_budgets(conn: sqlite3.Connection) -> None:
    """Update existing monthly budgets to new contract values (80 pts/month)."""
    new_budgets = [
        (2026, 4, 80), (2026, 5, 80), (2026, 6, 80),
        (2026, 7, 80), (2026, 8, 80), (2026, 9, 80),
        (2026, 10, 80), (2026, 11, 80), (2026, 12, 80),
        (2027, 1, 80), (2027, 2, 80), (2027, 3, 80),
    ]
    for year, month, points in new_budgets:
        conn.execute(
            "INSERT OR REPLACE INTO monthly_point_budgets "
            "(year, month, max_points) VALUES (?, ?, ?)",
            (year, month, points),
        )


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
        elif row["key"] == "hours_per_point":
            config.hours_per_point = Decimal(row["value"])
        elif row["key"] == "point_rate":
            config.point_rate = Decimal(row["value"])
        elif row["key"] == "points_start_date":
            config.points_start_date = (
                date.fromisoformat(row["value"]) if row["value"] else None
            )
        elif row["key"] == "contract_start":
            config.contract_start = (
                date.fromisoformat(row["value"]) if row["value"] else None
            )
        elif row["key"] == "contract_end":
            config.contract_end = (
                date.fromisoformat(row["value"]) if row["value"] else None
            )
        elif row["key"] == "annual_max_points":
            config.annual_max_points = int(row["value"]) if row["value"] else 825

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
    conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                 ("hours_per_point", str(config.hours_per_point)))
    conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                 ("point_rate", str(config.point_rate)))
    conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                 ("points_start_date",
                  config.points_start_date.isoformat() if config.points_start_date else ""))
    conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                 ("contract_start",
                  config.contract_start.isoformat() if config.contract_start else ""))
    conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                 ("contract_end",
                  config.contract_end.isoformat() if config.contract_end else ""))
    conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
                 ("annual_max_points", str(config.annual_max_points)))
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
    keys = row.keys()
    return Ticket(
        id=row["id"],
        description=row["description"],
        archived=bool(row["archived"]),
        created_at=date.fromisoformat(row["created_at"]) if row["created_at"] else None,
        points_entered=bool(row["points_entered"]) if "points_entered" in keys else False,
        deliverable_id=row["deliverable_id"] if "deliverable_id" in keys else None,
        billed=bool(row["billed"]) if "billed" in keys else False,
        billed_year=row["billed_year"] if "billed_year" in keys else None,
        billed_month=row["billed_month"] if "billed_month" in keys else None,
    )


def save_ticket(ticket: Ticket) -> None:
    """Insert or update a ticket."""
    conn = get_connection()
    created_at = ticket.created_at or date.today()
    conn.execute(
        """
        INSERT OR REPLACE INTO tickets
            (id, description, archived, created_at, points_entered,
             deliverable_id, billed, billed_year, billed_month)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ticket.id,
            ticket.description,
            int(ticket.archived),
            created_at.isoformat(),
            int(ticket.points_entered),
            ticket.deliverable_id,
            int(ticket.billed),
            ticket.billed_year,
            ticket.billed_month,
        ),
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
    """Reopen a closed ticket and clear any billed state.

    Reopening implies the work isn't actually finished, so any prior
    billing claim must be retracted.
    """
    conn = get_connection()
    conn.execute(
        "UPDATE tickets SET archived = 0, billed = 0, "
        "billed_year = NULL, billed_month = NULL WHERE id = ?",
        (ticket_id,),
    )
    conn.commit()
    conn.close()


def rename_ticket(old_id: str, new_id: str) -> bool:
    """Rename a ticket, updating all allocations to the new ID.

    Returns True on success, False if the new ID already exists.
    """
    conn = get_connection()
    existing = conn.execute(
        "SELECT id FROM tickets WHERE id = ?", (new_id,),
    ).fetchone()
    if existing:
        conn.close()
        return False
    conn.execute(
        "UPDATE ticket_allocations SET ticket_id = ? WHERE ticket_id = ?",
        (new_id, old_id),
    )
    conn.execute(
        "UPDATE tickets SET id = ? WHERE id = ?",
        (new_id, old_id),
    )
    conn.commit()
    conn.close()
    return True


def set_points_entered(ticket_id: str, entered: bool) -> None:
    """Set whether points have been entered in Jira for a ticket."""
    conn = get_connection()
    conn.execute(
        "UPDATE tickets SET points_entered = ? WHERE id = ?",
        (int(entered), ticket_id),
    )
    conn.commit()
    conn.close()


def get_ticket_lifetime_hours(
    start_date: date,
    ticket_ids: list[str] | None = None,
) -> dict[str, Decimal]:
    """Get total allocated hours per ticket from start_date onwards.

    Args:
        start_date: Only count hours on or after this date.
        ticket_ids: If provided, only return hours for these tickets.

    Returns:
        Dict mapping ticket_id to total hours.
    """
    conn = get_connection()
    if ticket_ids:
        placeholders = ",".join("?" * len(ticket_ids))
        rows = conn.execute(
            f"""
            SELECT ticket_id, SUM(CAST(hours AS REAL)) as total
            FROM ticket_allocations
            WHERE date >= ? AND ticket_id IN ({placeholders})
            GROUP BY ticket_id
            """,
            [start_date.isoformat(), *ticket_ids],
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT ticket_id, SUM(CAST(hours AS REAL)) as total
            FROM ticket_allocations
            WHERE date >= ?
            GROUP BY ticket_id
            """,
            (start_date.isoformat(),),
        ).fetchall()
    conn.close()
    return {
        row["ticket_id"]: Decimal(str(row["total"])).quantize(Decimal("0.01"))
        for row in rows
    }


# --- Ticket Allocation Functions ---


def _row_to_allocation(row: sqlite3.Row) -> TicketAllocation:
    """Convert a database row to a TicketAllocation object."""
    return TicketAllocation(
        ticket_id=row["ticket_id"],
        date=date.fromisoformat(row["date"]),
        hours=Decimal(row["hours"]),
        description=row["description"] if row["description"] else None,
        entered_on_client=bool(row["entered_on_client"]) if row["entered_on_client"] else False,
    )


def save_allocation(allocation: TicketAllocation) -> None:
    """Insert or update a ticket allocation."""
    conn = get_connection()
    conn.execute(
        """
        INSERT OR REPLACE INTO ticket_allocations
            (ticket_id, date, hours, description, entered_on_client)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            allocation.ticket_id,
            allocation.date.isoformat(),
            str(allocation.hours),
            allocation.description,
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


# --- Work Package Functions ---


def _row_to_work_package(row: sqlite3.Row) -> WorkPackage:
    """Convert a database row to a WorkPackage object."""
    return WorkPackage(id=row["id"], title=row["title"])


def get_all_work_packages() -> list[WorkPackage]:
    """Get all work packages."""
    conn = get_connection()
    rows = conn.execute("SELECT * FROM work_packages ORDER BY id").fetchall()
    conn.close()
    return [_row_to_work_package(row) for row in rows]


def get_work_package(wp_id: str) -> WorkPackage | None:
    """Get a single work package by ID."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM work_packages WHERE id = ?", (wp_id,)
    ).fetchone()
    conn.close()
    return _row_to_work_package(row) if row else None


def save_work_package(wp: WorkPackage) -> None:
    """Insert or update a work package."""
    conn = get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO work_packages (id, title) VALUES (?, ?)",
        (wp.id, wp.title),
    )
    conn.commit()
    conn.close()


def delete_work_package(wp_id: str) -> bool:
    """Delete a work package. Returns False if it has deliverables."""
    conn = get_connection()
    row = conn.execute(
        "SELECT COUNT(*) as c FROM deliverables WHERE work_package_id = ?",
        (wp_id,),
    ).fetchone()
    if row["c"] > 0:
        conn.close()
        return False
    conn.execute("DELETE FROM work_packages WHERE id = ?", (wp_id,))
    conn.commit()
    conn.close()
    return True


# --- Deliverable Functions ---


def _row_to_deliverable(row: sqlite3.Row) -> Deliverable:
    """Convert a database row to a Deliverable object."""
    return Deliverable(
        id=row["id"],
        work_package_id=row["work_package_id"],
        title=row["title"],
        active=bool(row["active"]),
    )


def get_all_deliverables(active_only: bool = True) -> list[Deliverable]:
    """Get all deliverables, optionally filtered to active only."""
    conn = get_connection()
    if active_only:
        rows = conn.execute(
            "SELECT * FROM deliverables WHERE active = 1 ORDER BY id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM deliverables ORDER BY id"
        ).fetchall()
    conn.close()
    return [_row_to_deliverable(row) for row in rows]


def get_deliverables_for_work_package(
    wp_id: str, active_only: bool = True,
) -> list[Deliverable]:
    """Get all deliverables for a work package."""
    conn = get_connection()
    if active_only:
        rows = conn.execute(
            "SELECT * FROM deliverables WHERE work_package_id = ? AND active = 1 "
            "ORDER BY id",
            (wp_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM deliverables WHERE work_package_id = ? ORDER BY id",
            (wp_id,),
        ).fetchall()
    conn.close()
    return [_row_to_deliverable(row) for row in rows]


def get_deliverable(del_id: str) -> Deliverable | None:
    """Get a single deliverable by ID."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM deliverables WHERE id = ?", (del_id,)
    ).fetchone()
    conn.close()
    return _row_to_deliverable(row) if row else None


def save_deliverable(d: Deliverable) -> None:
    """Insert or update a deliverable."""
    conn = get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO deliverables "
        "(id, work_package_id, title, active) VALUES (?, ?, ?, ?)",
        (d.id, d.work_package_id, d.title, int(d.active)),
    )
    conn.commit()
    conn.close()


def delete_deliverable(del_id: str) -> bool:
    """Delete a deliverable. Returns False if tickets reference it."""
    conn = get_connection()
    row = conn.execute(
        "SELECT COUNT(*) as c FROM tickets WHERE deliverable_id = ?",
        (del_id,),
    ).fetchone()
    if row["c"] > 0:
        conn.close()
        return False
    conn.execute("DELETE FROM deliverables WHERE id = ?", (del_id,))
    conn.commit()
    conn.close()
    return True


def set_ticket_deliverable(ticket_id: str, deliverable_id: str | None) -> None:
    """Set the deliverable for a ticket."""
    conn = get_connection()
    conn.execute(
        "UPDATE tickets SET deliverable_id = ? WHERE id = ?",
        (deliverable_id, ticket_id),
    )
    conn.commit()
    conn.close()


# --- Monthly Point Budget Functions ---


def get_monthly_point_budget(year: int, month: int) -> int | None:
    """Get the point budget for a specific month."""
    conn = get_connection()
    row = conn.execute(
        "SELECT max_points FROM monthly_point_budgets "
        "WHERE year = ? AND month = ?",
        (year, month),
    ).fetchone()
    conn.close()
    return row["max_points"] if row else None


def save_monthly_point_budget(year: int, month: int, max_points: int) -> None:
    """Insert or update a monthly point budget."""
    conn = get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO monthly_point_budgets "
        "(year, month, max_points) VALUES (?, ?, ?)",
        (year, month, max_points),
    )
    conn.commit()
    conn.close()


# --- Monthly Billing Functions ---


def get_monthly_billing(year: int, month: int) -> MonthlyBilling:
    """Get billing record for a month (creates default if not exists)."""
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM monthly_billing WHERE year = ? AND month = ?",
        (year, month),
    ).fetchone()
    conn.close()
    if row:
        return MonthlyBilling(
            year=row["year"],
            month=row["month"],
            finalised=bool(row["finalised"]),
        )
    return MonthlyBilling(year=year, month=month, finalised=False)


def save_monthly_billing(billing: MonthlyBilling) -> None:
    """Insert or update a monthly billing record."""
    conn = get_connection()
    conn.execute(
        "INSERT OR REPLACE INTO monthly_billing "
        "(year, month, finalised) VALUES (?, ?, ?)",
        (billing.year, billing.month, int(billing.finalised)),
    )
    conn.commit()
    conn.close()


@dataclass
class DeliverableBillingLine:
    """A single line in the billing breakdown."""

    deliverable_id: str | None
    deliverable_title: str
    work_package_id: str
    work_package_title: str
    hours: Decimal
    points: Decimal
    amount_ex_vat: Decimal
    amount_inc_vat: Decimal


def get_billable_tickets(
    contract_start: date | None = None,
) -> list[Ticket]:
    """Get all tickets that are closed but not yet billed.

    If contract_start is given, only returns tickets that have at least
    one allocation on or after that date - so closed tickets whose
    hours pre-date the points contract (and were thus billed hourly)
    are excluded from the points-billing list.
    """
    conn = get_connection()
    if contract_start is not None:
        rows = conn.execute(
            """
            SELECT t.* FROM tickets t
            WHERE t.archived = 1 AND t.billed = 0
              AND EXISTS (
                  SELECT 1 FROM ticket_allocations ta
                  WHERE ta.ticket_id = t.id AND ta.date >= ?
              )
            ORDER BY t.id
            """,
            (contract_start.isoformat(),),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM tickets WHERE archived = 1 AND billed = 0 ORDER BY id",
        ).fetchall()
    conn.close()
    return [_row_to_ticket(row) for row in rows]


def get_current_bill_summary(
    hours_per_point: Decimal,
    point_rate: Decimal,
    vat_rate: Decimal,
    contract_start: date | None = None,
) -> list[DeliverableBillingLine]:
    """Get billing breakdown by deliverable for all closed-and-unbilled tickets.

    Returns a list of DeliverableBillingLine, one per deliverable
    (plus one for unlinked allocations if any). Sums allocations on
    each billable ticket. If contract_start is given, only counts
    allocations on or after that date - so pre-points-system hours
    (already billed hourly) don't appear here.
    """
    conn = get_connection()
    sql = """
        SELECT
            t.deliverable_id,
            d.title as deliverable_title,
            d.work_package_id,
            wp.title as work_package_title,
            SUM(CAST(ta.hours AS REAL)) as total_hours
        FROM ticket_allocations ta
        JOIN tickets t ON ta.ticket_id = t.id
        LEFT JOIN deliverables d ON t.deliverable_id = d.id
        LEFT JOIN work_packages wp ON d.work_package_id = wp.id
        WHERE t.archived = 1 AND t.billed = 0
    """
    params: list[object] = []
    if contract_start is not None:
        sql += " AND ta.date >= ?"
        params.append(contract_start.isoformat())
    sql += """
        GROUP BY t.deliverable_id
        HAVING total_hours > 0
        ORDER BY d.work_package_id, t.deliverable_id
    """
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    lines: list[DeliverableBillingLine] = []
    for row in rows:
        hours = Decimal(str(row["total_hours"])).quantize(Decimal("0.01"))
        points = (hours / hours_per_point).to_integral_value(rounding=ROUND_CEILING)
        amount_ex = (points * point_rate).quantize(Decimal("0.01"))
        amount_inc = (amount_ex * (1 + vat_rate)).quantize(Decimal("0.01"))
        lines.append(DeliverableBillingLine(
            deliverable_id=row["deliverable_id"],
            deliverable_title=row["deliverable_title"] or "Unlinked",
            work_package_id=row["work_package_id"] or "",
            work_package_title=row["work_package_title"] or "",
            hours=hours,
            points=points,
            amount_ex_vat=amount_ex,
            amount_inc_vat=amount_inc,
        ))
    return lines


def get_billed_points_total(
    hours_per_point: Decimal,
    up_to_year: int | None = None,
    up_to_month: int | None = None,
    contract_start: date | None = None,
) -> Decimal:
    """Get total points already billed (closed + billed tickets).

    Optionally filter to bills marked with billed_year/billed_month
    on or before the given year/month. If contract_start is given,
    only counts allocations on or after that date - so any hours that
    pre-date the points contract (billed under the prior hourly scheme)
    don't pollute the points-system YTD totals.
    """
    conn = get_connection()
    sql = """
        SELECT COALESCE(SUM(CAST(ta.hours AS REAL)), 0) as total
        FROM ticket_allocations ta
        JOIN tickets t ON ta.ticket_id = t.id
        WHERE t.billed = 1
    """
    params: list[object] = []
    if contract_start is not None:
        sql += " AND ta.date >= ?"
        params.append(contract_start.isoformat())
    if up_to_year is not None and up_to_month is not None:
        sql += (
            " AND (t.billed_year < ? OR "
            "(t.billed_year = ? AND t.billed_month <= ?))"
        )
        params.extend([up_to_year, up_to_year, up_to_month])
    row = conn.execute(sql, params).fetchone()
    conn.close()
    total_hours = Decimal(str(row["total"])).quantize(Decimal("0.01"))
    return (total_hours / hours_per_point).to_integral_value(rounding=ROUND_CEILING)


def get_monthly_points_breakdown(
    year: int,
    month: int,
    hours_per_point: Decimal,
) -> tuple[int, int, int]:
    """Get points breakdown for work done in a single month.

    Returns (billed_points, billable_points, speculative_points):
    - billed: closed and already billed
    - billable: closed but not yet billed
    - speculative: still open
    """
    from calendar import monthrange

    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    conn = get_connection()
    rows = conn.execute(
        """
        SELECT t.archived, t.billed, ta.ticket_id,
               SUM(CAST(ta.hours AS REAL)) as total
        FROM ticket_allocations ta
        JOIN tickets t ON ta.ticket_id = t.id
        WHERE ta.date >= ? AND ta.date <= ?
        GROUP BY ta.ticket_id, t.archived, t.billed
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    conn.close()

    billed = 0
    billable = 0
    speculative = 0
    for row in rows:
        hours = Decimal(str(row["total"]))
        pts = int((hours / hours_per_point).to_integral_value(rounding=ROUND_CEILING))
        if row["archived"] and row["billed"]:
            billed += pts
        elif row["archived"]:
            billable += pts
        else:
            speculative += pts
    return billed, billable, speculative


def get_carryover_tickets(
    year: int, month: int,
    contract_start: date | None = None,
) -> list[tuple[Ticket, Decimal]]:
    """Get unbilled tickets with allocations strictly before the given month.

    Excludes tickets that already have allocations in the given month
    (those are already shown in the main allocations list). Returns
    each ticket paired with its allocated hours total. If contract_start
    is given, only counts allocations on or after that date - so
    tickets with only pre-points-system work (already billed hourly)
    don't appear as carryover.
    """
    from calendar import monthrange

    month_start = date(year, month, 1).isoformat()
    month_end = date(year, month, monthrange(year, month)[1]).isoformat()

    conn = get_connection()
    sql = """
        SELECT t.*,
               COALESCE(SUM(CAST(ta.hours AS REAL)), 0) as lifetime_hours
        FROM tickets t
        JOIN ticket_allocations ta ON ta.ticket_id = t.id
        WHERE t.billed = 0
          AND ta.date < ?
    """
    params: list[object] = [month_start]
    if contract_start is not None:
        sql += " AND ta.date >= ?"
        params.append(contract_start.isoformat())
    sql += """
          AND NOT EXISTS (
              SELECT 1 FROM ticket_allocations ta2
              WHERE ta2.ticket_id = t.id
                AND ta2.date >= ? AND ta2.date <= ?
          )
        GROUP BY t.id
        HAVING lifetime_hours > 0
        ORDER BY t.id
    """
    params.extend([month_start, month_end])
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    return [
        (
            _row_to_ticket(row),
            Decimal(str(row["lifetime_hours"])).quantize(Decimal("0.01")),
        )
        for row in rows
    ]


def finalise_bill(
    year: int, month: int,
    contract_start: date | None = None,
) -> list[str]:
    """Mark all currently-billable tickets as billed for the given month.

    Atomic: marks tickets and saves the monthly_billing record in one
    transaction. Returns the list of ticket IDs that were billed. If
    contract_start is given, only tickets with at least one allocation
    on or after that date are marked - so historical hourly-billed
    tickets aren't pulled into the points billing record.
    """
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        if contract_start is not None:
            rows = conn.execute(
                """
                SELECT t.id FROM tickets t
                WHERE t.archived = 1 AND t.billed = 0
                  AND EXISTS (
                      SELECT 1 FROM ticket_allocations ta
                      WHERE ta.ticket_id = t.id AND ta.date >= ?
                  )
                """,
                (contract_start.isoformat(),),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id FROM tickets WHERE archived = 1 AND billed = 0",
            ).fetchall()
        ticket_ids = [row["id"] for row in rows]
        if ticket_ids:
            placeholders = ",".join("?" * len(ticket_ids))
            conn.execute(
                f"UPDATE tickets SET billed = 1, billed_year = ?, "
                f"billed_month = ? WHERE id IN ({placeholders}) "
                f"AND archived = 1 AND billed = 0",
                [year, month, *ticket_ids],
            )
        conn.execute(
            "INSERT OR REPLACE INTO monthly_billing "
            "(year, month, finalised) VALUES (?, ?, 1)",
            (year, month),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return ticket_ids


def unfinalise_bill(year: int, month: int) -> None:
    """Reverse a bill finalisation - clear billed marks and the record."""
    conn = get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE tickets SET billed = 0, billed_year = NULL, "
            "billed_month = NULL WHERE billed_year = ? AND billed_month = ?",
            (year, month),
        )
        conn.execute(
            "UPDATE monthly_billing SET finalised = 0 "
            "WHERE year = ? AND month = ?",
            (year, month),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_cumulative_point_budget(
    contract_start: date,
    up_to_year: int,
    up_to_month: int,
) -> int:
    """Sum monthly point budgets from contract_start month to up_to month inclusive."""
    total = 0
    y, m = contract_start.year, contract_start.month
    while (y, m) <= (up_to_year, up_to_month):
        budget = get_monthly_point_budget(y, m)
        if budget:
            total += budget
        if m == 12:
            y += 1
            m = 1
        else:
            m += 1
    return total
