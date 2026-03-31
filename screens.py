"""Modal screens for the timesheet application."""

from __future__ import annotations

from datetime import date, time, timedelta

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
from textual.widgets import Button, Checkbox, DataTable, Input, Label, TextArea
from textual.screen import ModalScreen

from models import Ticket, TicketAllocation, TimeEntry
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
        max-height: 85%;
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


class DeliverableSelectScreen(ModalScreen[str | None | bool]):
    """Modal screen to select a deliverable.

    Returns:
        str: The selected deliverable ID
        None: Clear the deliverable
        False: Cancelled (no change)
    """

    CSS = """
    DeliverableSelectScreen {
        align: center middle;
    }

    #del-select-dialog {
        width: 80;
        height: 80%;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
    }

    #del-select-title {
        width: 100%;
        text-align: center;
        text-style: bold;
        margin-bottom: 1;
    }

    #del-select-table {
        height: 1fr;
    }

    #del-select-buttons {
        height: 3;
        margin-top: 1;
        align: center middle;
    }

    #del-select-buttons Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, current_id: str | None = None) -> None:
        super().__init__()
        self.current_id = current_id

    def compose(self) -> ComposeResult:
        with Vertical(id="del-select-dialog"):
            yield Label("Select Deliverable", id="del-select-title")
            yield DataTable(id="del-select-table")
            with Horizontal(id="del-select-buttons"):
                yield Button("Clear", variant="warning", id="clear-btn")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        table = self.query_one("#del-select-table", DataTable)
        table.cursor_type = "row"
        table.add_column("ID", width=10, key="id")
        table.add_column("Title", width=35, key="title")
        table.add_column("Work Package", width=28, key="wp")
        self._refresh_table()

    def _refresh_table(self) -> None:
        table = self.query_one("#del-select-table", DataTable)
        table.clear()

        deliverables = storage.get_all_deliverables(active_only=True)
        work_packages = {wp.id: wp.title for wp in storage.get_all_work_packages()}

        highlight_row: int | None = None
        for i, d in enumerate(deliverables):
            wp_label = f"{d.work_package_id}: {work_packages.get(d.work_package_id, '')}"
            table.add_row(d.id, d.title, wp_label, key=d.id)
            if d.id == self.current_id:
                highlight_row = i

        if highlight_row is not None:
            table.move_cursor(row=highlight_row)
        table.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.control.id == "del-select-table" and event.row_key:
            self.dismiss(str(event.row_key.value))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(False)
        elif event.button.id == "clear-btn":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(False)


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

    #deliverable-row {
        width: 100%;
        height: 3;
        margin-bottom: 1;
    }

    #deliverable-label {
        width: 1fr;
        height: 3;
        padding: 1 1;
        background: $boost;
    }

    #pick-deliverable {
        width: auto;
        min-width: 10;
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
        self._deliverable_id: str | None = (
            ticket.deliverable_id if ticket else None
        )

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

            with Vertical(classes="field-group"):
                yield Label("Deliverable", classes="field-label")
                with Horizontal(id="deliverable-row"):
                    yield Label(
                        self._format_deliverable_label(),
                        id="deliverable-label",
                    )
                    yield Button("Pick", id="pick-deliverable")

            with Horizontal(id="ticket-buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", variant="default", id="cancel")

    def _format_deliverable_label(self) -> str:
        """Format the deliverable display label."""
        if not self._deliverable_id:
            return "None"
        d = storage.get_deliverable(self._deliverable_id)
        if d:
            return f"{d.id}: {d.title}"
        return self._deliverable_id

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
        elif event.button.id == "pick-deliverable":
            self._pick_deliverable()

    def _pick_deliverable(self) -> None:
        """Open the deliverable select screen."""
        def handle_result(result: str | None | bool) -> None:
            if result is False:
                return  # Cancelled
            # result is None (clear) or a deliverable ID string
            self._deliverable_id = result if isinstance(result, str) else None
            self.query_one("#deliverable-label", Label).update(
                self._format_deliverable_label()
            )

        self.app.push_screen(
            DeliverableSelectScreen(self._deliverable_id), handle_result,
        )

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
            points_entered=self.ticket.points_entered if self.ticket else False,
            deliverable_id=self._deliverable_id,
        )
        self.dismiss(ticket)


class TicketManagementScreen(ModalScreen[None]):
    """Modal screen for managing tickets."""

    CSS = """
    TicketManagementScreen {
        align: center middle;
    }

    #tickets-dialog {
        width: 95;
        height: 85%;
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
        Binding("escape", "close", "Dismiss"),
        Binding("n", "new_ticket", "New"),
        Binding("e", "edit_ticket", "Edit"),
        Binding("a", "toggle_archive", "Close Ticket"),
        Binding("p", "toggle_points_entered", "Pts Entered"),
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
                yield Checkbox("Show closed", id="tickets-show-archived")

            yield DataTable(id="tickets-table")

            with Horizontal(id="tickets-footer"):
                yield Button("New [n]", id="btn-new")
                yield Button("Edit [e]", id="btn-edit")
                yield Button("Close Ticket [a]", id="btn-archive")
                yield Button("Pts Entered [p]", id="btn-pts-entered")
                yield Button("Delete [d]", id="btn-delete")
                yield Button("Dismiss [Esc]", id="btn-close")

    def on_mount(self) -> None:
        """Set up the table and load data."""
        table = self.query_one("#tickets-table", DataTable)
        table.cursor_type = "row"
        table.add_column("ID", width=10)
        table.add_column("Description", width=35)
        table.add_column("Deliverable", width=12)
        table.add_column("Status", width=10)
        table.add_column("Pts", width=4)
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
            status = "Closed" if ticket.archived else "Open"
            pts_entered = "Y" if ticket.points_entered else ""
            del_label = ticket.deliverable_id if ticket.deliverable_id else "!"
            table.add_row(
                ticket.id,
                ticket.description[:35],
                del_label,
                status,
                pts_entered,
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
        elif button_id == "btn-pts-entered":
            self.action_toggle_points_entered()
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
        """Close or reopen the selected ticket."""
        ticket_id = self._get_selected_ticket_id()
        if not ticket_id:
            self.app.notify("No ticket selected", severity="warning")
            return

        ticket = storage.get_ticket(ticket_id)
        if ticket:
            if ticket.archived:
                storage.unarchive_ticket(ticket_id)
                self.app.notify(f"Ticket {ticket_id} reopened")
            else:
                storage.archive_ticket(ticket_id)
                self.app.notify(f"Ticket {ticket_id} closed")
            search = self.query_one("#tickets-search", Input).value
            self._refresh_table(search)

    def action_toggle_points_entered(self) -> None:
        """Toggle whether points have been entered in Jira for the selected ticket."""
        ticket_id = self._get_selected_ticket_id()
        if not ticket_id:
            self.app.notify("No ticket selected", severity="warning")
            return

        ticket = storage.get_ticket(ticket_id)
        if ticket:
            new_state = not ticket.points_entered
            storage.set_points_entered(ticket_id, new_state)
            status = "entered" if new_state else "not entered"
            self.app.notify(f"Ticket {ticket_id}: points {status}")
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


class DeliverableManagementScreen(ModalScreen[None]):
    """Modal screen for managing work packages and deliverables."""

    CSS = """
    DeliverableManagementScreen {
        align: center middle;
    }

    #del-mgmt-dialog {
        width: 90;
        height: 85%;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
    }

    #del-mgmt-title {
        width: 100%;
        text-align: center;
        text-style: bold;
        margin-bottom: 1;
    }

    #del-mgmt-table {
        height: 1fr;
    }

    #del-mgmt-footer {
        width: 100%;
        height: auto;
        margin-top: 1;
        align: center middle;
    }

    #del-mgmt-footer Button {
        width: auto;
        min-width: 10;
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "close", "Dismiss"),
        Binding("w", "new_work_package", "New WP"),
        Binding("n", "new_deliverable", "New Del"),
        Binding("e", "edit_item", "Edit"),
        Binding("a", "toggle_active", "Active"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="del-mgmt-dialog"):
            yield Label("Deliverable Management", id="del-mgmt-title")
            yield DataTable(id="del-mgmt-table")
            with Horizontal(id="del-mgmt-footer"):
                yield Button("New WP [w]", id="btn-new-wp")
                yield Button("New Del [n]", id="btn-new-del")
                yield Button("Edit [e]", id="btn-edit")
                yield Button("Toggle Active [a]", id="btn-toggle")
                yield Button("Dismiss [Esc]", id="btn-close")

    def on_mount(self) -> None:
        table = self.query_one("#del-mgmt-table", DataTable)
        table.cursor_type = "row"
        table.add_column("Type", width=5)
        table.add_column("ID", width=10)
        table.add_column("Title", width=45)
        table.add_column("Active", width=7)
        self._refresh_table()

    def _refresh_table(self) -> None:
        table = self.query_one("#del-mgmt-table", DataTable)
        table.clear()

        work_packages = storage.get_all_work_packages()
        for wp in work_packages:
            table.add_row("WP", wp.id, wp.title, "", key=f"wp:{wp.id}")
            deliverables = storage.get_deliverables_for_work_package(
                wp.id, active_only=False,
            )
            for d in deliverables:
                active = "Yes" if d.active else "No"
                table.add_row("  Del", d.id, f"  {d.title}", active, key=f"del:{d.id}")

    def _get_selected_key(self) -> tuple[str, str] | None:
        """Get the type and ID of the selected row. Returns ('wp', id) or ('del', id)."""
        table = self.query_one("#del-mgmt-table", DataTable)
        if table.row_count == 0:
            return None
        row_key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key
        key_str = str(row_key.value)
        if key_str.startswith("wp:"):
            return ("wp", key_str[3:])
        if key_str.startswith("del:"):
            return ("del", key_str[4:])
        return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle Enter on table row - edit the item."""
        if event.control.id == "del-mgmt-table":
            self.action_edit_item()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-close":
            self.dismiss(None)
        elif event.button.id == "btn-new-wp":
            self.action_new_work_package()
        elif event.button.id == "btn-new-del":
            self.action_new_deliverable()
        elif event.button.id == "btn-edit":
            self.action_edit_item()
        elif event.button.id == "btn-toggle":
            self.action_toggle_active()

    def action_close(self) -> None:
        self.dismiss(None)

    def action_new_work_package(self) -> None:
        """Create a new work package."""
        self.app.push_screen(
            EditWorkPackageScreen(), self._on_wp_saved,
        )

    def _on_wp_saved(self, result: tuple[str, str] | None) -> None:
        if result:
            from models import WorkPackage
            wp = WorkPackage(id=result[0], title=result[1])
            storage.save_work_package(wp)
            self.app.notify(f"Work package {wp.id} saved")
            self._refresh_table()

    def action_new_deliverable(self) -> None:
        """Create a new deliverable."""
        self.app.push_screen(
            EditDeliverableScreen(), self._on_del_saved,
        )

    def _on_del_saved(self, result: tuple[str, str, str] | None) -> None:
        if result:
            from models import Deliverable
            d = Deliverable(id=result[0], work_package_id=result[1], title=result[2])
            storage.save_deliverable(d)
            self.app.notify(f"Deliverable {d.id} saved")
            self._refresh_table()

    def action_edit_item(self) -> None:
        """Edit the selected work package or deliverable."""
        selected = self._get_selected_key()
        if not selected:
            return

        item_type, item_id = selected
        if item_type == "wp":
            wp = storage.get_work_package(item_id)
            if wp:
                self.app.push_screen(
                    EditWorkPackageScreen(wp.id, wp.title),
                    self._on_wp_saved,
                )
        elif item_type == "del":
            d = storage.get_deliverable(item_id)
            if d:
                self.app.push_screen(
                    EditDeliverableScreen(d.id, d.work_package_id, d.title),
                    self._on_del_saved,
                )

    def action_toggle_active(self) -> None:
        """Toggle active state of a deliverable."""
        selected = self._get_selected_key()
        if not selected or selected[0] != "del":
            self.app.notify("Select a deliverable to toggle", severity="warning")
            return

        d = storage.get_deliverable(selected[1])
        if d:
            d.active = not d.active
            storage.save_deliverable(d)
            self._refresh_table()


class EditWorkPackageScreen(ModalScreen[tuple[str, str] | None]):
    """Modal for editing a work package ID and title."""

    CSS = """
    EditWorkPackageScreen {
        align: center middle;
    }

    #wp-edit-dialog {
        width: 50;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
    }

    #wp-edit-dialog Input {
        width: 100%;
        margin-bottom: 1;
    }

    #wp-edit-buttons {
        height: 3;
        align: center middle;
    }

    #wp-edit-buttons Button {
        margin: 0 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(
        self, wp_id: str = "", title: str = "",
    ) -> None:
        super().__init__()
        self.wp_id = wp_id
        self.wp_title = title
        self.is_edit = bool(wp_id)

    def compose(self) -> ComposeResult:
        label = "Edit Work Package" if self.is_edit else "New Work Package"
        with Vertical(id="wp-edit-dialog"):
            yield Label(label)
            yield Label("ID (e.g. WP2a):", classes="field-label")
            yield Input(
                value=self.wp_id, id="wp-id",
                disabled=self.is_edit,
            )
            yield Label("Title:", classes="field-label")
            yield Input(value=self.wp_title, id="wp-title")
            with Horizontal(id="wp-edit-buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        if self.is_edit:
            self.query_one("#wp-title", Input).focus()
        else:
            self.query_one("#wp-id", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "wp-id":
            self.query_one("#wp-title", Input).focus()
        elif event.input.id == "wp-title":
            self._save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "save":
            self._save()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _save(self) -> None:
        wp_id = self.query_one("#wp-id", Input).value.strip()
        title = self.query_one("#wp-title", Input).value.strip()
        if not wp_id or not title:
            self.app.notify("Both fields are required", severity="error")
            return
        self.dismiss((wp_id, title))


class EditDeliverableScreen(ModalScreen[tuple[str, str, str] | None]):
    """Modal for editing a deliverable."""

    CSS = """
    EditDeliverableScreen {
        align: center middle;
    }

    #del-edit-dialog {
        width: 55;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
    }

    #del-edit-dialog Input {
        width: 100%;
        margin-bottom: 1;
    }

    #del-edit-buttons {
        height: 3;
        align: center middle;
    }

    #del-edit-buttons Button {
        margin: 0 1;
    }
    """

    BINDINGS = [Binding("escape", "cancel", "Cancel", show=False)]

    def __init__(
        self, del_id: str = "", wp_id: str = "", title: str = "",
    ) -> None:
        super().__init__()
        self.del_id = del_id
        self.del_wp_id = wp_id
        self.del_title = title
        self.is_edit = bool(del_id)

    def compose(self) -> ComposeResult:
        label = "Edit Deliverable" if self.is_edit else "New Deliverable"
        with Vertical(id="del-edit-dialog"):
            yield Label(label)
            yield Label("ID (e.g. WP2a-D1):", classes="field-label")
            yield Input(
                value=self.del_id, id="del-id",
                disabled=self.is_edit,
            )
            yield Label("Work Package ID:", classes="field-label")
            yield Input(
                value=self.del_wp_id, id="del-wp-id",
                disabled=self.is_edit,
            )
            yield Label("Title:", classes="field-label")
            yield Input(value=self.del_title, id="del-title")
            with Horizontal(id="del-edit-buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        if self.is_edit:
            self.query_one("#del-title", Input).focus()
        else:
            self.query_one("#del-id", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "del-id":
            self.query_one("#del-wp-id", Input).focus()
        elif event.input.id == "del-wp-id":
            self.query_one("#del-title", Input).focus()
        elif event.input.id == "del-title":
            self._save()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "save":
            self._save()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _save(self) -> None:
        del_id = self.query_one("#del-id", Input).value.strip()
        wp_id = self.query_one("#del-wp-id", Input).value.strip()
        title = self.query_one("#del-title", Input).value.strip()
        if not del_id or not wp_id or not title:
            self.app.notify("All fields are required", severity="error")
            return
        # Validate work package exists
        if not storage.get_work_package(wp_id):
            self.app.notify(f"Work package {wp_id} does not exist", severity="error")
            return
        self.dismiss((del_id, wp_id, title))


class TicketSelectScreen(ModalScreen[Ticket | None]):
    """Modal screen for selecting a ticket with search."""

    CSS = """
    TicketSelectScreen {
        align: center middle;
    }

    #select-dialog {
        width: 70;
        height: 80%;
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


