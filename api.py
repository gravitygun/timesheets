"""HTTP API exposing a narrow slice of timesheet operations.

The intent is automation: an external client (e.g. an AI assistant on a remote
dev machine, reached over an SSH RemoteForward) records attendance, reads
tickets and posts ticket allocations. The TUI remains the source of truth
for everything else (config, work packages, deliverables, billing, ticket
deletion).

All operations delegate to ``storage.py`` so the TUI and the API share a
single SQLite database (WAL mode is enabled in ``storage.init_db``).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, AsyncIterator

from fastapi import Body, FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

import storage
from models import Deliverable, Ticket, TicketAllocation, TimeEntry


# --- Serialisation helpers -------------------------------------------------

# Hours are sent and received as strings to avoid float drift.
HoursStr = Annotated[
    Decimal,
    PlainSerializer(lambda v: format(v.quantize(Decimal("0.01")), "f"), return_type=str),
]

# Adjustment types this endpoint will accept. Note the TUI also recognises
# "T" (Training) - see utils.ADJUST_TYPES and screens.py - so an existing
# Training day cannot currently be rewritten through the API.
ADJUST_TYPES = ("L", "P", "S")


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


class EntryIn(BaseModel):
    """Full-replacement body for PUT /entries/{date}.

    This is a PUT, not a PATCH: omitted optional fields are cleared on an
    existing entry rather than left alone.
    """

    clock_in: str = Field(description='24-hour "HH:MM", e.g. "09:15"')
    clock_out: str = Field(description='24-hour "HH:MM", e.g. "17:15"')
    lunch_minutes: int = Field(ge=0)
    adjustment_minutes: int | None = Field(default=None, ge=0)
    adjust_type: str | None = None
    comment: str | None = None


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


class TicketPatch(BaseModel):
    """Partial ticket update - only the fields supplied are changed.

    A supplied ``id`` that differs from the path's is a rename; allocations
    follow the ticket so nothing is orphaned.
    """

    id: str | None = Field(default=None, min_length=1, max_length=8)
    description: str | None = Field(default=None, min_length=1)
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


class AllocationPatch(BaseModel):
    """Partial allocation update; only the fields supplied are changed.

    Changing ``hours`` clears ``entered_on_client`` unless this same body sets
    it - the client's system is holding the old figure. Changing only the
    description leaves the flag alone, since the billed figure is unaffected.
    """

    hours: HoursStr | None = None
    description: str | None = None
    entered_on_client: bool | None = None


class MarkEnteredIn(BaseModel):
    """Date range (inclusive both ends) to flip entered_on_client across."""

    model_config = ConfigDict(populate_by_name=True)

    # "from" is a keyword, so the field is named from_ and aliased on the wire.
    from_: date = Field(alias="from")
    to: date
    entered_on_client: bool = True


class AllocationRef(BaseModel):
    ticket_id: str
    date: date


class MarkEnteredOut(BaseModel):
    changed: int = Field(description="how many allocations actually flipped")
    entered_on_client: bool
    allocations: list[AllocationRef]


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


def _parse_clock(value: str, field: str) -> time:
    """Parse an "HH:MM" clock string, or raise a 422 naming the field."""
    try:
        return datetime.strptime(value.strip(), "%H:%M").time()
    except ValueError:
        raise HTTPException(
            status_code=422,
            detail=f'{field} must be a 24-hour "HH:MM" time, got {value!r}',
        ) from None


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


@app.put("/entries/{entry_date}", response_model=EntryOut)
def put_entry(
    entry_date: date,
    payload: Annotated[EntryIn, Body()],
) -> EntryOut:
    """Create or replace the attendance record for a date.

    The billed check comes first: once a day's work has been entered on the
    client's system the record is closed, and no amount of well-formed input
    should quietly rewrite it.
    """
    # A day counts as billed only if it *has* allocations and every one of
    # them has been entered client-side. Guarding on `all()` alone would
    # reject every date with no allocations at all, i.e. most of them.
    allocations = storage.get_allocations_for_date(entry_date)
    if allocations and all(a.entered_on_client for a in allocations):
        raise HTTPException(
            status_code=409,
            detail=f"entry for {entry_date.isoformat()} is already billed",
        )

    clock_in = _parse_clock(payload.clock_in, "clock_in")
    clock_out = _parse_clock(payload.clock_out, "clock_out")
    if clock_out <= clock_in:
        raise HTTPException(
            status_code=422,
            detail=(
                f"clock_out ({payload.clock_out}) must be after "
                f"clock_in ({payload.clock_in})"
            ),
        )

    # Lunch is subtracted from the clocked span, so an oversized value would
    # produce negative worked_hours and quietly corrupt the billing figures.
    span_minutes = (
        clock_out.hour * 60 + clock_out.minute
    ) - (clock_in.hour * 60 + clock_in.minute)
    if payload.lunch_minutes > span_minutes:
        raise HTTPException(
            status_code=422,
            detail=(
                f"lunch_minutes ({payload.lunch_minutes}) cannot exceed the "
                f"{span_minutes} minutes between clock_in and clock_out"
            ),
        )

    adjust_type = payload.adjust_type
    if adjust_type is not None:
        adjust_type = adjust_type.strip().upper()
        if adjust_type not in ADJUST_TYPES:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"adjust_type must be one of {', '.join(ADJUST_TYPES)}, "
                    f"got {payload.adjust_type!r}"
                ),
            )

    # CLAUDE.md: adjustment hours require a type. The TUI enforces this, and
    # max_hours only subtracts "P" adjustments, so an untyped one is data the
    # rest of the app cannot reason about.
    if payload.adjustment_minutes and adjust_type is None:
        raise HTTPException(
            status_code=422,
            detail="adjustment_minutes requires an adjust_type",
        )

    storage.save_entry(
        TimeEntry(
            date=entry_date,
            day_of_week=entry_date.strftime("%a"),
            clock_in=clock_in,
            lunch_duration=timedelta(minutes=payload.lunch_minutes)
            if payload.lunch_minutes
            else None,
            clock_out=clock_out,
            adjustment=timedelta(minutes=payload.adjustment_minutes)
            if payload.adjustment_minutes
            else None,
            adjust_type=adjust_type,
            comment=payload.comment,
        )
    )
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


@app.patch("/tickets/{ticket_id}", response_model=TicketOut)
def patch_ticket(
    ticket_id: str,
    payload: Annotated[TicketPatch, Body()],
) -> TicketOut:
    """Update a ticket's description, deliverable and/or ID.

    Renaming goes through ``storage.rename_ticket``, which moves the ticket
    and its allocations together in one transaction.
    """
    ticket = storage.get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail=f"ticket {ticket_id!r} not found")

    # Validate before mutating anything, so a rejected patch changes nothing.
    if payload.deliverable_id is not None:
        if storage.get_deliverable(payload.deliverable_id) is None:
            raise HTTPException(
                status_code=422,
                detail=f"deliverable {payload.deliverable_id!r} not found",
            )

    if payload.id is not None and payload.id != ticket.id:
        if not storage.rename_ticket(ticket.id, payload.id):
            raise HTTPException(
                status_code=409, detail=f"ticket {payload.id!r} already exists"
            )
        ticket.id = payload.id

    if payload.description is not None or payload.deliverable_id is not None:
        if payload.description is not None:
            ticket.description = payload.description
        if payload.deliverable_id is not None:
            ticket.deliverable_id = payload.deliverable_id
        # Writes the whole row, so billed/points_entered state must ride along.
        storage.save_ticket(ticket)

    updated = storage.get_ticket(ticket.id)
    assert updated is not None
    return _ticket_to_out(updated)


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


@app.post("/allocations/mark-entered", response_model=MarkEnteredOut)
def mark_allocations_entered(
    payload: Annotated[MarkEnteredIn, Body()],
) -> MarkEnteredOut:
    """Flip entered_on_client across a date range in one call.

    Reports only the allocations that actually changed, so re-running over the
    same range reports nothing rather than re-claiming work.
    """
    if payload.from_ > payload.to:
        raise HTTPException(
            status_code=422,
            detail=(
                f"'from' ({payload.from_.isoformat()}) must not be after "
                f"'to' ({payload.to.isoformat()})"
            ),
        )
    changed = storage.mark_allocations_entered(
        payload.from_, payload.to, payload.entered_on_client,
    )
    return MarkEnteredOut(
        changed=len(changed),
        entered_on_client=payload.entered_on_client,
        allocations=[
            AllocationRef(ticket_id=tid, date=d) for tid, d in changed
        ],
    )


@app.patch(
    "/allocations/{ticket_id}/{alloc_date}", response_model=AllocationOut,
)
def patch_allocation(
    ticket_id: str,
    alloc_date: date,
    payload: Annotated[AllocationPatch, Body()],
) -> AllocationOut:
    """Update one allocation's hours, description and/or entered flag."""
    found = storage.update_allocation(
        ticket_id,
        alloc_date,
        hours=payload.hours,
        description=payload.description,
        entered_on_client=payload.entered_on_client,
    )
    if not found:
        raise HTTPException(
            status_code=404,
            detail=(
                f"no allocation for ticket {ticket_id!r} on "
                f"{alloc_date.isoformat()}"
            ),
        )
    for a in storage.get_allocations_for_date(alloc_date):
        if a.ticket_id == ticket_id:
            return _allocation_to_out(a)
    raise HTTPException(status_code=500, detail="allocation read-back failed")


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
