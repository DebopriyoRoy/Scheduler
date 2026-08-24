from dataclasses import dataclass
from decimal import Decimal

from django.db.models import Sum
from django.db.models.functions import Coalesce

from scheduling.models import AssignmentType, Employee, ScheduleAssignment, ScheduleRun


@dataclass(frozen=True)
class EmployeeMetrics:
    employee: Employee
    confirmed_paid_hours: Decimal
    confirmed_shift_count: int
    on_call_assignment_count: int
    weekend_assignment_count: int
    server_shift_count: int
    bartender_shift_count: int
    busser_shift_count: int
    fifty_fifty_count: int


def metrics_for_employee(employee: Employee, schedule_run: ScheduleRun) -> EmployeeMetrics:
    assignments = ScheduleAssignment.objects.filter(schedule_run=schedule_run, employee=employee)
    confirmed = assignments.exclude(assignment_type=AssignmentType.ON_CALL)
    paid = confirmed.aggregate(total=Coalesce(Sum("scheduled_paid_hours"), Decimal("0.00")))[
        "total"
    ]
    return EmployeeMetrics(
        employee=employee,
        confirmed_paid_hours=employee.opening_recent_hours + paid,
        confirmed_shift_count=employee.opening_recent_shift_count + confirmed.count(),
        on_call_assignment_count=assignments.filter(assignment_type=AssignmentType.ON_CALL).count(),
        weekend_assignment_count=sum(
            1 for item in assignments.select_related("show") if item.show.date.weekday() >= 5
        ),
        server_shift_count=assignments.filter(role__name="Server").count(),
        bartender_shift_count=assignments.filter(role__name="Bartender").count(),
        busser_shift_count=assignments.filter(role__name="Busser").count(),
        fifty_fifty_count=assignments.filter(assignment_type=AssignmentType.FIFTY_FIFTY).count(),
    )


def all_employee_metrics(schedule_run: ScheduleRun) -> list[EmployeeMetrics]:
    employee_ids = schedule_run.assignments.values_list("employee_id", flat=True).distinct()
    return [
        metrics_for_employee(employee, schedule_run)
        for employee in Employee.objects.filter(id__in=employee_ids).order_by("display_name")
    ]
