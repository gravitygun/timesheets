"""Tests for storage.py - database operations."""

from datetime import date, time, timedelta
from decimal import Decimal

import pytest

from models import TimeEntry, Config


# We need to set TIMESHEET_DB before importing storage
@pytest.fixture(autouse=True)
def temp_database(tmp_path, monkeypatch):
    """Use a temporary database for all tests."""
    db_path = tmp_path / "test_timesheet.db"
    monkeypatch.setenv("TIMESHEET_DB", str(db_path))

    # Re-import storage to pick up new DB_PATH
    import importlib
    import storage
    importlib.reload(storage)

    # Initialise the database
    storage.init_db()

    yield storage

    # Clean up
    if db_path.exists():
        db_path.unlink()


class TestInitDb:
    """Tests for init_db function."""

    def test_creates_tables(self, temp_database):
        """Test that init_db creates the required tables."""
        storage = temp_database
        conn = storage.get_connection()

        # Check time_entries table exists
        result = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='time_entries'"
        ).fetchone()
        assert result is not None

        # Check config table exists
        result = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='config'"
        ).fetchone()
        assert result is not None

        conn.close()

    def test_creates_index(self, temp_database):
        """Test that init_db creates the date index."""
        storage = temp_database
        conn = storage.get_connection()

        result = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_entries_date'"
        ).fetchone()
        assert result is not None

        conn.close()

    def test_idempotent(self, temp_database):
        """Test that init_db can be called multiple times safely."""
        storage = temp_database
        # Call init_db again - should not raise
        storage.init_db()
        storage.init_db()


class TestSaveAndGetEntry:
    """Tests for save_entry and get_entry functions."""

    def test_save_and_retrieve_full_entry(self, temp_database):
        """Test saving and retrieving a complete time entry."""
        storage = temp_database
        entry = TimeEntry(
            date=date(2026, 1, 15),
            day_of_week="Wed",
            clock_in=time(9, 0),
            lunch_duration=timedelta(minutes=30),
            clock_out=time(17, 0),
            adjustment=timedelta(hours=1),
            adjust_type="T",
            comment="Training session",
        )

        storage.save_entry(entry)
        retrieved = storage.get_entry(date(2026, 1, 15))

        assert retrieved is not None
        assert retrieved.date == date(2026, 1, 15)
        assert retrieved.day_of_week == "Wed"
        assert retrieved.clock_in == time(9, 0)
        assert retrieved.lunch_duration == timedelta(minutes=30)
        assert retrieved.clock_out == time(17, 0)
        assert retrieved.adjustment == timedelta(hours=1)
        assert retrieved.adjust_type == "T"
        assert retrieved.comment == "Training session"

    def test_save_and_retrieve_minimal_entry(self, temp_database):
        """Test saving and retrieving a minimal time entry."""
        storage = temp_database
        entry = TimeEntry(
            date=date(2026, 1, 15),
            day_of_week="Wed",
        )

        storage.save_entry(entry)
        retrieved = storage.get_entry(date(2026, 1, 15))

        assert retrieved is not None
        assert retrieved.date == date(2026, 1, 15)
        assert retrieved.clock_in is None
        assert retrieved.lunch_duration is None
        assert retrieved.clock_out is None
        assert retrieved.adjustment is None
        assert retrieved.adjust_type is None
        assert retrieved.comment is None

    def test_save_and_retrieve_adjustment_only(self, temp_database):
        """Test saving and retrieving an adjustment-only entry (e.g., holiday)."""
        storage = temp_database
        entry = TimeEntry(
            date=date(2026, 1, 15),
            day_of_week="Wed",
            adjustment=timedelta(hours=7, minutes=30),
            adjust_type="P",
            comment="Bank Holiday",
        )

        storage.save_entry(entry)
        retrieved = storage.get_entry(date(2026, 1, 15))

        assert retrieved is not None
        assert retrieved.clock_in is None
        assert retrieved.clock_out is None
        assert retrieved.adjustment == timedelta(hours=7, minutes=30)
        assert retrieved.adjust_type == "P"
        assert retrieved.comment == "Bank Holiday"

    def test_get_nonexistent_entry(self, temp_database):
        """Test getting an entry that doesn't exist."""
        storage = temp_database
        retrieved = storage.get_entry(date(2026, 12, 25))
        assert retrieved is None

    def test_update_existing_entry(self, temp_database):
        """Test that saving an entry with same date updates it."""
        storage = temp_database

        # Save initial entry
        entry1 = TimeEntry(
            date=date(2026, 1, 15),
            day_of_week="Wed",
            clock_in=time(9, 0),
            clock_out=time(17, 0),
        )
        storage.save_entry(entry1)

        # Save updated entry
        entry2 = TimeEntry(
            date=date(2026, 1, 15),
            day_of_week="Wed",
            clock_in=time(10, 0),
            clock_out=time(18, 0),
            comment="Updated",
        )
        storage.save_entry(entry2)

        # Retrieve and verify it's updated
        retrieved = storage.get_entry(date(2026, 1, 15))
        assert retrieved.clock_in == time(10, 0)
        assert retrieved.clock_out == time(18, 0)
        assert retrieved.comment == "Updated"