class EditAllocationScreen(ModalScreen[tuple[str, str, str] | None]):
    """Modal screen for editing hours allocated to a ticket.

    Returns (ticket_id, hours_string, description) or None if cancelled.
    """

    CSS = """
    EditAllocationScreen {
        align: center middle;
    }

    #alloc-dialog {
        width: 70%;
        max-width: 90;
        height: auto;
        max-height: 75%;
        min-height: 16;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
        overflow-y: auto;
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

    #desc-group {
        height: auto;
        max-height: 100%;
    }

    .field-label {
        height: 1;
        margin-bottom: 0;
        color: $text-muted;
    }

    .field-group Input {
        width: 100%;
    }

    #alloc-description {
        width: 100%;
        height: auto;
        min-height: 3;
        max-height: 20;
        border: tall $primary;
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
        current_description: str = "",
    ):
        super().__init__()
        self.ticket = ticket
        self.current_hours = current_hours
        self.remaining_hours = remaining_hours
        self.current_description = current_description

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

            with Vertical(classes="field-group", id="desc-group"):
                yield Label("Description", classes="field-label")
                yield TextArea(
                    self.current_description,
                    id="alloc-description",
                )

            with Horizontal(id="alloc-buttons"):
                yield Button("Save", variant="primary", id="save")
                yield Button("Cancel", variant="default", id="cancel")

    def on_mount(self) -> None:
        """Focus the hours input."""
        self.query_one("#alloc-hours", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Move to description on Enter in hours field."""
        if event.input.id == "alloc-hours":
            self.query_one("#alloc-description", TextArea).focus()

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

        description = self.query_one("#alloc-description", TextArea).text.strip()
        self.dismiss((self.ticket.id, hours_str, description))


