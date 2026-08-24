from datetime import date, timedelta

from django.core.exceptions import ImproperlyConfigured
from django.db import transaction

from scheduling.models import (
    Employee,
    FiftyFiftyRotationConfig,
    OfficeAssignment,
    OfficeRotationConfig,
)


def _other_employee(name: str, pair: tuple[str, str]) -> Employee:
    other_name = pair[1] if name == pair[0] else pair[0]
    try:
        return Employee.objects.get(display_name=other_name, active=True)
    except Employee.DoesNotExist as exc:
        raise ImproperlyConfigured(
            f"Rotation employee {other_name!r} is missing or inactive."
        ) from exc


@transaction.atomic
def generate_office_assignments(start_date: date, end_date: date) -> list[OfficeAssignment]:
    config = OfficeRotationConfig.objects.select_related("seed_saturday_employee").first()
    if config is None:
        return []
    second = _other_employee(config.seed_saturday_employee.display_name, ("Yana", "Khrystyna"))
    created_or_updated: list[OfficeAssignment] = []
    current = start_date
    while current <= end_date:
        if current.weekday() in {5, 6}:
            saturday = current if current.weekday() == 5 else current - timedelta(days=1)
            weeks = (saturday - config.seed_date).days // 7
            saturday_employee = config.seed_saturday_employee if weeks % 2 == 0 else second
            sunday_employee = second if weeks % 2 == 0 else config.seed_saturday_employee
            employee = saturday_employee if current.weekday() == 5 else sunday_employee
            assignment, _ = OfficeAssignment.objects.update_or_create(
                employee=employee,
                date=current,
                defaults={
                    "start_time": config.office_start_time,
                    "end_time": config.office_end_time,
                    "source": "ROTATION",
                    "notes": "Generated from the weekend office rotation seed.",
                },
            )
            OfficeAssignment.objects.filter(date=current, source="ROTATION").exclude(
                pk=assignment.pk
            ).delete()
            created_or_updated.append(assignment)
        current += timedelta(days=1)
    return created_or_updated


class FiftyFiftyRotation:
    """Stateful deterministic Yana/Kate alternation for one schedule generation."""

    def __init__(self):
        config = FiftyFiftyRotationConfig.objects.select_related("seed_employee").first()
        self.next_name = config.seed_employee.display_name if config else "Yana"

    def ordered_candidates(self, eligible_names: set[str]) -> list[str]:
        pair = ("Yana", "Kate")
        if not eligible_names:
            return []
        if self.next_name in eligible_names:
            remaining = [name for name in pair if name in eligible_names and name != self.next_name]
            return [self.next_name, *remaining]
        return [name for name in pair if name in eligible_names]

    def record_assignment(self, assigned_name: str, both_eligible: bool) -> None:
        if both_eligible:
            self.next_name = "Kate" if assigned_name == "Yana" else "Yana"
