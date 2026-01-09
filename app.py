#!/usr/bin/env python3
"""Timesheet TUI application."""

from __future__ import annotations

from datetime import date, time, timedelta
from decimal import Decimal
from calendar import monthrange

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, Grid
from textual.widgets import Static, Footer, DataTable, Input, Button, Label, Select
from textual.screen import ModalScreen
from textual.coordinate import Coordinate
from rich.text import Text

import storage
from models import TimeEntry


def get_week_start(d: date) -> date:
    """Get the Saturday that starts the week containing date d."""
    # Saturday = 5 in weekday()
    days_since_saturday = (d.weekday() + 2) % 7
    return d - timedelta(days=days_since_saturday)


def get_weeks_in_month(year: int, month: int) -> list[tuple[date, date]]:
    """Get list of (week_start, week_end) tuples that overlap with the month."""
    first_day = date(year, month, 1)
    last_day = date(year, month, monthrange(year, month)[1])

    weeks = []
    week_start = get_week_start(first_day)

    while week_start <= last_day:
        week_end = week_start + timedelta(days=6)
        weeks.append((week_start, week_end))
        week_start = week_start + timedelta(days=7)

    return weeks


ADJUST_TYPES = [
    ("", "None"),
    ("P", "P - Public Holiday"),
    ("L", "L - Leave"),
    ("S", "S - Sick"),
    ("T", "T - Training"),
]