class MoveAllocationScreen(ModalScreen[date | None]):
    """Modal screen for picking a target day to move an allocation to.

    Returns the target date or None if cancelled.
    """

    CSS = """
    MoveAllocationScreen {
        align: center middle;
    }

    #move-dialog {
        width: 50;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
    }

    #move-title {
        width: 100%;
        text-align: center;
        text-style: bold;
        margin-bottom: 1;
    }

    #move-info {
        width: 100%;
        margin-bottom: 1;
    }

    #move-day {
        width: 100%;
        margin-bottom: 1;
    }

    #move-buttons {
        width: 100%;
        height: auto;
        margin-top: 1;
        align: center middle;
    }

    #move-buttons Button {
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
        ticket_id: str,
        source_date: date,
        hours: str,
        year: int,
        month: int,
    ) -> None:
        super().__init__()
        self.ticket_id = ticket_id
        self.source_date = source_date
        self.hours = hours
        self.year = year
        self.month = month

    def compose(self) -> ComposeResult:
        source_str = self.source_date.strftime("%a %d %b")
        with Vertical(id="move-dialog"):
            yield Label("Move Allocation", id="move-title")
            yield Label(
                f"{self.ticket_id} ({self.hours}h) on {source_str}",
                id="move-info",
            )
            yield Label("Move to day (1\u201331):")
            yield Input(
                id="move-day",
                placeholder="Day number",
                type="integer",
            )
            with Horizontal(id="move-buttons"):
                yield Button("Move", variant="primary", id="move-btn")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#move-day", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "move-day":
            self._move()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "move-btn":
            self._move()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _move(self) -> None:
        day_str = self.query_one("#move-day", Input).value.strip()
        if not day_str:
            self.app.notify("Enter a day number", severity="error")
            return

        from calendar import monthrange

        try:
            day = int(day_str)
            num_days = monthrange(self.year, self.month)[1]
            if day < 1 or day > num_days:
                self.app.notify(
                    f"Day must be between 1 and {num_days}", severity="error",
                )
                return
        except ValueError:
            self.app.notify("Invalid day number", severity="error")
            return

        target = date(self.year, self.month, day)
        if target == self.source_date:
            self.app.notify("That's the same day", severity="warning")
            return

        self.dismiss(target)


class ExportAllocationsScreen(ModalScreen[str | None]):
    """Modal to configure and export allocations report to a text file."""

    CSS = """
    ExportAllocationsScreen {
        align: center middle;
    }

    #export-dialog {
        width: 70;
        height: auto;
        padding: 1 2;
        background: $surface;
        border: thick $primary;
    }

    #export-dialog Label {
        margin-bottom: 1;
    }

    #export-month, #export-path {
        width: 100%;
        margin-bottom: 1;
    }

    #export-buttons {
        margin-top: 1;
        height: 3;
        align: center middle;
    }

    #export-buttons Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, year: int, month: int) -> None:
        super().__init__()
        self.initial_year = year
        self.initial_month = month

    def compose(self) -> ComposeResult:
        from pathlib import Path

        default_path = str(
            Path.home()
            / f"time-allocations-{self.initial_year}-{self.initial_month:02d}.txt"
        )
        with Vertical(id="export-dialog"):
            yield Label("Export Allocations Report")
            yield Label("Month (YYYY-MM):")
            yield Input(
                id="export-month",
                value=f"{self.initial_year}-{self.initial_month:02d}",
                placeholder="YYYY-MM",
            )
            yield Label("Output file:")
            yield Input(
                id="export-path",
                value=default_path,
            )
            with Horizontal(id="export-buttons"):
                yield Button("Export", variant="primary", id="export-btn")
                yield Button("Cancel", id="cancel")

    def on_mount(self) -> None:
        self.query_one("#export-month", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        """Update default path when month changes."""
        if event.input.id == "export-month":
            from pathlib import Path

            month_str = event.value.strip()
            try:
                parts = month_str.split("-")
                year = int(parts[0])
                month = int(parts[1])
                if 1 <= month <= 12 and 1900 <= year <= 2100:
                    path_input = self.query_one("#export-path", Input)
                    current = path_input.value
                    # Only update if it looks like the default pattern
                    if "time-allocations-" in current:
                        path_input.value = str(
                            Path.home()
                            / f"time-allocations-{year}-{month:02d}.txt"
                        )
            except (ValueError, IndexError):
                pass

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "export-month":
            self.query_one("#export-path", Input).focus()
        elif event.input.id == "export-path":
            self._export()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
        elif event.button.id == "export-btn":
            self._export()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def _export(self) -> None:
        month_str = self.query_one("#export-month", Input).value.strip()
        output_path = self.query_one("#export-path", Input).value.strip()

        if not month_str or not output_path:
            self.app.notify("Both fields are required", severity="error")
            return

        try:
            parts = month_str.split("-")
            year = int(parts[0])
            month = int(parts[1])
            if month < 1 or month > 12:
                raise ValueError
        except (ValueError, IndexError):
            self.app.notify("Invalid month format (use YYYY-MM)", severity="error")
            return

        from pathlib import Path

        output = Path(output_path).expanduser()

        from calendar import monthrange
        from itertools import groupby

        from decimal import Decimal

        adjust_type_labels = {
            "L": "Leave", "S": "Sick", "T": "Training", "P": "Public Holiday",
        }

        def fmt_hours(h: Decimal) -> str:
            """Format hours concisely, e.g. '3h', '1.5h'."""
            return f"{h.normalize()}h"

        allocations = storage.get_allocations_for_month(year, month)
        entries = storage.get_month_entries(year, month)

        # Index entries by date
        entries_by_date: dict[date, TimeEntry] = {e.date: e for e in entries}

        # Index allocations by date
        allocs_by_date: dict[date, list[TicketAllocation]] = {}
        for d, group in groupby(allocations, key=lambda a: a.date):
            allocs_by_date[d] = list(group)

        # Build ticket description lookup
        ticket_ids = {a.ticket_id for a in allocations}
        tickets: dict[str, str] = {}
        for tid in ticket_ids:
            ticket = storage.get_ticket(tid)
            tickets[tid] = ticket.description if ticket else "Unknown"

        # Iterate through all days in the month
        num_days = monthrange(year, month)[1]
        lines: list[str] = []
        for day_num in range(1, num_days + 1):
            d = date(year, month, day_num)
            entry = entries_by_date.get(d)
            day_allocs = allocs_by_date.get(d, [])

            has_allocs = len(day_allocs) > 0
            has_time = entry is not None and (
                entry.clock_in is not None or entry.adjust_type is not None
            )

            if not has_allocs and not has_time:
                continue

            lines.append("-" * 49)
            lines.append(d.strftime("%a %d %B"))
            lines.append("")

            # Show adjustment type if present (Leave, Sick, etc.)
            if entry and entry.adjust_type:
                label = adjust_type_labels.get(
                    entry.adjust_type, entry.adjust_type,
                )
                hours = fmt_hours(entry.adjusted_hours)
                lines.append(f"{label} ({hours})")
                lines.append("")

            for alloc in day_allocs:
                hours = fmt_hours(alloc.hours)
                lines.append(
                    f"{alloc.ticket_id}: {tickets[alloc.ticket_id]} ({hours})",
                )
                lines.append("")
                if alloc.description:
                    lines.append(alloc.description)
                lines.append("")

        if not lines:
            self.app.notify(
                f"Nothing to export for {year}-{month:02d}", severity="warning",
            )
            return

        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text("\n".join(lines), encoding="utf-8")
        except OSError as exc:
            self.app.notify(f"Failed to write file: {exc}", severity="error")
            return

        self.dismiss(str(output))
