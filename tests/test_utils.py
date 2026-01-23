"""Tests for utils.py - week calculation utilities."""

from datetime import date

from utils import get_week_start, get_weeks_in_month, ADJUST_TYPES


class TestGetWeekStart:
    """Tests for get_week_start function."""

    def test_saturday_returns_same_day(self):
        """Saturday should return itself as week start."""
        sat = date(2026, 1, 10)  # A Saturday
        assert sat.weekday() == 5  # Verify it's Saturday
        assert get_week_start(sat) == sat

    def test_sunday_returns_previous_saturday(self):
        """Sunday should return the previous Saturday."""
        sun = date(2026, 1, 11)  # A Sunday
        assert sun.weekday() == 6  # Verify it's Sunday
        assert get_week_start(sun) == date(2026, 1, 10)

    def test_monday_returns_previous_saturday(self):
        """Monday should return the previous Saturday."""
        mon = date(2026, 1, 12)  # A Monday
        assert mon.weekday() == 0  # Verify it's Monday
        assert get_week_start(mon) == date(2026, 1, 10)

    def test_friday_returns_previous_saturday(self):
        """Friday should return the previous Saturday."""
        fri = date(2026, 1, 16)  # A Friday
        assert fri.weekday() == 4  # Verify it's Friday
        assert get_week_start(fri) == date(2026, 1, 10)

    def test_wednesday_returns_previous_saturday(self):
        """Wednesday should return the previous Saturday."""
        wed = date(2026, 1, 14)  # A Wednesday
        assert wed.weekday() == 2  # Verify it's Wednesday
        assert get_week_start(wed) == date(2026, 1, 10)

    def test_week_start_across_month_boundary(self):
        """Test week start when it crosses a month boundary."""
        # January 1, 2026 is a Thursday
        jan1 = date(2026, 1, 1)
        assert jan1.weekday() == 3  # Verify it's Thursday
        # Week start should be December 27, 2025 (Saturday)
        assert get_week_start(jan1) == date(2025, 12, 27)

    def test_week_start_across_year_boundary(self):
        """Test week start when it crosses a year boundary."""
        # December 31, 2025 is a Wednesday
        dec31 = date(2025, 12, 31)
        assert dec31.weekday() == 2  # Verify it's Wednesday
        # Week start should be December 27, 2025 (Saturday)
        assert get_week_start(dec31) == date(2025, 12, 27)


class TestGetWeeksInMonth:
    """Tests for get_weeks_in_month function."""

    def test_january_2026(self):
        """Test weeks in January 2026."""
        weeks = get_weeks_in_month(2026, 1)

        # January 2026: 1st is Thursday, 31st is Saturday
        # Should have 6 weeks (Dec 27 - Jan 2, Jan 3-9, Jan 10-16, Jan 17-23, Jan 24-30, Jan 31 - Feb 6)
        assert len(weeks) == 6

        # First week should start Dec 27, 2025
        assert weeks[0][0] == date(2025, 12, 27)
        assert weeks[0][1] == date(2026, 1, 2)

        # Last week should start Jan 31
        assert weeks[5][0] == date(2026, 1, 31)
        assert weeks[5][1] == date(2026, 2, 6)

    def test_february_2026(self):
        """Test weeks in February 2026 (non-leap year)."""
        weeks = get_weeks_in_month(2026, 2)

        # February 2026: 1st is Sunday, 28th is Saturday
        # First week starts Jan 31
        assert weeks[0][0] == date(2026, 1, 31)

        # Last week should end on Feb 28 or cover it
        last_week_start = weeks[-1][0]
        last_week_end = weeks[-1][1]
        assert last_week_start <= date(2026, 2, 28) <= last_week_end

    def test_february_2024_leap_year(self):
        """Test weeks in February 2024 (leap year)."""
        weeks = get_weeks_in_month(2024, 2)

        # February 2024 has 29 days
        # Check that Feb 29 is covered
        last_week_start = weeks[-1][0]
        last_week_end = weeks[-1][1]
        assert last_week_start <= date(2024, 2, 29) <= last_week_end

    def test_week_tuples_are_seven_days(self):
        """Each week tuple should span exactly 7 days."""
        weeks = get_weeks_in_month(2026, 1)
        for start, end in weeks:
            assert (end - start).days == 6  # Start to end is 6 days (7 days total)

    def test_weeks_are_contiguous(self):
        """Weeks should be contiguous (no gaps)."""
        weeks = get_weeks_in_month(2026, 1)
        for i in range(len(weeks) - 1):
            current_end = weeks[i][1]
            next_start = weeks[i + 1][0]
            assert (next_start - current_end).days == 1

    def test_all_days_of_month_covered(self):
        """All days of the month should be covered by some week."""
        weeks = get_weeks_in_month(2026, 3)  # March 2026

        # Check that every day in March is in some week
        for day in range(1, 32):  # March has 31 days
            d = date(2026, 3, day)
            covered = False
            for start, end in weeks:
                if start <= d <= end:
                    covered = True
                    break
            assert covered, f"Day {d} not covered by any week"


class TestAdjustTypes:
    """Tests for ADJUST_TYPES constant."""

    def test_adjust_types_structure(self):
        """Test that ADJUST_TYPES has expected structure."""
        assert isinstance(ADJUST_TYPES, list)
        assert len(ADJUST_TYPES) == 5

    def test_adjust_types_codes(self):
        """Test that all expected codes are present."""
        codes = [t[0] for t in ADJUST_TYPES]
        assert "" in codes  # None/empty
        assert "P" in codes  # Public Holiday
        assert "L" in codes  # Leave
        assert "S" in codes  # Sick
        assert "T" in codes  # Training

    def test_adjust_types_labels(self):
        """Test that labels are descriptive."""
        for code, label in ADJUST_TYPES:
            assert isinstance(label, str)
            assert len(label) > 0
            if code:  # Non-empty codes should have descriptive labels
                assert code in label
