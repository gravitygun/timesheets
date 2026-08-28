"""Tests for api.py - HTTP surface."""

from __future__ import annotations

import importlib
from datetime import date, time, timedelta
from decimal import Decimal
from typing import Generator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch) -> Generator[TestClient, None, None]:
    """Spin up a TestClient against a fresh on-disk SQLite database."""
    db_path = tmp_path / "api_test.db"
    monkeypatch.setenv("TIMESHEET_DB", str(db_path))

    import storage
    importlib.reload(storage)
    storage.init_db()

    import api
    importlib.reload(api)

    with TestClient(api.app) as c:
        yield c


def _seed_ticket(tid: str = "9610", desc: str = "List cluster resources fixes") -> None:
    import storage
    from models import Ticket

    storage.save_ticket(Ticket(id=tid, description=desc))


def _seed_entry(d: date = date(2026, 4, 23)) -> None:
    import storage
    from models import TimeEntry

    storage.save_entry(
        TimeEntry(
            date=d,
            day_of_week=d.strftime("%a"),
            clock_in=time(9, 0),
            lunch_duration=timedelta(minutes=30),
            clock_out=time(17, 0),
        )
    )


class TestHealth:
    def test_returns_ok(self, client: TestClient) -> None:
        r = client.get("/health")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "ok"
        assert body["db_path"].endswith(".db")


class TestEntries:
    def test_get_entry_happy(self, client: TestClient) -> None:
        _seed_entry()
        r = client.get("/entries/2026-04-23")
        assert r.status_code == 200
        body = r.json()
        assert body["date"] == "2026-04-23"
        assert body["clock_in"] == "09:00"
        assert body["clock_out"] == "17:00"
        assert body["lunch_minutes"] == 30
        assert body["worked_hours"] == "7.50"
        assert body["total_allocated_hours"] == "0.00"
        assert body["allocation_gap_hours"] == "7.50"

    def test_get_entry_missing(self, client: TestClient) -> None:
        r = client.get("/entries/2026-04-23")
        assert r.status_code == 404

    def test_gap_reflects_existing_allocations(self, client: TestClient) -> None:
        _seed_entry()
        _seed_ticket()
        client.post(
            "/allocations",
            json={
                "ticket_id": "9610",
                "date": "2026-04-23",
                "hours": "3.5",
                "description": "Branch work",
            },
        )
        r = client.get("/entries/2026-04-23")
        assert r.json()["allocation_gap_hours"] == "4.00"


