"""Read what Square actually holds for a schedule run and reconcile it locally.

Synchronisation only ever ran outward: a roster was pushed to Square as drafts, and
anything management then changed there - swapping a bartender, shifting a start time,
adding someone the engine never considered - stayed invisible to this application. The
local run silently stopped describing reality, and the next generated schedule would
quietly undo those decisions.

Reading is deliberately separate from adopting. `compare_run_with_square` only looks;
`adopt_square_version` writes the differences into the local run, recording each as a
management override. Neither ever writes to Square.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import transaction
from django.utils import timezone

from integrations.square.client import SquareClient
from integrations.square.config import SquareConfig, SquareEnvironment
from scheduling.models import (
    AssignmentType,
    Employee,
    Role,
    ScheduleAssignment,
    ScheduleRun,
    ShiftTemplate,
    Show,
    SquareEmployeeMapping,
    SquareLocationMapping,
    SquareRoleMapping,
)
from scheduling.services.availability import LocalAvailabilityProvider

MATCHED = "MATCHED"
ADDED_IN_SQUARE = "ADDED_IN_SQUARE"
REMOVED_FROM_SQUARE = "REMOVED_FROM_SQUARE"
EDITED_IN_SQUARE = "EDITED_IN_SQUARE"
UNMAPPED = "UNMAPPED"


class SquareReadError(RuntimeError):
    """Square could not be read. The message is safe to show a user."""


@dataclass
class ShiftDifference:
    kind: str
    date: dt.date
    employee: Employee | None
    employee_name: str
    local: dict | None = None
    square: dict | None = None
    availability_note: str = ""
    square_shift_id: str = ""

    @property
    def is_difference(self) -> bool:
        return self.kind != MATCHED


@dataclass
class ReconcileReport:
    schedule_run: ScheduleRun
    matched: int = 0
    differences: list[ShiftDifference] = field(default_factory=list)
    square_total: int = 0
    local_total: int = 0
    published_count: int = 0
    read_at: dt.datetime | None = None

    def of_kind(self, kind: str) -> list[ShiftDifference]:
        return [d for d in self.differences if d.kind == kind]

    @property
    def has_differences(self) -> bool:
        return bool(self.differences)


def _production_client() -> tuple[SquareClient, str]:
    config = SquareConfig.from_env()
    if not config.token_is_configured:
        raise SquareReadError(
            "no Square access token is saved yet. Add one on the Square connection page."
        )
    mapping = SquareLocationMapping.objects.filter(
        environment=SquareEnvironment.PRODUCTION.value, active=True
    ).first()
    location_id = config.location_id or (mapping.square_location_id if mapping else "")
    if not location_id:
        raise SquareReadError("no Square location is configured.")
    production = SquareConfig(
        environment=SquareEnvironment.PRODUCTION,
        sandbox_access_token=config.sandbox_access_token,
        production_access_token=config.production_access_token,
        location_id=location_id,
        api_version=config.api_version,
        request_timeout_seconds=config.request_timeout_seconds,
    )
    return SquareClient(production), location_id


def _local_time(value: str) -> str:
    return value[11:16] if value else ""


def compare_run_with_square(schedule_run: ScheduleRun) -> ReconcileReport:
    """READ ONLY. What Square holds for this run's dates, against what we hold."""
    client, location_id = _production_client()
    env = SquareEnvironment.PRODUCTION.value

    try:
        shifts = client.search_scheduled_shifts(
            {
                "filter": {
                    "start": {
                        "start_at": f"{schedule_run.start_date}T00:00:00Z",
                        "end_at": f"{schedule_run.end_date}T23:59:59Z",
                    },
                    "location_ids": [location_id],
                }
            }
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the user
        raise SquareReadError(str(exc)) from exc

    employee_by_tm = {
        m.square_team_member_id: m.employee
        for m in SquareEmployeeMapping.objects.filter(environment=env).select_related("employee")
        if m.square_team_member_id
    }
    try:
        job_titles = {j["id"]: j["title"] for j in client.list_jobs()}
    except Exception:  # noqa: BLE001 - titles are cosmetic
        job_titles = {}
    job_by_role = {
        m.role_id: m.square_job_id
        for m in SquareRoleMapping.objects.filter(environment=env)
    }
    tm_by_employee = {
        m.employee_id: m.square_team_member_id
        for m in SquareEmployeeMapping.objects.filter(environment=env)
    }

    report = ReconcileReport(schedule_run=schedule_run, read_at=timezone.now())

    square_side: dict[tuple[str, dt.date], dict] = {}
    for shift in shifts:
        draft = shift.get("draft_shift_details")
        details = draft or shift.get("shift_details") or {}
        if draft is None:
            report.published_count += 1
        started = details.get("start_at", "")
        if not started:
            continue
        key = (details.get("team_member_id", ""), dt.date.fromisoformat(started[:10]))
        square_side[key] = {
            "job_id": details.get("job_id", ""),
            "job": job_titles.get(details.get("job_id", ""), "Unknown job"),
            "start": _local_time(started),
            "end": _local_time(details.get("end_at", "")),
            "start_raw": started,
            "end_raw": details.get("end_at", ""),
            "shift_id": shift.get("id", ""),
            "published": draft is None,
        }
    report.square_total = len(square_side)

    local_side: dict[tuple[str, dt.date], ScheduleAssignment] = {}
    for assignment in schedule_run.assignments.select_related(
        "employee", "role", "shift_template", "show"
    ):
        team_member = tm_by_employee.get(assignment.employee_id, "")
        local_side[(team_member, assignment.show.date)] = assignment
    report.local_total = len(local_side)

    provider = LocalAvailabilityProvider()

    def availability_note(employee: Employee, day: dt.date, start: str, end: str) -> str:
        if not (employee and start and end):
            return ""
        try:
            s_h, s_m = (int(part) for part in start.split(":"))
            e_h, e_m = (int(part) for part in end.split(":"))
        except ValueError:
            return ""
        result = provider.check(employee, day, dt.time(s_h, s_m), dt.time(e_h, e_m))
        if result.available:
            return ""
        return "; ".join(result.reasons) or "Outside their recorded availability."

    for key in sorted(set(local_side) | set(square_side), key=lambda k: (k[1], k[0])):
        team_member, day = key
        assignment = local_side.get(key)
        remote = square_side.get(key)
        employee = assignment.employee if assignment else employee_by_tm.get(team_member)
        name = employee.display_name if employee else f"Unmapped ({team_member[:12]})"

        local_view = (
            {
                "job": job_titles.get(
                    job_by_role.get(assignment.role_id, ""), assignment.role.name
                ),
                "start": f"{timezone.localtime(assignment.start_datetime):%H:%M}",
                "end": f"{timezone.localtime(assignment.end_datetime):%H:%M}",
                "position": assignment.shift_template.name,
                "assignment_id": assignment.id,
            }
            if assignment
            else None
        )

        if assignment and remote:
            same = (
                local_view["start"] == remote["start"]
                and local_view["end"] == remote["end"]
                and local_view["job"] == remote["job"]
            )
            if same:
                report.matched += 1
                continue
            kind = EDITED_IN_SQUARE
        elif remote:
            kind = ADDED_IN_SQUARE if employee else UNMAPPED
        else:
            kind = REMOVED_FROM_SQUARE

        report.differences.append(
            ShiftDifference(
                kind=kind,
                date=day,
                employee=employee,
                employee_name=name,
                local=local_view,
                square=remote,
                availability_note=(
                    availability_note(employee, day, remote["start"], remote["end"])
                    if remote
                    else ""
                ),
                square_shift_id=remote["shift_id"] if remote else "",
            )
        )

    return report


def _template_for(role: Role, schedule_run: ScheduleRun, show: Show) -> ShiftTemplate | None:
    """A free position of this role on this show, so the adopted shift has somewhere to sit."""
    taken = set(
        schedule_run.assignments.filter(show=show).values_list("shift_template_id", flat=True)
    )
    return (
        ShiftTemplate.objects.filter(active=True, role=role)
        .exclude(id__in=taken)
        .order_by("position_order")
        .first()
    )


@transaction.atomic
def adopt_square_version(schedule_run: ScheduleRun, report: ReconcileReport, user=None) -> dict:
    """Bring the local run into line with Square. Never writes to Square.

    Adopted shifts are marked as management overrides: they record a decision a person
    made in Square, not something the engine produced, and must not be silently undone.
    Eligibility is not enforced here - Square is the operational record, and refusing to
    write down a shift that is already scheduled would leave the application describing
    a roster that does not exist. Anything outside a person's availability is reported
    instead, so it can be corrected rather than hidden.
    """
    env = SquareEnvironment.PRODUCTION.value
    role_by_job = {
        m.square_job_id: m.role
        for m in SquareRoleMapping.objects.filter(environment=env).select_related("role")
    }
    applied = {"updated": 0, "added": 0, "removed": 0, "skipped": []}

    for diff in report.differences:
        if diff.kind == UNMAPPED:
            applied["skipped"].append(f"{diff.date}: {diff.employee_name} is not mapped to staff")
            continue

        if diff.kind == REMOVED_FROM_SQUARE:
            ScheduleAssignment.objects.filter(id=diff.local["assignment_id"]).delete()
            applied["removed"] += 1
            continue

        show = Show.objects.filter(active=True, date=diff.date).order_by("start_time").first()
        if show is None:
            applied["skipped"].append(f"{diff.date}: no active show on that date")
            continue

        role = role_by_job.get(diff.square["job_id"])
        if role is None:
            applied["skipped"].append(
                f"{diff.date}: Square job '{diff.square['job']}' is not mapped to a role"
            )
            continue

        start = dt.datetime.fromisoformat(diff.square["start_raw"])
        end = dt.datetime.fromisoformat(diff.square["end_raw"])
        hours = Decimal(str(round((end - start).total_seconds() / 3600, 2)))
        reason = (
            f"Adopted from Square on {timezone.localtime():%d %b %Y}. "
            f"Square holds {diff.square['job']} {diff.square['start']}-{diff.square['end']}."
        )

        if diff.kind == EDITED_IN_SQUARE:
            assignment = ScheduleAssignment.objects.get(id=diff.local["assignment_id"])
            template = assignment.shift_template
        else:
            template = _template_for(role, schedule_run, show)
            if template is None:
                applied["skipped"].append(
                    f"{diff.date}: no free {role.name} position for {diff.employee_name}"
                )
                continue
            assignment = ScheduleAssignment(
                schedule_run=schedule_run,
                show=show,
                employee=diff.employee,
                shift_template=template,
            )

        on_call = template.assignment_type == AssignmentType.ON_CALL
        assignment.role = role
        assignment.assignment_type = template.assignment_type
        assignment.start_datetime = start
        assignment.end_datetime = end
        assignment.scheduled_paid_hours = Decimal("0.00") if on_call else hours
        assignment.on_call_hours = hours if on_call else Decimal("0.00")
        assignment.manually_overridden = True
        assignment.override_reason = reason
        assignment.selection_reason = reason
        assignment.full_clean()
        assignment.save()
        applied["added" if diff.kind == ADDED_IN_SQUARE else "updated"] += 1

    return applied