class ConfirmScreen(ModalScreen[bool]):
    """Simple confirmation dialog."""

    CSS = """
    ConfirmScreen {
        align: center middle;
    }

    #confirm-dialog {
        width: 50;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: thick $warning;
    }

    #confirm-buttons {
        width: 100%;
        height: auto;
        margin-top: 1;
    }

    #confirm-buttons Button {
        width: 1fr;
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("y", "confirm", "Yes"),
        Binding("n", "cancel", "No"),
    ]

    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-dialog"):
            yield Label(self.message)
            with Horizontal(id="confirm-buttons"):
                yield Button("Yes (Y)", variant="warning", id="yes")
                yield Button("No (N)", variant="default", id="no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class EditDayScreen(ModalScreen[TimeEntry | None]):
    """Modal screen for editing a day's time entry."""

    CSS = """
    EditDayScreen {
        align: center middle;
    }

    #edit-dialog {
        width: 50;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
    }

    #edit-dialog Label {
        width: 100%;
        margin-bottom: 1;
    }

    #edit-dialog Input {
        width: 100%;
        margin-bottom: 1;
    }

    #edit-dialog Select {
        width: 100%;
        margin-bottom: 1;
    }

    #edit-buttons {
        width: 100%;
        height: auto;
        margin-top: 1;
    }

    #edit-buttons Button {
        width: 1fr;
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, entry: TimeEntry):
        super().__init__()
        self.entry = entry

    def compose(self) -> ComposeResult:
        with Vertical(id="edit-dialog"):
            yield Label(f"Edit {self.entry.day_of_week} {self.entry.date.strftime('%b %d, %Y')}")

            yield Label("Clock In (HH:MM):")
            yield Input(
                value=self.entry.clock_in.strftime("%H:%M") if self.entry.clock_in else "",
                placeholder="09:00",
                id="clock-in"
            )

            yield Label("Lunch (minutes):")
            yield Input(
                value=str(int(self.entry.lunch_duration.total_seconds() // 60)) if self.entry.lunch_duration else "",
                placeholder="30",
                id="lunch"
            )

            yield Label("Clock Out (HH:MM):")
            yield Input(
                value=self.entry.clock_out.strftime("%H:%M") if self.entry.clock_out else "",
                placeholder="17:30",
                id="clock-out"
            )

            yield Label("Adjustment (hours):")
            yield Input(
                value=str(self.entry.adjusted_hours) if self.entry.adjustment else "",
                placeholder="0",
                id="adjustment"
            )

            yield Label("Adjust Type:")
            # Only use adjust_type if it's a valid option, otherwise default to ""
            valid_types = [t[0] for t in ADJUST_TYPES]
            current_type = self.entry.adjust_type if self.entry.adjust_type in valid_types else ""
            yield Select(
                options=[(label, value) for value, label in ADJUST_TYPES],
                value=current_type,
                id="adjust-type"
            )

            yield Label("Comment:")
            yield Input(
                value=self.entry.comment or "",
                placeholder="",
                id="comment"
            )

            with Horizontal(id="edit-buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", variant="default", id="cancel")

    def _parse_time(self, val: str) -> time | None:
        """Parse HH:MM to time object."""
        val = val.strip()
        if not val:
            return None
        try:
            parts = val.split(":")
            return time(int(parts[0]), int(parts[1]))
        except (ValueError, IndexError):
            return None

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "save":
            self._save_entry()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _save_entry(self) -> None:
        clock_in = self._parse_time(self.query_one("#clock-in", Input).value)
        clock_out = self._parse_time(self.query_one("#clock-out", Input).value)

        lunch_val = self.query_one("#lunch", Input).value.strip()
        lunch = timedelta(minutes=int(lunch_val)) if lunch_val else None

        adj_val = self.query_one("#adjustment", Input).value.strip()
        adjustment = timedelta(hours=float(adj_val)) if adj_val else None

        adjust_type_val = self.query_one("#adjust-type", Select).value
        adjust_type = adjust_type_val if adjust_type_val else None
        comment = self.query_one("#comment", Input).value.strip() or None

        # Validate: adjustment requires a type
        if adjustment and not adjust_type:
            self.app.notify("Adjustment hours require an adjust type", severity="error")
            return

        updated = TimeEntry(
            date=self.entry.date,
            day_of_week=self.entry.day_of_week,
            clock_in=clock_in,
            lunch_duration=lunch,
            clock_out=clock_out,
            adjustment=adjustment,
            adjust_type=adjust_type,
            comment=comment,
        )
        self.dismiss(updated)


class CombinedHeader(Static):
    """Shows month name on left and week navigation on right."""

    def __init__(self, year: int, month: int, **kwargs):
        super().__init__(**kwargs)
        self.year = year
        self.month = month

    def update_display(self, week_num: int, total_weeks: int, week_start: date, week_end: date):
        from rich.text import Text
        month_name = date(self.year, self.month, 1).strftime("%B %Y")
        start_str = week_start.strftime("%b %d")
        end_str = week_end.strftime("%b %d")
        week_nav = f"◄ Week {week_num}/{total_weeks} ({start_str} - {end_str}) ►"

        # Create a text with left and right justified content
        text = Text()
        text.append(f"  {month_name}")
        text.append(" " * 20)  # Some padding
        text.append(week_nav)
        text.align("left", 0, len(text))

        # For proper right justification, use explicit spacing
        # Calculate terminal width - we'll use a simple approach with fixed spacing
        line = f"  {month_name}" + " " * (80 - len(month_name) - len(week_nav) - 4) + week_nav
        self.update(line)


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

        summary = []
        summary.append(f"                                             Worked  {float(worked):>6.2g}h      ({worked_days:>5.2f}d)")
        summary.append(f"                                    of max possible  {float(max_hours):>6.2g}h      ({max_days:>5.2f}d)")
        summary.append(f"                                              Leave  {float(leave):>6.2g}h      ({leave_days:>5.2f}d)")
        summary.append(f"                                               Sick  {float(sick):>6.2g}h      ({sick_days:>5.2f}d)")
        summary.append(f"                                           Training  {float(training):>6.2g}h      ({training_days:>5.2f}d)")
        summary.append(f"                                                P/H  {float(public_holiday):>6.2g}h      ({ph_days:>5.2f}d)")
        summary.append(f"                                              TOTAL  {float(total):>6.2g}h      ({total_days:>5.2f}d)")

        self.update("\n".join(summary))


class TimesheetApp(App):
    """Main timesheet application."""

    CSS = """
    Screen {
        background: $surface;
    }

    #combined-header {
        height: 1;
        background: $primary;
        color: $text;
        padding: 1;
        text-style: bold;
    }

    #weekly-summary {
        height: auto;
        padding: 1 2;
        color: $text;
    }

    #week-table {
        height: 1fr;
        margin: 1 2;
    }

    DataTable {
        height: 100%;
    }

    DataTable > .datatable--cursor {
        background: $secondary;
        color: $text;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("left", "prev_week", "◄ Week"),
        Binding("right", "next_week", "Week ►"),
        Binding("up", "cursor_up", "▲", show=False),
        Binding("down", "cursor_down", "▼", show=False),
        Binding("m", "select_month", "Month"),
        Binding("t", "goto_today", "Today"),
        Binding("$", "toggle_money", "£"),
        Binding("e", "edit_day", "Edit"),
        Binding("h", "populate_holidays", "Holidays"),
        Binding("L", "quick_leave", "Leave", show=False),
        Binding("S", "quick_sick", "Sick", show=False),
        Binding("T", "quick_training", "Training", show=False),
    ]

    def __init__(self):
        super().__init__()
        storage.init_db()

        # Start with current month and week
        today = date.today()
        self.current_year = today.year
        self.current_month = today.month
        self.weeks = get_weeks_in_month(self.current_year, self.current_month)
        self.current_week_idx = self._find_week_for_date(today)
        self.entries: dict[date, TimeEntry] = {}

    def _find_week_for_date(self, d: date) -> int:
        """Find which week index contains the given date."""
        for i, (start, end) in enumerate(self.weeks):
            if start <= d <= end:
                return i
        return 0

    def _get_week_month(self, week_start: date, week_end: date) -> tuple[int, int]:
        """Determine which month a week belongs to based on weekday majority.

        Returns (year, month) of the month that contains the majority of weekdays.
        """
        weekday_months: dict[tuple[int, int], int] = {}

        current = week_start
        while current <= week_end:
            if current.weekday() < 5:  # Mon-Fri
                key = (current.year, current.month)
                weekday_months[key] = weekday_months.get(key, 0) + 1
            current += timedelta(days=1)

        if not weekday_months:
            return (week_start.year, week_start.month)

        return max(weekday_months.keys(), key=lambda k: weekday_months[k])

    def _sync_month_to_week(self):
        """Update current month if the current week belongs to a different month."""
        week_start, week_end = self.weeks[self.current_week_idx]
        year, month = self._get_week_month(week_start, week_end)

        if year != self.current_year or month != self.current_month:
            self.current_year = year
            self.current_month = month

            # Regenerate weeks for new month and find current week in it
            self.weeks = get_weeks_in_month(self.current_year, self.current_month)
            self.current_week_idx = self._find_week_for_date(week_start)

            # Reload month data
            self._load_month_data()

            # Update header
            header = self.query_one("#combined-header", CombinedHeader)
            header.year = self.current_year
            header.month = self.current_month

    def compose(self) -> ComposeResult:
        yield CombinedHeader(self.current_year, self.current_month, id="combined-header")
        yield WeeklySummary(id="weekly-summary")
        yield Container(DataTable(id="week-table"), id="table-container")
        yield Footer()

    def on_mount(self):
        self._setup_table()
        self._load_month_data()
        # Ensure header matches initial state
        header = self.query_one("#combined-header", CombinedHeader)
        header.year = self.current_year
        header.month = self.current_month
        self._refresh_display()
        self._select_date(date.today())

    def _setup_table(self):
        table = self.query_one("#week-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("Day", "Date", "In", "Lunch", "Out", "Worked", "Adj", "Type", "Comment")

    def _load_month_data(self):
        """Load all entries for current month into memory."""
        entries = storage.get_month_entries(self.current_year, self.current_month)
        self.entries = {e.date: e for e in entries}

    def _get_or_create_entry(self, d: date) -> TimeEntry:
        """Get entry for date or create empty one."""
        if d in self.entries:
            return self.entries[d]

        return TimeEntry(
            date=d,
            day_of_week=d.strftime("%a"),
        )

    def _count_weekdays(self, start: date, end: date) -> int:
        """Count weekdays (Mon-Fri) in a date range."""
        count = 0
        current = start
        while current <= end:
            if current.weekday() < 5:  # Mon=0 to Fri=4
                count += 1
            current += timedelta(days=1)
        return count

    def _refresh_display(self):
        # Update combined header
        combined_header = self.query_one("#combined-header", CombinedHeader)
        config = storage.get_config()

        week_start, week_end = self.weeks[self.current_week_idx]

        combined_header.update_display(
            self.current_week_idx + 1,
            len(self.weeks),
            week_start,
            week_end
        )

        # Calculate week totals and breakdown by type
        week_worked = Decimal("0")
        week_leave = Decimal("0")
        week_sick = Decimal("0")
        week_training = Decimal("0")
        week_public_holiday = Decimal("0")

        for i in range(7):
            d = week_start + timedelta(days=i)
            entry = self._get_or_create_entry(d)
            week_worked += entry.worked_hours

            # Categorize adjustments by type
            if entry.adjusted_hours:
                if entry.adjust_type == "L":
                    week_leave += entry.adjusted_hours
                elif entry.adjust_type == "S":
                    week_sick += entry.adjusted_hours
                elif entry.adjust_type == "T":
                    week_training += entry.adjusted_hours
                elif entry.adjust_type == "P":
                    week_public_holiday += entry.adjusted_hours

        # Calculate max week hours: weekdays × daily hours - public holiday adjustments
        week_weekdays = self._count_weekdays(week_start, week_end)
        week_max_hours = (Decimal(week_weekdays) * config.standard_day_hours) - week_public_holiday

        # Update weekly summary
        weekly_summary = self.query_one("#weekly-summary", WeeklySummary)
        weekly_summary.update_display(
            week_worked,
            week_max_hours,
            week_leave,
            week_sick,
            week_training,
            week_public_holiday,
            config
        )

        # Update table
        table = self.query_one("#week-table", DataTable)
        table.clear()

        for i in range(7):
            d = week_start + timedelta(days=i)
            entry = self._get_or_create_entry(d)

            in_str = entry.clock_in.strftime("%H:%M") if entry.clock_in else "-"
            lunch_str = f"{int(entry.lunch_duration.total_seconds() // 60):02d}m" if entry.lunch_duration else "-"
            out_str = entry.clock_out.strftime("%H:%M") if entry.clock_out else "-"
            worked_str = f"{float(entry.worked_hours):g}h" if entry.worked_hours else "-"
            adj_str = f"{float(entry.adjusted_hours):g}h" if entry.adjusted_hours else "-"
            type_str = entry.adjust_type or ""
            comment_str = (entry.comment[:20] + "...") if entry.comment and len(entry.comment) > 20 else (entry.comment or "")

            # Highlight if this day is in the current billing month
            date_str = d.strftime("%b %d")
            if d.month != self.current_month:
                date_str = f"({date_str})"

            # Dim weekend rows
            is_weekend = entry.day_of_week in ("Sat", "Sun")
            style = "dim" if is_weekend else ""

            table.add_row(
                Text(entry.day_of_week, style=style),
                Text(date_str, style=style),
                Text(in_str, style=style),
                Text(lunch_str, style=style),
                Text(out_str, style=style),
                Text(worked_str, style=style),
                Text(adj_str, style=style),
                Text(type_str, style=style),
                Text(comment_str, style=style),
                key=d.isoformat(),
            )

    def action_prev_week(self):
        if self.current_week_idx > 0:
            self.current_week_idx -= 1
            self._refresh_display()
        else:
            # Go to previous month - don't sync, honor user's explicit navigation
            if self.current_month == 1:
                self.current_year -= 1
                self.current_month = 12
            else:
                self.current_month -= 1

            self.weeks = get_weeks_in_month(self.current_year, self.current_month)
            self.current_week_idx = len(self.weeks) - 1
            self._load_month_data()
            header = self.query_one("#combined-header", CombinedHeader)
            header.year = self.current_year
            header.month = self.current_month
            self._refresh_display()

    def action_next_week(self):
        if self.current_week_idx < len(self.weeks) - 1:
            self.current_week_idx += 1
            self._refresh_display()
        else:
            # Go to next month - don't sync, honor user's explicit navigation
            if self.current_month == 12:
                self.current_year += 1
                self.current_month = 1
            else:
                self.current_month += 1

            self.weeks = get_weeks_in_month(self.current_year, self.current_month)
            self.current_week_idx = 0
            self._load_month_data()
            header = self.query_one("#combined-header", CombinedHeader)
            header.year = self.current_year
            header.month = self.current_month
            self._refresh_display()

    def action_cursor_up(self):
        table = self.query_one("#week-table", DataTable)
        table.action_cursor_up()

    def action_cursor_down(self):
        table = self.query_one("#week-table", DataTable)
        table.action_cursor_down()

    def action_toggle_money(self):
        # TODO: Implement earnings display toggle for new layout
        pass

    def action_goto_today(self):
        today = date.today()
        self.current_year = today.year
        self.current_month = today.month
        self.weeks = get_weeks_in_month(self.current_year, self.current_month)
        self.current_week_idx = self._find_week_for_date(today)
        self._load_month_data()
        self._refresh_display()
        self._select_date(today)

        header = self.query_one("#combined-header", CombinedHeader)
        header.year = self.current_year
        header.month = self.current_month

    def _select_date(self, target: date):
        """Move cursor to the row for a specific date."""
        table = self.query_one("#week-table", DataTable)
        week_start, _ = self.weeks[self.current_week_idx]

        # Find which row (0-6) corresponds to the target date
        for row_idx in range(7):
            row_date = week_start + timedelta(days=row_idx)
            if row_date == target:
                table.move_cursor(row=row_idx)
                break

    def action_select_month(self):
        # TODO: implement month picker modal
        pass

    def action_populate_holidays(self):
        """Pre-populate UK bank holidays for the current month."""
        config = storage.get_config()
        count = storage.populate_holidays(
            self.current_year,
            self.current_month,
            config.standard_day_hours
        )
        # Reload data and refresh
        self._load_month_data()
        self._refresh_display()
        self.notify(f"Added {count} holiday entries" if count else "No new holidays to add")

    def action_edit_day(self):
        """Open edit modal for selected day."""
        table = self.query_one("#week-table", DataTable)
        row_key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key

        if row_key:
            selected_date = date.fromisoformat(str(row_key.value))
            entry = self._get_or_create_entry(selected_date)
            self.push_screen(EditDayScreen(entry), self._on_edit_complete)

    def _on_edit_complete(self, result: TimeEntry | None) -> None:
        """Handle result from edit modal."""
        if result:
            storage.save_entry(result)
            self.entries[result.date] = result
            self._refresh_display()

    def _get_selected_date(self) -> date | None:
        """Get the currently selected date from the table."""
        table = self.query_one("#week-table", DataTable)
        row_key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key
        if row_key:
            return date.fromisoformat(str(row_key.value))
        return None

    def _entry_is_blank(self, entry: TimeEntry) -> bool:
        """Check if an entry has no data."""
        return (
            entry.clock_in is None
            and entry.clock_out is None
            and entry.adjustment is None
        )

    def _apply_quick_adjust(self, adjust_type: str, type_name: str) -> None:
        """Apply a quick adjustment to the selected day."""
        selected_date = self._get_selected_date()
        if not selected_date:
            return

        entry = self._get_or_create_entry(selected_date)
        config = storage.get_config()

        def do_apply(confirmed: bool = True) -> None:
            if not confirmed:
                return
            new_entry = TimeEntry(
                date=entry.date,
                day_of_week=entry.day_of_week,
                clock_in=None,
                lunch_duration=None,
                clock_out=None,
                adjustment=timedelta(hours=float(config.standard_day_hours)),
                adjust_type=adjust_type,
                comment=None,
            )
            storage.save_entry(new_entry)
            self.entries[new_entry.date] = new_entry

            # Remember cursor position and move to next row if possible
            table = self.query_one("#week-table", DataTable)
            current_row = table.cursor_row

            self._refresh_display()

            # Move to next row, or stay if at the end
            if current_row < 6:
                table.move_cursor(row=current_row + 1)
            else:
                table.move_cursor(row=current_row)

            self.notify(f"{type_name} recorded for {entry.date.strftime('%b %d')}")

        if self._entry_is_blank(entry):
            do_apply()
        else:
            self.push_screen(
                ConfirmScreen(f"Overwrite existing entry for {entry.date.strftime('%b %d')}?"),
                do_apply
            )

    def action_quick_leave(self) -> None:
        """Quick add leave day."""
        self._apply_quick_adjust("L", "Leave")

    def action_quick_sick(self) -> None:
        """Quick add sick day."""
        self._apply_quick_adjust("S", "Sick")

    def action_quick_training(self) -> None:
        """Quick add training day."""
        self._apply_quick_adjust("T", "Training")

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle Enter key on table row."""
        if event.row_key:
            selected_date = date.fromisoformat(str(event.row_key.value))
            entry = self._get_or_create_entry(selected_date)
            self.push_screen(EditDayScreen(entry), self._on_edit_complete)


def main():
    app = TimesheetApp()
    app.run()


if __name__ == "__main__":
    main()
