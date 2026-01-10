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
