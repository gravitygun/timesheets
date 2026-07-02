from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time, timedelta
from decimal import Decimal


@dataclass
class TimeEntry:
    date: date
    day_of_week: str
    clock_in: time | None = None
    lunch_duration: timedelta | None = None
    clock_out: time | None = None
    adjustment: timedelta | None = None
    adjust_type: str | None = None
    comment: str | None = None

    @property
    def worked_hours(self) -> Decimal:
        """Calculate hours worked as decimal."""
        if not self.clock_in or not self.clock_out:
            return Decimal("0")

        start = timedelta(hours=self.clock_in.hour, minutes=self.clock_in.minute)
        end = timedelta(hours=self.clock_out.hour, minutes=self.clock_out.minute)
        lunch = self.lunch_duration or timedelta()

        worked = end - start - lunch
        return Decimal(str(worked.total_seconds() / 3600)).quantize(Decimal("0.01"))

    @property
    def adjusted_hours(self) -> Decimal:
        """Adjustment hours as decimal."""
        if not self.adjustment:
            return Decimal("0")
        return Decimal(str(self.adjustment.total_seconds() / 3600)).quantize(Decimal("0.01"))

    @property
    def total_hours(self) -> Decimal:
        """Total billable hours (worked + adjustment)."""
        return self.worked_hours + self.adjusted_hours


@dataclass
class Config:
    hourly_rate: Decimal = Decimal("97")
    currency: str = "GBP"
    standard_day_hours: Decimal = Decimal("7.5")
    vat_rate: Decimal = Decimal("0.20")
    hours_per_point: Decimal = Decimal("2")
    point_rate: Decimal = Decimal("200")
    points_start_date: date | None = None
    contract_start: date | None = None
    contract_end: date | None = None
    annual_max_points: int = 960


@dataclass
class Ticket:
    """A ticket/project that time can be allocated to."""

    id: str  # Max 8 characters
    description: str
    archived: bool = False
    created_at: date | None = None
    points_entered: bool = False
    deliverable_id: str | None = None
    billed: bool = False
    billed_year: int | None = None
    billed_month: int | None = None


@dataclass
class TicketAllocation:
    """Hours allocated to a ticket on a specific date."""

    ticket_id: str
    date: date
    hours: Decimal
    description: str | None = None
    entered_on_client: bool = False


@dataclass
class WorkPackage:
    """A work package grouping related deliverables."""

    id: str
    title: str


@dataclass
class Deliverable:
    """A billable deliverable within a work package."""

    id: str
    work_package_id: str
    title: str
    active: bool = True


@dataclass
class MonthlyBilling:
    """Tracks billing finalisation state for a month."""

    year: int
    month: int
    finalised: bool = False


@dataclass
class InvoiceSettings:
    """Fixed 'who's billing whom' details for generating invoices.

    Single company / single customer only (current situation). Defaults are
    seeded from the existing paper invoices so the form is usable immediately.
    Address fields are multi-line, newline-separated.
    """

    company_name: str = "BLITTERBYTE CONSULTING LIMITED"
    company_address: str = "1 Cedar Office Park\nCobham Road\nWimborne"
    company_postcode: str = "BH21 7SB"
    company_vat_number: str = "477154076"
    company_reg_number: str = "15953233"
    bank_sort_code: str = "040333"
    bank_account_number: str = "17169396"
    customer_name: str = "LA International Accounts Payable"
    customer_address: str = "International House\nFestival Way\nStoke-on-Trent"
    customer_postcode: str = "ST1 5UB"
    contract_po: str = "127127"
    last_invoice_number: int = 0
