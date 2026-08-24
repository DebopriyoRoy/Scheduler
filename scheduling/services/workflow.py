from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from scheduling.models import (
    ScheduleAssignment,
    ScheduleRun,
    ScheduleRunStatus,
    SchedulingWarning,
    WarningSeverity,
)
from scheduling.services.eligibility import EligibilityService


@transaction.atomic
def override_assignment(
    assignment: ScheduleAssignment,
    replacement,
    reason: str,
) -> ScheduleAssignment:
    if assignment.schedule_run.status in {
        ScheduleRunStatus.APPROVED,
        ScheduleRunStatus.SYNCED_TO_SQUARE,
    }:
        raise ValidationError("Approved schedules cannot be modified.")
    if len(reason.strip()) < 5:
        raise ValidationError("An override reason of at least five characters is required.")
    result = EligibilityService().evaluate(
        replacement,
        assignment.role,
        assignment.show,
        assignment.shift_template,
        assignment.schedule_run,
        assignment.start_datetime,
        assignment.end_datetime,
    )
    if not result.eligible:
        raise ValidationError("Replacement is ineligible: " + "; ".join(result.reasons))
    original = assignment.employee.display_name
    assignment.employee = replacement
    assignment.manually_overridden = True
    assignment.override_reason = reason.strip()
    assignment.selection_reason = (
        f"Management override: {original} replaced by {replacement.display_name}. "
        f"Reason: {reason.strip()}"
    )
    assignment.full_clean()
    assignment.save()
    return assignment


@transaction.atomic
def approve_schedule(schedule_run: ScheduleRun, user) -> ScheduleRun:
    if schedule_run.status in {ScheduleRunStatus.APPROVED, ScheduleRunStatus.SYNCED_TO_SQUARE}:
        return schedule_run
    if schedule_run.warnings.filter(severity=WarningSeverity.ERROR, resolved=False).exists():
        raise ValidationError("Resolve all hard validation errors before approval.")
    if not schedule_run.assignments.exists():
        raise ValidationError("A schedule with no assignments cannot be approved.")
    schedule_run.status = ScheduleRunStatus.APPROVED
    schedule_run.approved_by = user
    schedule_run.approved_at = timezone.now()
    schedule_run.save(update_fields=["status", "approved_by", "approved_at"])
    return schedule_run


@transaction.atomic
def resolve_warning(warning: SchedulingWarning, note: str) -> SchedulingWarning:
    if len(note.strip()) < 5:
        raise ValidationError("A resolution note of at least five characters is required.")
    if warning.schedule_run.status in {
        ScheduleRunStatus.APPROVED,
        ScheduleRunStatus.SYNCED_TO_SQUARE,
    }:
        raise ValidationError("Warnings on approved schedules cannot be changed.")
    warning.resolved = True
    warning.resolution_note = note.strip()
    warning.save(update_fields=["resolved", "resolution_note"])
    return warning
