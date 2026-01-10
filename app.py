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
from models import TimeEntry
from utils import get_weeks_in_month
from screens import ConfirmScreen, EditDayScreen
from widgets import CombinedHeader, WeeklySummary


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

    #week-earnings {
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
        Binding("left", "prev_week", "◄"),
        Binding("right", "next_week", "►"),
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
    ]

    def __init__(self):
        super().__init__()
        storage.init_db()

        # View mode: "week", "month", or "year"
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

        # Privacy mode: hide earnings by default
        self.show_money = False

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
        yield Container(DataTable(id="week-table"), id="week-table-container")
        yield Static(id="week-earnings", classes="hidden")
        yield WeeklySummary(id="weekly-summary")
        # Month view widgets (hidden by default)
        yield Static(id="month-header", classes="hidden")
        yield Container(DataTable(id="month-table"), id="month-table-container", classes="hidden")
        yield Static(id="month-summary", classes="hidden")
        # Year view widgets (hidden by default)
        yield Static(id="year-header", classes="hidden")
        yield Container(DataTable(id="year-table"), id="year-table-container", classes="hidden")
        yield Static(id="year-summary", classes="hidden")
        yield Footer()

    def on_mount(self):
        self._setup_week_table()
        self._setup_month_table()
        self._setup_year_table()
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
        table.add_column("Adj", width=7)
        table.add_column("Type", width=5)
        table.add_column("Comment", width=50)  # Much wider for comments

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
        else:
            self._refresh_year_display()

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
            text = Text()
            text.append(f"                                           Billable  £{float(billable_amount):,.2f}      (£{float(total_with_vat):,.2f} inc VAT)")
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
            comment_str = (entry.comment[:47] + "...") if entry.comment and len(entry.comment) > 47 else (entry.comment or "")

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
        text.append(f"                                    of max possible  {float(month_max):>6g}h      ({round(max_days, 2):>5g}d)\n")

        leave_line = f"                                              Leave  {float(month_leave):>6g}h      ({round(leave_days, 2):>5g}d)\n"
        text.append(leave_line, style="dim" if month_leave == 0 else "")

        sick_line = f"                                               Sick  {float(month_sick):>6g}h      ({round(sick_days, 2):>5g}d)\n"
        text.append(sick_line, style="dim" if month_sick == 0 else "")

        training_line = f"                                           Training  {float(month_training):>6g}h      ({round(training_days, 2):>5g}d)\n"
        text.append(training_line, style="dim" if month_training == 0 else "")

        ph_line = f"                                                P/H  {float(month_ph):>6g}h      ({round(ph_days, 2):>5g}d)\n"
        text.append(ph_line, style="dim" if month_ph == 0 else "")

        text.append(f"                                              TOTAL  {float(month_total):>6g}h      ({round(total_days, 2):>5g}d)")

        if self.show_money:
            month_billable = month_worked * config.hourly_rate
            month_with_vat = month_billable * (1 + config.vat_rate)
            text.append(f"\n                                           Billable  £{float(month_billable):>,.2f}      (£{float(month_with_vat):,.2f} inc VAT)")

        month_summary.update(text)

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
        text.append(f"                                    of max possible  {max_days:>6g}d\n")

        leave_line = f"                                              Leave  {leave_days:>6g}d\n"
        text.append(leave_line, style="dim" if year_leave == 0 else "")

        sick_line = f"                                               Sick  {sick_days:>6g}d\n"
        text.append(sick_line, style="dim" if year_sick == 0 else "")

        training_line = f"                                           Training  {training_days:>6g}d\n"
        text.append(training_line, style="dim" if training_days == 0 else "")

        ph_line = f"                                                P/H  {ph_days:>6g}d\n"
        text.append(ph_line, style="dim" if year_ph == 0 else "")

        text.append(f"                                              TOTAL  {total_days:>6g}d")

        if self.show_money:
            year_billable = year_worked * config.hourly_rate
            year_with_vat = year_billable * (1 + config.vat_rate)
            text.append(f"\n                                           Billable  £{float(year_billable):>,.2f}      (£{float(year_with_vat):,.2f} inc VAT)")

        year_summary.update(text)

    def _set_view_mode(self, mode: str):
        """Switch between view modes and toggle widget visibility."""
        self.view_mode = mode

        # Week view widgets
        week_widgets = ["#combined-header", "#week-table-container", "#week-earnings", "#weekly-summary"]
        # Month view widgets
        month_widgets = ["#month-header", "#month-table-container", "#month-summary"]
        # Year view widgets
        year_widgets = ["#year-header", "#year-table-container", "#year-summary"]

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

    def _update_bindings_for_mode(self, mode: str):
        """Update footer bindings based on view mode."""
        # Use refresh_bindings to trigger check_action re-evaluation
        self.refresh_bindings()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        """Check if an action is available based on current view mode."""
        if action == "week_view":
            return self.view_mode != "week"
        elif action == "month_view":
            return self.view_mode != "month"
        elif action == "year_view":
            return self.view_mode != "year"
        elif action == "populate_holidays":
            return self.view_mode == "year"
        elif action == "edit_day":
            return self.view_mode == "week"
        return True

    def action_prev_week(self):
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
        if self.view_mode == "month":
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

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle Enter/double-click on table row."""
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
        else:
            # In week view, selecting a day opens edit modal
            if event.row_key:
                selected_date = date.fromisoformat(str(event.row_key.value))
                entry = self._get_or_create_entry(selected_date)
                self.push_screen(EditDayScreen(entry), self._on_edit_complete)

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
        """Open edit modal for selected day (week view only)."""
        if self.view_mode != "week":
            return
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

def main():
    app = TimesheetApp()
    app.run()


if __name__ == "__main__":
    main()
