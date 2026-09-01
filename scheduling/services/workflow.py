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
    SquareSyncAuditLog,
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
    swap: bool = False,
) -> ScheduleAssignment:
    """Replace the person on a shift, optionally moving the shift window with them.

    A replacement is rarely like-for-like: the person who can cover often cannot cover
    the same hours, so the times have to be adjustable or the override gets rejected
    for an availability gap that a fifteen-minute shift is enough to close.

    Eligibility is checked against the *new* window, never the generated one. Checking
    the old hours and then saving different ones would wave through exactly the case
    the check exists to catch.
    """
    if len(reason.strip()) < 5:
        raise ValidationError("An override reason of at least five characters is required.")

    start_datetime, end_datetime = _shift_window(assignment, start_time, end_time)

    # Somebody already working this show can be moved onto this shift. Refusing that as
    # "already assigned another role for this show" is the engine arguing with a
    # decision the manager has already made: putting Daniel behind the bar means taking
    # him off on-call, not cloning him. The old slot is vacated as part of the move and
    # reported as a shortage, so the hole it leaves is visible rather than silent.
    vacating = (
        ScheduleAssignment.objects.filter(
            schedule_run=assignment.schedule_run,
            show=assignment.show,
            employee=replacement,
        )
        .exclude(pk=assignment.pk)
        .select_related("shift_template", "role")
        .first()
    )

    # A swap exchanges two people who are both already on this show: the replacement
    # takes this shift and the person leaving it takes theirs. Without it the only way
    # to trade two positions was to empty one, fill the other, then remember to come
    # back - three steps for one decision, with a hole in the roster in between.
    if swap and vacating is not None:
        return _swap_positions(assignment, vacating, reason)

    result = EligibilityService().evaluate(
        replacement,
        assignment.role,
        assignment.show,
        assignment.shift_template,
        assignment.schedule_run,
        start_datetime,
        end_datetime,
        # Neither the shift being rewritten nor a shift the person is leaving counts
        # against them. Without the first, keeping the same person and moving only the
        # hours is refused for clashing with itself.
        exclude_assignments=[assignment, vacating],
    )
    if not result.eligible:
        raise ValidationError("Replacement is ineligible: " + "; ".join(result.reasons))

    vacated_note = ""
    if vacating is not None:
        vacated_note = (
            f" Moved off {vacating.shift_template.name} "
            f"({_describe(vacating.start_datetime, vacating.end_datetime)}), now unfilled."
        )
        _report_vacated_slot(vacating)
        vacating.delete()

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
        f"Management override: {original} replaced by {replacement.display_name}."
        f"{moved}{vacated_note} Reason: {reason.strip()}"
    )
    assignment.full_clean()
    assignment.save()
    _flag_square_divergence(
        assignment.schedule_run, f"{assignment.shift_template.name} reassigned"
    )
    return assignment


def _swap_positions(
    first: ScheduleAssignment, second: ScheduleAssignment, reason: str
) -> ScheduleAssignment:
    """Exchange two people's positions on one show, each keeping the other's hours.

    Both directions are checked before either is written, so a swap that is only valid
    one way is refused outright rather than leaving the roster half-changed.

    Note the row for the second position is rebuilt, so its primary key changes; hold a
    reference by position rather than by id across a swap.
    """
    service = EligibilityService()
    for moving, into in ((second.employee, first), (first.employee, second)):
        result = service.evaluate(
            moving,
            into.role,
            into.show,
            into.shift_template,
            into.schedule_run,
            into.start_datetime,
            into.end_datetime,
            exclude_assignments=[first, second],
        )
        if not result.eligible:
            raise ValidationError(
                f"{moving.display_name} cannot take {into.shift_template.name}: "
                + "; ".join(result.reasons)
            )

    first_person, second_person = first.employee, second.employee
    first_slot, second_slot = first.shift_template.name, second.shift_template.name

    def describe(person, came_from, went_to):
        return (
            f"Management swap: {person.display_name} moved from {came_from} to "
            f"{went_to}. Reason: {reason.strip()}"
        )

    # One person per show and one person per position are both database constraints,
    # and a straight swap breaks them halfway through: whichever row is written first
    # duplicates the other. There is no spare value to park a row on - employee is not
    # nullable and neither row may leave the show - so the second row is rebuilt rather
    # than updated in place, and its Square audit entries are carried across so no
    # record of what was already sent is lost.
    kept = {
        field: getattr(second, field)
        for field in (
            "schedule_run",
            "show",
            "role",
            "assignment_type",
            "shift_template",
            "start_datetime",
            "end_datetime",
        )
    }
    audit_ids = list(second.square_sync_audit_logs.values_list("pk", flat=True))
    second.delete()

    first.employee = second_person
    first.manually_overridden = True
    first.override_reason = reason.strip()
    first.selection_reason = describe(second_person, second_slot, first_slot)
    _apply_hours(first)
    first.full_clean()
    first.save()

    rebuilt = ScheduleAssignment(
        **kept,
        employee=first_person,
        manually_overridden=True,
        override_reason=reason.strip(),
        selection_reason=describe(first_person, first_slot, second_slot),
    )
    _apply_hours(rebuilt)
    rebuilt.full_clean()
    rebuilt.save()
    if audit_ids:
        SquareSyncAuditLog.objects.filter(pk__in=audit_ids).update(assignment=rebuilt)

    _flag_square_divergence(
        first.schedule_run, f"{first_slot} and {second_slot} swapped"
    )
    return first


def _report_vacated_slot(vacated: ScheduleAssignment) -> None:
    """Raise the same shortage the generator would for the slot just emptied.

    Moving somebody leaves their old position open. Deleting the row quietly would take
    a staffed slot off the schedule with nothing to show a person is now missing, and
    the run would look approvable when it is a body short.
    """
    from scheduling.models import WarningType
    from scheduling.services.engine import SHORTAGE_TYPES

    warning_type = SHORTAGE_TYPES.get(
        (vacated.role.name, vacated.assignment_type),
        WarningType.ROLE_CONFIGURATION_ERROR,
    )
    SchedulingWarning.objects.create(
        schedule_run=vacated.schedule_run,
        show=vacated.show,
        warning_type=warning_type,
        severity=WarningSeverity.ERROR,
        message=(
            f"{vacated.shift_template.name} is unfilled: "
            f"{vacated.employee.display_name} was moved to another position on this show. "
            "Add a person or accept the gap with a reason."
        ),
    )


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



def _flag_square_divergence(schedule_run, what: str) -> None:
    """Say out loud that Square no longer matches, after editing a synced run.

    Editing an approved run is fine - a mistake found after approval is still a
    mistake. Editing one whose shifts are already in Square is also fine, but it
    leaves the drafts there showing the old roster, and nothing else on the page
    would say so. A WARNING, not an ERROR: it must not block re-approval, it must
    just be impossible to miss.
    """
    from scheduling.models import WarningType

    if schedule_run.status != ScheduleRunStatus.SYNCED_TO_SQUARE:
        return
    SchedulingWarning.objects.update_or_create(
        schedule_run=schedule_run,
        warning_type=WarningType.SQUARE_OUT_OF_DATE,
        show=None,
        defaults={
            "severity": WarningSeverity.WARNING,
            "resolved": False,
            "message": (
                f"This schedule was changed after being sent to Square ({what}). "
                "The shifts in Square still show the previous roster - re-sync, or "
                "correct them in Square."
            ),
        },
    )


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
    _flag_square_divergence(schedule_run, f"{template.name} filled")

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
