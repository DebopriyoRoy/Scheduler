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
        entries = list(EmployeeAvailability.objects.filter(employee=employee, date=shift_date))
        if not entries:
            return AvailabilityResult(
                False,
                AvailabilityType.UNKNOWN,
                ("Availability is unknown for this date.",),
            )

        if any(entry.availability_type == AvailabilityType.AVAILABLE_ALL_DAY for entry in entries):
            return AvailabilityResult(
                True, AvailabilityType.AVAILABLE_ALL_DAY, ("Available all day.",)
            )

        window_entries = [
            e for e in entries if e.availability_type == AvailabilityType.AVAILABLE_WINDOW
        ]
        for entry in window_entries:
            if entry.start_time and entry.end_time:
                if entry.start_time <= start_time and entry.end_time >= end_time:
                    return AvailabilityResult(
                        True,
                        entry.availability_type,
                        (
                            f"Available from {entry.start_time:%H:%M} to {entry.end_time:%H:%M}.",
                        ),
                    )

        if any(entry.availability_type == AvailabilityType.UNAVAILABLE for entry in entries):
            return AvailabilityResult(
                False, AvailabilityType.UNAVAILABLE, ("Marked unavailable.",)
            )

        if window_entries:
            windows_str = ", ".join(
                f"{e.start_time:%H:%M}–{e.end_time:%H:%M}"
                for e in window_entries
                if e.start_time and e.end_time
            )
            return AvailabilityResult(
                False,
                AvailabilityType.AVAILABLE_WINDOW,
                (
                    f"Shift window {start_time:%H:%M}–{end_time:%H:%M} is not "
                    f"fully covered by available window(s): {windows_str}.",
                ),
            )

        return AvailabilityResult(
            False,
            AvailabilityType.UNKNOWN,
            ("Availability is unknown and cannot be treated as available.",),
        )
