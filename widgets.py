"""Custom widgets for the timesheet application."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from textual.widgets import Static
from rich.text import Text


class CombinedHeader(Static):
    """Shows month name on left and week navigation on right."""

    def __init__(self, year: int, month: int, **kwargs):
        super().__init__(**kwargs)
        self.year = year
        self.month = month
        self.week_nav_start = 0
        self.left_arrow_pos = 0
        self.right_arrow_pos = 0

    def update_display(self, week_num: int, total_weeks: int, week_start: date, week_end: date):
        month_name = date(self.year, self.month, 1).strftime("%B %Y")
        title = f"WEEK {week_num}: {month_name}"
        start_str = week_start.strftime("%b %d")
        end_str = week_end.strftime("%b %d")
        week_nav = f"◄ {week_num}/{total_weeks} ({start_str} - {end_str}) ►"

        # Align week navigation to end at column 74 (matching summary right edge)
        # Summary format: 45 spaces + label + hours(6) + "h      (" + days(5) + "d)" = ~74 chars
        target_end_col = 74
        week_nav_start = target_end_col - len(week_nav)

        # Store positions for click detection
        self.week_nav_start = week_nav_start
        self.left_arrow_pos = week_nav_start  # Position of ◄
        self.right_arrow_pos = week_nav_start + len(week_nav) - 1  # Position of ►

        # Create a Text with left and right justified content
        text = Text()
        text.append(title, style="bold")

        # Calculate spacing to position week_nav at the right spot
        spacing = week_nav_start - len(title)
        if spacing > 0:
            text.append(" " * spacing)
        else:
            text.append("  ")  # Minimum spacing

        text.append(week_nav, style="bold")

        self.update(text)

    def on_click(self, event) -> None:
        """Handle clicks on the arrows for week navigation."""
        # Get click position (column)
        click_col = event.x

        # Check if click is on left arrow (◄)
        if self.left_arrow_pos <= click_col < self.left_arrow_pos + 2:
            self.app.action_prev_week()  # type: ignore[attr-defined]
        # Check if click is on right arrow (►)
        elif self.right_arrow_pos <= click_col < self.right_arrow_pos + 2:
            self.app.action_next_week()  # type: ignore[attr-defined]


class WeeklySummary(Static):
    """Shows weekly hours breakdown by type."""

    def update_display(self, worked: Decimal, max_hours: Decimal, leave: Decimal, sick: Decimal, training: Decimal, public_holiday: Decimal, config):
        total = worked + leave + sick + training + public_holiday

        # Convert to days (assuming standard_day_hours)
        std_day = float(config.standard_day_hours)
        worked_days = float(worked) / std_day if worked else 0
        max_days = float(max_hours) / std_day if max_hours else 0
        leave_days = float(leave) / std_day if leave else 0
        sick_days = float(sick) / std_day if sick else 0
        training_days = float(training) / std_day if training else 0
        ph_days = float(public_holiday) / std_day if public_holiday else 0
        total_days = float(total) / std_day if total else 0

        # Create Text with conditional dimming for zero values
        text = Text()

        # Worked and target max are never dimmed
        text.append(f"                                             Worked  {float(worked):>6g}h      ({round(worked_days, 2):>5g}d)\n")
        pct = (float(worked) / float(max_hours) * 100) if max_hours else 0
        text.append(f"                                      of target max  {float(max_hours):>6g}h      ({round(max_days, 2):>5g}d)   ({pct:.1f}%)\n")

        # Leave - dim if zero
        leave_line = f"                                              Leave  {float(leave):>6g}h      ({round(leave_days, 2):>5g}d)\n"
        text.append(leave_line, style="dim" if leave == 0 else "")

        # Sick - dim if zero
        sick_line = f"                                               Sick  {float(sick):>6g}h      ({round(sick_days, 2):>5g}d)\n"
        text.append(sick_line, style="dim" if sick == 0 else "")

        # Training - dim if zero
        training_line = f"                                           Training  {float(training):>6g}h      ({round(training_days, 2):>5g}d)\n"
        text.append(training_line, style="dim" if training == 0 else "")

        # P/H - dim if zero
        ph_line = f"                                                P/H  {float(public_holiday):>6g}h      ({round(ph_days, 2):>5g}d)\n"
        text.append(ph_line, style="dim" if public_holiday == 0 else "")

        # TOTAL is never dimmed
        text.append(f"                                              TOTAL  {float(total):>6g}h      ({round(total_days, 2):>5g}d)")

        self.update(text)
