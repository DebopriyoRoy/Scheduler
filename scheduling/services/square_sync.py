import logging
from dataclasses import dataclass, field
from typing import Any

from django.utils import timezone

from integrations.square.client import SquareClient
from integrations.square.exceptions import SquareAPIError, SquareProductionWriteBlocked
from scheduling.models import (
    Employee,
    Role,
    ScheduleRun,
    ScheduleRunStatus,
    SquareLocation,
)

logger = logging.getLogger(__name__)


class SquareSyncError(Exception):
    """Base exception for Square schedule synchronization failures."""


class SquareSyncValidationError(SquareSyncError):
    """Raised when an approved schedule fails pre-sync validation checks."""


@dataclass
class SyncValidationResult:
    schedule_run: ScheduleRun
    is_valid: bool
    location_id: str
    location_name: str
    unmapped_employees: list[Employee] = field(default_factory=list)
    unmapped_roles: list[Role] = field(default_factory=list)
    existing_shifts: list[dict[str, Any]] = field(default_factory=list)
    assignments_payload: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def validate_schedule_for_sync(
    schedule_run: ScheduleRun,
    client: SquareClient | None = None,
) -> SyncValidationResult:
    """Validates an approved schedule version prior to syncing draft shifts to Square Sandbox."""
    errors: list[str] = []
    warnings: list[str] = []

    # 1. Enforce schedule status
    if schedule_run.status not in {ScheduleRunStatus.APPROVED, ScheduleRunStatus.SYNCED_TO_SQUARE}:
        errors.append(
            f"Schedule run status is '{schedule_run.get_status_display()}'. "
            "Only approved schedule versions can be synced to Square Sandbox."
        )

    # 2. Check Square configuration & safety guards
    client = client or SquareClient()
    try:
        client.config.require_sandbox()
        client.config.assert_write_allowed()
    except SquareProductionWriteBlocked as exc:
        errors.append(str(exc))

    if not client.config.token_is_configured:
        errors.append("SQUARE_SANDBOX_ACCESS_TOKEN is not configured in the environment.")

    # 3. Determine location ID
    location_id = client.config.location_id
    location_name = "Square Sandbox Location"
    if not location_id:
        active_location = SquareLocation.objects.filter(active=True).first()
        if active_location:
            location_id = active_location.square_location_id
            location_name = active_location.name
        else:
            errors.append("No active Square location ID found in environment or database.")

    # 4. Inspect assignments for unmapped employees & roles
    assignments = schedule_run.assignments.select_related(
        "employee", "role", "show", "shift_template"
    ).all()
    if not assignments.exists():
        errors.append("Schedule run contains no assignments to sync.")

    unmapped_employees_set: set[Employee] = set()
    unmapped_roles_set: set[Role] = set()
    assignments_payload: list[dict[str, Any]] = []

    for assignment in assignments:
        has_unmapped = False
        if not assignment.employee.square_team_member_id:
            unmapped_employees_set.add(assignment.employee)
            has_unmapped = True

        if not assignment.role.square_job_id:
            unmapped_roles_set.add(assignment.role)
            has_unmapped = True

        # Generate deterministic idempotency key
        timestamp = int(assignment.updated_at.timestamp())
        idempotency_key = f"spirit-shift-v1-{assignment.id}-{timestamp}"

        # ISO 8601 timestamps
        start_iso = assignment.start_datetime.isoformat()
        end_iso = assignment.end_datetime.isoformat()

        notes = (
            f"Spirit Schedule #{schedule_run.id} | Show: {assignment.show.title} | "
            f"Role: {assignment.role.name} ({assignment.get_assignment_type_display()})"
        )

        assignments_payload.append(
            {
                "assignment_id": assignment.id,
                "employee_name": assignment.employee.display_name,
                "employee_square_id": assignment.employee.square_team_member_id or "UNMAPPED",
                "role_name": assignment.role.name,
                "role_square_id": assignment.role.square_job_id or "UNMAPPED",
                "show_title": assignment.show.title,
                "show_date": assignment.show.date.isoformat(),
                "assignment_type": assignment.get_assignment_type_display(),
                "start_at": start_iso,
                "end_at": end_iso,
                "idempotency_key": idempotency_key,
                "notes": notes,
                "has_unmapped": has_unmapped,
            }
        )

    unmapped_employees = sorted(unmapped_employees_set, key=lambda e: e.display_name)
    unmapped_roles = sorted(unmapped_roles_set, key=lambda r: r.name)

    if unmapped_employees:
        emp_names = ", ".join(e.display_name for e in unmapped_employees)
        errors.append(f"The following employees lack Square team member IDs: {emp_names}")

    if unmapped_roles:
        role_names = ", ".join(r.name for r in unmapped_roles)
        errors.append(f"The following roles lack Square job IDs: {role_names}")

    # 5. Check existing Square Sandbox scheduled shifts for conflicts
    existing_shifts: list[dict[str, Any]] = []
    if client.config.token_is_configured and not errors:
        try:
            start_bound = f"{schedule_run.start_date.isoformat()}T00:00:00Z"
            end_bound = f"{schedule_run.end_date.isoformat()}T23:59:59Z"
            query = {
                "filter": {
                    "start_at": {"start_at": start_bound, "end_at": end_bound},
                    "location_ids": [location_id] if location_id else [],
                }
            }
            existing_shifts = client.search_scheduled_shifts(query)
            if existing_shifts:
                warnings.append(
                    f"Found {len(existing_shifts)} existing shift(s) in Square Sandbox "
                    f"between {schedule_run.start_date} and {schedule_run.end_date}."
                )
        except (SquareAPIError, Exception) as exc:
            warnings.append(f"Unable to query existing Square Sandbox shifts: {exc}")

    is_valid = len(errors) == 0

    return SyncValidationResult(
        schedule_run=schedule_run,
        is_valid=is_valid,
        location_id=location_id or "",
        location_name=location_name,
        unmapped_employees=unmapped_employees,
        unmapped_roles=unmapped_roles,
        existing_shifts=existing_shifts,
        assignments_payload=assignments_payload,
        errors=errors,
        warnings=warnings,
    )


