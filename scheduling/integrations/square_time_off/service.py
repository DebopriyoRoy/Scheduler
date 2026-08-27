"""Bringing Square's time off into the local table."""

from __future__ import annotations

from dataclasses import dataclass, field

from scheduling.integrations.square_time_off.live_provider import fetch_time_off_rows
from scheduling.integrations.square_time_off.normalizer import TimeOffRow, parse_rows
from scheduling.models import Employee, EmployeeTimeOff, TimeOffSource


@dataclass
class TimeOffSyncResult:
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    unmatched: list[str] = field(default_factory=list)
    rows_seen: int = 0

    @property
    def summary(self) -> str:
        parts = [
            f"{self.rows_seen} row{'' if self.rows_seen == 1 else 's'} read",
            f"{self.created} added",
            f"{self.updated} updated",
            f"{self.unchanged} unchanged",
        ]
        if self.unmatched:
            names = ", ".join(sorted(set(self.unmatched)))
            parts.append(f"{len(self.unmatched)} unmatched ({names})")
        return ", ".join(parts) + "."


def _match(name: str) -> Employee | None:
    """Square's own spelling first, then the first name.

    Names are typed into two systems by different people, so an exact match cannot be
    the only route - but a first-name match is only safe while it is unambiguous.
    """
    employee = Employee.objects.filter(display_name__iexact=name, active=True).first()
    if employee:
        return employee
    first = name.split()[0]
    matches = Employee.objects.filter(first_name__iexact=first, active=True)
    return matches.first() if matches.count() == 1 else None


def sync_time_off(rows: list[TimeOffRow] | None = None) -> TimeOffSyncResult:
    """Replace this employee's Square-sourced entries with what Square now holds.

    Square rows carry no stable id in the page, so a row is identified by the person
    and the dates it covers. Entries typed in here by hand are never touched - they
    have their own source and are somebody's deliberate decision.
    """
    if rows is None:
        rows = parse_rows(fetch_time_off_rows())

    result = TimeOffSyncResult(rows_seen=len(rows))
    seen_keys = []
    for row in rows:
        employee = _match(row.employee_name)
        if employee is None:
            result.unmatched.append(row.employee_name)
            continue

        key = (employee.id, row.start_date, row.end_date)
        seen_keys.append(key)
        existing = EmployeeTimeOff.objects.filter(
            employee=employee,
            start_date=row.start_date,
            end_date=row.end_date,
            source=TimeOffSource.SQUARE,
        ).first()

        if existing is None:
            EmployeeTimeOff.objects.create(
                employee=employee,
                start_date=row.start_date,
                end_date=row.end_date,
                status=row.status,
                reason=row.reason,
                source=TimeOffSource.SQUARE,
                requested_time=row.requested_time,
                approved_all_day=row.approved_all_day,
                approved_partial=row.approved_partial,
            )
            result.created += 1
        elif (
            existing.status != row.status
            or existing.reason != row.reason
            or existing.requested_time != row.requested_time
            or existing.approved_all_day != row.approved_all_day
            or existing.approved_partial != row.approved_partial
        ):
            existing.status = row.status
            existing.reason = row.reason
            existing.requested_time = row.requested_time
            existing.approved_all_day = row.approved_all_day
            existing.approved_partial = row.approved_partial
            existing.save(
                update_fields=[
                    "status", "reason", "requested_time",
                    "approved_all_day", "approved_partial",
                ]
            )
            result.updated += 1
        else:
            result.unchanged += 1

    # A request withdrawn in Square must stop blocking rosters here too.
    stale = EmployeeTimeOff.objects.filter(source=TimeOffSource.SQUARE)
    for entry in stale:
        if (entry.employee_id, entry.start_date, entry.end_date) not in seen_keys:
            entry.delete()

    return result
