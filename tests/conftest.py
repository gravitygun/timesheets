"""Shared fixtures for tests."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from datetime import date, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Generator

import pytest

# Set up test database before importing storage
_test_db_fd, _test_db_path = tempfile.mkstemp(suffix=".db")
os.environ["TIMESHEET_DB"] = _test_db_path


@pytest.fixture(scope="session", autouse=True)
def setup_test_db() -> Generator[Path, None, None]:
    """Set up a test database for the entire test session."""
    import storage

    # Reinitialise storage module with test db path
    storage.DB_PATH = Path(_test_db_path)
    storage.init_db()

    yield Path(_test_db_path)

    # Cleanup
    os.close(_test_db_fd)
    os.unlink(_test_db_path)


@pytest.fixture
def db_connection(setup_test_db: Path) -> Generator[sqlite3.Connection, None, None]:
    """Provide a database connection for tests."""
    import storage

    conn = storage.get_connection()
    yield conn
    conn.close()


@pytest.fixture
def clean_db(setup_test_db: Path) -> Generator[None, None, None]:
    """Clean database tables before each test."""
    import storage

    conn = storage.get_connection()
    conn.execute("DELETE FROM ticket_allocations")
    conn.execute("DELETE FROM tickets")
    conn.execute("DELETE FROM time_entries")
    conn.execute("DELETE FROM config")
    conn.commit()
    conn.close()

    yield


@pytest.fixture
def sample_time_entry():
    """Create a sample TimeEntry for testing."""
    from models import TimeEntry

    return TimeEntry(
        date=date(2026, 1, 27),
        day_of_week="Mon",
        clock_in=time(9, 0),
        lunch_duration=timedelta(minutes=30),
        clock_out=time(17, 0),
        adjustment=None,
        adjust_type=None,
        comment="Test entry",
    )


@pytest.fixture
def sample_time_entry_with_adjustment():
    """Create a sample TimeEntry with adjustment."""
    from models import TimeEntry

    return TimeEntry(
        date=date(2026, 1, 28),
        day_of_week="Tue",
        clock_in=None,
        lunch_duration=None,
        clock_out=None,
        adjustment=timedelta(hours=7.5),
        adjust_type="L",
        comment="Leave day",
    )


@pytest.fixture
def sample_ticket():
    """Create a sample Ticket for testing."""
    from models import Ticket

    return Ticket(
        id="PROJ-123",
        description="Test project ticket",
        archived=False,
        created_at=date(2026, 1, 1),
    )


@pytest.fixture
def sample_allocation():
    """Create a sample TicketAllocation for testing."""
    from models import TicketAllocation

    return TicketAllocation(
        ticket_id="PROJ-123",
        date=date(2026, 1, 27),
        hours=Decimal("4.5"),
    )


@pytest.fixture
def sample_config():
    """Create a sample Config for testing."""
    from models import Config

    return Config(
        hourly_rate=Decimal("97"),
        currency="GBP",
        standard_day_hours=Decimal("7.5"),
        vat_rate=Decimal("0.20"),
    )
