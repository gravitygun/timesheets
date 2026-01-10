"""Modal screens for the timesheet application."""

from __future__ import annotations

from datetime import time, timedelta

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Input, Label, Select
from textual.screen import ModalScreen

from models import TimeEntry
from utils import ADJUST_TYPES


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
        adjust_type = str(adjust_type_val) if isinstance(adjust_type_val, str) and adjust_type_val else None
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
