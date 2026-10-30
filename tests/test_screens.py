"""Tests for the screens module."""

from __future__ import annotations

import asyncio
from datetime import date, time, timedelta
from pathlib import Path

import pytest
from textual.app import App
from textual.notifications import SeverityLevel
from textual.widgets import Button, Input, RadioButton

from models import Ticket, TimeEntry
from screens import (
    ConfirmScreen,
    EditAllocationScreen,
    EditDayScreen,
    EditTicketScreen,
    ExportAllocationsScreen,
    ExportBillScreen,
    TicketManagementScreen,
    TicketSelectScreen,
    UpdateAvailableScreen,
)


class TestConfirmScreen:
    """Tests for the ConfirmScreen."""

    def test_init_with_message(self):
        """Test ConfirmScreen initialisation with message."""
        screen = ConfirmScreen("Are you sure?")
        assert screen.message == "Are you sure?"

    def test_message_required(self):
        """Test ConfirmScreen requires a message."""
        # ConfirmScreen requires a message parameter
        screen = ConfirmScreen("Delete this item?")
        assert screen.message == "Delete this item?"


class TestEditDayScreen:
    """Tests for the EditDayScreen."""

    def test_init_with_entry(self):
        """Test EditDayScreen initialisation with entry."""
        entry = TimeEntry(
            date=date(2026, 1, 27),
            day_of_week="Mon",
            clock_in=time(9, 0),
            lunch_duration=timedelta(minutes=30),
            clock_out=time(17, 0),
        )
        screen = EditDayScreen(entry)

        assert screen.entry == entry

    def test_init_with_empty_entry(self):
        """Test EditDayScreen with empty entry."""
        entry = TimeEntry(
            date=date(2026, 1, 27),
            day_of_week="Mon",
        )
        screen = EditDayScreen(entry)

        assert screen.entry.clock_in is None
        assert screen.entry.clock_out is None


