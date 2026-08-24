from dataclasses import dataclass
from datetime import date, time
from typing import Protocol

from scheduling.models import AvailabilityType, Employee, EmployeeAvailability


@dataclass(frozen=True)
class AvailabilityResult:
    available: bool
    availability_type: str
    reasons: tuple[str, ...]


class AvailabilityProvider(Protocol):
    def check(
        self,
        employee: Employee,
        shift_date: date,
        start_time: time,
        end_time: time,
    ) -> AvailabilityResult: ...


class LocalAvailabilityProvider:
    """Read availability from the local management-maintained availability table."""

    def check(
        self,
        employee: Employee,
        shift_date: date,
        start_time: time,
        end_time: time,
    ) -> AvailabilityResult:
        try:
            entry = EmployeeAvailability.objects.get(employee=employee, date=shift_date)
        except EmployeeAvailability.DoesNotExist:
            return AvailabilityResult(
                False,
                AvailabilityType.UNKNOWN,
                ("Availability is unknown for this date.",),
            )

        if entry.availability_type == AvailabilityType.AVAILABLE_ALL_DAY:
            return AvailabilityResult(True, entry.availability_type, ("Available all day.",))
        if entry.availability_type == AvailabilityType.AVAILABLE_WINDOW:
            if entry.start_time <= start_time and entry.end_time >= end_time:
                return AvailabilityResult(
                    True,
                    entry.availability_type,
                    (f"Available from {entry.start_time:%H:%M} to {entry.end_time:%H:%M}.",),
                )
            return AvailabilityResult(
                False,
                entry.availability_type,
                (f"Available only from {entry.start_time:%H:%M} to {entry.end_time:%H:%M}.",),
            )
        if entry.availability_type == AvailabilityType.UNAVAILABLE:
            return AvailabilityResult(False, entry.availability_type, ("Marked unavailable.",))
        return AvailabilityResult(
            False,
            AvailabilityType.UNKNOWN,
            ("Availability is unknown and cannot be treated as available.",),
        )