class TestGetEntriesRange:
    """Tests for get_entries_range function."""

    def test_get_entries_in_range(self, temp_database):
        """Test getting entries within a date range."""
        storage = temp_database

        # Create entries
        for day in range(10, 16):  # Jan 10-15
            entry = TimeEntry(
                date=date(2026, 1, day),
                day_of_week="Day",
                clock_in=time(9, 0),
                clock_out=time(17, 0),
            )
            storage.save_entry(entry)

        # Get subset
        entries = storage.get_entries_range(date(2026, 1, 12), date(2026, 1, 14))

        assert len(entries) == 3
        dates = [e.date for e in entries]
        assert date(2026, 1, 12) in dates
        assert date(2026, 1, 13) in dates
        assert date(2026, 1, 14) in dates

    def test_get_entries_range_empty(self, temp_database):
        """Test getting entries from empty range."""
        storage = temp_database
        entries = storage.get_entries_range(date(2026, 6, 1), date(2026, 6, 30))
        assert entries == []

    def test_get_entries_range_ordered(self, temp_database):
        """Test that entries are returned in date order."""
        storage = temp_database

        # Save entries in reverse order
        for day in [15, 12, 14, 10, 13, 11]:
            entry = TimeEntry(
                date=date(2026, 1, day),
                day_of_week="Day",
            )
            storage.save_entry(entry)

        entries = storage.get_entries_range(date(2026, 1, 10), date(2026, 1, 15))

        # Should be in ascending date order
        for i in range(len(entries) - 1):
            assert entries[i].date < entries[i + 1].date


class TestGetMonthEntries:
    """Tests for get_month_entries function."""

    def test_get_month_entries(self, temp_database):
        """Test getting all entries for a month."""
        storage = temp_database

        # Create entries for January
        for day in [5, 10, 15, 20, 25]:
            entry = TimeEntry(
                date=date(2026, 1, day),
                day_of_week="Day",
            )
            storage.save_entry(entry)

        # Create entry for February (should not be included)
        storage.save_entry(TimeEntry(date=date(2026, 2, 1), day_of_week="Sun"))

        entries = storage.get_month_entries(2026, 1)

        assert len(entries) == 5
        for e in entries:
            assert e.date.month == 1


class TestConfig:
    """Tests for config operations."""

    def test_get_default_config(self, temp_database):
        """Test getting default config when none saved."""
        storage = temp_database
        config = storage.get_config()

        assert config.hourly_rate == Decimal("97")
        assert config.currency == "GBP"
        assert config.standard_day_hours == Decimal("7.5")
        assert config.vat_rate == Decimal("0.20")

    def test_save_and_get_config(self, temp_database):
        """Test saving and retrieving config."""
        storage = temp_database

        config = Config(
            hourly_rate=Decimal("120"),
            currency="USD",
            standard_day_hours=Decimal("8"),
            vat_rate=Decimal("0.15"),
        )
        storage.save_config(config)

        retrieved = storage.get_config()

        assert retrieved.hourly_rate == Decimal("120")
        assert retrieved.currency == "USD"
        assert retrieved.standard_day_hours == Decimal("8")
        assert retrieved.vat_rate == Decimal("0.15")

    def test_update_config(self, temp_database):
        """Test updating config values."""
        storage = temp_database

        # Save initial config
        config1 = Config(hourly_rate=Decimal("100"))
        storage.save_config(config1)

        # Update config
        config2 = Config(hourly_rate=Decimal("110"))
        storage.save_config(config2)

        # Retrieve and verify
        retrieved = storage.get_config()
        assert retrieved.hourly_rate == Decimal("110")


