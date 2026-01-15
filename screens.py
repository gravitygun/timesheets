"""Modal screens for the timesheet application."""

from __future__ import annotations

from datetime import time, timedelta

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Label
from textual.screen import ModalScreen

from models import TimeEntry


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
