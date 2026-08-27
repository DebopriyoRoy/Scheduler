from dataclasses import dataclass
from datetime import datetime

from django.db.models import Q

from scheduling.models import (
    Employee,
    EmployeeTimeOff,
    OfficeAssignment,
    Role,
    ScheduleAssignment,
    ScheduleRun,
    ScheduleRunStatus,
    ShiftTemplate,
    Show,
    TimeOffStatus,
)
from scheduling.services.availability import AvailabilityProvider, LocalAvailabilityProvider

EXCLUDED_MANAGER_NAMES = {
    "debroah sweetapple",
    "deborah sweetapple",
    "john haris",
    "john harris",
}

# Roles that involve serving or handling alcohol; bussers (under 19) are barred
# from all of them regardless of any other role they hold.
ALCOHOL_ADJACENT_ROLES = {"Server", "Bartender", "50/50"}

@dataclass(frozen=True)
class EligibilityResult:
    eligible: bool
    reasons: tuple[str, ...]


class EligibilityService:
    def __init__(self, availability_provider: AvailabilityProvider | None = None):
        self.availability_provider = availability_provider or LocalAvailabilityProvider()

    def evaluate(
        self,
        employee: Employee,
        role: Role,
        show: Show,
        shift_template: ShiftTemplate,
        schedule_run: ScheduleRun,
        start_datetime: datetime,
        end_datetime: datetime,
    ) -> EligibilityResult:
        reasons: list[str] = []
        if not employee.active:
            reasons.append("Employee is inactive.")
        if (
            employee.excluded_from_automatic_scheduling
            or employee.display_name.strip().casefold() in EXCLUDED_MANAGER_NAMES
        ):
            reasons.append("Employee is explicitly excluded from automatic scheduling.")
        employee_role = employee.employee_roles.filter(role=role, active=True).first()
        if employee_role is None:
            reasons.append(f"Employee is not qualified for the {role.name} role.")

        # Bussers are the under-19 role and cannot hold any alcohol-service or
        # server-facing position, even if a data-entry error ever grants them one.
        if role.name in ALCOHOL_ADJACENT_ROLES and employee.employee_roles.filter(
            role__name="Busser", active=True
        ).exists():
            reasons.append(
                "Bussers are under the legal drinking age and cannot be scheduled "
                "for alcohol-service or server roles."
            )

        # There is deliberately no rank gate on any server position. Square carries a
        # single "Service" job with no lead grade, and the published rosters show
        # Level 3 staff working the earliest, longest floor shifts. The positions
        # differ only in when they start, not in seniority.

        # Use the actual computed shift window (anchored to this show's own
        # doors-open/wrap-up time), not the shift template's static clock time.
        shift_start_time = start_datetime.time()
        shift_end_time = end_datetime.time()

        availability = self.availability_provider.check(
            employee,
            show.date,
            shift_start_time,
            shift_end_time,
        )
        if not availability.available:
            reasons.extend(availability.reasons)

        # Approved time off only. A pending request is a question a manager has not
        # answered yet, and a declined one is an answer of no - treating either as a
        # refusal would quietly overrule the person whose decision it is.
        time_off = EmployeeTimeOff.objects.filter(
            employee=employee,
            status=TimeOffStatus.APPROVED,
            start_date__lte=show.date,
            end_date__gte=show.date,
        )
        for absence in time_off:
            if absence.covers(show.date, shift_start_time, shift_end_time):
                window = (
                    "all day"
                    if absence.is_whole_day
                    else f"{absence.start_time:%H:%M}-{absence.end_time:%H:%M}"
                )
                detail = f" ({absence.reason})" if absence.reason else ""
                reasons.append(f"Approved time off on this date ({window}){detail}.")
                break

        office_conflict = OfficeAssignment.objects.filter(
            employee=employee,
            date=show.date,
            start_time__lt=shift_end_time,
            end_time__gt=shift_start_time,
        ).exists()
        if office_conflict:
            reasons.append("Office assignment overlaps this shift.")

        overlapping_assignments = ScheduleAssignment.objects.filter(
            employee=employee,
            start_datetime__lt=end_datetime,
            end_datetime__gt=start_datetime,
        ).filter(
            Q(schedule_run=schedule_run)
            | Q(
                schedule_run__status__in=[
                    ScheduleRunStatus.APPROVED,
                    ScheduleRunStatus.SYNCED_TO_SQUARE,
                ]
            )
        )
        if overlapping_assignments.exists():
            reasons.append("An existing schedule assignment overlaps this shift.")
        if ScheduleAssignment.objects.filter(
            schedule_run=schedule_run,
            show=show,
            employee=employee,
        ).exists():
            reasons.append("Employee is already assigned another role for this show.")

        return EligibilityResult(not reasons, tuple(reasons or ["All hard constraints passed."]))

    def evaluate_simple(self, employee: Employee, show: Show) -> bool:
        """Fast check for historical eligibility during opportunity rate calculation."""
        if not employee.active or employee.excluded_from_automatic_scheduling:
            return False
        if employee.display_name.strip().casefold() in EXCLUDED_MANAGER_NAMES:
            return False
        availability = self.availability_provider.check(
            employee,
            show.date,
            show.start_time,
            show.end_time,
        )
        return availability.available