class TestHolidays:
    """Tests for holiday-related functions."""

    def test_get_uk_holidays(self, temp_database):
        """Test getting UK bank holidays."""
        storage = temp_database
        holidays = storage.get_uk_holidays(2026)

        # Should have some holidays
        assert len(holidays) > 0

        # Check for known holidays
        # New Year's Day 2026 is January 1 (Thursday)
        assert date(2026, 1, 1) in holidays

        # Christmas Day 2026 is December 25 (Friday)
        assert date(2026, 12, 25) in holidays

    def test_get_holidays_in_range(self, temp_database):
        """Test getting holidays in a specific range."""
        storage = temp_database
        holidays = storage.get_holidays_in_range(date(2026, 12, 1), date(2026, 12, 31))

        # December 2026 should have Christmas Day and Boxing Day
        assert date(2026, 12, 25) in holidays
        # Boxing Day is Saturday, so substitute will be Monday Dec 28
        # (or depends on exact UK holiday rules)

    def test_get_holidays_excludes_weekends(self, temp_database):
        """Test that get_holidays_in_range excludes weekend holidays."""
        storage = temp_database
        holidays = storage.get_holidays_in_range(date(2026, 1, 1), date(2026, 12, 31))

        # All returned holidays should be weekdays
        for d in holidays:
            assert d.weekday() < 5, f"{d} is a weekend day"

    def test_get_working_days(self, temp_database):
        """Test getting working days in a range."""
        storage = temp_database
        # January 2026: 1st is Thursday, has 22 weekdays normally
        working_days = storage.get_working_days(date(2026, 1, 1), date(2026, 1, 31))

        # Should exclude weekends
        for d in working_days:
            assert d.weekday() < 5

        # Should exclude New Year's Day
        assert date(2026, 1, 1) not in working_days

    def test_populate_holidays(self, temp_database):
        """Test populating holidays for a month."""
        storage = temp_database

        # Populate holidays for December 2026
        count = storage.populate_holidays(2026, 12, Decimal("7.5"))

        # Should have created some entries
        assert count > 0

        # Check that entries were created with correct data
        entries = storage.get_month_entries(2026, 12)
        holiday_entries = [e for e in entries if e.adjust_type == "P"]

        for entry in holiday_entries:
            assert entry.adjustment == timedelta(hours=7, minutes=30)
            assert entry.comment is not None  # Should have holiday name

    def test_populate_holidays_idempotent(self, temp_database):
        """Test that populate_holidays doesn't duplicate entries."""
        storage = temp_database

        count1 = storage.populate_holidays(2026, 12, Decimal("7.5"))
        assert count1 > 0  # First call should create entries

        count2 = storage.populate_holidays(2026, 12, Decimal("7.5"))
        assert count2 == 0  # Second call should create no new entries

    def test_populate_holidays_preserves_existing(self, temp_database):
        """Test that populate_holidays doesn't overwrite existing entries."""
        storage = temp_database

        # First, manually create an entry for Christmas
        manual_entry = TimeEntry(
            date=date(2026, 12, 25),
            day_of_week="Fri",
            clock_in=time(9, 0),
            clock_out=time(12, 0),
            comment="Worked half day",
        )
        storage.save_entry(manual_entry)

        # Now populate holidays
        storage.populate_holidays(2026, 12, Decimal("7.5"))

        # Check that our manual entry wasn't overwritten
        retrieved = storage.get_entry(date(2026, 12, 25))
        assert retrieved.clock_in == time(9, 0)
        assert retrieved.comment == "Worked half day"
