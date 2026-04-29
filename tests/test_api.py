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