class _EditDayHarness(App):
    """Hosts an EditDayScreen and records what it dismisses with."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[TimeEntry | None] = []
        self.errors: list[str] = []
        self.focused_id: str | None = None

    def on_mount(self) -> None:
        entry = TimeEntry(date=date(2026, 10, 15), day_of_week="Thu")
        self.push_screen(EditDayScreen(entry), self.results.append)

    def notify(
        self, message, *, severity: SeverityLevel = "information", **kwargs
    ) -> None:
        if severity == "error":
            self.errors.append(str(message))
        super().notify(message, severity=severity, **kwargs)


def _save_day(**fields: str) -> _EditDayHarness:
    """Fill in EditDayScreen fields (by input id), save, return the harness."""

    async def run() -> _EditDayHarness:
        app = _EditDayHarness()
        async with app.run_test() as pilot:
            screen = app.screen
            assert isinstance(screen, EditDayScreen)
            for field_id, value in fields.items():
                screen.query_one(f"#{field_id.replace('_', '-')}", Input).value = value
            await pilot.pause()  # let auto-advance on the type field settle
            screen.query_one("#save", Button).press()
            await pilot.pause()
            app.focused_id = app.focused.id if app.focused else None
        return app

    return asyncio.run(run())


class TestEditDayScreenValidation:
    """Bad input in EditDayScreen is rejected, not crashed on or dropped."""

    def test_valid_entry_saves(self):
        app = _save_day(
            clock_in="09:15", lunch="30", clock_out="17:30",
            adjustment="1.5", adjust_type="T",
        )

        assert app.errors == []
        [entry] = app.results
        assert entry is not None
        assert entry.clock_in == time(9, 15)
        assert entry.lunch_duration == timedelta(minutes=30)
        assert entry.clock_out == time(17, 30)
        assert entry.adjustment == timedelta(hours=1.5)

    def test_blank_fields_save_as_none(self):
        app = _save_day()

        assert app.errors == []
        [entry] = app.results
        assert entry is not None
        assert entry.clock_in is None
        assert entry.lunch_duration is None

    @pytest.mark.parametrize(
        ("fields", "bad_field"),
        [
            ({"clock_in": "09:15", "lunch": "3-", "clock_out": "17:30"}, "lunch"),
            ({"lunch": "-30"}, "lunch"),
            ({"lunch": "30.5"}, "lunch"),
            ({"clock_in": "9.15"}, "clock-in"),
            ({"clock_in": "25:00"}, "clock-in"),
            ({"clock_out": "17:30:00"}, "clock-out"),
            ({"adjustment": "abc", "adjust_type": "L"}, "adjustment"),
            ({"adjustment": "nan", "adjust_type": "L"}, "adjustment"),
            ({"adjustment": "inf", "adjust_type": "L"}, "adjustment"),
            ({"clock_in": "17:30", "clock_out": "09:00"}, "clock-out"),
            ({"clock_in": "09:00", "clock_out": "09:00"}, "clock-out"),
            ({"clock_in": "09:00", "lunch": "60", "clock_out": "10:00"}, "lunch"),
        ],
    )
    def test_invalid_input_is_rejected(self, fields, bad_field):
        app = _save_day(**fields)

        assert app.results == []  # dialog stays open, nothing saved
        assert len(app.errors) == 1
        assert app.focused_id == bad_field


class TestEditTicketScreen:
    """Tests for the EditTicketScreen."""

    def test_init_new_ticket(self):
        """Test EditTicketScreen for new ticket."""
        screen = EditTicketScreen()
        assert screen.ticket is None

    def test_init_edit_ticket(self):
        """Test EditTicketScreen for editing existing ticket."""
        ticket = Ticket(
            id="PROJ-123",
            description="Test project",
            archived=False,
            created_at=date(2026, 1, 1),
        )
        screen = EditTicketScreen(ticket)

        assert screen.ticket == ticket
        assert screen.ticket is not None
        assert screen.ticket.id == "PROJ-123"


class TestTicketManagementScreen:
    """Tests for the TicketManagementScreen."""

    def test_init(self):
        """Test TicketManagementScreen initialisation."""
        screen = TicketManagementScreen()
        assert screen.show_archived is False

    def test_bindings_defined(self):
        """Test that all bindings are defined."""
        screen = TicketManagementScreen()

        # BINDINGS contains Binding objects
        binding_keys = [getattr(b, "key", None) for b in screen.BINDINGS]

        assert "escape" in binding_keys
        assert "n" in binding_keys
        assert "e" in binding_keys
        assert "a" in binding_keys
        assert "d" in binding_keys
        assert "p" in binding_keys


class TestTicketSelectScreen:
    """Tests for the TicketSelectScreen."""

    def test_bindings_defined(self):
        """Test that all bindings are defined."""
        screen = TicketSelectScreen()

        binding_keys = [getattr(b, "key", None) for b in screen.BINDINGS]

        assert "escape" in binding_keys
        assert "n" in binding_keys


class TestEditAllocationScreen:
    """Tests for the EditAllocationScreen."""

    def test_init_new_allocation(self):
        """Test EditAllocationScreen for new allocation."""
        ticket = Ticket(
            id="PROJ-123",
            description="Test project",
        )
        screen = EditAllocationScreen(
            ticket=ticket,
            current_hours="",
            remaining_hours="7.5",
        )

        assert screen.ticket == ticket
        assert screen.current_hours == ""
        assert screen.remaining_hours == "7.5"
        assert screen.current_description == ""

    def test_init_edit_allocation(self):
        """Test EditAllocationScreen for editing existing allocation."""
        ticket = Ticket(
            id="PROJ-123",
            description="Test project",
        )
        screen = EditAllocationScreen(
            ticket=ticket,
            current_hours="4.5",
            remaining_hours="3.0",
            current_description="Did some work",
        )

        assert screen.current_hours == "4.5"
        assert screen.remaining_hours == "3.0"
        assert screen.current_description == "Did some work"

    def test_init_multiline_description(self):
        """Test EditAllocationScreen with multiline description."""
        ticket = Ticket(id="TEST", description="Test")
        desc = "Line one\nLine two\nLine three"
        screen = EditAllocationScreen(
            ticket=ticket,
            current_hours="2.0",
            current_description=desc,
        )

        assert screen.current_description == desc

    def test_bindings_defined(self):
        """Test that escape binding is defined."""
        ticket = Ticket(id="TEST", description="Test")
        screen = EditAllocationScreen(ticket)

        binding_keys = [getattr(b, "key", None) for b in screen.BINDINGS]

        assert "escape" in binding_keys


class TestUpdateAvailableScreen:
    """Tests for the out-of-sync startup dialog."""

    def test_init_stores_counts(self):
        screen = UpdateAvailableScreen("main", 1, 2, 3)

        assert screen.branch == "main"
        assert screen.ahead == 1
        assert screen.behind == 2
        assert screen.uncommitted == 3

    def test_uncommitted_defaults_to_none_pending(self):
        """Older three-argument construction still works."""
        screen = UpdateAvailableScreen("main", 0, 2)

        assert screen.uncommitted == 0


class _ExportHarness(App):
    """Hosts ExportAllocationsScreen and records what it dismisses with."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[str | None] = []

    def on_mount(self) -> None:
        self.push_screen(ExportAllocationsScreen(2026, 10), self.results.append)


