from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from scheduling.models import AvailabilityType, Employee, EmployeeAvailability

ST_JOHNS_TZ = ZoneInfo("America/St_Johns")


# The shortest stretch worth calling somebody in for. Below this the trip to work
# costs more than the shift is worth, so a narrower overlap is treated as no overlap.
MIN_FITTED_SHIFT_HOURS = 3.0

# ...except when the alternative is nobody at all. A slot that would otherwise sit
# empty leaves the room short, which is worse than a short shift, so the engine may
# reach down to here to fill one - and only to fill one.
ABSOLUTE_MIN_SHIFT_HOURS = 2.0


def _interval(day: date, start: time, end: time) -> tuple[datetime, datetime]:
    """Concrete start/end for a window, rolling past midnight when it wraps."""
    begin = datetime.combine(day, start, tzinfo=ST_JOHNS_TZ)
    finish_day = day + timedelta(days=1) if end <= start else day
    return begin, datetime.combine(finish_day, end, tzinfo=ST_JOHNS_TZ)


@dataclass(frozen=True)
class AvailabilityResult:
    available: bool
    availability_type: str
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class FittedWindow:
    """The part of a shift an employee can actually work.

    The engine used to demand that availability cover a call window end to end, and
    rejected anyone who fell short by so much as a minute. A human scheduler does not
    think that way: someone free 16:00-20:30 is not "unavailable" for a shift that runs
    to 23:00, they are available for the first three hours of it. This is that overlap.
    """

    start_time: time
    end_time: time
    hours: float
    covers_full_shift: bool


class AvailabilityProvider(Protocol):
    def check(
        self,
        employee: Employee,
        shift_date: date,
        start_time: time,
        end_time: time,
    ) -> AvailabilityResult: ...

    def fit(
        self,
        employee: Employee,
        shift_date: date,
        start_time: time,
        end_time: time,
        minimum_hours: float = MIN_FITTED_SHIFT_HOURS,
    ) -> "FittedWindow | None": ...


class LocalAvailabilityProvider:
    """Read availability from the local management-maintained availability table."""

    def fit(
        self,
        employee: Employee,
        shift_date: date,
        start_time: time,
        end_time: time,
        minimum_hours: float = MIN_FITTED_SHIFT_HOURS,
    ) -> FittedWindow | None:
        return _fit_local(employee, shift_date, start_time, end_time, minimum_hours)

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


def _fit_local(
    employee: Employee,
    shift_date: date,
    start_time: time,
    end_time: time,
    minimum_hours: float = MIN_FITTED_SHIFT_HOURS,
) -> FittedWindow | None:
    """The largest slice of this shift the employee is actually free for.

    Returns None when they cannot work a usable part of it: nothing on file for the
    date, marked unavailable, or an overlap too short to be worth the trip. An unknown
    is still a hard no - a blank record is not permission.
    """
    entries = list(EmployeeAvailability.objects.filter(employee=employee, date=shift_date))
    if not entries:
        return None
    if any(e.availability_type == AvailabilityType.UNAVAILABLE for e in entries):
        return None

    shift_start, shift_end = _interval(shift_date, start_time, end_time)
    full_hours = (shift_end - shift_start).total_seconds() / 3600

    if any(e.availability_type == AvailabilityType.AVAILABLE_ALL_DAY for e in entries):
        return FittedWindow(start_time, end_time, round(full_hours, 2), True)

    best: tuple[datetime, datetime] | None = None
    for entry in entries:
        if entry.availability_type != AvailabilityType.AVAILABLE_WINDOW:
            continue
        if not entry.start_time or not entry.end_time:
            continue
        free_start, free_end = _interval(shift_date, entry.start_time, entry.end_time)
        overlap_start = max(shift_start, free_start)
        overlap_end = min(shift_end, free_end)
        if overlap_end <= overlap_start:
            continue
        if best is None or (overlap_end - overlap_start) > (best[1] - best[0]):
            best = (overlap_start, overlap_end)

    if best is None:
        return None
    hours = (best[1] - best[0]).total_seconds() / 3600
    # A shift shorter than the minimum is still a real shift: the 50/50 runs well
    # under three hours by design, and a floor above its own length would make the
    # position impossible to staff by anyone at all. Never ask for more than the
    # shift actually is.
    if hours + 1e-9 < min(minimum_hours, full_hours):
        return None
    return FittedWindow(
        best[0].time(),
        best[1].time(),
        round(hours, 2),
        # Equal within a second: float hours alone would call 5.999999 a partial shift.
        abs((best[1] - best[0]).total_seconds() - (shift_end - shift_start).total_seconds()) < 1,
    )
