"""Tests for the widgets module."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from unittest.mock import MagicMock


from models import Config
from widgets import CombinedHeader, DayHeader, DaySummary, DayTimeEntry, WeeklySummary


class TestCombinedHeader:
    """Tests for the CombinedHeader widget."""

    def test_init(self):
        """Test CombinedHeader initialisation."""
        header = CombinedHeader(2026, 1)

        assert header.year == 2026
        assert header.month == 1
        assert header.week_nav_start == 0
        assert header.left_arrow_pos == 0
        assert header.right_arrow_pos == 0

    def test_update_display(self):
        """Test updating the header display."""
        header = CombinedHeader(2026, 1)

        # Mock the update method since we can't render without an app
        header.update = MagicMock()

        header.update_display(
            week_num=2,
            total_weeks=5,
            week_start=date(2026, 1, 3),
            week_end=date(2026, 1, 9),
        )

        # Check that update was called
        header.update.assert_called_once()

        # Check that positions were calculated
        assert header.week_nav_start > 0
        assert header.left_arrow_pos >= 0
        assert header.right_arrow_pos > header.left_arrow_pos

    def test_update_display_positions(self):
        """Test that arrow positions are calculated correctly."""
        header = CombinedHeader(2026, 1)
        header.update = MagicMock()

        header.update_display(
            week_num=1,
            total_weeks=4,
            week_start=date(2026, 1, 1),
            week_end=date(2026, 1, 7),
        )

        # Right arrow should be after left arrow
        assert header.right_arrow_pos > header.left_arrow_pos

        # Week nav start should be at left arrow position
        assert header.week_nav_start == header.left_arrow_pos


class TestWeeklySummary:
    """Tests for the WeeklySummary widget."""

    def test_update_display_all_zero(self):
        """Test display with all zero values."""
        summary = WeeklySummary()
        summary.update = MagicMock()

        config = Config()
        summary.update_display(
            worked=Decimal("0"),
            max_hours=Decimal("37.5"),
            leave=Decimal("0"),
            sick=Decimal("0"),
            training=Decimal("0"),
            public_holiday=Decimal("0"),
            config=config,
        )

        summary.update.assert_called_once()

    def test_update_display_with_worked_hours(self):
        """Test display with worked hours."""
        summary = WeeklySummary()
        summary.update = MagicMock()

        config = Config()
        summary.update_display(
            worked=Decimal("30"),
            max_hours=Decimal("37.5"),
            leave=Decimal("0"),
            sick=Decimal("0"),
            training=Decimal("0"),
            public_holiday=Decimal("0"),
            config=config,
        )

        summary.update.assert_called_once()
        # Verify the Text object was created (check call args)
        call_args = summary.update.call_args[0][0]
        text_str = str(call_args)
        assert "Worked" in text_str
        assert "30" in text_str

    def test_update_display_with_all_types(self):
        """Test display with all adjustment types."""
        summary = WeeklySummary()
        summary.update = MagicMock()

        config = Config()
        summary.update_display(
            worked=Decimal("22.5"),
            max_hours=Decimal("37.5"),
            leave=Decimal("7.5"),
            sick=Decimal("0"),
            training=Decimal("7.5"),
            public_holiday=Decimal("0"),
            config=config,
        )

        summary.update.assert_called_once()

    def test_update_display_percentage_calculation(self):
        """Test that percentage is calculated correctly."""
        summary = WeeklySummary()
        summary.update = MagicMock()

        config = Config()
        summary.update_display(
            worked=Decimal("30"),
            max_hours=Decimal("37.5"),  # 80%
            leave=Decimal("0"),
            sick=Decimal("0"),
            training=Decimal("0"),
            public_holiday=Decimal("0"),
            config=config,
        )

        call_args = summary.update.call_args[0][0]
        text_str = str(call_args)
        assert "80.0%" in text_str

    def test_update_display_zero_max_hours(self):
        """Test display when max hours is zero (avoid division by zero)."""
        summary = WeeklySummary()
        summary.update = MagicMock()

        config = Config()
        summary.update_display(
            worked=Decimal("0"),
            max_hours=Decimal("0"),
            leave=Decimal("0"),
            sick=Decimal("0"),
            training=Decimal("0"),
            public_holiday=Decimal("0"),
            config=config,
        )

        # Should not raise an error
        summary.update.assert_called_once()


class TestDayHeader:
    """Tests for the DayHeader widget."""

    def test_init(self):
        """Test DayHeader initialisation."""
        header = DayHeader()

        assert header.current_date is None
        assert header.worked_hours == Decimal("0")

    def test_update_display(self):
        """Test updating the day header display."""
        header = DayHeader()
        header.update = MagicMock()

        test_date = date(2026, 1, 27)
        worked = Decimal("7.50")

        header.update_display(test_date, worked)

        assert header.current_date == test_date
        assert header.worked_hours == worked
        header.update.assert_called_once()

    def test_update_display_content(self):
        """Test that display content is correct."""
        header = DayHeader()
        header.update = MagicMock()

        test_date = date(2026, 1, 27)  # Tuesday
        worked = Decimal("7.50")

        header.update_display(test_date, worked)

        call_args = header.update.call_args[0][0]
        text_str = str(call_args)

        assert "DAY:" in text_str
        assert "Tue" in text_str
        assert "Jan 27" in text_str
        assert "2026" in text_str
        # Worked hours now shown in DaySummary, not header

    def test_update_display_zero_hours(self):
        """Test display with zero worked hours."""
        header = DayHeader()
        header.update = MagicMock()

        header.update_display(date(2026, 1, 27), Decimal("0"))

        call_args = header.update.call_args[0][0]
        text_str = str(call_args)
        # Header no longer shows worked hours (now in DaySummary)
        assert "DAY:" in text_str


class TestDaySummary:
    """Tests for the DaySummary widget."""

    def test_update_display_no_hours(self):
        """Test display when no hours worked."""
        summary = DaySummary()
        summary.update = MagicMock()

        summary.update_display(
            allocated=Decimal("0"),
            worked=Decimal("0"),
        )

        call_args = summary.update.call_args[0][0]
        text_str = str(call_args)
        assert "No hours" in text_str

    def test_update_display_exact_match(self):
        """Test display when allocation exactly matches worked."""
        summary = DaySummary()
        summary.update = MagicMock()

        summary.update_display(
            allocated=Decimal("7.5"),
            worked=Decimal("7.5"),
        )

        call_args = summary.update.call_args[0][0]
        text_str = str(call_args)
        assert "Worked: 7.50h" in text_str
        assert "Exact" in text_str
        assert "100.0%" in text_str

    def test_update_display_under_allocated(self):
        """Test display when under-allocated."""
        summary = DaySummary()
        summary.update = MagicMock()

        summary.update_display(
            allocated=Decimal("5"),
            worked=Decimal("7.5"),
        )

        call_args = summary.update.call_args[0][0]
        text_str = str(call_args)
        assert "Under" in text_str

    def test_update_display_over_allocated(self):
        """Test display when over-allocated."""
        summary = DaySummary()
        summary.update = MagicMock()

        summary.update_display(
            allocated=Decimal("10"),
            worked=Decimal("7.5"),
        )

        call_args = summary.update.call_args[0][0]
        text_str = str(call_args)
        assert "Over" in text_str

    def test_update_display_percentage_calculation(self):
        """Test percentage calculation."""
        summary = DaySummary()
        summary.update = MagicMock()

        # 50% allocated
        summary.update_display(
            allocated=Decimal("3.75"),
            worked=Decimal("7.5"),
        )

        call_args = summary.update.call_args[0][0]
        text_str = str(call_args)
        assert "50.0%" in text_str


class TestDayTimeEntry:
    """Tests for the DayTimeEntry widget."""

    def test_update_display_full_entry(self):
        """Test display with full time entry data."""
        entry = DayTimeEntry()
        entry.update = MagicMock()

        entry.update_display(
            clock_in="09:00",
            lunch="30m",
            clock_out="17:00",
            adjustment="-",
            adjust_type="",
            comment="Working on project",
        )

        entry.update.assert_called_once()
        call_args = entry.update.call_args[0][0]
        text_str = str(call_args)
        assert "In:" in text_str
        assert "09:00" in text_str
        assert "Lunch:" in text_str
        assert "30m" in text_str
        assert "Out:" in text_str
        assert "17:00" in text_str
        assert "Comment:" in text_str
        assert "Working on project" in text_str

    def test_update_display_empty_entry(self):
        """Test display with no time entry data."""
        entry = DayTimeEntry()
        entry.update = MagicMock()

        entry.update_display(
            clock_in="-",
            lunch="-",
            clock_out="-",
            adjustment="-",
            adjust_type="",
            comment="",
        )

        entry.update.assert_called_once()
        call_args = entry.update.call_args[0][0]
        text_str = str(call_args)
        assert "In:" in text_str
        # Comment should not appear if empty
        assert "Comment:" not in text_str

    def test_update_display_adjustment_only(self):
        """Test display with adjustment only (leave/sick day)."""
        entry = DayTimeEntry()
        entry.update = MagicMock()

        entry.update_display(
            clock_in="-",
            lunch="-",
            clock_out="-",
            adjustment="7.5h",
            adjust_type="L",
            comment="Annual leave",
        )

        entry.update.assert_called_once()
        call_args = entry.update.call_args[0][0]
        text_str = str(call_args)
        assert "Adj:" in text_str
        assert "7.5h" in text_str
        assert "Type:" in text_str
        assert "L" in text_str
        assert "Annual leave" in text_str
