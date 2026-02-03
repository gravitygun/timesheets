"""Tests for the app module."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch



class TestAppUtilityMethods:
    """Tests for utility methods in TimesheetApp."""

    def test_find_week_for_date(self):
        """Test finding which week contains a date."""
        from app import TimesheetApp

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            # Set up known weeks
            app.weeks = [
                (date(2026, 1, 3), date(2026, 1, 9)),
                (date(2026, 1, 10), date(2026, 1, 16)),
                (date(2026, 1, 17), date(2026, 1, 23)),
            ]

            # Test finding week for date in first week
            assert app._find_week_for_date(date(2026, 1, 5)) == 0

            # Test finding week for date in second week
            assert app._find_week_for_date(date(2026, 1, 12)) == 1

            # Test finding week for date in third week
            assert app._find_week_for_date(date(2026, 1, 20)) == 2

    def test_find_week_for_date_not_found(self):
        """Test finding week for date not in any week returns 0."""
        from app import TimesheetApp

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()
            app.weeks = [
                (date(2026, 1, 3), date(2026, 1, 9)),
            ]

            # Date outside any week should return 0
            assert app._find_week_for_date(date(2026, 2, 1)) == 0

    def test_get_week_month_majority_in_first_month(self):
        """Test determining week's month when majority is in first month."""
        from app import TimesheetApp

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            # Week with Mon-Fri mostly in January
            # Sat Jan 31, Sun Feb 1, Mon Feb 2, Tue Feb 3, Wed Feb 4, Thu Feb 5, Fri Feb 6
            # Only 1 weekday in Jan (none - Jan 31 is Sat), 5 weekdays in Feb
            week_start = date(2026, 1, 31)
            week_end = date(2026, 2, 6)

            year, month = app._get_week_month(week_start, week_end)

            # Should be February (more weekdays there)
            assert month == 2

    def test_get_week_month_all_same_month(self):
        """Test determining week's month when all days in same month."""
        from app import TimesheetApp

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            # Week entirely in January
            week_start = date(2026, 1, 10)  # Saturday
            week_end = date(2026, 1, 16)  # Friday

            year, month = app._get_week_month(week_start, week_end)

            assert year == 2026
            assert month == 1

    def test_count_weekdays_full_week(self):
        """Test counting weekdays in a full week."""
        from app import TimesheetApp

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            # Monday to Friday
            start = date(2026, 1, 26)  # Monday
            end = date(2026, 1, 30)  # Friday

            count = app._count_weekdays(start, end)

            assert count == 5

    def test_count_weekdays_with_weekend(self):
        """Test counting weekdays including weekend."""
        from app import TimesheetApp

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            # Full week Sat-Fri
            start = date(2026, 1, 24)  # Saturday
            end = date(2026, 1, 30)  # Friday

            count = app._count_weekdays(start, end)

            assert count == 5  # Only Mon-Fri

    def test_count_weekdays_filter_month(self):
        """Test counting weekdays filtered by month."""
        from app import TimesheetApp

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            # Week spanning Jan-Feb
            start = date(2026, 1, 31)  # Saturday
            end = date(2026, 2, 6)  # Friday

            # Only count February days
            count = app._count_weekdays(start, end, filter_month=2)

            # Feb 2-6 = Mon-Fri = 5 weekdays
            assert count == 5

    def test_entry_is_blank_true(self):
        """Test entry_is_blank returns True for empty entry."""
        from app import TimesheetApp
        from models import TimeEntry

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            entry = TimeEntry(
                date=date(2026, 1, 27),
                day_of_week="Mon",
            )

            assert app._entry_is_blank(entry) is True

    def test_entry_is_blank_false_clock_in(self):
        """Test entry_is_blank returns False when clock_in set."""
        from app import TimesheetApp
        from datetime import time
        from models import TimeEntry

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            entry = TimeEntry(
                date=date(2026, 1, 27),
                day_of_week="Mon",
                clock_in=time(9, 0),
            )

            assert app._entry_is_blank(entry) is False

    def test_entry_is_blank_false_adjustment(self):
        """Test entry_is_blank returns False when adjustment set."""
        from app import TimesheetApp
        from models import TimeEntry

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            entry = TimeEntry(
                date=date(2026, 1, 27),
                day_of_week="Mon",
                adjustment=timedelta(hours=7.5),
                adjust_type="L",
            )

            assert app._entry_is_blank(entry) is False


