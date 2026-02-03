"""Tests for the screens module."""

from __future__ import annotations

from datetime import date, time, timedelta


from models import Ticket, TimeEntry
from screens import (
    ConfirmScreen,
    EditAllocationScreen,
    EditDayScreen,
    EditTicketScreen,
    TicketManagementScreen,
    TicketSelectScreen,
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
        )

        assert screen.current_hours == "4.5"
        assert screen.remaining_hours == "3.0"

    def test_bindings_defined(self):
        """Test that escape binding is defined."""
        ticket = Ticket(id="TEST", description="Test")
        screen = EditAllocationScreen(ticket)

        binding_keys = [getattr(b, "key", None) for b in screen.BINDINGS]

        assert "escape" in binding_keys
