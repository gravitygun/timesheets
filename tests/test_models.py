"""Tests for models.py - TimeEntry and Config dataclasses."""

from datetime import date, time, timedelta
from decimal import Decimal

from models import TimeEntry, Config


class TestTimeEntry:
    """Tests for TimeEntry dataclass."""

    def test_worked_hours_full_day(self):
        """Test worked hours for a standard 7.5 hour day."""
        entry = TimeEntry(
            date=date(2026, 1, 15),
            day_of_week="Wed",
            clock_in=time(9, 0),
            lunch_duration=timedelta(minutes=30),
            clock_out=time(17, 0),
        )
        assert entry.worked_hours == Decimal("7.50")

    def test_worked_hours_no_lunch(self):
        """Test worked hours with no lunch break."""
        entry = TimeEntry(
            date=date(2026, 1, 15),
            day_of_week="Wed",
            clock_in=time(9, 0),
            clock_out=time(17, 0),
        )
        assert entry.worked_hours == Decimal("8.00")

    def test_worked_hours_short_day(self):
        """Test worked hours for a short day."""
        entry = TimeEntry(
            date=date(2026, 1, 15),
            day_of_week="Wed",
            clock_in=time(10, 0),
            lunch_duration=timedelta(minutes=30),
            clock_out=time(14, 30),
        )
        assert entry.worked_hours == Decimal("4.00")

    def test_worked_hours_long_day(self):
        """Test worked hours for a long day with overtime."""
        entry = TimeEntry(
            date=date(2026, 1, 15),
            day_of_week="Wed",
            clock_in=time(8, 0),
            lunch_duration=timedelta(minutes=60),
            clock_out=time(19, 0),
        )
        assert entry.worked_hours == Decimal("10.00")

    def test_worked_hours_no_clock_in(self):
        """Test worked hours when clock_in is missing."""
        entry = TimeEntry(
            date=date(2026, 1, 15),
            day_of_week="Wed",
            clock_out=time(17, 0),
        )
        assert entry.worked_hours == Decimal("0")

    def test_worked_hours_no_clock_out(self):
        """Test worked hours when clock_out is missing."""
        entry = TimeEntry(
            date=date(2026, 1, 15),
            day_of_week="Wed",
            clock_in=time(9, 0),
        )
        assert entry.worked_hours == Decimal("0")

    def test_worked_hours_empty_entry(self):
        """Test worked hours for an empty entry."""
        entry = TimeEntry(
            date=date(2026, 1, 15),
            day_of_week="Wed",
        )
        assert entry.worked_hours == Decimal("0")

    def test_worked_hours_with_minutes(self):
        """Test worked hours with non-round minutes."""
        entry = TimeEntry(
            date=date(2026, 1, 15),
            day_of_week="Wed",
            clock_in=time(9, 15),
            lunch_duration=timedelta(minutes=30),
            clock_out=time(17, 30),
        )
        assert entry.worked_hours == Decimal("7.75")

    def test_adjusted_hours_full_day(self):
        """Test adjusted hours for a full day off."""
        entry = TimeEntry(
            date=date(2026, 1, 15),
            day_of_week="Wed",
            adjustment=timedelta(hours=7, minutes=30),
            adjust_type="L",
        )
        assert entry.adjusted_hours == Decimal("7.50")

    def test_adjusted_hours_half_day(self):
        """Test adjusted hours for a half day."""
        entry = TimeEntry(
            date=date(2026, 1, 15),
            day_of_week="Wed",
            adjustment=timedelta(hours=3, minutes=45),
            adjust_type="L",
        )
        assert entry.adjusted_hours == Decimal("3.75")

    def test_adjusted_hours_none(self):
        """Test adjusted hours when no adjustment."""
        entry = TimeEntry(
            date=date(2026, 1, 15),
            day_of_week="Wed",
        )
        assert entry.adjusted_hours == Decimal("0")

    def test_total_hours_worked_only(self):
        """Test total hours with only worked time."""
        entry = TimeEntry(
            date=date(2026, 1, 15),
            day_of_week="Wed",
            clock_in=time(9, 0),
            lunch_duration=timedelta(minutes=30),
            clock_out=time(17, 0),
        )
        assert entry.total_hours == Decimal("7.50")

    def test_total_hours_adjustment_only(self):
        """Test total hours with only adjustment."""
        entry = TimeEntry(
            date=date(2026, 1, 15),
            day_of_week="Wed",
            adjustment=timedelta(hours=7, minutes=30),
            adjust_type="P",
        )
        assert entry.total_hours == Decimal("7.50")

    def test_total_hours_worked_plus_adjustment(self):
        """Test total hours with both worked time and adjustment."""
        entry = TimeEntry(
            date=date(2026, 1, 15),
            day_of_week="Wed",
            clock_in=time(9, 0),
            lunch_duration=timedelta(minutes=30),
            clock_out=time(13, 0),  # Half day worked (3.5h)
            adjustment=timedelta(hours=4),  # Plus 4h adjustment
            adjust_type="T",
        )
        assert entry.total_hours == Decimal("7.50")

    def test_total_hours_empty(self):
        """Test total hours for empty entry."""
        entry = TimeEntry(
            date=date(2026, 1, 15),
            day_of_week="Wed",
        )
        assert entry.total_hours == Decimal("0")


class TestConfig:
    """Tests for Config dataclass."""

    def test_default_values(self):
        """Test default config values."""
        config = Config()
        assert config.hourly_rate == Decimal("97")
        assert config.currency == "GBP"
        assert config.standard_day_hours == Decimal("7.5")
        assert config.vat_rate == Decimal("0.20")

    def test_custom_values(self):
        """Test config with custom values."""
        config = Config(
            hourly_rate=Decimal("120"),
            currency="USD",
            standard_day_hours=Decimal("8"),
            vat_rate=Decimal("0.10"),
        )
        assert config.hourly_rate == Decimal("120")
        assert config.currency == "USD"
        assert config.standard_day_hours == Decimal("8")
        assert config.vat_rate == Decimal("0.10")