class TestGetAllocationStatus:
    """Tests for _get_allocation_status method."""

    def test_no_worked_hours(self):
        """Test status when no worked hours."""
        from app import TimesheetApp

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            result = app._get_allocation_status(date(2026, 1, 27), Decimal("0"))

            assert str(result) == "-"

    def test_no_allocations(self):
        """Test status when worked but no allocations."""
        from app import TimesheetApp
        import storage

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            with patch.object(storage, 'get_total_allocated_hours', return_value=Decimal("0")):
                result = app._get_allocation_status(date(2026, 1, 27), Decimal("7.5"))

            assert str(result) == "?"

    def test_under_allocated(self):
        """Test status when under-allocated."""
        from app import TimesheetApp
        import storage

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            with patch.object(storage, 'get_total_allocated_hours', return_value=Decimal("5")):
                result = app._get_allocation_status(date(2026, 1, 27), Decimal("7.5"))

            assert "↓" in str(result)

    def test_over_allocated(self):
        """Test status when over-allocated."""
        from app import TimesheetApp
        import storage

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            with patch.object(storage, 'get_total_allocated_hours', return_value=Decimal("10")):
                result = app._get_allocation_status(date(2026, 1, 27), Decimal("7.5"))

            assert "↑" in str(result)

    def test_exact_allocation(self):
        """Test status when exactly allocated."""
        from app import TimesheetApp
        import storage

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            with patch.object(storage, 'get_total_allocated_hours', return_value=Decimal("7.5")):
                result = app._get_allocation_status(date(2026, 1, 27), Decimal("7.5"))

            assert "✓" in str(result)


class TestHasAllocationMismatch:
    """Tests for _has_allocation_mismatch method."""

    def test_no_entry(self):
        """Test mismatch check when no entry exists."""
        from app import TimesheetApp

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            result = app._has_allocation_mismatch(date(2026, 1, 27), {})

            assert result is False

    def test_no_worked_hours(self):
        """Test mismatch check when no worked hours."""
        from app import TimesheetApp
        from models import TimeEntry

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            entries_dict = {
                date(2026, 1, 27): TimeEntry(
                    date=date(2026, 1, 27),
                    day_of_week="Mon",
                )
            }

            result = app._has_allocation_mismatch(date(2026, 1, 27), entries_dict)

            assert result is False

    def test_matching_allocation(self):
        """Test mismatch check when allocation matches."""
        from app import TimesheetApp
        from datetime import time
        from models import TimeEntry
        import storage

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            entries_dict = {
                date(2026, 1, 27): TimeEntry(
                    date=date(2026, 1, 27),
                    day_of_week="Mon",
                    clock_in=time(9, 0),
                    lunch_duration=timedelta(minutes=30),
                    clock_out=time(17, 0),  # 7.5 hours
                )
            }

            with patch.object(storage, 'get_total_allocated_hours', return_value=Decimal("7.5")):
                result = app._has_allocation_mismatch(date(2026, 1, 27), entries_dict)

            assert result is False

    def test_mismatched_allocation(self):
        """Test mismatch check when allocation doesn't match."""
        from app import TimesheetApp
        from datetime import time
        from models import TimeEntry
        import storage

        with patch.object(TimesheetApp, 'run'):
            app = TimesheetApp()

            entries_dict = {
                date(2026, 1, 27): TimeEntry(
                    date=date(2026, 1, 27),
                    day_of_week="Mon",
                    clock_in=time(9, 0),
                    lunch_duration=timedelta(minutes=30),
                    clock_out=time(17, 0),  # 7.5 hours
                )
            }

            with patch.object(storage, 'get_total_allocated_hours', return_value=Decimal("5")):
                result = app._has_allocation_mismatch(date(2026, 1, 27), entries_dict)

            assert result is True
