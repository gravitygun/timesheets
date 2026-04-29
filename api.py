"""HTTP API exposing a narrow slice of timesheet operations.

The intent is automation: an external client (e.g. an AI assistant on a remote
dev machine, reached over an SSH RemoteForward) reads attendance + tickets and
posts ticket allocations. The TUI remains the source of truth for everything
else (config, work packages, deliverables, billing, ticket rename/delete).

All operations delegate to ``storage.py`` so the TUI and the API share a
single SQLite database (WAL mode is enabled in ``storage.init_db``).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal
from typing import Annotated, AsyncIterator

from fastapi import Body, FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, Field, PlainSerializer

import storage
from models import Deliverable, Ticket, TicketAllocation


# --- Serialisation helpers -------------------------------------------------

# Hours are sent and received as strings to avoid float drift.
HoursStr = Annotated[
    Decimal,
    PlainSerializer(lambda v: format(v.quantize(Decimal("0.01")), "f"), return_type=str),
]


# --- Response models -------------------------------------------------------


class HealthOut(BaseModel):
    status: str = "ok"
    db_path: str


class EntryOut(BaseModel):
    date: date
    day_of_week: str
    clock_in: str | None
    lunch_minutes: int | None
    clock_out: str | None
    adjustment_minutes: int | None
    adjust_type: str | None
    comment: str | None
    worked_hours: HoursStr
    adjusted_hours: HoursStr
    total_allocated_hours: HoursStr
    allocation_gap_hours: HoursStr = Field(
        description="worked_hours minus total_allocated_hours; positive means under-allocated",
    )


class TicketOut(BaseModel):
    id: str
    description: str
    archived: bool
    created_at: date | None
    deliverable_id: str | None


class TicketIn(BaseModel):
    id: str = Field(min_length=1, max_length=8)
    description: str = Field(min_length=1)
    deliverable_id: str | None = None


class DeliverableOut(BaseModel):
    id: str
    work_package_id: str
    title: str
    active: bool


class AllocationOut(BaseModel):
    ticket_id: str
    date: date
    hours: HoursStr
    description: str | None
    entered_on_client: bool


class AllocationIn(BaseModel):
    ticket_id: str = Field(min_length=1, max_length=8)
    date: date
    hours: HoursStr
    description: str | None = None


# --- Conversions -----------------------------------------------------------


def _ticket_to_out(t: Ticket) -> TicketOut:
    return TicketOut(
        id=t.id,
        description=t.description,
        archived=t.archived,
        created_at=t.created_at,
        deliverable_id=t.deliverable_id,
    )


def _deliverable_to_out(d: Deliverable) -> DeliverableOut:
    return DeliverableOut(
        id=d.id,
        work_package_id=d.work_package_id,
        title=d.title,
        active=d.active,
    )


def _allocation_to_out(a: TicketAllocation) -> AllocationOut:
    return AllocationOut(
        ticket_id=a.ticket_id,
        date=a.date,
        hours=a.hours,
        description=a.description,
        entered_on_client=a.entered_on_client,
    )


def _entry_to_out(d: date) -> EntryOut:
    entry = storage.get_entry(d)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"no entry for {d.isoformat()}")
    allocated = storage.get_total_allocated_hours(d)
    return EntryOut(
        date=entry.date,
        day_of_week=entry.day_of_week,
        clock_in=entry.clock_in.strftime("%H:%M") if entry.clock_in else None,
        lunch_minutes=int(entry.lunch_duration.total_seconds() // 60)
        if entry.lunch_duration
        else None,
        clock_out=entry.clock_out.strftime("%H:%M") if entry.clock_out else None,
        adjustment_minutes=int(entry.adjustment.total_seconds() // 60)
        if entry.adjustment
        else None,
        adjust_type=entry.adjust_type,
        comment=entry.comment,
        worked_hours=entry.worked_hours,
        adjusted_hours=entry.adjusted_hours,
        total_allocated_hours=allocated,
        allocation_gap_hours=entry.worked_hours - allocated,
    )


# --- App -------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    storage.init_db()
    yield


app = FastAPI(
    title="Timesheets API",
    version="1.0.0",
    summary="Narrow HTTP surface for automating ticket allocation entry.",
    lifespan=_lifespan,
)


@app.get("/health", response_model=HealthOut)
def health() -> HealthOut:
    return HealthOut(db_path=str(storage.DB_PATH))


# --- Entries ---------------------------------------------------------------


@app.get("/entries/{entry_date}", response_model=EntryOut)
def get_entry(entry_date: date) -> EntryOut:
    return _entry_to_out(entry_date)


# --- Tickets ---------------------------------------------------------------


@app.get("/tickets", response_model=list[TicketOut])
def list_tickets(
    q: Annotated[str | None, Query(description="case-insensitive substring match on id or description")] = None,
    include_archived: bool = False,
) -> list[TicketOut]:
    if q:
        tickets = storage.search_tickets(q, include_archived=include_archived)
    else:
        tickets = storage.get_all_tickets(include_archived=include_archived)
    return [_ticket_to_out(t) for t in tickets]


@app.get("/tickets/{ticket_id}", response_model=TicketOut)
def get_ticket(ticket_id: str) -> TicketOut:
    ticket = storage.get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"ticket {ticket_id!r} not found")
    return _ticket_to_out(ticket)


@app.post("/tickets", response_model=TicketOut, status_code=201)
def create_ticket(payload: Annotated[TicketIn, Body()]) -> TicketOut:
    if storage.get_ticket(payload.id) is not None:
        raise HTTPException(status_code=409, detail=f"ticket {payload.id!r} already exists")
    if payload.deliverable_id is not None:
        if storage.get_deliverable(payload.deliverable_id) is None:
            raise HTTPException(
                status_code=422,
                detail=f"deliverable {payload.deliverable_id!r} not found",
            )
    storage.save_ticket(
        Ticket(
            id=payload.id,
            description=payload.description,
            deliverable_id=payload.deliverable_id,
        )
    )
    created = storage.get_ticket(payload.id)
    assert created is not None
    return _ticket_to_out(created)


@app.post("/tickets/{ticket_id}/archive", response_model=TicketOut)
def archive_ticket(ticket_id: str) -> TicketOut:
    if storage.get_ticket(ticket_id) is None:
        raise HTTPException(status_code=404, detail=f"ticket {ticket_id!r} not found")
    storage.archive_ticket(ticket_id)
    updated = storage.get_ticket(ticket_id)
    assert updated is not None
    return _ticket_to_out(updated)


@app.post("/tickets/{ticket_id}/unarchive", response_model=TicketOut)
def unarchive_ticket(ticket_id: str) -> TicketOut:
    if storage.get_ticket(ticket_id) is None:
        raise HTTPException(status_code=404, detail=f"ticket {ticket_id!r} not found")
    storage.unarchive_ticket(ticket_id)
    updated = storage.get_ticket(ticket_id)
    assert updated is not None
    return _ticket_to_out(updated)


# --- Deliverables ----------------------------------------------------------


@app.get("/deliverables", response_model=list[DeliverableOut])
def list_deliverables(active_only: bool = True) -> list[DeliverableOut]:
    return [_deliverable_to_out(d) for d in storage.get_all_deliverables(active_only=active_only)]


# --- Allocations -----------------------------------------------------------


@app.get("/allocations/{alloc_date}", response_model=list[AllocationOut])
def list_allocations(alloc_date: date) -> list[AllocationOut]:
    return [_allocation_to_out(a) for a in storage.get_allocations_for_date(alloc_date)]


@app.get("/allocations/month/{year}/{month}", response_model=list[AllocationOut])
def list_allocations_month(
    year: Annotated[int, Path(ge=2000, le=2100)],
    month: Annotated[int, Path(ge=1, le=12)],
) -> list[AllocationOut]:
    return [_allocation_to_out(a) for a in storage.get_allocations_for_month(year, month)]


@app.post("/allocations", response_model=AllocationOut, status_code=201)
def upsert_allocation(payload: Annotated[AllocationIn, Body()]) -> AllocationOut:
    if storage.get_ticket(payload.ticket_id) is None:
        raise HTTPException(
            status_code=404,
            detail=f"ticket {payload.ticket_id!r} not found - create it first via POST /tickets",
        )
    storage.save_allocation(
        TicketAllocation(
            ticket_id=payload.ticket_id,
            date=payload.date,
            hours=payload.hours,
            description=payload.description,
        )
    )
    for a in storage.get_allocations_for_date(payload.date):
        if a.ticket_id == payload.ticket_id:
            return _allocation_to_out(a)
    raise HTTPException(status_code=500, detail="allocation save round-trip failed")


@app.delete("/allocations/{ticket_id}/{alloc_date}", status_code=204)
def delete_allocation(ticket_id: str, alloc_date: date) -> None:
    existing = [
        a
        for a in storage.get_allocations_for_date(alloc_date)
        if a.ticket_id == ticket_id
    ]
    if not existing:
        raise HTTPException(
            status_code=404,
            detail=f"no allocation for ticket {ticket_id!r} on {alloc_date.isoformat()}",
        )
    storage.delete_allocation(ticket_id, alloc_date)
