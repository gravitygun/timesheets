#!/usr/bin/env python3
"""Timesheet TUI application."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.widgets import Static, Footer, DataTable
from textual.coordinate import Coordinate
from rich.text import Text

import storage
from models import Ticket, TicketAllocation, TimeEntry
from utils import get_weeks_in_month
from screens import (
    ConfirmScreen,
    EditAllocationScreen,
    EditDayScreen,
    TicketManagementScreen,
    TicketSelectScreen,
)
from widgets import CombinedHeader, DayHeader, DaySummary, DayTimeEntry, WeeklySummary


class TimesheetDataTable(DataTable):
    """Custom DataTable that delegates left/right keys to the app for navigation in week/month views."""

    def on_key(self, event) -> None:
        """Handle key events - intercept left/right in week/month views, 'c' in allocations."""
        if not hasattr(self.app, 'view_mode'):
            return  # Let default handling occur

        view_mode = self.app.view_mode  # type: ignore[attr-defined]

        if event.key == "left" and view_mode in ("week", "month"):
            if hasattr(self.app, 'action_prev_week'):
                self.app.action_prev_week()  # type: ignore[attr-defined]
            self.scroll_x = 0
            event.prevent_default()
            event.stop()
        elif event.key == "right" and view_mode in ("week", "month"):
            if hasattr(self.app, 'action_next_week'):
                self.app.action_next_week()  # type: ignore[attr-defined]
            self.scroll_x = 0
            event.prevent_default()
            event.stop()
        elif event.key == "c" and view_mode == "allocations":
            # Toggle entered state with 'c' key
            if hasattr(self.app, '_toggle_allocation_entered_state'):
                self.app._toggle_allocation_entered_state()  # type: ignore[attr-defined]
            event.prevent_default()
            event.stop()
        # For other keys/views, don't intercept - let DataTable handle normally


class TimesheetApp(App):
    """Main timesheet application."""

    CSS = """
    Screen {
        background: $surface;
    }

    #combined-header {
        height: auto;
        background: $primary;
        color: $text;
        padding: 0 1;
        text-style: bold;
    }

    #weekly-summary {
        height: auto;
        padding: 1 2;
        color: $text;
    }

    #week-earnings, #month-earnings, #year-earnings {
        height: auto;
        padding: 0 2;
        color: $text;
        text-style: bold;
    }

    #week-table {
        height: 1fr;
        margin: 1 2;
    }

    #year-header {
        height: auto;
        background: $primary;
        color: $text;
        padding: 0 1;
        text-style: bold;
    }

    #year-table {
        height: 1fr;
        margin: 1 2;
    }

    #year-summary {
        height: auto;
        padding: 1 2;
        color: $text;
    }

    #month-header {
        height: auto;
        background: $primary;
        color: $text;
        padding: 0 1;
        text-style: bold;
    }

    #month-table {
        height: 1fr;
        margin: 1 2;
    }

    #month-summary {
        height: auto;
        padding: 1 2;
        color: $text;
    }

    .hidden {
        display: none;
    }

    #day-header {
        height: auto;
        background: $primary;
        color: $text;
        padding: 0 1;
        text-style: bold;
    }

    #day-time-entry {
        height: auto;
        padding: 0 2;
        color: $text;
    }

    #day-summary {
        height: auto;
        padding: 1 2;
        color: $text;
    }

    DataTable {
        height: 100%;
    }

    DataTable > .datatable--cursor {
        background: $secondary;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        # left/right handled in on_key to allow Input cursor movement in modals
        Binding("up", "cursor_up", "▲", show=False),
        Binding("down", "cursor_down", "▼", show=False),
        Binding("w", "week_view", "Week"),
        Binding("m", "month_view", "Month"),
        Binding("y", "year_view", "Year"),
        Binding("t", "goto_today", "Today"),
        Binding("$", "toggle_money", "£"),
        Binding("e", "edit_day", "Edit"),
        Binding("h", "populate_holidays", "Holidays"),
        Binding("L", "quick_leave", "Leave", show=False),
        Binding("S", "quick_sick", "Sick", show=False),
        Binding("T", "quick_training", "Training", show=False),
        Binding("ctrl+x", "cut_day", "Cut", show=False),
        Binding("ctrl+c", "copy_day", "Copy", show=False),
        Binding("ctrl+v", "paste_day", "Paste", show=False),
        Binding("question_mark", "toggle_help", "?", show=False),
        Binding("K", "manage_tickets", "Tickets"),
        # Day view bindings (shown when in day view via check_action)
        Binding("a", "add_allocation", "Add"),
        Binding("d", "delete_allocation", "Delete"),
        Binding("c", "toggle_entered", "Entered"),
        Binding("escape", "back_to_week", "Back"),
        # Allocations report
        Binding("M", "allocations_view", "Allocs"),
    ]

    def __init__(self):
        super().__init__()
        storage.init_db()

        # View mode: "week", "month", "year", or "day"
        self.view_mode = "week"

        # Start with current month and week
        today = date.today()
        self.current_year = today.year
        self.current_month = today.month
        self.weeks = get_weeks_in_month(self.current_year, self.current_month)
        self.current_week_idx = self._find_week_for_date(today)
        self.entries: dict[date, TimeEntry] = {}

        # Company year for year view (Sep-Aug)
        if today.month >= 9:
            self.company_year_start = today.year
        else:
            self.company_year_start = today.year - 1

        # Track last selected date in week view for restoring selection
        self.last_selected_date: date | None = None

        # Day view: currently selected date and its allocations
        self.day_view_date: date | None = None
        self.day_allocations: list[TicketAllocation] = []

        # Privacy mode: hide earnings by default
        self.show_money = False

        # Clipboard for cut/copy/paste
        self._day_clipboard: TimeEntry | None = None

        # Help panel state
        self._help_panel_visible = False

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
        # Week view widgets
        yield CombinedHeader(self.current_year, self.current_month, id="combined-header")
        yield Container(TimesheetDataTable(id="week-table"), id="week-table-container")
        yield Static(id="week-earnings", classes="hidden")
        yield WeeklySummary(id="weekly-summary")
        # Month view widgets (hidden by default)
        yield Static(id="month-header", classes="hidden")
        yield Container(TimesheetDataTable(id="month-table"), id="month-table-container", classes="hidden")
        yield Static(id="month-earnings", classes="hidden")
        yield Static(id="month-summary", classes="hidden")
        # Year view widgets (hidden by default)
        yield Static(id="year-header", classes="hidden")
        yield Container(TimesheetDataTable(id="year-table"), id="year-table-container", classes="hidden")
        yield Static(id="year-earnings", classes="hidden")
        yield Static(id="year-summary", classes="hidden")
        # Day view widgets (hidden by default)
        yield DayHeader(id="day-header", classes="hidden")
        yield DayTimeEntry(id="day-time-entry", classes="hidden")
        yield Container(TimesheetDataTable(id="day-table"), id="day-table-container", classes="hidden")
        yield DaySummary(id="day-summary", classes="hidden")
        # Allocations report widgets (hidden by default)
        yield Static(id="allocations-header", classes="hidden")
        yield Container(TimesheetDataTable(id="allocations-table"), id="allocations-table-container", classes="hidden")
        yield Static(id="allocations-summary", classes="hidden")
        yield Footer()

    def on_mount(self):
        self._setup_week_table()
        self._setup_month_table()
        self._setup_year_table()
        self._setup_day_table()
        self._setup_allocations_table()
        self._load_month_data()
        # Ensure header matches initial state
        header = self.query_one("#combined-header", CombinedHeader)
        header.year = self.current_year
        header.month = self.current_month
        # Set up initial binding visibility
        self._update_bindings_for_mode(self.view_mode)
        self._refresh_display()
        self._select_date(date.today())
        # Focus the week table
        self.query_one("#week-table", DataTable).focus()

    def _setup_week_table(self):
        table = self.query_one("#week-table", DataTable)
        table.cursor_type = "row"
        table.add_column("Day", width=4)
        table.add_column("Date", width=8)
        table.add_column("In", width=7)
        table.add_column("Lunch", width=7)
        table.add_column("Out", width=7)
        table.add_column("Worked", width=8)
        table.add_column("Alloc", width=5)
        table.add_column("Adj", width=7)
        table.add_column("Type", width=5)
        table.add_column("Comment", width=48)  # Slightly smaller to fit Alloc

    def _setup_month_table(self):
        table = self.query_one("#month-table", DataTable)
        table.cursor_type = "row"
        table.add_column("W/C Mon", width=12)
        table.add_column("Worked", width=8)
        table.add_column("Poss", width=8)
        table.add_column("L", width=6)
        table.add_column("S", width=6)
        table.add_column("T", width=6)
        table.add_column("P", width=6)
        table.add_column("Total", width=8)

    def _setup_year_table(self):
        table = self.query_one("#year-table", DataTable)
        table.cursor_type = "row"
        table.add_column("Month", width=12)
        table.add_column("Worked", width=8)
        table.add_column("Poss", width=8)
        table.add_column("L", width=6)
        table.add_column("S", width=6)
        table.add_column("T", width=6)
        table.add_column("P", width=6)
        table.add_column("Total", width=8)
        if self.show_money:
            table.add_column("Bill", width=10)
            table.add_column("+VAT", width=10)

    def _setup_day_table(self):
        """Set up the day allocations table."""
        table = self.query_one("#day-table", DataTable)
        table.cursor_type = "row"
        table.add_column("Ticket", width=10)
        table.add_column("Description", width=40)
        table.add_column("Hours", width=8)
        table.add_column("Entered", width=7)

    def _setup_allocations_table(self):
        """Set up the allocations report table (columns added dynamically)."""
        table = self.query_one("#allocations-table", DataTable)
        table.cursor_type = "cell"  # Cell mode for clicking individual allocations
        # Columns are added dynamically in _refresh_allocations_display

    def _rebuild_tables(self):
        """Rebuild table columns when show_money changes."""
        # Rebuild month table
        month_table = self.query_one("#month-table", DataTable)
        month_table.clear(columns=True)
        month_table.add_column("W/C Mon", width=12)
        month_table.add_column("Worked", width=8)
        month_table.add_column("Poss", width=8)
        month_table.add_column("L", width=6)
        month_table.add_column("S", width=6)
        month_table.add_column("T", width=6)
        month_table.add_column("P", width=6)
        month_table.add_column("Total", width=8)
        if self.show_money:
            month_table.add_column("Bill", width=10)
            month_table.add_column("+VAT", width=10)

        # Rebuild year table
        year_table = self.query_one("#year-table", DataTable)
        year_table.clear(columns=True)
        year_table.add_column("Month", width=12)
        year_table.add_column("Worked", width=8)
        year_table.add_column("Poss", width=8)
        year_table.add_column("L", width=6)
        year_table.add_column("S", width=6)
        year_table.add_column("T", width=6)
        year_table.add_column("P", width=6)
        year_table.add_column("Total", width=8)
        if self.show_money:
            year_table.add_column("Bill", width=10)
            year_table.add_column("+VAT", width=10)

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

    def _count_weekdays(self, start: date, end: date, filter_month: int | None = None) -> int:
        """Count weekdays (Mon-Fri) in a date range.

        Args:
            start: Start date
            end: End date
            filter_month: If provided, only count days in this month (1-12)
        """
        count = 0
        current = start
        while current <= end:
            if current.weekday() < 5:  # Mon=0 to Fri=4
                if filter_month is None or current.month == filter_month:
                    count += 1
            current += timedelta(days=1)
        return count

    def _refresh_display(self):
        if self.view_mode == "week":
            self._refresh_week_display()
        elif self.view_mode == "month":
            self._refresh_month_display()
        elif self.view_mode == "year":
            self._refresh_year_display()
        elif self.view_mode == "day":
            self._refresh_day_display()
        elif self.view_mode == "allocations":
            self._refresh_allocations_display()

    def _refresh_week_display(self):
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

        # Calculate week totals and breakdown by type (filtered to current month only)
        week_worked = Decimal("0")
        week_leave = Decimal("0")
        week_sick = Decimal("0")
        week_training = Decimal("0")
        week_public_holiday = Decimal("0")

        for i in range(7):
            d = week_start + timedelta(days=i)
            # Only include days from the current month in totals
            if d.month != self.current_month:
                continue
            entry = self._get_or_create_entry(d)
            week_worked += entry.worked_hours

            # Categorise adjustments by type
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
        # Only count weekdays in the current month
        week_weekdays = self._count_weekdays(week_start, week_end, filter_month=self.current_month)
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

        # Update earnings display
        week_earnings = self.query_one("#week-earnings", Static)
        if self.show_money:
            week_earnings.remove_class("hidden")
            # Billable = worked hours only (not leave/sick/training/P/H)
            billable_hours = week_worked
            billable_amount = billable_hours * config.hourly_rate
            vat_amount = billable_amount * config.vat_rate
            total_with_vat = billable_amount + vat_amount
            # Format amount to fixed width for alignment with summary below
            amount_str = f"£{float(billable_amount):,.2f}"
            vat_str = f"£{float(total_with_vat):,.2f}"
            text = Text()
            text.append(f"                                           Billable  {amount_str:>10}   ({vat_str} inc VAT)")
            week_earnings.update(text)
        else:
            week_earnings.add_class("hidden")

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
            comment_str = (entry.comment[:45] + "...") if entry.comment and len(entry.comment) > 45 else (entry.comment or "")

            # Calculate allocation status
            alloc_status = self._get_allocation_status(d, entry.worked_hours)

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
                alloc_status,
                Text(adj_str, style=style),
                Text(type_str, style=style),
                Text(comment_str, style=style),
                key=d.isoformat(),
            )

    def _get_week_totals(self, week_start: date, week_end: date, filter_month: int | None = None) -> dict:
        """Calculate totals for a week.

        Args:
            week_start: Start of week (Saturday)
            week_end: End of week (Friday)
            filter_month: If provided, only count days in this month (1-12)
        """
        config = storage.get_config()
        entries = storage.get_entries_range(week_start, week_end)
        entries_dict = {e.date: e for e in entries}

        worked = Decimal("0")
        leave = Decimal("0")
        sick = Decimal("0")
        training = Decimal("0")
        public_holiday = Decimal("0")

        # Count weekdays in week (optionally filtered by month)
        weekdays = 0
        current = week_start
        while current <= week_end:
            if current.weekday() < 5:  # Mon-Fri
                if filter_month is None or current.month == filter_month:
                    weekdays += 1
            current += timedelta(days=1)

        # Sum up entries (optionally filtered by month)
        for entry_date, entry in entries_dict.items():
            if filter_month is not None and entry_date.month != filter_month:
                continue
            worked += entry.worked_hours
            if entry.adjusted_hours:
                if entry.adjust_type == "L":
                    leave += entry.adjusted_hours
                elif entry.adjust_type == "S":
                    sick += entry.adjusted_hours
                elif entry.adjust_type == "T":
                    training += entry.adjusted_hours
                elif entry.adjust_type == "P":
                    public_holiday += entry.adjusted_hours

        max_hours = (Decimal(weekdays) * config.standard_day_hours) - public_holiday
        total = worked + leave + sick + training + public_holiday

        return {
            "worked": worked,
            "max_hours": max_hours,
            "leave": leave,
            "sick": sick,
            "training": training,
            "public_holiday": public_holiday,
            "total": total,
        }

    def _refresh_month_display(self):
        """Refresh the month view (weekly summaries)."""
        config = storage.get_config()

        # Update month header
        month_header = self.query_one("#month-header", Static)
        month_name = date(self.current_year, self.current_month, 1).strftime("%B %Y")
        month_header.update(Text(f"MONTH: {month_name}", style="bold"))

        # Build table data
        table = self.query_one("#month-table", DataTable)
        table.clear()

        # Month totals
        month_worked = Decimal("0")
        month_max = Decimal("0")
        month_leave = Decimal("0")
        month_sick = Decimal("0")
        month_training = Decimal("0")
        month_ph = Decimal("0")
        month_total = Decimal("0")

        for week_start, week_end in self.weeks:
            # Find Monday of this week (week commencing)
            # Weeks always start on Saturday, so Monday is 2 days later
            monday = week_start + timedelta(days=2)

            # Get week totals (filtered to only include days in current month)
            totals = self._get_week_totals(week_start, week_end, filter_month=self.current_month)
            wc_str = monday.strftime("%d %b")
            # Put in parentheses if Monday is from a different month
            if monday.month != self.current_month:
                wc_str = f"({wc_str})"

            # Check if this is a future week with no data
            today = date.today()
            is_future = week_start > today

            # Dim if future with no work
            if is_future and totals["worked"] == 0:
                style = "dim"
            else:
                style = ""

            row_data = [
                Text(wc_str, style=style),
                Text(f"{float(totals['worked']):g}h" if totals['worked'] else "-", style=style),
                Text(f"{float(totals['max_hours']):g}h" if totals['max_hours'] else "-", style=style),
                Text(f"{float(totals['leave']):g}h" if totals['leave'] else "-", style=style),
                Text(f"{float(totals['sick']):g}h" if totals['sick'] else "-", style=style),
                Text(f"{float(totals['training']):g}h" if totals['training'] else "-", style=style),
                Text(f"{float(totals['public_holiday']):g}h" if totals['public_holiday'] else "-", style=style),
                Text(f"{float(totals['total']):g}h" if totals['total'] else "-", style=style),
            ]

            if self.show_money:
                billable = totals['worked'] * config.hourly_rate
                with_vat = billable * (1 + config.vat_rate)
                row_data.append(Text(f"£{float(billable):,.0f}" if billable else "-", style=style))
                row_data.append(Text(f"£{float(with_vat):,.0f}" if with_vat else "-", style=style))

            table.add_row(*row_data, key=f"{week_start.isoformat()}")

            # Accumulate month totals
            month_worked += totals["worked"]
            month_max += totals["max_hours"]
            month_leave += totals["leave"]
            month_sick += totals["sick"]
            month_training += totals["training"]
            month_ph += totals["public_holiday"]
            month_total += totals["total"]

        # Update month summary
        month_summary = self.query_one("#month-summary", Static)
        text = Text()

        # Convert to days
        std_day = float(config.standard_day_hours)
        worked_days = float(month_worked) / std_day if month_worked else 0
        max_days = float(month_max) / std_day if month_max else 0
        leave_days = float(month_leave) / std_day if month_leave else 0
        sick_days = float(month_sick) / std_day if month_sick else 0
        training_days = float(month_training) / std_day if month_training else 0
        ph_days = float(month_ph) / std_day if month_ph else 0
        total_days = float(month_total) / std_day if month_total else 0

        text.append(f"                                             Worked  {float(month_worked):>6g}h      ({round(worked_days, 2):>5g}d)\n")
        pct = (float(month_worked) / float(month_max) * 100) if month_max else 0
        text.append(f"                                      of target max  {float(month_max):>6g}h      ({round(max_days, 2):>5g}d)   ({pct:.1f}%)\n")

        # Show "of target max to date" only for current month
        today = date.today()
        if self.current_year == today.year and self.current_month == today.month:
            month_start = date(self.current_year, self.current_month, 1)
            max_to_date = self._get_max_hours_to_date(month_start, today)
            max_to_date_days = float(max_to_date) / std_day if max_to_date else 0
            pct_to_date = (float(month_worked) / float(max_to_date) * 100) if max_to_date else 0
            text.append(f"                              of target max to date  {float(max_to_date):>6g}h      ({round(max_to_date_days, 2):>5g}d)   ({pct_to_date:.1f}%)\n")

        leave_line = f"                                              Leave  {float(month_leave):>6g}h      ({round(leave_days, 2):>5g}d)\n"
        text.append(leave_line, style="dim" if month_leave == 0 else "")

        sick_line = f"                                               Sick  {float(month_sick):>6g}h      ({round(sick_days, 2):>5g}d)\n"
        text.append(sick_line, style="dim" if month_sick == 0 else "")

        training_line = f"                                           Training  {float(month_training):>6g}h      ({round(training_days, 2):>5g}d)\n"
        text.append(training_line, style="dim" if month_training == 0 else "")

        ph_line = f"                                                P/H  {float(month_ph):>6g}h      ({round(ph_days, 2):>5g}d)\n"
        text.append(ph_line, style="dim" if month_ph == 0 else "")

        text.append(f"                                              TOTAL  {float(month_total):>6g}h      ({round(total_days, 2):>5g}d)")

        month_summary.update(text)

        # Update earnings display (between table and summary)
        month_earnings = self.query_one("#month-earnings", Static)
        if self.show_money:
            month_earnings.remove_class("hidden")
            month_billable = month_worked * config.hourly_rate
            month_with_vat = month_billable * (1 + config.vat_rate)
            amount_str = f"£{float(month_billable):,.2f}"
            vat_str = f"£{float(month_with_vat):,.2f}"
            earnings_text = Text()
            earnings_text.append(f"                                           Billable  {amount_str:>10}   ({vat_str} inc VAT)")
            month_earnings.update(earnings_text)
        else:
            month_earnings.add_class("hidden")

    def _get_month_totals(self, year: int, month: int) -> dict:
        """Calculate totals for a month."""
        from calendar import monthrange

        entries = storage.get_month_entries(year, month)
        entries_dict = {e.date: e for e in entries}
        config = storage.get_config()

        worked = Decimal("0")
        leave = Decimal("0")
        sick = Decimal("0")
        training = Decimal("0")
        public_holiday = Decimal("0")

        # Count weekdays in month
        first_day = date(year, month, 1)
        last_day = date(year, month, monthrange(year, month)[1])
        weekdays = 0
        current = first_day
        while current <= last_day:
            if current.weekday() < 5:  # Mon-Fri
                weekdays += 1
            current += timedelta(days=1)

        # Sum up entries
        for _, entry in entries_dict.items():
            worked += entry.worked_hours
            if entry.adjusted_hours:
                if entry.adjust_type == "L":
                    leave += entry.adjusted_hours
                elif entry.adjust_type == "S":
                    sick += entry.adjusted_hours
                elif entry.adjust_type == "T":
                    training += entry.adjusted_hours
                elif entry.adjust_type == "P":
                    public_holiday += entry.adjusted_hours

        max_hours = (Decimal(weekdays) * config.standard_day_hours) - public_holiday
        total = worked + leave + sick + training + public_holiday

        return {
            "worked": worked,
            "max_hours": max_hours,
            "leave": leave,
            "sick": sick,
            "training": training,
            "public_holiday": public_holiday,
            "total": total,
        }

    def _get_max_hours_to_date(self, start_date: date, end_date: date) -> Decimal:
        """Calculate max workable hours from start_date to end_date (inclusive).

        Counts weekdays and subtracts public holiday hours from entries.
        """
        config = storage.get_config()

        # Count weekdays in range
        weekdays = 0
        current = start_date
        while current <= end_date:
            if current.weekday() < 5:  # Mon-Fri
                weekdays += 1
            current += timedelta(days=1)

        # Get public holiday hours from entries in this range
        public_holiday_hours = Decimal("0")
        for entry_date, entry in self.entries.items():
            if start_date <= entry_date <= end_date:
                if entry.adjust_type == "P" and entry.adjusted_hours:
                    public_holiday_hours += entry.adjusted_hours

        return (Decimal(weekdays) * config.standard_day_hours) - public_holiday_hours

    def _refresh_year_display(self):
        config = storage.get_config()
        std_day = float(config.standard_day_hours)

        # Update year header
        year_header = self.query_one("#year-header", Static)
        year_label = f"{self.company_year_start}-{self.company_year_start + 1}"
        year_header.update(Text(f"YEAR: {year_label}", style="bold"))

        # Build table data
        table = self.query_one("#year-table", DataTable)
        table.clear()

        # Company year months: Sep, Oct, Nov, Dec, Jan, Feb, Mar, Apr, May, Jun, Jul, Aug
        months = [
            (self.company_year_start, 9),
            (self.company_year_start, 10),
            (self.company_year_start, 11),
            (self.company_year_start, 12),
            (self.company_year_start + 1, 1),
            (self.company_year_start + 1, 2),
            (self.company_year_start + 1, 3),
            (self.company_year_start + 1, 4),
            (self.company_year_start + 1, 5),
            (self.company_year_start + 1, 6),
            (self.company_year_start + 1, 7),
            (self.company_year_start + 1, 8),
        ]

        # Year totals
        year_worked = Decimal("0")
        year_max = Decimal("0")
        year_leave = Decimal("0")
        year_sick = Decimal("0")
        year_training = Decimal("0")
        year_ph = Decimal("0")
        year_total = Decimal("0")

        for year, month in months:
            totals = self._get_month_totals(year, month)
            month_name = date(year, month, 1).strftime("%b %Y")

            # Convert hours to days
            worked_d = round(float(totals['worked']) / std_day, 2) if totals['worked'] else 0
            max_d = round(float(totals['max_hours']) / std_day, 2) if totals['max_hours'] else 0
            leave_d = round(float(totals['leave']) / std_day, 2) if totals['leave'] else 0
            sick_d = round(float(totals['sick']) / std_day, 2) if totals['sick'] else 0
            training_d = round(float(totals['training']) / std_day, 2) if totals['training'] else 0
            ph_d = round(float(totals['public_holiday']) / std_day, 2) if totals['public_holiday'] else 0
            total_d = round(float(totals['total']) / std_day, 2) if totals['total'] else 0

            # Check if this is a future month with no data
            today = date.today()
            is_future = (year > today.year) or (year == today.year and month > today.month)

            # Dim if future with no work, or if only has public holidays (no actual work)
            if is_future and totals["worked"] == 0:
                style = "dim"
            else:
                style = ""

            row_data = [
                Text(month_name, style=style),
                Text(f"{worked_d:g}d" if worked_d else "-", style=style),
                Text(f"{max_d:g}d" if max_d else "-", style=style),
                Text(f"{leave_d:g}d" if leave_d else "-", style=style),
                Text(f"{sick_d:g}d" if sick_d else "-", style=style),
                Text(f"{training_d:g}d" if training_d else "-", style=style),
                Text(f"{ph_d:g}d" if ph_d else "-", style=style),
                Text(f"{total_d:g}d" if total_d else "-", style=style),
            ]

            if self.show_money:
                billable = totals['worked'] * config.hourly_rate
                with_vat = billable * (1 + config.vat_rate)
                row_data.append(Text(f"£{float(billable):,.0f}" if billable else "-", style=style))
                row_data.append(Text(f"£{float(with_vat):,.0f}" if with_vat else "-", style=style))

            table.add_row(*row_data, key=f"{year}-{month:02d}")

            # Accumulate year totals
            year_worked += totals["worked"]
            year_max += totals["max_hours"]
            year_leave += totals["leave"]
            year_sick += totals["sick"]
            year_training += totals["training"]
            year_ph += totals["public_holiday"]
            year_total += totals["total"]

        # Update year summary
        year_summary = self.query_one("#year-summary", Static)
        text = Text()

        worked_days = round(float(year_worked) / std_day, 2) if year_worked else 0
        max_days = round(float(year_max) / std_day, 2) if year_max else 0
        leave_days = round(float(year_leave) / std_day, 2) if year_leave else 0
        sick_days = round(float(year_sick) / std_day, 2) if year_sick else 0
        training_days = round(float(year_training) / std_day, 2) if year_training else 0
        ph_days = round(float(year_ph) / std_day, 2) if year_ph else 0
        total_days = round(float(year_total) / std_day, 2) if year_total else 0

        text.append(f"                                             Worked  {worked_days:>6g}d\n")
        pct = (worked_days / max_days * 100) if max_days else 0
        text.append(f"                                      of target max  {max_days:>6g}d   ({pct:.1f}%)\n")

        # Show "of target max to date" only for current company year
        today = date.today()
        if today.month >= 9:
            current_company_year = today.year
        else:
            current_company_year = today.year - 1
        if self.company_year_start == current_company_year:
            year_start = date(self.company_year_start, 9, 1)
            max_to_date = self._get_max_hours_to_date(year_start, today)
            max_to_date_days = round(float(max_to_date) / std_day, 2) if max_to_date else 0
            pct_to_date = (worked_days / max_to_date_days * 100) if max_to_date_days else 0
            text.append(f"                              of target max to date  {max_to_date_days:>6g}d   ({pct_to_date:.1f}%)\n")

        leave_line = f"                                              Leave  {leave_days:>6g}d\n"
        text.append(leave_line, style="dim" if year_leave == 0 else "")

        sick_line = f"                                               Sick  {sick_days:>6g}d\n"
        text.append(sick_line, style="dim" if year_sick == 0 else "")

        training_line = f"                                           Training  {training_days:>6g}d\n"
        text.append(training_line, style="dim" if training_days == 0 else "")

        ph_line = f"                                                P/H  {ph_days:>6g}d\n"
        text.append(ph_line, style="dim" if year_ph == 0 else "")

        text.append(f"                                              TOTAL  {total_days:>6g}d")

        year_summary.update(text)

        # Update earnings display (between table and summary)
        year_earnings = self.query_one("#year-earnings", Static)
        if self.show_money:
            year_earnings.remove_class("hidden")
            year_billable = year_worked * config.hourly_rate
            year_with_vat = year_billable * (1 + config.vat_rate)
            amount_str = f"£{float(year_billable):,.2f}"
            vat_str = f"£{float(year_with_vat):,.2f}"
            earnings_text = Text()
            earnings_text.append(f"                                           Billable  {amount_str:>10}   ({vat_str} inc VAT)")
            year_earnings.update(earnings_text)
        else:
            year_earnings.add_class("hidden")

    def _refresh_day_display(self):
        """Refresh the day view (ticket allocations for a single day)."""
        if not self.day_view_date:
            return

        # Get the time entry for this day (fetch from storage directly
        # in case it's a boundary day from an adjacent month)
        entry = storage.get_entry(self.day_view_date)
        worked_hours = entry.worked_hours if entry else Decimal("0")

        # Update day header
        day_header = self.query_one("#day-header", DayHeader)
        day_header.update_display(self.day_view_date, worked_hours)

        # Update time entry details
        day_time_entry = self.query_one("#day-time-entry", DayTimeEntry)
        if entry:
            in_str = entry.clock_in.strftime("%H:%M") if entry.clock_in else "-"
            lunch_str = f"{int(entry.lunch_duration.total_seconds() // 60)}m" if entry.lunch_duration else "-"
            out_str = entry.clock_out.strftime("%H:%M") if entry.clock_out else "-"
            adj_str = f"{float(entry.adjusted_hours):g}h" if entry.adjusted_hours else "-"
            type_str = entry.adjust_type or ""
            comment_str = entry.comment or ""
        else:
            in_str = "-"
            lunch_str = "-"
            out_str = "-"
            adj_str = "-"
            type_str = ""
            comment_str = ""
        day_time_entry.update_display(in_str, lunch_str, out_str, adj_str, type_str, comment_str)

        # Load allocations for this day
        self.day_allocations = storage.get_allocations_for_date(self.day_view_date)

        # Build table data
        table = self.query_one("#day-table", DataTable)
        table.clear()

        for alloc in self.day_allocations:
            ticket = storage.get_ticket(alloc.ticket_id)
            desc = ticket.description[:40] if ticket else "(unknown)"
            entered = Text("✓", style="green") if alloc.entered_on_client else Text("-", style="dim")
            table.add_row(
                alloc.ticket_id,
                desc,
                f"{float(alloc.hours):.2f}h",
                entered,
                key=alloc.ticket_id,
            )

        # Calculate total allocated
        total_allocated = sum((a.hours for a in self.day_allocations), Decimal("0"))

        # Update day summary
        day_summary = self.query_one("#day-summary", DaySummary)
        day_summary.update_display(total_allocated, worked_hours)

    def _get_allocation_status(self, d: date, worked_hours: Decimal) -> Text:
        """Get the allocation status indicator for a day.

        Returns a styled Text object:
        - `?` (dim) = no allocations
        - `↓` (yellow) = under-allocated
        - `↑` (red) = over-allocated
        - `✓` (green) = exactly allocated
        """
        if worked_hours == 0:
            return Text("-", style="dim")

        total_allocated = storage.get_total_allocated_hours(d)

        if total_allocated == 0:
            return Text("?", style="dim")
        elif total_allocated < worked_hours:
            return Text("↓", style="yellow")
        elif total_allocated > worked_hours:
            return Text("↑", style="red")
        else:
            return Text("✓", style="green")

    def _get_allocation_status_with_sep(self, d: date, worked_hours: Decimal, is_friday: bool) -> Text:
        """Get allocation status indicator, with separator for Friday columns."""
        if worked_hours == 0:
            symbol, style = "-", "dim"
        else:
            total_allocated = storage.get_total_allocated_hours(d)
            if total_allocated == 0:
                symbol, style = "?", "dim"
            elif total_allocated < worked_hours:
                symbol, style = "↓", "yellow"
            elif total_allocated > worked_hours:
                symbol, style = "↑", "red"
            else:
                symbol, style = "✓", "green"

        cell = Text()
        # Right-justify all day columns for consistency (5 chars to match icon+hours width)
        cell.append(f"    {symbol}", style=style)
        if is_friday:
            cell.append("│", style="dim")
        return cell

    def _refresh_allocations_display(self):
        """Refresh the allocations report (tickets × days matrix)."""
        from calendar import monthrange

        # Update header
        alloc_header = self.query_one("#allocations-header", Static)
        month_name = date(self.current_year, self.current_month, 1).strftime("%B %Y")
        alloc_header.update(Text(f"ALLOCATIONS: {month_name}", style="bold"))

        # Get days in month
        num_days = monthrange(self.current_year, self.current_month)[1]

        # Get all allocations for the month
        allocations = storage.get_allocations_for_month(self.current_year, self.current_month)

        # Get all entries for the month (for worked hours and adjust types)
        entries = storage.get_month_entries(self.current_year, self.current_month)
        entries_dict = {e.date: e for e in entries}

        # Build dicts: ticket_id -> {date -> hours} and (ticket_id, date) -> allocation
        ticket_hours: dict[str, dict[date, Decimal]] = {}
        alloc_lookup: dict[tuple[str, date], TicketAllocation] = {}
        for alloc in allocations:
            if alloc.ticket_id not in ticket_hours:
                ticket_hours[alloc.ticket_id] = {}
            ticket_hours[alloc.ticket_id][alloc.date] = alloc.hours
            alloc_lookup[(alloc.ticket_id, alloc.date)] = alloc

        # Get all tickets that have allocations this month
        ticket_ids = sorted(ticket_hours.keys())

        # Calculate optimal ticket column width (min 6, max 10)
        max_ticket_len = max((len(tid) for tid in ticket_ids), default=6)
        ticket_col_width = min(max(max_ticket_len, 6), 10)

        # Determine which days to show (exclude weekends unless they have worked hours)
        days_to_show: list[int] = []
        for day in range(1, num_days + 1):
            d = date(self.current_year, self.current_month, day)
            is_weekend = d.weekday() >= 5
            if not is_weekend:
                days_to_show.append(day)
            else:
                # Include weekend day only if it has worked hours
                entry = entries_dict.get(d)
                if entry and entry.worked_hours > 0:
                    days_to_show.append(day)

        # Store for click handling
        self._alloc_days_to_show = days_to_show

        # Rebuild table with correct columns
        table = self.query_one("#allocations-table", DataTable)
        table.clear(columns=True)

        # Add columns: Ticket, Description, then each day, then Total
        # Track Fridays for adding vertical separators
        friday_days = set()
        table.add_column("Ticket", width=ticket_col_width)
        table.add_column("Description", width=20)
        for day in days_to_show:
            d = date(self.current_year, self.current_month, day)
            if d.weekday() == 4:  # Friday - week boundary
                friday_days.add(day)
                # Right-justify day number, add │ separator (extra width for circle icon)
                table.add_column(f"{day:>5}│", width=6)
            else:
                # Right-justify all day headers for consistency (extra width for circle icon)
                table.add_column(f"{day:>5}", width=5)
        table.add_column("Total", width=6)

        # Add rows for each ticket
        for ticket_id in ticket_ids:
            ticket = storage.get_ticket(ticket_id)
            desc = ticket.description[:18] if ticket else ""
            row_data: list[str | Text] = [ticket_id, desc]
            row_total = Decimal("0")

            for day in days_to_show:
                d = date(self.current_year, self.current_month, day)
                hours = ticket_hours[ticket_id].get(d, Decimal("0"))
                row_total += hours

                is_weekend = d.weekday() >= 5
                is_friday = day in friday_days

                if hours > 0:
                    # Check entered state for icon and styling
                    alloc = alloc_lookup.get((ticket_id, d))
                    if alloc and alloc.entered_on_client:
                        icon = "●"
                        icon_style = "dim green" if is_weekend else "green"
                    else:
                        icon = "○"
                        icon_style = "dim yellow" if is_weekend else "yellow"
                    text_style = "dim" if is_weekend else ""
                    content = f"{float(hours):g}"
                    cell = Text()
                    # Coloured icon + right-justified hours
                    cell.append(icon, style=icon_style)
                    cell.append(f"{content:>4}", style=text_style)
                    if is_friday:
                        cell.append("│", style="dim")
                else:
                    # No allocation for this ticket on this day
                    cell = Text()
                    cell.append("    -", style="dim")
                    if is_friday:
                        cell.append("│", style="dim")

                row_data.append(cell)

            row_data.append(Text(f"{float(row_total):g}", style="bold"))
            table.add_row(*row_data, key=ticket_id)

        # Add summary rows: Worked, Status, and Week Total
        worked_row: list[str | Text] = ["Worked", ""]
        status_row: list[str | Text] = ["Status", ""]
        week_total_row: list[str | Text] = ["Wk Tot", ""]
        week_total = Decimal("0")
        month_total = Decimal("0")

        for day in days_to_show:
            d = date(self.current_year, self.current_month, day)
            entry = entries_dict.get(d)
            worked = entry.worked_hours if entry else Decimal("0")
            is_weekend = d.weekday() >= 5
            is_friday = day in friday_days

            # Accumulate weekly and monthly totals (worked hours only, not adjustments)
            if entry:
                week_total += entry.worked_hours
                month_total += entry.worked_hours

            if worked > 0:
                content = f"{float(worked):g}"
                style = "dim" if is_weekend else ""
                cell = Text()
                cell.append(f"{content:>5}", style=style)
                if is_friday:
                    cell.append("│", style="dim")
                worked_row.append(cell)
            elif entry and entry.adjust_type:
                # Show adjust type for leave/sick/PH days in Worked row
                content = f"[{entry.adjust_type}]"
                cell = Text()
                cell.append(f"{content:>5}", style="dim")
                if is_friday:
                    cell.append("│", style="dim")
                worked_row.append(cell)
            else:
                cell = Text()
                cell.append("    -", style="dim")
                if is_friday:
                    cell.append("│", style="dim")
                worked_row.append(cell)

            # Status indicator
            status_cell = self._get_allocation_status_with_sep(d, worked, is_friday)
            status_row.append(status_cell)

            # Week total - show on Friday, empty otherwise
            if is_friday:
                cell = Text()
                cell.append(f"{float(week_total):>5g}", style="bold")
                cell.append("│", style="dim")
                week_total_row.append(cell)
                week_total = Decimal("0")  # Reset for next week
            else:
                cell = Text()
                cell.append("     ", style="dim")
                week_total_row.append(cell)

        worked_row.append(Text(""))  # No total for worked row
        status_row.append(Text(""))  # No total for status row
        week_total_row.append(Text(f"{float(month_total):g}", style="bold"))  # Month grand total

        table.add_row(*worked_row, key="__worked__")
        table.add_row(*status_row, key="__status__")
        table.add_row(*week_total_row, key="__week_total__")

        # Update summary
        alloc_summary = self.query_one("#allocations-summary", Static)
        total_allocated = sum((a.hours for a in allocations), Decimal("0"))
        total_worked = sum((e.worked_hours for e in entries), Decimal("0"))
        mismatch_days = sum(
            1 for day in days_to_show
            if self._has_allocation_mismatch(
                date(self.current_year, self.current_month, day),
                entries_dict
            )
        )

        text = Text()
        text.append(f"Total allocated: {float(total_allocated):.1f}h")
        text.append(f"   Total worked: {float(total_worked):.1f}h")
        if mismatch_days > 0:
            text.append(f"   Mismatched days: {mismatch_days}", style="red")
        alloc_summary.update(text)

    def _has_allocation_mismatch(self, d: date, entries_dict: dict) -> bool:
        """Check if a day has an allocation mismatch."""
        entry = entries_dict.get(d)
        if not entry or entry.worked_hours == 0:
            return False
        allocated = storage.get_total_allocated_hours(d)
        return allocated != entry.worked_hours

    def _set_view_mode(self, mode: str):
        """Switch between view modes and toggle widget visibility."""
        self.view_mode = mode

        # Week view widgets
        week_widgets = ["#combined-header", "#week-table-container", "#week-earnings", "#weekly-summary"]
        # Month view widgets
        month_widgets = ["#month-header", "#month-table-container", "#month-earnings", "#month-summary"]
        # Year view widgets
        year_widgets = ["#year-header", "#year-table-container", "#year-earnings", "#year-summary"]
        # Day view widgets
        day_widgets = ["#day-header", "#day-time-entry", "#day-table-container", "#day-summary"]
        # Allocations report widgets
        alloc_widgets = ["#allocations-header", "#allocations-table-container", "#allocations-summary"]

        for widget_id in week_widgets:
            widget = self.query_one(widget_id)
            if mode == "week":
                widget.remove_class("hidden")
            else:
                widget.add_class("hidden")

        for widget_id in month_widgets:
            widget = self.query_one(widget_id)
            if mode == "month":
                widget.remove_class("hidden")
            else:
                widget.add_class("hidden")

        for widget_id in year_widgets:
            widget = self.query_one(widget_id)
            if mode == "year":
                widget.remove_class("hidden")
            else:
                widget.add_class("hidden")

        for widget_id in day_widgets:
            widget = self.query_one(widget_id)
            if mode == "day":
                widget.remove_class("hidden")
            else:
                widget.add_class("hidden")

        for widget_id in alloc_widgets:
            widget = self.query_one(widget_id)
            if mode == "allocations":
                widget.remove_class("hidden")
            else:
                widget.add_class("hidden")

        # Update binding visibility based on view mode
        self._update_bindings_for_mode(mode)

        self._refresh_display()

        # Focus the appropriate table for the view
        if mode == "week":
            self.query_one("#week-table", DataTable).focus()
        elif mode == "month":
            self.query_one("#month-table", DataTable).focus()
        elif mode == "year":
            self.query_one("#year-table", DataTable).focus()
        elif mode == "day":
            self.query_one("#day-table", DataTable).focus()
        elif mode == "allocations":
            self.query_one("#allocations-table", DataTable).focus()

    def _update_bindings_for_mode(self, mode: str):
        """Update footer bindings based on view mode."""
        # Use refresh_bindings to trigger check_action re-evaluation
        self.refresh_bindings()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Check if an action is available based on current view mode."""
        if action == "week_view":
            # Show in day view (goes back to week) but not in week view itself
            return self.view_mode != "week"
        elif action == "month_view":
            return self.view_mode not in ("month", "day")
        elif action == "year_view":
            return self.view_mode not in ("year", "day")
        elif action == "populate_holidays":
            return self.view_mode == "year"
        elif action == "edit_day":
            # Show Edit in week view and day view
            return True if self.view_mode in ("week", "day") else None
        elif action == "add_allocation":
            # Only show Add in day view
            return True if self.view_mode == "day" else None
        elif action == "delete_allocation":
            # Only show Delete in day view
            return True if self.view_mode == "day" else None
        elif action == "back_to_week":
            # Only show Back in day view
            return True if self.view_mode == "day" else None
        elif action == "toggle_entered":
            # Only show Entered toggle in day view
            return True if self.view_mode == "day" else None
        elif action == "allocations_view":
            return self.view_mode != "allocations"
        return True

    def action_prev_week(self):
        if self.view_mode == "day":
            # Navigate to previous day with worked hours
            self._navigate_to_prev_worked_day()
            return

        if self.view_mode == "year":
            # Navigate to previous year
            self.company_year_start -= 1
            self._refresh_display()
            return

        if self.view_mode == "month":
            # Navigate to previous month
            if self.current_month == 1:
                self.current_year -= 1
                self.current_month = 12
            else:
                self.current_month -= 1
            self.weeks = get_weeks_in_month(self.current_year, self.current_month)
            self._load_month_data()
            header = self.query_one("#combined-header", CombinedHeader)
            header.year = self.current_year
            header.month = self.current_month
            self._refresh_display()
            return

        # Remember which day of week (row) is selected
        table = self.query_one("#week-table", DataTable)
        selected_row = table.cursor_row

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

        # Restore the same day of week selection
        table.move_cursor(row=selected_row)

    def action_next_week(self):
        if self.view_mode == "day":
            # Navigate to next day with worked hours
            self._navigate_to_next_worked_day()
            return

        if self.view_mode == "year":
            # Navigate to next year
            self.company_year_start += 1
            self._refresh_display()
            return

        if self.view_mode == "month":
            # Navigate to next month
            if self.current_month == 12:
                self.current_year += 1
                self.current_month = 1
            else:
                self.current_month += 1
            self.weeks = get_weeks_in_month(self.current_year, self.current_month)
            self._load_month_data()
            header = self.query_one("#combined-header", CombinedHeader)
            header.year = self.current_year
            header.month = self.current_month
            self._refresh_display()
            return

        # Remember which day of week (row) is selected
        table = self.query_one("#week-table", DataTable)
        selected_row = table.cursor_row

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

        # Restore the same day of week selection
        table.move_cursor(row=selected_row)

    def action_cursor_up(self):
        if self.view_mode == "year":
            table = self.query_one("#year-table", DataTable)
        elif self.view_mode == "month":
            table = self.query_one("#month-table", DataTable)
        else:
            table = self.query_one("#week-table", DataTable)
        table.action_cursor_up()

    def action_cursor_down(self):
        if self.view_mode == "year":
            table = self.query_one("#year-table", DataTable)
        elif self.view_mode == "month":
            table = self.query_one("#month-table", DataTable)
        else:
            table = self.query_one("#week-table", DataTable)
        table.action_cursor_down()

    def action_toggle_money(self):
        """Toggle display of billable amounts."""
        self.show_money = not self.show_money
        # Rebuild tables with new column structure
        self._rebuild_tables()
        self._refresh_display()

    def action_toggle_help(self):
        """Toggle display of keyboard shortcuts panel."""
        # Toggle built-in Textual help panel
        if self._help_panel_visible:
            self.action_hide_help_panel()
        else:
            self.action_show_help_panel()
        self._help_panel_visible = not self._help_panel_visible

    def action_manage_tickets(self):
        """Open ticket management screen."""
        self.push_screen(TicketManagementScreen())

    def action_add_allocation(self):
        """Add a new allocation in day view."""
        if self.view_mode != "day" or not self.day_view_date:
            return

        # Capture the date at the start of the flow (don't rely on day_view_date later
        # as it could change if user navigates while modal is open)
        self._allocation_target_date = self.day_view_date

        # Open ticket selector
        self.push_screen(TicketSelectScreen(), self._on_ticket_selected_for_allocation)

    def _on_ticket_selected_for_allocation(self, ticket: Ticket | None) -> None:
        """Handle ticket selection for new allocation."""
        # Use the captured target date, not day_view_date (which may have changed)
        target_date = getattr(self, '_allocation_target_date', None)
        if not ticket or not target_date:
            return

        # Reload allocations for the target date (in case they differ from displayed)
        target_allocations = storage.get_allocations_for_date(target_date)

        # Check if already allocated
        existing = next((a for a in target_allocations if a.ticket_id == ticket.id), None)
        if existing:
            self.notify(f"{ticket.id} already has an allocation. Edit it instead.", severity="warning")
            return

        # Calculate remaining hours (fetch entry from storage for boundary days)
        entry = storage.get_entry(target_date)
        worked = entry.worked_hours if entry else Decimal("0")
        total_allocated = sum((a.hours for a in target_allocations), Decimal("0"))
        remaining = worked - total_allocated

        self.push_screen(
            EditAllocationScreen(
                ticket,
                current_hours="",
                remaining_hours=str(remaining),
            ),
            self._on_allocation_edited,
        )

    def action_delete_allocation(self):
        """Delete the selected allocation in day view."""
        if self.view_mode != "day" or not self.day_view_date:
            return

        table = self.query_one("#day-table", DataTable)
        if table.row_count == 0:
            self.notify("No allocations to delete", severity="warning")
            return

        row_key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key
        if row_key:
            ticket_id = str(row_key.value)
            self.push_screen(
                ConfirmScreen(f"Delete allocation for {ticket_id}?"),
                lambda confirmed: self._on_delete_allocation_confirmed(confirmed, ticket_id),
            )

    def _on_delete_allocation_confirmed(self, confirmed: bool | None, ticket_id: str) -> None:
        """Handle delete allocation confirmation."""
        if confirmed and self.day_view_date:
            storage.delete_allocation(ticket_id, self.day_view_date)
            self.notify(f"Deleted allocation for {ticket_id}")
            self._refresh_display()

    def action_toggle_entered(self):
        """Toggle the entered_on_client flag for the selected allocation."""
        if self.view_mode != "day" or not self.day_view_date:
            return

        table = self.query_one("#day-table", DataTable)
        if table.row_count == 0:
            return

        row_key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key
        if not row_key:
            return

        ticket_id = str(row_key.value)
        alloc = next((a for a in self.day_allocations if a.ticket_id == ticket_id), None)
        if not alloc:
            return

        # Toggle the flag
        new_value = not alloc.entered_on_client
        updated_alloc = TicketAllocation(
            ticket_id=alloc.ticket_id,
            date=alloc.date,
            hours=alloc.hours,
            entered_on_client=new_value,
        )
        storage.save_allocation(updated_alloc)

        status = "marked as entered" if new_value else "unmarked"
        self.notify(f"{ticket_id} {status}")
        self._refresh_display()

    def action_back_to_week(self):
        """Return to week view from day view."""
        if self.view_mode != "day":
            return

        # Use the current day view date (may have changed via navigation)
        target_date = self.day_view_date

        if target_date:
            # Switch to the correct month if needed
            if target_date.year != self.current_year or target_date.month != self.current_month:
                self.current_year = target_date.year
                self.current_month = target_date.month
                self.weeks = get_weeks_in_month(self.current_year, self.current_month)
                self._load_month_data()
                header = self.query_one("#combined-header", CombinedHeader)
                header.year = self.current_year
                header.month = self.current_month

            # Find the week containing the target date
            self.current_week_idx = self._find_week_for_date(target_date)
            self.last_selected_date = target_date

        self._set_view_mode("week")

        # Select the day we were viewing
        if target_date:
            self._select_date(target_date)

    def action_goto_today(self):
        today = date.today()
        self.current_year = today.year
        self.current_month = today.month
        self.weeks = get_weeks_in_month(self.current_year, self.current_month)
        self.current_week_idx = self._find_week_for_date(today)

        # Update company year
        if today.month >= 9:
            self.company_year_start = today.year
        else:
            self.company_year_start = today.year - 1

        self._load_month_data()

        # Switch to week view if not already
        if self.view_mode != "week":
            self._set_view_mode("week")
        else:
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

    def _select_day_in_week(self):
        """Select appropriate day when entering week view.

        Priority:
        1. If returning to same week, restore previously selected day
        2. If today is in the week, select today
        3. Otherwise select Monday
        """
        week_start, week_end = self.weeks[self.current_week_idx]
        today = date.today()

        # Check if last selected date is in this week
        if self.last_selected_date and week_start <= self.last_selected_date <= week_end:
            self._select_date(self.last_selected_date)
        # Check if today is in this week
        elif week_start <= today <= week_end:
            self._select_date(today)
        # Otherwise select Monday (2 days after Saturday start)
        else:
            monday = week_start + timedelta(days=2)
            self._select_date(monday)

    def action_allocations_view(self):
        """Switch to allocations report view."""
        if self.view_mode == "week":
            self.last_selected_date = self._get_selected_date()
        self._set_view_mode("allocations")

    def action_year_view(self):
        """Switch to year view."""
        if self.view_mode != "year":
            # Save selected date if coming from week view
            if self.view_mode == "week":
                self.last_selected_date = self._get_selected_date()
            # Remember current month to select it in year view
            current_month_key = f"{self.current_year}-{self.current_month:02d}"
            self._set_view_mode("year")
            # Select the row for the current month
            table = self.query_one("#year-table", DataTable)
            for row_idx, row_key in enumerate(table.rows.keys()):
                if str(row_key.value) == current_month_key:
                    table.move_cursor(row=row_idx)
                    break

    def action_month_view(self):
        """Switch to month view."""
        if self.view_mode == "year":
            # In year view, navigate to the selected month
            table = self.query_one("#year-table", DataTable)
            row_key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key
            if row_key:
                parts = str(row_key.value).split("-")
                year = int(parts[0])
                month = int(parts[1])
                self._navigate_to_month_view(year, month)
        elif self.view_mode != "month":
            # Save selected date if coming from week view
            if self.view_mode == "week":
                self.last_selected_date = self._get_selected_date()
            # Remember current week to select it in month view
            current_week_start = self.weeks[self.current_week_idx][0] if self.weeks else None
            self._set_view_mode("month")
            # Select the row for the current week
            if current_week_start:
                table = self.query_one("#month-table", DataTable)
                for row_idx, row_key in enumerate(table.rows.keys()):
                    if str(row_key.value) == current_week_start.isoformat():
                        table.move_cursor(row=row_idx)
                        break

    def action_week_view(self):
        """Switch to week view."""
        if self.view_mode == "day":
            # From day view, go to the week containing the current day
            self.action_back_to_week()
        elif self.view_mode == "month":
            # In month view, navigate to the selected week
            table = self.query_one("#month-table", DataTable)
            row_key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key
            if row_key:
                week_start = date.fromisoformat(str(row_key.value))
                # Find the week index for this week
                for i, (start, end) in enumerate(self.weeks):
                    if start == week_start:
                        self.current_week_idx = i
                        break
                self._set_view_mode("week")
                self._select_day_in_week()
        elif self.view_mode != "week":
            self._set_view_mode("week")
            self._select_day_in_week()

    def _navigate_to_month_view(self, year: int, month: int):
        """Navigate to a specific month in month view."""
        self.current_year = year
        self.current_month = month
        self.weeks = get_weeks_in_month(self.current_year, self.current_month)
        self.current_week_idx = 0
        self._load_month_data()
        header = self.query_one("#combined-header", CombinedHeader)
        header.year = self.current_year
        header.month = self.current_month
        self._set_view_mode("month")

    def _navigate_to_month(self, year: int, month: int):
        """Navigate to a specific month in week view."""
        self.current_year = year
        self.current_month = month
        self.weeks = get_weeks_in_month(self.current_year, self.current_month)
        self.current_week_idx = 0
        self._load_month_data()
        header = self.query_one("#combined-header", CombinedHeader)
        header.year = self.current_year
        header.month = self.current_month

        if self.view_mode != "week":
            self._set_view_mode("week")
        else:
            self._refresh_display()
        self._select_day_in_week()

    def _navigate_to_day_view(self, d: date, auto_edit: bool = True) -> None:
        """Navigate to day view for a specific date.

        Args:
            d: The date to navigate to.
            auto_edit: If True, automatically open edit modal when day has no
                       worked hours and no adjusted hours.
        """
        self.day_view_date = d
        self.last_selected_date = d  # Remember for returning to week view
        self._set_view_mode("day")

        # Auto-open edit modal if no worked hours and no adjusted hours
        if auto_edit:
            entry = storage.get_entry(d)
            if entry is None or (entry.worked_hours == 0 and entry.adjusted_hours == 0):
                # Create entry if it doesn't exist
                if entry is None:
                    entry = TimeEntry(date=d, day_of_week=d.strftime("%a"))
                self.push_screen(EditDayScreen(entry), self._on_day_edit_complete)

    def _navigate_to_prev_worked_day(self) -> None:
        """Navigate to previous day with worked hours."""
        if not self.day_view_date:
            return

        # Search backwards from current day for a day with worked hours
        current = self.day_view_date - timedelta(days=1)
        # Search up to 365 days back
        for _ in range(365):
            entry = storage.get_entry(current)
            if entry and entry.worked_hours > 0:
                self._navigate_to_day_view(current, auto_edit=False)
                return
            current -= timedelta(days=1)
        # No day found - stay on current day

    def _navigate_to_next_worked_day(self) -> None:
        """Navigate to next day with worked hours."""
        if not self.day_view_date:
            return

        # Search forwards from current day for a day with worked hours
        current = self.day_view_date + timedelta(days=1)
        # Search up to 365 days forward
        for _ in range(365):
            entry = storage.get_entry(current)
            if entry and entry.worked_hours > 0:
                self._navigate_to_day_view(current, auto_edit=False)
                return
            current += timedelta(days=1)
        # No day found - stay on current day

    def _edit_allocation(self, ticket_id: str) -> None:
        """Edit an existing allocation."""
        if not self.day_view_date:
            return

        # Capture the date at the start of the flow (for consistency with add flow)
        self._allocation_target_date = self.day_view_date

        # Find the current allocation
        alloc = next((a for a in self.day_allocations if a.ticket_id == ticket_id), None)
        if not alloc:
            return

        ticket = storage.get_ticket(ticket_id)
        if not ticket:
            return

        # Calculate remaining hours (fetch entry from storage for boundary days)
        entry = storage.get_entry(self.day_view_date)
        worked = entry.worked_hours if entry else Decimal("0")
        total_allocated = sum((a.hours for a in self.day_allocations), Decimal("0"))
        remaining = worked - total_allocated + alloc.hours  # Add back current allocation

        self.push_screen(
            EditAllocationScreen(
                ticket,
                current_hours=str(alloc.hours),
                remaining_hours=str(remaining),
            ),
            self._on_allocation_edited,
        )

    def _on_allocation_edited(self, result: tuple[str, str] | None) -> None:
        """Handle allocation edit result."""
        # Use the captured target date, not day_view_date (which may have changed)
        target_date = getattr(self, '_allocation_target_date', None)
        if result and target_date:
            ticket_id, hours_str = result
            hours = Decimal(hours_str)
            if hours > 0:
                alloc = TicketAllocation(
                    ticket_id=ticket_id,
                    date=target_date,
                    hours=hours,
                )
                storage.save_allocation(alloc)
                self.notify(f"Allocated {hours}h to {ticket_id}")
            else:
                # Zero hours means delete the allocation
                storage.delete_allocation(ticket_id, target_date)
                self.notify(f"Removed allocation for {ticket_id}")
            self._refresh_display()

    def on_data_table_cell_highlighted(self, event: DataTable.CellHighlighted) -> None:
        """Highlight the ticket ID when cursor moves to any cell in that row."""
        if self.view_mode != "allocations":
            return

        table = self.query_one("#allocations-table", DataTable)
        row_key = event.cell_key.row_key

        # Skip if no row key or if it's a summary row
        if not row_key or str(row_key.value).startswith("__"):
            # Restore previous highlight if any
            prev_row = getattr(self, '_alloc_highlighted_row', None)
            if prev_row:
                self._restore_ticket_cell(table, prev_row)
                self._alloc_highlighted_row = None
            return

        ticket_id = str(row_key.value)
        prev_row = getattr(self, '_alloc_highlighted_row', None)

        # If same row, nothing to do
        if prev_row == ticket_id:
            return

        # Restore previous row's ticket cell
        if prev_row:
            self._restore_ticket_cell(table, prev_row)

        # Highlight current row's ticket cell
        self._highlight_ticket_cell(table, ticket_id)
        self._alloc_highlighted_row = ticket_id

    def _highlight_ticket_cell(self, table: DataTable, ticket_id: str) -> None:
        """Highlight the ticket ID cell with reverse video style."""
        first_col_key = list(table.columns.keys())[0]
        table.update_cell(ticket_id, first_col_key, Text(ticket_id, style="reverse"))

    def _restore_ticket_cell(self, table: DataTable, ticket_id: str) -> None:
        """Restore the ticket ID cell to normal style."""
        first_col_key = list(table.columns.keys())[0]
        try:
            table.update_cell(ticket_id, first_col_key, ticket_id)
        except Exception:
            pass  # Row may no longer exist

    def _toggle_allocation_entered_state(self) -> None:
        """Toggle entered state for the currently selected allocation cell."""
        if self.view_mode != "allocations":
            return

        table = self.query_one("#allocations-table", DataTable)

        # Get current cursor position
        cursor_row = table.cursor_row
        cursor_col = table.cursor_column

        # Get the row key from cursor position
        if cursor_row >= len(table.rows):
            return
        row_key = list(table.rows.keys())[cursor_row]

        # Skip summary rows
        if str(row_key.value).startswith("__"):
            return

        ticket_id = str(row_key.value)

        # Columns: 0=Ticket, 1=Description, 2..n-1=days, n=Total
        days_to_show = getattr(self, '_alloc_days_to_show', [])
        day_col_start = 2
        day_col_end = day_col_start + len(days_to_show)

        if cursor_col < day_col_start or cursor_col >= day_col_end:
            return

        # Get the day for this column
        day_index = cursor_col - day_col_start
        if day_index >= len(days_to_show):
            return

        day = days_to_show[day_index]
        d = date(self.current_year, self.current_month, day)

        # Check if there's an allocation for this ticket/date
        alloc = next(
            (a for a in storage.get_allocations_for_date(d) if a.ticket_id == ticket_id),
            None
        )

        if alloc:
            # Toggle the entered state
            updated = TicketAllocation(
                ticket_id=alloc.ticket_id,
                date=alloc.date,
                hours=alloc.hours,
                entered_on_client=not alloc.entered_on_client,
            )
            storage.save_allocation(updated)
            status = "entered" if updated.entered_on_client else "not entered"
            self.notify(f"{ticket_id} on {d.strftime('%b %d')}: {status}")

            # Refresh and restore cursor position
            self._refresh_display()
            table = self.query_one("#allocations-table", DataTable)
            table.move_cursor(row=cursor_row, column=cursor_col)

    def on_data_table_cell_selected(self, event: DataTable.CellSelected) -> None:
        """Handle Enter/click on cell in allocations view to toggle entered state."""
        if self.view_mode != "allocations":
            return
        self._toggle_allocation_entered_state()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle Enter/double-click on table row."""
        # Get the table ID to ensure we're handling the correct table
        table_id = event.control.id

        # Map view modes to their expected table IDs
        expected_table_ids = {
            "year": "year-table",
            "month": "month-table",
            "week": "week-table",
            "day": "day-table",
            "allocations": "allocations-table",
        }

        # Only process if the event is from the expected table for this view mode
        if table_id != expected_table_ids.get(self.view_mode):
            return

        if self.view_mode == "year":
            # In year view, selecting a month navigates to that month's month view
            if event.row_key:
                parts = str(event.row_key.value).split("-")
                year = int(parts[0])
                month = int(parts[1])
                self._navigate_to_month_view(year, month)
        elif self.view_mode == "month":
            # In month view, selecting a week navigates to that week in week view
            if event.row_key:
                week_start = date.fromisoformat(str(event.row_key.value))
                # Find the week index for this week
                for i, (start, end) in enumerate(self.weeks):
                    if start == week_start:
                        self.current_week_idx = i
                        break
                self._set_view_mode("week")
                self._select_day_in_week()
        elif self.view_mode == "week":
            # In week view, selecting a day navigates to day view
            if event.row_key:
                selected_date = date.fromisoformat(str(event.row_key.value))
                self._navigate_to_day_view(selected_date)
        elif self.view_mode == "day":
            # In day view, selecting an allocation opens edit
            if event.row_key:
                ticket_id = str(event.row_key.value)
                self._edit_allocation(ticket_id)

    def action_populate_holidays(self):
        """Pre-populate UK bank holidays for the company year (year view only)."""
        if self.view_mode != "year":
            return

        config = storage.get_config()

        # Populate holidays for all months in the company year (Sep-Aug)
        months = [
            (self.company_year_start, 9),
            (self.company_year_start, 10),
            (self.company_year_start, 11),
            (self.company_year_start, 12),
            (self.company_year_start + 1, 1),
            (self.company_year_start + 1, 2),
            (self.company_year_start + 1, 3),
            (self.company_year_start + 1, 4),
            (self.company_year_start + 1, 5),
            (self.company_year_start + 1, 6),
            (self.company_year_start + 1, 7),
            (self.company_year_start + 1, 8),
        ]

        total_count = 0
        for year, month in months:
            count = storage.populate_holidays(year, month, config.standard_day_hours)
            total_count += count

        # Refresh display
        self._refresh_display()
        self.notify(f"Added {total_count} holiday entries" if total_count else "No new holidays to add")

    def action_edit_day(self):
        """Open edit modal for selected day."""
        if self.view_mode == "week":
            table = self.query_one("#week-table", DataTable)
            current_row = table.cursor_row
            row_key = table.coordinate_to_cell_key(Coordinate(current_row, 0)).row_key

            if row_key:
                selected_date = date.fromisoformat(str(row_key.value))
                entry = self._get_or_create_entry(selected_date)
                # Store current row for advancing after edit
                self._edit_row = current_row
                self.push_screen(EditDayScreen(entry), self._on_edit_complete)
        elif self.view_mode == "day" and self.day_view_date:
            # In day view, edit the current day's time entry
            entry = storage.get_entry(self.day_view_date)
            if not entry:
                entry = TimeEntry(
                    date=self.day_view_date,
                    day_of_week=self.day_view_date.strftime("%a"),
                )
            self.push_screen(EditDayScreen(entry), self._on_day_edit_complete)

    def _on_day_edit_complete(self, result: TimeEntry | None) -> None:
        """Handle result from edit modal in day view."""
        if result:
            storage.save_entry(result)
            # Also update local cache if in current month
            if result.date.year == self.current_year and result.date.month == self.current_month:
                self.entries[result.date] = result
            self._refresh_display()

    def _on_edit_complete(self, result: TimeEntry | None) -> None:
        """Handle result from edit modal."""
        if result:
            storage.save_entry(result)
            self.entries[result.date] = result
            self._refresh_display()

        # Move to next row (or stay on last row)
        table = self.query_one("#week-table", DataTable)
        if hasattr(self, '_edit_row'):
            next_row = min(self._edit_row + 1, 6)  # 7 rows (0-6)
            table.move_cursor(row=next_row)
            del self._edit_row

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

        def do_apply(confirmed: bool | None = True) -> None:
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

    def action_cut_day(self) -> None:
        """Cut the selected day (copy to clipboard and clear)."""
        if self.view_mode != "week":
            return
        selected_date = self._get_selected_date()
        if not selected_date:
            return
        entry = self.entries.get(selected_date)
        if not entry or self._entry_is_blank(entry):
            self.notify("Nothing to cut")
            return

        # Remember cursor position
        table = self.query_one("#week-table", DataTable)
        current_row = table.cursor_row

        # Copy to clipboard
        self._day_clipboard = entry

        # Clear the entry
        cleared = TimeEntry(
            date=entry.date,
            day_of_week=entry.day_of_week,
            clock_in=None,
            lunch_duration=None,
            clock_out=None,
            adjustment=None,
            adjust_type=None,
            comment=None,
        )
        storage.save_entry(cleared)
        self.entries[cleared.date] = cleared
        self._refresh_display()

        # Restore cursor position
        table.move_cursor(row=current_row)
        self.notify(f"Cut {entry.date.strftime('%b %d')}")

    def action_copy_day(self) -> None:
        """Copy the selected day to clipboard."""
        if self.view_mode != "week":
            return
        selected_date = self._get_selected_date()
        if not selected_date:
            return
        entry = self.entries.get(selected_date)
        if not entry or self._entry_is_blank(entry):
            self.notify("Nothing to copy")
            return

        self._day_clipboard = entry
        self.notify(f"Copied {entry.date.strftime('%b %d')}")

    def action_paste_day(self) -> None:
        """Paste clipboard contents to the selected day."""
        if self.view_mode != "week":
            return
        if not self._day_clipboard:
            self.notify("Clipboard is empty")
            return
        selected_date = self._get_selected_date()
        if not selected_date:
            return

        # Get or create entry for target date
        target = self._get_or_create_entry(selected_date)

        def do_paste(confirmed: bool | None) -> None:
            if not confirmed:
                return
            # Remember cursor position
            table = self.query_one("#week-table", DataTable)
            current_row = table.cursor_row

            # Create new entry with clipboard data but target date
            pasted = TimeEntry(
                date=target.date,
                day_of_week=target.day_of_week,
                clock_in=self._day_clipboard.clock_in if self._day_clipboard else None,
                lunch_duration=self._day_clipboard.lunch_duration if self._day_clipboard else None,
                clock_out=self._day_clipboard.clock_out if self._day_clipboard else None,
                adjustment=self._day_clipboard.adjustment if self._day_clipboard else None,
                adjust_type=self._day_clipboard.adjust_type if self._day_clipboard else None,
                comment=self._day_clipboard.comment if self._day_clipboard else None,
            )
            storage.save_entry(pasted)
            self.entries[pasted.date] = pasted
            self._refresh_display()

            # Restore cursor position
            table.move_cursor(row=current_row)
            self.notify(f"Pasted to {target.date.strftime('%b %d')}")

        if self._entry_is_blank(target):
            do_paste(True)
        else:
            self.push_screen(
                ConfirmScreen(f"Overwrite existing entry for {target.date.strftime('%b %d')}?"),
                do_paste
            )


def main():
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--db-info":
        from datetime import datetime
        db_path = storage.DB_PATH
        print(f"Database: {db_path}")
        if db_path.exists():
            mtime = datetime.fromtimestamp(db_path.stat().st_mtime)
            size = db_path.stat().st_size
            print(f"Modified: {mtime.strftime('%Y-%m-%d %H:%M:%S')}")
            print(f"Size: {size:,} bytes")
        else:
            print("Status: Does not exist (will be created on first run)")
        return

    app = TimesheetApp()
    app.run()


if __name__ == "__main__":
    main()
