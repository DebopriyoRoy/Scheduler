from dataclasses import dataclass
from datetime import datetime

from django.db.models import Q

from scheduling.models import (
    Employee,
    OfficeAssignment,
    Role,
    ScheduleAssignment,
    ScheduleRun,
    ScheduleRunStatus,
    ShiftTemplate,
    Show,
)
from scheduling.services.availability import AvailabilityProvider, LocalAvailabilityProvider

EXCLUDED_MANAGER_NAMES = {
    "debroah sweetapple",
    "deborah sweetapple",
    "john haris",
    "john harris",
}


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
        if not employee.employee_roles.filter(role=role, active=True).exists():
            reasons.append(f"Employee is not qualified for the {role.name} role.")

        availability = self.availability_provider.check(
            employee,
            show.date,
            shift_template.start_time,
            shift_template.end_time,
        )
        if not availability.available:
            reasons.extend(availability.reasons)

        office_conflict = OfficeAssignment.objects.filter(
            employee=employee,
            date=show.date,
            start_time__lt=shift_template.end_time,
            end_time__gt=shift_template.start_time,
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