def sync_schedule_to_sandbox(
    schedule_run: ScheduleRun,
    client: SquareClient | None = None,
    location_id: str | None = None,
) -> dict[str, Any]:
    """Publishes draft shifts for an approved schedule to Square Sandbox."""
    client = client or SquareClient()

    validation = validate_schedule_for_sync(schedule_run, client=client)
    if not validation.is_valid:
        error_msg = "; ".join(validation.errors)
        raise SquareSyncValidationError(f"Cannot sync schedule #{schedule_run.id}: {error_msg}")

    target_location_id = location_id or validation.location_id
    if not target_location_id:
        raise SquareSyncValidationError("No target Square location ID specified for sync.")

    synced_shifts: list[dict[str, Any]] = []

    for item in validation.assignments_payload:
        draft_shift = client.create_draft_shift(
            idempotency_key=item["idempotency_key"],
            team_member_id=item["employee_square_id"],
            job_id=item["role_square_id"],
            location_id=target_location_id,
            start_at=item["start_at"],
            end_at=item["end_at"],
            notes=item["notes"],
        )
        synced_shifts.append(draft_shift)

    # Transition schedule run status
    schedule_run.status = ScheduleRunStatus.SYNCED_TO_SQUARE
    now_str = timezone.now().strftime("%Y-%m-%d %H:%M:%S")
    sync_note = f"Synced {len(synced_shifts)} draft shift(s) to Square Sandbox on {now_str}."
    schedule_run.notes = f"{schedule_run.notes}\n{sync_note}".strip()
    schedule_run.save()

    logger.info("Successfully synced schedule run #%s to Square Sandbox.", schedule_run.id)

    return {
        "schedule_run_id": schedule_run.id,
        "synced_count": len(synced_shifts),
        "location_id": target_location_id,
        "status": schedule_run.status,
        "synced_shifts": synced_shifts,
    }
