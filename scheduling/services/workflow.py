from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from scheduling.models import (
    AssignmentType,
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
    *,
    start_time=None,
    end_time=None,
) -> ScheduleAssignment:
    """Replace the person on a shift, optionally moving the shift window with them.

    A replacement is rarely like-for-like: the person who can cover often cannot cover
    the same hours, so the times have to be adjustable or the override gets rejected
    for an availability gap that a fifteen-minute shift is enough to close.

    Eligibility is checked against the *new* window, never the generated one. Checking
    the old hours and then saving different ones would wave through exactly the case
    the check exists to catch.
    """
    if assignment.schedule_run.status in {
        ScheduleRunStatus.APPROVED,
        ScheduleRunStatus.SYNCED_TO_SQUARE,
    }:
        raise ValidationError("Approved schedules cannot be modified.")
    if len(reason.strip()) < 5:
        raise ValidationError("An override reason of at least five characters is required.")

    start_datetime, end_datetime = _shift_window(assignment, start_time, end_time)

    result = EligibilityService().evaluate(
        replacement,
        assignment.role,
        assignment.show,
        assignment.shift_template,
        assignment.schedule_run,
        start_datetime,
        end_datetime,
    )
    if not result.eligible:
        raise ValidationError("Replacement is ineligible: " + "; ".join(result.reasons))

    original = assignment.employee.display_name
    original_window = _describe(assignment.start_datetime, assignment.end_datetime)
    new_window = _describe(start_datetime, end_datetime)

    assignment.employee = replacement
    assignment.start_datetime = start_datetime
    assignment.end_datetime = end_datetime
    _apply_hours(assignment)
    assignment.manually_overridden = True
    assignment.override_reason = reason.strip()
    moved = (
        ""
        if original_window == new_window
        else f" Shift moved {original_window} to {new_window}."
    )
    assignment.selection_reason = (
        f"Management override: {original} replaced by {replacement.display_name}.{moved} "
        f"Reason: {reason.strip()}"
    )
    assignment.full_clean()
    assignment.save()
    return assignment


def _shift_window(assignment: ScheduleAssignment, start_time, end_time):
    """Local start/end times back onto the assignment's own date.

    An end at or before the start is read as running past midnight rather than as a
    mistake - a late bar shift finishing at 00:30 is ordinary here.
    """
    if start_time is None and end_time is None:
        return assignment.start_datetime, assignment.end_datetime

    local_start = timezone.localtime(assignment.start_datetime)
    start_time = start_time or local_start.time()
    end_time = end_time or timezone.localtime(assignment.end_datetime).time()

    start = local_start.replace(
        hour=start_time.hour, minute=start_time.minute, second=0, microsecond=0
    )
    end = start.replace(hour=end_time.hour, minute=end_time.minute, second=0, microsecond=0)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def _apply_hours(assignment: ScheduleAssignment) -> None:
    """Keep the paid/on-call hours consistent with the window actually saved.

    Same derivation the engine uses when it first builds the shift; leaving the
    generated figures behind after moving the times would misreport the workload
    summary and send the wrong hours to Square.
    """
    duration = Decimal(
        str(round((assignment.end_datetime - assignment.start_datetime).total_seconds() / 3600, 2))
    )
    is_on_call = assignment.assignment_type == AssignmentType.ON_CALL
    assignment.scheduled_paid_hours = Decimal("0.00") if is_on_call else duration
    assignment.on_call_hours = duration if is_on_call else Decimal("0.00")


def _describe(start, end) -> str:
    return f"{timezone.localtime(start):%H:%M}-{timezone.localtime(end):%H:%M}"


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


@transaction.atomic
def fill_assignment(
    schedule_run: ScheduleRun,
    show,
    template,
    employee,
    reason: str,
    *,
    start_time=None,
    end_time=None,
):
    """Staff a slot the generator left short.

    A shortage means nobody passed the eligibility checks, which is usually a gap in
    the availability on file rather than a genuinely empty building. Management knows
    who can actually come in, so the slot has to be fillable by hand - otherwise the
    run carries a hard error nobody can clear and can never be approved.

    The same eligibility checks still run. This is a manual override of *who*, not a
    way to bypass double-booking, qualification, or an office clash - the times are
    adjustable precisely so a near-miss on availability can be closed honestly.
    """
    from scheduling.models import ScheduleAssignment
    from scheduling.services.engine import SHORTAGE_TYPES, shift_window_for

    if schedule_run.status in {ScheduleRunStatus.APPROVED, ScheduleRunStatus.SYNCED_TO_SQUARE}:
        raise ValidationError("Approved schedules cannot be modified.")
    if len(reason.strip()) < 5:
        raise ValidationError("A reason of at least five characters is required.")
    if schedule_run.assignments.filter(show=show, shift_template=template).exists():
        raise ValidationError("That position is already filled.")

    start, end = shift_window_for(show, template)
    if start_time or end_time:
        local_start = timezone.localtime(start)
        start_time = start_time or local_start.time()
        end_time = end_time or timezone.localtime(end).time()
        start = local_start.replace(
            hour=start_time.hour, minute=start_time.minute, second=0, microsecond=0
        )
        end = start.replace(hour=end_time.hour, minute=end_time.minute, second=0, microsecond=0)
        if end <= start:
            end += timedelta(days=1)

    result = EligibilityService().evaluate(
        employee, template.role, show, template, schedule_run, start, end
    )
    if not result.eligible:
        raise ValidationError("That person cannot take this shift: " + "; ".join(result.reasons))

    assignment = ScheduleAssignment(
        schedule_run=schedule_run,
        show=show,
        employee=employee,
        role=template.role,
        assignment_type=template.assignment_type,
        shift_template=template,
        start_datetime=start,
        end_datetime=end,
        manually_overridden=True,
        override_reason=reason.strip(),
        selection_reason=(
            f"Management filled a shortage: {employee.display_name} added to "
            f"{template.name}. Reason: {reason.strip()}"
        ),
    )
    _apply_hours(assignment)
    assignment.full_clean()
    assignment.save()

    # The shortage warning is why this run cannot be approved. Leaving it standing
    # after the gap has been closed would block approval over a problem that no
    # longer exists, and there is no longer a screen for clearing it by hand.
    warning_type = SHORTAGE_TYPES.get((template.role.name, template.assignment_type))
    if warning_type:
        schedule_run.warnings.filter(
            show=show, warning_type=warning_type, resolved=False
        ).update(
            resolved=True,
            resolution_note=f"Filled by {employee.display_name}. {reason.strip()}",
        )
    return assignment