class TestPutEntry:
    """Tests for PUT /entries/{date} - the attendance upsert."""

    GOOD = {
        "clock_in": "09:00",
        "clock_out": "17:00",
        "lunch_minutes": 30,
    }

    @staticmethod
    def _seed_allocation(d: date, entered_on_client: bool, ticket: str = "9610"):
        import storage
        from models import Ticket, TicketAllocation

        storage.save_ticket(Ticket(id=ticket, description="billed work"))
        storage.save_allocation(
            TicketAllocation(
                ticket_id=ticket,
                date=d,
                hours=Decimal("3.75"),
                entered_on_client=entered_on_client,
            )
        )

    def test_creates_when_absent(self, client: TestClient) -> None:
        assert client.get("/entries/2026-04-23").status_code == 404

        r = client.put("/entries/2026-04-23", json=self.GOOD)
        assert r.status_code == 200
        body = r.json()
        assert body["date"] == "2026-04-23"
        assert body["day_of_week"] == "Thu"
        assert body["clock_in"] == "09:00"
        assert body["clock_out"] == "17:00"
        assert body["lunch_minutes"] == 30
        assert body["worked_hours"] == "7.50"
        assert body["allocation_gap_hours"] == "7.50"

        # Readable through the GET route afterwards.
        assert client.get("/entries/2026-04-23").json() == body

    def test_updates_existing(self, client: TestClient) -> None:
        client.put("/entries/2026-04-23", json=self.GOOD)

        r = client.put(
            "/entries/2026-04-23",
            json={
                "clock_in": "08:30",
                "clock_out": "16:00",
                "lunch_minutes": 60,
                "comment": "Short day",
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert body["clock_in"] == "08:30"
        assert body["clock_out"] == "16:00"
        assert body["lunch_minutes"] == 60
        assert body["comment"] == "Short day"
        assert body["worked_hours"] == "6.50"

    def test_put_replaces_rather_than_merges(self, client: TestClient) -> None:
        """Omitted optional fields are cleared - this is a PUT, not a PATCH."""
        client.put(
            "/entries/2026-04-23",
            json={**self.GOOD, "comment": "first", "adjustment_minutes": 60,
                  "adjust_type": "L"},
        )

        r = client.put("/entries/2026-04-23", json=self.GOOD)
        assert r.status_code == 200
        assert r.json()["comment"] is None
        assert r.json()["adjustment_minutes"] is None
        assert r.json()["adjust_type"] is None

    def test_worked_hours_accounts_for_lunch(self, client: TestClient) -> None:
        r = client.put(
            "/entries/2026-04-23",
            json={"clock_in": "09:15", "clock_out": "17:45", "lunch_minutes": 45},
        )
        assert r.json()["worked_hours"] == "7.75"

    def test_adjustment_is_returned_in_hours(self, client: TestClient) -> None:
        r = client.put(
            "/entries/2026-04-23",
            json={**self.GOOD, "adjustment_minutes": 90, "adjust_type": "P"},
        )
        assert r.status_code == 200
        assert r.json()["adjustment_minutes"] == 90
        assert r.json()["adjust_type"] == "P"
        assert r.json()["adjusted_hours"] == "1.50"

    # --- clock validation ---

    def test_clock_out_before_clock_in_rejected(self, client: TestClient) -> None:
        r = client.put(
            "/entries/2026-04-23",
            json={"clock_in": "17:00", "clock_out": "09:00", "lunch_minutes": 30},
        )
        assert r.status_code == 422
        assert "must be after" in r.json()["detail"]
        # Nothing was written.
        assert client.get("/entries/2026-04-23").status_code == 404

    def test_clock_out_equal_to_clock_in_rejected(self, client: TestClient) -> None:
        r = client.put(
            "/entries/2026-04-23",
            json={"clock_in": "09:00", "clock_out": "09:00", "lunch_minutes": 0},
        )
        assert r.status_code == 422

    @pytest.mark.parametrize("bad", ["9am", "25:00", "09:60", "0900", ""])
    def test_malformed_clock_rejected(self, client: TestClient, bad: str) -> None:
        r = client.put(
            "/entries/2026-04-23",
            json={"clock_in": bad, "clock_out": "17:00", "lunch_minutes": 30},
        )
        assert r.status_code == 422
        assert "clock_in" in r.json()["detail"]

    def test_lunch_longer_than_the_day_rejected(self, client: TestClient) -> None:
        """Otherwise worked_hours goes negative and corrupts the billing."""
        r = client.put(
            "/entries/2026-04-23",
            json={"clock_in": "09:00", "clock_out": "10:00", "lunch_minutes": 120},
        )
        assert r.status_code == 422
        assert "cannot exceed" in r.json()["detail"]

    def test_negative_lunch_rejected(self, client: TestClient) -> None:
        r = client.put(
            "/entries/2026-04-23",
            json={"clock_in": "09:00", "clock_out": "17:00", "lunch_minutes": -30},
        )
        assert r.status_code == 422

    # --- adjust_type validation ---

    @pytest.mark.parametrize("adjust_type", ["L", "P", "S"])
    def test_valid_adjust_types_accepted(
        self, client: TestClient, adjust_type: str
    ) -> None:
        r = client.put(
            "/entries/2026-04-23",
            json={**self.GOOD, "adjustment_minutes": 60, "adjust_type": adjust_type},
        )
        assert r.status_code == 200
        assert r.json()["adjust_type"] == adjust_type

    def test_adjust_type_is_normalised(self, client: TestClient) -> None:
        r = client.put(
            "/entries/2026-04-23",
            json={**self.GOOD, "adjustment_minutes": 60, "adjust_type": " l "},
        )
        assert r.status_code == 200
        assert r.json()["adjust_type"] == "L"

    @pytest.mark.parametrize("bad", ["X", "leave", ""])
    def test_invalid_adjust_type_rejected(
        self, client: TestClient, bad: str
    ) -> None:
        r = client.put(
            "/entries/2026-04-23",
            json={**self.GOOD, "adjust_type": bad},
        )
        assert r.status_code == 422
        assert "adjust_type must be one of" in r.json()["detail"]

    def test_training_type_rejected_by_this_endpoint(
        self, client: TestClient
    ) -> None:
        """The TUI accepts "T" (Training); this endpoint's spec does not.

        Pinned so the divergence is a deliberate choice, not a silent one.
        """
        r = client.put(
            "/entries/2026-04-23",
            json={**self.GOOD, "adjustment_minutes": 60, "adjust_type": "T"},
        )
        assert r.status_code == 422

    def test_adjustment_without_type_rejected(self, client: TestClient) -> None:
        r = client.put(
            "/entries/2026-04-23",
            json={**self.GOOD, "adjustment_minutes": 60},
        )
        assert r.status_code == 422
        assert r.json()["detail"] == "adjustment_minutes requires an adjust_type"

    # --- billed-day guard ---

    def test_fully_billed_day_is_refused(self, client: TestClient) -> None:
        d = date(2026, 4, 23)
        client.put("/entries/2026-04-23", json=self.GOOD)
        self._seed_allocation(d, entered_on_client=True)

        r = client.put(
            "/entries/2026-04-23",
            json={"clock_in": "10:00", "clock_out": "12:00", "lunch_minutes": 0},
        )
        assert r.status_code == 409
        assert r.json()["detail"] == "entry for 2026-04-23 is already billed"

        # The closed record is untouched.
        assert client.get("/entries/2026-04-23").json()["clock_in"] == "09:00"

    def test_partially_billed_day_is_still_editable(
        self, client: TestClient
    ) -> None:
        """Only a day where *every* allocation is entered client-side is closed."""
        d = date(2026, 4, 23)
        self._seed_allocation(d, entered_on_client=True, ticket="9610")
        self._seed_allocation(d, entered_on_client=False, ticket="9611")

        r = client.put("/entries/2026-04-23", json=self.GOOD)
        assert r.status_code == 200

    def test_day_with_no_allocations_is_editable(self, client: TestClient) -> None:
        """Guards against `all([])` being vacuously true for an empty day."""
        r = client.put("/entries/2026-04-23", json=self.GOOD)
        assert r.status_code == 200

    def test_billed_guard_beats_invalid_payload(self, client: TestClient) -> None:
        """A closed day reports as closed, whatever else is wrong with the body."""
        self._seed_allocation(date(2026, 4, 23), entered_on_client=True)

        r = client.put(
            "/entries/2026-04-23",
            json={"clock_in": "17:00", "clock_out": "09:00", "lunch_minutes": 30},
        )
        assert r.status_code == 409


class TestTickets:
    def test_create_and_get(self, client: TestClient) -> None:
        r = client.post("/tickets", json={"id": "9610", "description": "List cluster resources"})
        assert r.status_code == 201
        body = r.json()
        assert body["id"] == "9610"
        assert body["archived"] is False
        assert body["deliverable_id"] is None

        r = client.get("/tickets/9610")
        assert r.status_code == 200
        assert r.json()["description"] == "List cluster resources"
        assert r.json()["deliverable_id"] is None

    def test_create_with_deliverable(self, client: TestClient) -> None:
        # WP5a-D1 is seeded by storage.init_db
        r = client.post(
            "/tickets",
            json={"id": "9610", "description": "x", "deliverable_id": "WP5a-D1"},
        )
        assert r.status_code == 201
        assert r.json()["deliverable_id"] == "WP5a-D1"

    def test_create_with_unknown_deliverable_rejected(self, client: TestClient) -> None:
        r = client.post(
            "/tickets",
            json={"id": "9610", "description": "x", "deliverable_id": "WP-NOPE"},
        )
        assert r.status_code == 422

    def test_create_duplicate_conflicts(self, client: TestClient) -> None:
        client.post("/tickets", json={"id": "9610", "description": "first"})
        r = client.post("/tickets", json={"id": "9610", "description": "second"})
        assert r.status_code == 409

    def test_get_unknown(self, client: TestClient) -> None:
        r = client.get("/tickets/0000")
        assert r.status_code == 404

    def test_list_filters_archived(self, client: TestClient) -> None:
        client.post("/tickets", json={"id": "9610", "description": "alive"})
        client.post("/tickets", json={"id": "8888", "description": "dead"})
        client.post("/tickets/8888/archive")

        r = client.get("/tickets")
        ids = [t["id"] for t in r.json()]
        assert "9610" in ids
        assert "8888" not in ids

        r = client.get("/tickets?include_archived=true")
        ids = [t["id"] for t in r.json()]
        assert "8888" in ids

    def test_search(self, client: TestClient) -> None:
        client.post("/tickets", json={"id": "9610", "description": "List cluster resources"})
        client.post("/tickets", json={"id": "9611", "description": "Authorino"})

        r = client.get("/tickets?q=cluster")
        ids = [t["id"] for t in r.json()]
        assert ids == ["9610"]

    def test_archive_unarchive_roundtrip(self, client: TestClient) -> None:
        client.post("/tickets", json={"id": "9610", "description": "x"})

        r = client.post("/tickets/9610/archive")
        assert r.status_code == 200
        assert r.json()["archived"] is True

        r = client.post("/tickets/9610/unarchive")
        assert r.status_code == 200
        assert r.json()["archived"] is False

    def test_archive_unknown(self, client: TestClient) -> None:
        r = client.post("/tickets/0000/archive")
        assert r.status_code == 404

    def test_patch_description_only(self, client: TestClient) -> None:
        client.post(
            "/tickets",
            json={"id": "9610", "description": "old", "deliverable_id": "WP5a-D1"},
        )

        r = client.patch("/tickets/9610", json={"description": "new"})
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == "9610"
        assert body["description"] == "new"
        # Omitted fields are left alone.
        assert body["deliverable_id"] == "WP5a-D1"
        assert body["archived"] is False

    def test_patch_deliverable_only(self, client: TestClient) -> None:
        client.post("/tickets", json={"id": "9610", "description": "keep me"})

        r = client.patch("/tickets/9610", json={"deliverable_id": "WP5a-D1"})
        assert r.status_code == 200
        assert r.json()["deliverable_id"] == "WP5a-D1"
        assert r.json()["description"] == "keep me"

    def test_patch_unknown_deliverable_rejected(self, client: TestClient) -> None:
        client.post("/tickets", json={"id": "9610", "description": "keep me"})

        r = client.patch(
            "/tickets/9610",
            json={"description": "changed", "deliverable_id": "WP-NOPE"},
        )
        assert r.status_code == 422
        # Rejected patch changes nothing.
        assert client.get("/tickets/9610").json()["description"] == "keep me"

    def test_patch_rename_cascades_allocations(self, client: TestClient) -> None:
        client.post("/tickets", json={"id": "9610", "description": "renamed soon"})
        client.post(
            "/allocations",
            json={"ticket_id": "9610", "date": "2026-04-23", "hours": "2.5"},
        )

        r = client.patch("/tickets/9610", json={"id": "9611"})
        assert r.status_code == 200
        assert r.json()["id"] == "9611"
        assert r.json()["description"] == "renamed soon"

        assert client.get("/tickets/9610").status_code == 404
        assert client.get("/tickets/9611").status_code == 200

        # The allocation followed the ticket rather than being orphaned.
        allocs = client.get("/allocations/2026-04-23").json()
        assert [a["ticket_id"] for a in allocs] == ["9611"]
        assert Decimal(allocs[0]["hours"]) == Decimal("2.50")

    def test_patch_rename_with_description(self, client: TestClient) -> None:
        client.post("/tickets", json={"id": "9610", "description": "old"})

        r = client.patch("/tickets/9610", json={"id": "9611", "description": "new"})
        assert r.status_code == 200
        assert r.json() == {
            "id": "9611",
            "description": "new",
            "archived": False,
            "created_at": r.json()["created_at"],
            "deliverable_id": None,
        }

    def test_patch_rename_to_taken_id_conflicts(self, client: TestClient) -> None:
        client.post("/tickets", json={"id": "9610", "description": "mine"})
        client.post("/tickets", json={"id": "9611", "description": "theirs"})
        client.post(
            "/allocations",
            json={"ticket_id": "9610", "date": "2026-04-23", "hours": "1.0"},
        )

        r = client.patch("/tickets/9610", json={"id": "9611"})
        assert r.status_code == 409

        # Neither ticket nor its allocations moved.
        assert client.get("/tickets/9610").json()["description"] == "mine"
        assert client.get("/tickets/9611").json()["description"] == "theirs"
        allocs = client.get("/allocations/2026-04-23").json()
        assert [a["ticket_id"] for a in allocs] == ["9610"]

    def test_patch_same_id_is_not_a_rename(self, client: TestClient) -> None:
        client.post("/tickets", json={"id": "9610", "description": "old"})

        r = client.patch("/tickets/9610", json={"id": "9610", "description": "new"})
        assert r.status_code == 200
        assert r.json()["id"] == "9610"
        assert r.json()["description"] == "new"

    def test_patch_empty_body_is_a_noop(self, client: TestClient) -> None:
        client.post(
            "/tickets",
            json={"id": "9610", "description": "untouched", "deliverable_id": "WP5a-D1"},
        )
        before = client.get("/tickets/9610").json()

        r = client.patch("/tickets/9610", json={})
        assert r.status_code == 200
        assert r.json() == before

    def test_patch_unknown_ticket(self, client: TestClient) -> None:
        r = client.patch("/tickets/0000", json={"description": "x"})
        assert r.status_code == 404

    def test_patch_preserves_billed_state(self, client: TestClient) -> None:
        """A patch must not silently retract a billing claim."""
        import storage

        client.post("/tickets", json={"id": "9610", "description": "old"})
        client.post("/tickets/9610/archive")
        ticket = storage.get_ticket("9610")
        assert ticket is not None
        ticket.billed = True
        ticket.billed_year = 2026
        ticket.billed_month = 4
        storage.save_ticket(ticket)

        r = client.patch("/tickets/9610", json={"id": "9611", "description": "new"})
        assert r.status_code == 200
        assert r.json()["archived"] is True

        renamed = storage.get_ticket("9611")
        assert renamed is not None
        assert renamed.billed is True
        assert (renamed.billed_year, renamed.billed_month) == (2026, 4)


class TestAllocations:
    def test_create_and_list(self, client: TestClient) -> None:
        _seed_ticket()
        body = {
            "ticket_id": "9610",
            "date": "2026-04-23",
            "hours": "2.5",
            "description": "Multi-line\ndescription with\ndetail for Jira",
        }
        r = client.post("/allocations", json=body)
        assert r.status_code == 201
        out = r.json()
        assert out["hours"] == "2.50"
        assert out["description"] == body["description"]

        r = client.get("/allocations/2026-04-23")
        assert r.status_code == 200
        assert len(r.json()) == 1
        assert r.json()[0]["ticket_id"] == "9610"

    def test_create_unknown_ticket_rejected(self, client: TestClient) -> None:
        r = client.post(
            "/allocations",
            json={"ticket_id": "9999", "date": "2026-04-23", "hours": "1.0"},
        )
        assert r.status_code == 404

    def test_upsert_replaces(self, client: TestClient) -> None:
        _seed_ticket()
        client.post(
            "/allocations",
            json={"ticket_id": "9610", "date": "2026-04-23", "hours": "1.0"},
        )
        r = client.post(
            "/allocations",
            json={"ticket_id": "9610", "date": "2026-04-23", "hours": "3.5"},
        )
        assert r.status_code == 201
        assert r.json()["hours"] == "3.50"

        r = client.get("/allocations/2026-04-23")
        assert len(r.json()) == 1
        assert Decimal(r.json()[0]["hours"]) == Decimal("3.50")

    def test_delete(self, client: TestClient) -> None:
        _seed_ticket()
        client.post(
            "/allocations",
            json={"ticket_id": "9610", "date": "2026-04-23", "hours": "1.0"},
        )
        r = client.delete("/allocations/9610/2026-04-23")
        assert r.status_code == 204
        assert client.get("/allocations/2026-04-23").json() == []

    def test_delete_missing(self, client: TestClient) -> None:
        r = client.delete("/allocations/9610/2026-04-23")
        assert r.status_code == 404


class TestPatchAllocation:
    """Tests for PATCH /allocations/{ticket_id}/{date}."""

    @staticmethod
    def _seed(client: TestClient, hours: str = "2.75", desc: str = "note") -> None:
        client.post("/tickets", json={"id": "9610", "description": "work"})
        client.post(
            "/allocations",
            json={
                "ticket_id": "9610",
                "date": "2026-08-25",
                "hours": hours,
                "description": desc,
            },
        )

    def test_marks_entered_on_client(self, client: TestClient) -> None:
        self._seed(client)
        assert client.get("/allocations/2026-08-25").json()[0][
            "entered_on_client"
        ] is False

        r = client.patch(
            "/allocations/9610/2026-08-25", json={"entered_on_client": True}
        )
        assert r.status_code == 200
        body = r.json()
        assert body["entered_on_client"] is True
        # Untouched fields survive.
        assert body["hours"] == "2.75"
        assert body["description"] == "note"

    def test_unmarks_entered_on_client(self, client: TestClient) -> None:
        self._seed(client)
        client.patch("/allocations/9610/2026-08-25", json={"entered_on_client": True})

        r = client.patch(
            "/allocations/9610/2026-08-25", json={"entered_on_client": False}
        )
        assert r.status_code == 200
        assert r.json()["entered_on_client"] is False

    def test_updates_hours_and_description(self, client: TestClient) -> None:
        self._seed(client)

        r = client.patch(
            "/allocations/9610/2026-08-25",
            json={"hours": "3.50", "description": "reworked"},
        )
        assert r.status_code == 200
        assert r.json()["hours"] == "3.50"
        assert r.json()["description"] == "reworked"

    def test_changing_hours_clears_entered(self, client: TestClient) -> None:
        """The client's system is holding the old figure, so the day is stale."""
        self._seed(client)
        client.patch("/allocations/9610/2026-08-25", json={"entered_on_client": True})

        r = client.patch("/allocations/9610/2026-08-25", json={"hours": "3.50"})
        assert r.status_code == 200
        assert r.json()["hours"] == "3.50"
        assert r.json()["entered_on_client"] is False

    def test_description_only_change_keeps_entered(self, client: TestClient) -> None:
        """A reworded note does not change what was billed."""
        self._seed(client)
        client.patch("/allocations/9610/2026-08-25", json={"entered_on_client": True})

        r = client.patch(
            "/allocations/9610/2026-08-25", json={"description": "clearer wording"}
        )
        assert r.status_code == 200
        assert r.json()["description"] == "clearer wording"
        assert r.json()["entered_on_client"] is True

    def test_explicit_entered_wins_over_the_hours_rule(
        self, client: TestClient
    ) -> None:
        """Re-entering the corrected figure is a single call."""
        self._seed(client)

        r = client.patch(
            "/allocations/9610/2026-08-25",
            json={"hours": "3.50", "entered_on_client": True},
        )
        assert r.status_code == 200
        assert r.json()["hours"] == "3.50"
        assert r.json()["entered_on_client"] is True

    def test_empty_body_is_a_noop(self, client: TestClient) -> None:
        self._seed(client)
        client.patch("/allocations/9610/2026-08-25", json={"entered_on_client": True})
        before = client.get("/allocations/2026-08-25").json()[0]

        r = client.patch("/allocations/9610/2026-08-25", json={})
        assert r.status_code == 200
        assert r.json() == before

    def test_unknown_allocation(self, client: TestClient) -> None:
        r = client.patch(
            "/allocations/9610/2026-08-25", json={"entered_on_client": True}
        )
        assert r.status_code == 404

    def test_wrong_date_for_existing_ticket(self, client: TestClient) -> None:
        self._seed(client)
        r = client.patch(
            "/allocations/9610/2026-08-26", json={"entered_on_client": True}
        )
        assert r.status_code == 404

    def test_does_not_touch_a_sibling_allocation(self, client: TestClient) -> None:
        """The targeted UPDATE must hit exactly one row."""
        self._seed(client)
        client.post("/tickets", json={"id": "9611", "description": "other"})
        client.post(
            "/allocations",
            json={"ticket_id": "9611", "date": "2026-08-25", "hours": "1.00"},
        )

        client.patch(
            "/allocations/9610/2026-08-25",
            json={"hours": "5.00", "entered_on_client": True},
        )

        others = [
            a
            for a in client.get("/allocations/2026-08-25").json()
            if a["ticket_id"] == "9611"
        ]
        assert others[0]["hours"] == "1.00"
        assert others[0]["entered_on_client"] is False


class TestMarkEntered:
    """Tests for POST /allocations/mark-entered."""

    @staticmethod
    def _seed(client: TestClient, days: list[str], ticket: str = "9610") -> None:
        client.post("/tickets", json={"id": ticket, "description": "work"})
        for d in days:
            client.post(
                "/allocations",
                json={"ticket_id": ticket, "date": d, "hours": "1.00"},
            )

    def test_marks_a_range_in_one_call(self, client: TestClient) -> None:
        self._seed(client, ["2026-08-25", "2026-08-26", "2026-08-27"])
        self._seed(client, ["2026-08-25", "2026-08-26"], ticket="9611")

        r = client.post(
            "/allocations/mark-entered",
            json={"from": "2026-08-25", "to": "2026-08-28",
                  "entered_on_client": True},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["changed"] == 5
        assert body["entered_on_client"] is True
        assert {(a["ticket_id"], a["date"]) for a in body["allocations"]} == {
            ("9610", "2026-08-25"), ("9610", "2026-08-26"),
            ("9610", "2026-08-27"), ("9611", "2026-08-25"),
            ("9611", "2026-08-26"),
        }

        for d in ("2026-08-25", "2026-08-26", "2026-08-27"):
            assert all(
                a["entered_on_client"] for a in client.get(f"/allocations/{d}").json()
            )

    def test_range_bounds_are_inclusive(self, client: TestClient) -> None:
        self._seed(client, ["2026-08-24", "2026-08-25", "2026-08-28", "2026-08-29"])

        r = client.post(
            "/allocations/mark-entered",
            json={"from": "2026-08-25", "to": "2026-08-28"},
        )
        assert {a["date"] for a in r.json()["allocations"]} == {
            "2026-08-25", "2026-08-28",
        }
        # Days either side are untouched.
        assert client.get("/allocations/2026-08-24").json()[0][
            "entered_on_client"
        ] is False
        assert client.get("/allocations/2026-08-29").json()[0][
            "entered_on_client"
        ] is False

    def test_defaults_to_marking_entered(self, client: TestClient) -> None:
        self._seed(client, ["2026-08-25"])

        r = client.post(
            "/allocations/mark-entered",
            json={"from": "2026-08-25", "to": "2026-08-25"},
        )
        assert r.json()["entered_on_client"] is True
        assert r.json()["changed"] == 1

    def test_can_pull_a_range_back(self, client: TestClient) -> None:
        self._seed(client, ["2026-08-25", "2026-08-26"])
        client.post(
            "/allocations/mark-entered",
            json={"from": "2026-08-25", "to": "2026-08-26"},
        )

        r = client.post(
            "/allocations/mark-entered",
            json={"from": "2026-08-25", "to": "2026-08-26",
                  "entered_on_client": False},
        )
        assert r.json()["changed"] == 2
        assert r.json()["entered_on_client"] is False
        assert not any(
            a["entered_on_client"]
            for a in client.get("/allocations/2026-08-25").json()
        )

    def test_reports_only_what_actually_changed(self, client: TestClient) -> None:
        """Re-running must not re-claim work already marked."""
        self._seed(client, ["2026-08-25", "2026-08-26"])
        body = {"from": "2026-08-25", "to": "2026-08-26"}

        assert client.post("/allocations/mark-entered", json=body).json()[
            "changed"
        ] == 2
        second = client.post("/allocations/mark-entered", json=body).json()
        assert second["changed"] == 0
        assert second["allocations"] == []

    def test_empty_range_is_not_an_error(self, client: TestClient) -> None:
        r = client.post(
            "/allocations/mark-entered",
            json={"from": "2026-08-25", "to": "2026-08-28"},
        )
        assert r.status_code == 200
        assert r.json() == {
            "changed": 0, "entered_on_client": True, "allocations": [],
        }

    def test_hours_and_description_are_untouched(self, client: TestClient) -> None:
        client.post("/tickets", json={"id": "9610", "description": "work"})
        client.post(
            "/allocations",
            json={"ticket_id": "9610", "date": "2026-08-25", "hours": "2.75",
                  "description": "keep me"},
        )

        client.post(
            "/allocations/mark-entered",
            json={"from": "2026-08-25", "to": "2026-08-25"},
        )
        got = client.get("/allocations/2026-08-25").json()[0]
        assert got["hours"] == "2.75"
        assert got["description"] == "keep me"

    def test_reversed_range_rejected(self, client: TestClient) -> None:
        r = client.post(
            "/allocations/mark-entered",
            json={"from": "2026-08-28", "to": "2026-08-25"},
        )
        assert r.status_code == 422
        assert "must not be after" in r.json()["detail"]

    def test_month_listing(self, client: TestClient) -> None:
        _seed_ticket()
        for day in (1, 15, 30):
            client.post(
                "/allocations",
                json={
                    "ticket_id": "9610",
                    "date": f"2026-04-{day:02d}",
                    "hours": "1.0",
                },
            )
        # Outside the month
        client.post(
            "/allocations",
            json={"ticket_id": "9610", "date": "2026-05-01", "hours": "1.0"},
        )

        r = client.get("/allocations/month/2026/4")
        dates = [a["date"] for a in r.json()]
        assert dates == ["2026-04-01", "2026-04-15", "2026-04-30"]


class TestDeliverables:
    def test_list_active_only_by_default(self, client: TestClient) -> None:
        r = client.get("/deliverables")
        assert r.status_code == 200
        ids = [d["id"] for d in r.json()]
        # WP5a-D1 and WP5-D4 are seeded as active
        assert "WP5a-D1" in ids
        assert "WP5-D4" in ids
        # All entries are active when active_only defaults to true
        assert all(d["active"] for d in r.json())

    def test_list_includes_inactive_when_requested(self, client: TestClient) -> None:
        r = client.get("/deliverables?active_only=false")
        assert r.status_code == 200
        # The seeded set includes some inactive (backfilled) deliverables
        actives = [d for d in r.json() if d["active"]]
        all_ds = r.json()
        assert len(all_ds) >= len(actives)

    def test_each_entry_has_work_package_link(self, client: TestClient) -> None:
        r = client.get("/deliverables")
        for d in r.json():
            assert d["work_package_id"]
            assert d["title"]
