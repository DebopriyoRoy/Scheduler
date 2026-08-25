from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from scheduling.models import AvailabilityType, Employee, EmployeeAvailability

ST_JOHNS_TZ = ZoneInfo("America/St_Johns")


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

        # Build timezone-aware shift datetimes in America/St_Johns
        s_start_dt = datetime.combine(shift_date, start_time, tzinfo=ST_JOHNS_TZ)
        if end_time <= start_time:
            s_end_dt = datetime.combine(
                shift_date + timedelta(days=1), end_time, tzinfo=ST_JOHNS_TZ
            )
        else:
            s_end_dt = datetime.combine(shift_date, end_time, tzinfo=ST_JOHNS_TZ)

        window_entries = [
            e for e in entries if e.availability_type == AvailabilityType.AVAILABLE_WINDOW
        ]
        for entry in window_entries:
            if entry.start_time and entry.end_time:
                a_start_dt = datetime.combine(
                    shift_date, entry.start_time, tzinfo=ST_JOHNS_TZ
                )
                if entry.end_time <= entry.start_time:
                    a_end_dt = datetime.combine(
                        shift_date + timedelta(days=1), entry.end_time, tzinfo=ST_JOHNS_TZ
                    )
                else:
                    a_end_dt = datetime.combine(
                        shift_date, entry.end_time, tzinfo=ST_JOHNS_TZ
                    )

                if a_start_dt <= s_start_dt and a_end_dt >= s_end_dt:
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