class TestExportAllocationsScreenJson:
    """The R dialog can write the JSON export for jira_sync."""

    def test_json_format_writes_export(self, tmp_path, monkeypatch):
        import json

        import screens

        doc = {"schema": "timesheets.allocations", "version": 1,
               "allocations": [{"ticket_id": "1"}]}
        monkeypatch.setattr(screens.storage, "get_allocations_export",
                            lambda year, month: {**doc, "month": [year, month]})
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        async def run() -> tuple[_ExportHarness, str, str]:
            app = _ExportHarness()
            async with app.run_test() as pilot:
                screen = app.screen
                assert isinstance(screen, ExportAllocationsScreen)
                path_input = screen.query_one("#export-path", Input)
                text_path = path_input.value
                screen.query_one("#format-json", RadioButton).value = True
                await pilot.pause()
                json_path = path_input.value
                screen.query_one("#export-btn", Button).press()
                await pilot.pause()
            return app, text_path, json_path

        app, text_path, json_path = asyncio.run(run())

        assert text_path.endswith("time-allocations-2026-10.txt")
        assert json_path.endswith("time-allocations-2026-10.json")
        [written] = app.results
        assert written == json_path
        assert json.loads(Path(json_path).read_text())["month"] == [2026, 10]


class _ExportBillHarness(App):
    def __init__(self, data: dict) -> None:
        super().__init__()
        self.data = data
        self.results: list[str | None] = []

    def on_mount(self) -> None:
        self.push_screen(ExportBillScreen(self.data), self.results.append)


class TestExportBillScreen:
    """J in the Billing view writes the bill for jira_sync."""

    def test_writes_the_export(self, tmp_path, monkeypatch):
        import json

        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        data = {"schema": "timesheets.bill", "version": 1,
                "billed": {"year": 2026, "month": 10},
                "lines": [{"deliverable_id": "WP2a-D1", "points": 3,
                           "tickets": [{"ticket_id": "10400"}]}]}

        async def run() -> tuple[_ExportBillHarness, str]:
            app = _ExportBillHarness(data)
            async with app.run_test() as pilot:
                screen = app.screen
                assert isinstance(screen, ExportBillScreen)
                path = screen.query_one("#bill-export-path", Input).value
                screen.query_one("#export-btn", Button).press()
                await pilot.pause()
            return app, path

        app, path = asyncio.run(run())
        assert path.endswith("bill-2026-10.json")
        assert app.results == [path]
        assert json.loads(Path(path).read_text()) == data
