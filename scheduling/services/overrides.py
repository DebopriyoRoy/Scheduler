"""Manual availability changes laid over what Square says.

Square is the source of truth for availability and time off, but it only knows what
staff have typed into it. The rest - a swap agreed at the bar, a call-in, a training
day - is recorded here as an AvailabilityOverride. Three rules govern them:

1. They are never touched by a Square refresh. Square rows are rewritten wholesale on
   every sync; overrides live in their own table.
2. They win. A removal makes someone unavailable whatever Square says; an addition
   makes them available whatever Square says about their hours.
3. Time off is the one exception, and only when it is new. An addition recorded while
   approved time off already existed was a deliberate decision, so it stands. Time off
   approved *after* the addition is Square changing its mind, and that is a conflict:
   the time off blocks until somebody chooses - keep the change, or use Square's.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time

from django.db import transaction
from django.utils import timezone

from scheduling.models import (
    AvailabilityOverride,
    AvailabilityType,
    Employee,
    EmployeeAvailability,
    EmployeeTimeOff,
    OverrideAction,
    TimeOffSource,
    TimeOffStatus,
)


def sync_owned_sources() -> tuple[str, ...]:
    # Imported late: the sync service pulls in the browser providers, which nothing
    # that only reads availability should have to load.
    from scheduling.integrations.square_availability.service import SYNC_OWNED_SOURCES

    return tuple(SYNC_OWNED_SOURCES)


def active_override(employee: Employee, day: date) -> AvailabilityOverride | None:
    return AvailabilityOverride.objects.filter(employee=employee, date=day, active=True).first()


def approved_time_off(employee: Employee, day: date) -> list[EmployeeTimeOff]:
    return list(
        EmployeeTimeOff.objects.filter(
            employee=employee,
            status=TimeOffStatus.APPROVED,
            start_date__lte=day,
            end_date__gte=day,
        )
    )


@dataclass(frozen=True)
class SquareView:
    """What Square itself says about one person on one date."""

    summary: str
    state: str
    start_time: time | None = None
    end_time: time | None = None


def square_view(employee: Employee, day: date) -> SquareView:
    """Square's own availability for the date, ignoring anything entered here."""
    rows = list(
        EmployeeAvailability.objects.filter(
            employee=employee, date=day, source__in=sync_owned_sources()
        ).order_by("start_time")
    )
    types = {row.availability_type for row in rows}
    if AvailabilityType.AVAILABLE_ALL_DAY in types:
        return SquareView("All day", AvailabilityType.AVAILABLE_ALL_DAY)
    windows = [
        row
        for row in rows
        if row.availability_type == AvailabilityType.AVAILABLE_WINDOW
        and row.start_time
        and row.end_time
    ]
    if windows:
        text = ", ".join(f"{w.start_time:%H:%M}–{w.end_time:%H:%M}" for w in windows)
        return SquareView(
            text,
            AvailabilityType.AVAILABLE_WINDOW,
            min(w.start_time for w in windows),
            max(w.end_time for w in windows),
        )
    if AvailabilityType.UNAVAILABLE in types:
        return SquareView("Not available", AvailabilityType.UNAVAILABLE)
    return SquareView("Nothing in Square", AvailabilityType.UNKNOWN)


def is_in_conflict(override: AvailabilityOverride) -> bool:
    """An addition that newer approved time off now contradicts, and nobody has ruled on."""
    if not override.active or override.action != OverrideAction.ADD:
        return False
    if override.time_off_at_creation or override.conflict_kept_at:
        return False
    return bool(approved_time_off(override.employee, override.date))


def supersedes_time_off(employee: Employee, day: date) -> bool:
    """Whether an addition for this date overrules approved time off.

    True when the addition was made knowing about the time off, or when a conflict was
    settled in the addition's favour. An unsettled conflict is False: the time off
    blocks until a person decides, because guessing wrong puts somebody on a shift
    they have booked off.
    """
    override = active_override(employee, day)
    if override is None or override.action != OverrideAction.ADD:
        return False
    return bool(override.time_off_at_creation or override.conflict_kept_at)


def conflicts_on(day: date) -> list[AvailabilityOverride]:
    candidates = AvailabilityOverride.objects.filter(
        date=day, active=True, action=OverrideAction.ADD
    ).select_related("employee", "created_by")
    return [override for override in candidates if is_in_conflict(override)]


@transaction.atomic
def record_override(
    employee: Employee,
    day: date,
    action: str,
    reason: str,
    user=None,
    start_time: time | None = None,
    end_time: time | None = None,
) -> AvailabilityOverride:
    """Record a manual change, retiring whatever change was already in force."""
    view = square_view(employee, day)
    override = AvailabilityOverride(
        employee=employee,
        date=day,
        action=action,
        reason=(reason or "").strip(),
        start_time=start_time,
        end_time=end_time,
        square_state_at_creation=view.summary,
        time_off_at_creation=bool(approved_time_off(employee, day)),
        created_by=user if getattr(user, "is_authenticated", False) else None,
    )
    override.full_clean(exclude=["employee"])

    previous = active_override(employee, day)
    if previous is not None:
        retire_override(previous, user, "Replaced by a newer change.")
    override.save()
    return override


def retire_override(override: AvailabilityOverride, user=None, reason: str = "") -> None:
    """Stop an override applying. Kept, not deleted: the audit trail needs it."""
    override.active = False
    override.retired_at = timezone.now()
    override.retired_by = user if getattr(user, "is_authenticated", False) else None
    override.retired_reason = (reason or "").strip()[:300]
    override.save(update_fields=["active", "retired_at", "retired_by", "retired_reason"])


def keep_override(override: AvailabilityOverride, user=None) -> None:
    """Settle a conflict in the override's favour: it now wins over the time off."""
    override.conflict_kept_at = timezone.now()
    override.conflict_kept_by = user if getattr(user, "is_authenticated", False) else None
    override.save(update_fields=["conflict_kept_at", "conflict_kept_by"])


def use_square(override: AvailabilityOverride, user=None) -> None:
    """Settle a conflict in Square's favour: the override is retired."""
    retire_override(override, user, "Square's time off used instead.")


def square_time_off(employee: Employee, day: date) -> list[EmployeeTimeOff]:
    """Every time-off entry covering the date, any status, Square's first."""
    entries = EmployeeTimeOff.objects.filter(
        employee=employee, start_date__lte=day, end_date__gte=day
    ).exclude(status__in=[TimeOffStatus.DECLINED, TimeOffStatus.CANCELLED])
    return sorted(entries, key=lambda e: (e.source != TimeOffSource.SQUARE, e.start_date))
