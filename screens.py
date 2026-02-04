"""Modal screens for the timesheet application."""

from __future__ import annotations

from datetime import date, time, timedelta

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.widgets import Button, Checkbox, DataTable, Input, Label
from textual.screen import ModalScreen

from models import Ticket, TimeEntry
import storage


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
        width: 70;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
    }

    #edit-title {
        width: 100%;
        text-align: center;
        text-style: bold;
        margin-bottom: 1;
    }

    .field-group {
        width: 1fr;
        height: auto;
        margin: 0 1 0 0;
    }

    .field-group:last-of-type {
        margin-right: 0;
    }

    .field-label {
        height: 1;
        margin-bottom: 0;
        color: $text-muted;
    }

    .field-row {
        width: 100%;
        height: auto;
        margin-bottom: 1;
    }

    .field-row Input {
        width: 100%;
    }

    /* Make comment field wider */
    #comment-group {
        width: 2fr;
    }

    #edit-buttons {
        width: 100%;
        height: auto;
        margin-top: 1;
        align: center middle;
    }

    #edit-buttons Button {
        width: auto;
        min-width: 12;
        margin: 0 2;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    # Field order for Enter key navigation
    FIELD_ORDER = ["clock-in", "lunch", "clock-out", "adjustment", "adjust-type", "comment"]

    def __init__(self, entry: TimeEntry):
        super().__init__()
        self.entry = entry

    def compose(self) -> ComposeResult:
        with Vertical(id="edit-dialog"):
            yield Label(
                f"Edit {self.entry.day_of_week} {self.entry.date.strftime('%b %d, %Y')}",
                id="edit-title"
            )

            # Row 1: Clock In, Lunch, Clock Out
            with Horizontal(classes="field-row"):
                with Vertical(classes="field-group"):
                    yield Label("In (HH:MM)", classes="field-label")
                    yield Input(
                        value=self.entry.clock_in.strftime("%H:%M") if self.entry.clock_in else "",
                        placeholder="09:00",
                        id="clock-in"
                    )
                with Vertical(classes="field-group"):
                    yield Label("Lunch (m)", classes="field-label")
                    yield Input(
                        value=str(int(self.entry.lunch_duration.total_seconds() // 60)) if self.entry.lunch_duration else "",
                        placeholder="30",
                        id="lunch"
                    )
                with Vertical(classes="field-group"):
                    yield Label("Out (HH:MM)", classes="field-label")
                    yield Input(
                        value=self.entry.clock_out.strftime("%H:%M") if self.entry.clock_out else "",
                        placeholder="17:30",
                        id="clock-out"
                    )

            # Row 2: Adjustment, Type, Comment
            with Horizontal(classes="field-row"):
                with Vertical(classes="field-group"):
                    yield Label("Adjust (h)", classes="field-label")
                    yield Input(
                        value=str(self.entry.adjusted_hours) if self.entry.adjustment else "",
                        placeholder="0",
                        id="adjustment"
                    )
                with Vertical(classes="field-group"):
                    yield Label("Type (L/S/T/P)", classes="field-label")
                    yield Input(
                        value=self.entry.adjust_type or "",
                        placeholder="L/S/T/P",
                        id="adjust-type",
                        max_length=1,
                    )
                with Vertical(classes="field-group", id="comment-group"):
                    yield Label("Comment", classes="field-label")
                    yield Input(
                        value=self.entry.comment or "",
                        placeholder="",
                        id="comment"
                    )

            # Buttons
            with Horizontal(id="edit-buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", variant="default", id="cancel")

    def on_mount(self) -> None:
        """Focus the first field on mount."""
        self.query_one("#clock-in", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Move to next field on Enter, or save if on last field."""
        current_id = event.input.id
        if current_id in self.FIELD_ORDER:
            current_idx = self.FIELD_ORDER.index(current_id)
            if current_idx < len(self.FIELD_ORDER) - 1:
                # Move to next field
                next_id = self.FIELD_ORDER[current_idx + 1]
                self.query_one(f"#{next_id}", Input).focus()
            else:
                # Last field - save
                self._save_entry()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Auto-uppercase the adjust type field and auto-advance."""
        if event.input.id == "adjust-type":
            val = event.value.upper()
            if val != event.value:
                event.input.value = val
            # Auto-advance to comment if valid type entered
            if val in ("L", "S", "T", "P"):
                self.query_one("#comment", Input).focus()

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

        adjust_type_val = self.query_one("#adjust-type", Input).value.strip().upper()
        adjust_type = adjust_type_val if adjust_type_val in ("L", "S", "T", "P") else None

        # Validate: if something was entered but it's not valid, show error
        if self.query_one("#adjust-type", Input).value.strip() and not adjust_type:
            self.app.notify("Invalid adjust type. Use L, S, T, or P", severity="error")
            return
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


class EditTicketScreen(ModalScreen[Ticket | None]):
    """Modal screen for creating or editing a ticket."""

    CSS = """
    EditTicketScreen {
        align: center middle;
    }

    #ticket-dialog {
        width: 60;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
    }

    #ticket-title {
        width: 100%;
        text-align: center;
        text-style: bold;
        margin-bottom: 1;
    }

    .field-group {
        width: 100%;
        height: auto;
        margin-bottom: 1;
    }

    .field-label {
        height: 1;
        margin-bottom: 0;
        color: $text-muted;
    }

    .field-group Input {
        width: 100%;
    }

    #ticket-buttons {
        width: 100%;
        height: auto;
        margin-top: 1;
        align: center middle;
    }

    #ticket-buttons Button {
        width: auto;
        min-width: 12;
        margin: 0 2;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, ticket: Ticket | None = None):
        super().__init__()
        self.ticket = ticket  # None means creating new

    def compose(self) -> ComposeResult:
        title = "Edit Ticket" if self.ticket else "New Ticket"
        with Vertical(id="ticket-dialog"):
            yield Label(title, id="ticket-title")

            with Vertical(classes="field-group"):
                yield Label("ID (max 8 chars)", classes="field-label")
                yield Input(
                    value=self.ticket.id if self.ticket else "",
                    placeholder="PROJ-123",
                    id="ticket-id",
                    max_length=8,
                    disabled=self.ticket is not None,  # Can't change ID of existing
                )

            with Vertical(classes="field-group"):
                yield Label("Description", classes="field-label")
                yield Input(
                    value=self.ticket.description if self.ticket else "",
                    placeholder="Description of the ticket",
                    id="ticket-description",
                )

            with Horizontal(id="ticket-buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", variant="default", id="cancel")

    def on_mount(self) -> None:
        """Focus the appropriate field on mount."""
        if self.ticket:
            self.query_one("#ticket-description", Input).focus()
        else:
            self.query_one("#ticket-id", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Move to next field or save."""
        if event.input.id == "ticket-id":
            self.query_one("#ticket-description", Input).focus()
        elif event.input.id == "ticket-description":
            self._save_ticket()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "save":
            self._save_ticket()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _save_ticket(self) -> None:
        ticket_id = self.query_one("#ticket-id", Input).value.strip().upper()
        description = self.query_one("#ticket-description", Input).value.strip()

        if not ticket_id:
            self.app.notify("Ticket ID is required", severity="error")
            return

        if not description:
            self.app.notify("Description is required", severity="error")
            return

        if len(ticket_id) > 8:
            self.app.notify("Ticket ID must be 8 characters or less", severity="error")
            return

        # Check for duplicate ID when creating new
        if not self.ticket and storage.get_ticket(ticket_id):
            self.app.notify(f"Ticket {ticket_id} already exists", severity="error")
            return

        ticket = Ticket(
            id=ticket_id,
            description=description,
            archived=self.ticket.archived if self.ticket else False,
            created_at=self.ticket.created_at if self.ticket else date.today(),
        )
        self.dismiss(ticket)


class TicketManagementScreen(ModalScreen[None]):
    """Modal screen for managing tickets."""

    CSS = """
    TicketManagementScreen {
        align: center middle;
    }

    #tickets-dialog {
        width: 80;
        height: 24;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
    }

    #tickets-title {
        width: 100%;
        text-align: center;
        text-style: bold;
        margin-bottom: 1;
    }

    #tickets-controls {
        width: 100%;
        height: auto;
        margin-bottom: 1;
    }

    #tickets-search {
        width: 1fr;
    }

    #tickets-show-archived {
        width: auto;
        margin-left: 2;
    }

    #tickets-table {
        height: 1fr;
    }

    #tickets-footer {
        width: 100%;
        height: auto;
        margin-top: 1;
        align: center middle;
    }

    #tickets-footer Button {
        width: auto;
        min-width: 10;
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "close", "Close"),
        Binding("n", "new_ticket", "New"),
        Binding("e", "edit_ticket", "Edit"),
        Binding("a", "toggle_archive", "Archive"),
        Binding("d", "delete_ticket", "Delete"),
    ]

    def __init__(self):
        super().__init__()
        self.show_archived = False

    def compose(self) -> ComposeResult:
        with Vertical(id="tickets-dialog"):
            yield Label("Ticket Management", id="tickets-title")

            with Horizontal(id="tickets-controls"):
                yield Input(placeholder="Search...", id="tickets-search")
                yield Checkbox("Show archived", id="tickets-show-archived")

            yield DataTable(id="tickets-table")

            with Horizontal(id="tickets-footer"):
                yield Button("New [n]", id="btn-new")
                yield Button("Edit [e]", id="btn-edit")
                yield Button("Archive [a]", id="btn-archive")
                yield Button("Delete [d]", id="btn-delete")
                yield Button("Close [Esc]", id="btn-close")

    def on_mount(self) -> None:
        """Set up the table and load data."""
        table = self.query_one("#tickets-table", DataTable)
        table.cursor_type = "row"
        table.add_column("ID", width=10)
        table.add_column("Description", width=40)
        table.add_column("Status", width=10)
        self._refresh_table()
        self.query_one("#tickets-search", Input).focus()

    def _refresh_table(self, search: str = "") -> None:
        """Refresh the ticket table."""
        table = self.query_one("#tickets-table", DataTable)
        table.clear()

        if search:
            tickets = storage.search_tickets(search, include_archived=self.show_archived)
        else:
            tickets = storage.get_all_tickets(include_archived=self.show_archived)

        for ticket in tickets:
            status = "Archived" if ticket.archived else "Active"
            table.add_row(
                ticket.id,
                ticket.description[:40],
                status,
                key=ticket.id,
            )

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter tickets as user types."""
        if event.input.id == "tickets-search":
            self._refresh_table(event.value)

    def on_key(self, event) -> None:
        """Handle key events for navigation."""
        # Move to table on down arrow or enter from search input
        if event.key in ("down", "enter"):
            search_input = self.query_one("#tickets-search", Input)
            if search_input.has_focus:
                self.query_one("#tickets-table", DataTable).focus()
                event.prevent_default()
                event.stop()

    def on_checkbox_changed(self, event: Checkbox.Changed) -> None:
        """Toggle show archived."""
        if event.checkbox.id == "tickets-show-archived":
            self.show_archived = event.value
            search = self.query_one("#tickets-search", Input).value
            self._refresh_table(search)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle Enter on table row - edit the ticket."""
        if event.control.id == "tickets-table":
            self.action_edit_ticket()

    def _get_selected_ticket_id(self) -> str | None:
        """Get the currently selected ticket ID."""
        table = self.query_one("#tickets-table", DataTable)
        if table.row_count == 0:
            return None
        row_key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key
        return str(row_key.value) if row_key else None

    def action_close(self) -> None:
        self.dismiss(None)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses."""
        button_id = event.button.id
        if button_id == "btn-new":
            self.action_new_ticket()
        elif button_id == "btn-edit":
            self.action_edit_ticket()
        elif button_id == "btn-archive":
            self.action_toggle_archive()
        elif button_id == "btn-delete":
            self.action_delete_ticket()
        elif button_id == "btn-close":
            self.action_close()

    def action_new_ticket(self) -> None:
        """Create a new ticket."""
        self.app.push_screen(EditTicketScreen(), self._on_ticket_edited)

    def action_edit_ticket(self) -> None:
        """Edit the selected ticket."""
        ticket_id = self._get_selected_ticket_id()
        if not ticket_id:
            self.app.notify("No ticket selected", severity="warning")
            return

        ticket = storage.get_ticket(ticket_id)
        if ticket:
            self.app.push_screen(EditTicketScreen(ticket), self._on_ticket_edited)

    def _on_ticket_edited(self, result: Ticket | None) -> None:
        """Handle ticket edit result."""
        if result:
            storage.save_ticket(result)
            self.app.notify(f"Ticket {result.id} saved")
            search = self.query_one("#tickets-search", Input).value
            self._refresh_table(search)

    def action_toggle_archive(self) -> None:
        """Archive or unarchive the selected ticket."""
        ticket_id = self._get_selected_ticket_id()
        if not ticket_id:
            self.app.notify("No ticket selected", severity="warning")
            return

        ticket = storage.get_ticket(ticket_id)
        if ticket:
            if ticket.archived:
                storage.unarchive_ticket(ticket_id)
                self.app.notify(f"Ticket {ticket_id} unarchived")
            else:
                storage.archive_ticket(ticket_id)
                self.app.notify(f"Ticket {ticket_id} archived")
            search = self.query_one("#tickets-search", Input).value
            self._refresh_table(search)

    def action_delete_ticket(self) -> None:
        """Delete the selected ticket."""
        ticket_id = self._get_selected_ticket_id()
        if not ticket_id:
            self.app.notify("No ticket selected", severity="warning")
            return

        if not storage.can_delete_ticket(ticket_id):
            self.app.notify(
                f"Cannot delete {ticket_id}: has time allocations",
                severity="error"
            )
            return

        self.app.push_screen(
            ConfirmScreen(f"Delete ticket {ticket_id}?"),
            self._on_delete_confirmed
        )

    def _on_delete_confirmed(self, confirmed: bool | None) -> None:
        """Handle delete confirmation."""
        if confirmed:
            ticket_id = self._get_selected_ticket_id()
            if ticket_id and storage.delete_ticket(ticket_id):
                self.app.notify(f"Ticket {ticket_id} deleted")
                search = self.query_one("#tickets-search", Input).value
                self._refresh_table(search)


class TicketSelectScreen(ModalScreen[Ticket | None]):
    """Modal screen for selecting a ticket with search."""

    CSS = """
    TicketSelectScreen {
        align: center middle;
    }

    #select-dialog {
        width: 70;
        height: 20;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
    }

    #select-title {
        width: 100%;
        text-align: center;
        text-style: bold;
        margin-bottom: 1;
    }

    #select-search {
        width: 100%;
        margin-bottom: 1;
    }

    #select-table {
        height: 1fr;
    }

    #select-footer {
        width: 100%;
        height: auto;
        margin-top: 1;
        align: center middle;
    }

    #select-footer Button {
        width: auto;
        min-width: 12;
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
        Binding("n", "new_ticket", "New"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="select-dialog"):
            yield Label("Select Ticket", id="select-title")
            yield Input(placeholder="Search...", id="select-search")
            yield DataTable(id="select-table")
            with Horizontal(id="select-footer"):
                yield Button("Select [Enter]", id="btn-select", variant="primary")
                yield Button("New [n]", id="btn-new")
                yield Button("Cancel [Esc]", id="btn-cancel")

    def on_mount(self) -> None:
        """Set up the table and load data."""
        table = self.query_one("#select-table", DataTable)
        table.cursor_type = "row"
        table.add_column("ID", width=10)
        table.add_column("Description", width=50)
        self._refresh_table()
        self.query_one("#select-search", Input).focus()

    def _refresh_table(self, search: str = "") -> None:
        """Refresh the ticket table."""
        table = self.query_one("#select-table", DataTable)
        table.clear()

        if search:
            tickets = storage.search_tickets(search, include_archived=False)
        else:
            tickets = storage.get_all_tickets(include_archived=False)

        for ticket in tickets:
            table.add_row(
                ticket.id,
                ticket.description[:50],
                key=ticket.id,
            )

    def on_input_changed(self, event: Input.Changed) -> None:
        """Filter tickets as user types."""
        if event.input.id == "select-search":
            self._refresh_table(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Move focus to table on Enter in search."""
        if event.input.id == "select-search":
            self.query_one("#select-table", DataTable).focus()

    def on_key(self, event) -> None:
        """Handle key events for navigation."""
        # Move to table on down arrow from search input
        if event.key == "down":
            search_input = self.query_one("#select-search", Input)
            if search_input.has_focus:
                self.query_one("#select-table", DataTable).focus()
                event.prevent_default()
                event.stop()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Select the ticket when row is activated."""
        if event.row_key:
            ticket_id = str(event.row_key.value)
            ticket = storage.get_ticket(ticket_id)
            self.dismiss(ticket)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses."""
        button_id = event.button.id
        if button_id == "btn-select":
            self._select_current_ticket()
        elif button_id == "btn-new":
            self.action_new_ticket()
        elif button_id == "btn-cancel":
            self.action_cancel()

    def _select_current_ticket(self) -> None:
        """Select the currently highlighted ticket."""
        table = self.query_one("#select-table", DataTable)
        if table.row_count == 0:
            self.app.notify("No ticket selected", severity="warning")
            return
        row_key = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0)).row_key
        if row_key:
            ticket_id = str(row_key.value)
            ticket = storage.get_ticket(ticket_id)
            self.dismiss(ticket)

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_new_ticket(self) -> None:
        """Create a new ticket inline."""
        self.app.push_screen(EditTicketScreen(), self._on_ticket_created)

    def _on_ticket_created(self, result: Ticket | None) -> None:
        """Handle new ticket creation."""
        if result:
            storage.save_ticket(result)
            self.app.notify(f"Ticket {result.id} created")
            # Select the newly created ticket
            self.dismiss(result)


class EditAllocationScreen(ModalScreen[tuple[str, str] | None]):
    """Modal screen for editing hours allocated to a ticket.

    Returns (ticket_id, hours_string) or None if cancelled.
    """

    CSS = """
    EditAllocationScreen {
        align: center middle;
    }

    #alloc-dialog {
        width: 52;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
    }

    #alloc-title {
        width: 100%;
        text-align: center;
        text-style: bold;
        margin-bottom: 1;
    }

    #alloc-ticket-info {
        width: 100%;
        margin-bottom: 1;
    }

    .field-group {
        width: 100%;
        height: auto;
        margin-bottom: 1;
    }

    .field-label {
        height: 1;
        margin-bottom: 0;
        color: $text-muted;
    }

    .field-group Input {
        width: 100%;
    }

    #alloc-remaining {
        width: 100%;
        margin-bottom: 1;
        color: $text-muted;
    }

    #alloc-buttons {
        width: 100%;
        height: auto;
        margin-top: 1;
        align: center middle;
    }

    #alloc-buttons Button {
        width: auto;
        min-width: 12;
        margin: 0 2;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(
        self,
        ticket: Ticket,
        current_hours: str = "",
        remaining_hours: str = "",
    ):
        super().__init__()
        self.ticket = ticket
        self.current_hours = current_hours
        self.remaining_hours = remaining_hours

    def compose(self) -> ComposeResult:
        title = "Edit Allocation" if self.current_hours else "Add Allocation"
        with Vertical(id="alloc-dialog"):
            yield Label(title, id="alloc-title")
            yield Label(
                f"{self.ticket.id}: {self.ticket.description}",
                id="alloc-ticket-info"
            )

            with Vertical(classes="field-group"):
                yield Label("Hours", classes="field-label")
                yield Input(
                    value=self.current_hours,
                    placeholder="0.00",
                    id="alloc-hours",
                )

            if self.remaining_hours:
                yield Label(
                    f"Remaining to allocate: {self.remaining_hours}h",
                    id="alloc-remaining"
                )

            with Horizontal(id="alloc-buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", variant="default", id="cancel")

    def on_mount(self) -> None:
        """Focus the hours input."""
        self.query_one("#alloc-hours", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Save on Enter."""
        if event.input.id == "alloc-hours":
            self._save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "save":
            self._save()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _save(self) -> None:
        hours_str = self.query_one("#alloc-hours", Input).value.strip()

        if not hours_str:
            self.app.notify("Hours is required", severity="error")
            return

        try:
            hours = float(hours_str)
            if hours < 0:
                self.app.notify("Hours must be positive", severity="error")
                return
        except ValueError:
            self.app.notify("Invalid hours value", severity="error")
            return

        self.dismiss((self.ticket.id, hours_str))
