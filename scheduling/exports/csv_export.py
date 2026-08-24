import csv
from io import StringIO


def detailed_schedule_csv(schedule_run) -> str:
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "date",
            "show",
            "employee",
            "role",
            "assignment_type",
            "start",
            "end",
            "paid_hours",
            "on_call_hours",
            "selection_reason",
            "manual_override",
            "override_reason",
        ]
    )
    for assignment in schedule_run.assignments.select_related(
        "show", "employee", "role", "shift_template"
    ):
        writer.writerow(
            [
                assignment.show.date.isoformat(),
                assignment.show.title,
                assignment.employee.display_name,
                assignment.role.name,
                assignment.get_assignment_type_display(),
                assignment.start_datetime.isoformat(),
                assignment.end_datetime.isoformat(),
                assignment.scheduled_paid_hours,
                assignment.on_call_hours,
                assignment.selection_reason,
                assignment.manually_overridden,
                assignment.override_reason,
            ]
        )
    return output.getvalue()
