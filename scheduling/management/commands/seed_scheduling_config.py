from datetime import time

from django.core.management.base import BaseCommand
from django.db import transaction

from scheduling.models import AssignmentType, Role, ShiftTemplate, StaffingRule

SHIFT_TEMPLATES = (
    (
        "lead-server",
        "Lead Server",
        "Server",
        AssignmentType.CONFIRMED,
        time(15),
        time(21, 30),
        6.5,
        0,
        10,
    ),
    ("server-2", "Server 2", "Server", AssignmentType.CONFIRMED, time(17), time(23), 6, 0, 20),
    ("server-3", "Server 3", "Server", AssignmentType.CONFIRMED, time(17), time(23), 6, 0, 30),
    (
        "on-call-server",
        "On-call Server",
        "Server",
        AssignmentType.ON_CALL,
        time(17, 30),
        time(23),
        0,
        5.5,
        40,
    ),
    ("bartender", "Bartender", "Bartender", AssignmentType.CONFIRMED, time(15), time(21), 6, 0, 50),
    (
        "on-call-bartender",
        "On-call Bartender",
        "Bartender",
        AssignmentType.ON_CALL,
        time(17, 30),
        time(23),
        0,
        5.5,
        60,
    ),
    ("busser", "Busser", "Busser", AssignmentType.CONFIRMED, time(18), time(21, 30), 3.5, 0, 70),
    (
        "fifty-fifty",
        "50/50",
        "50/50",
        AssignmentType.FIFTY_FIFTY,
        time(18),
        time(21, 30),
        3.5,
        0,
        80,
    ),
)


STAFFING_RULES = (
    ("Server", 3, 1),
    ("Bartender", 1, 1),
    ("Busser", 1, 0),
    ("50/50", 1, 0),
)


class Command(BaseCommand):
    help = "Seed the deterministic Spirit scheduling templates and standard staffing rules."

    @transaction.atomic
    def handle(self, *args, **options):
        roles = {
            name: Role.objects.get_or_create(name=name)[0]
            for name in ("Server", "Bartender", "Busser", "50/50")
        }
        for (
            code,
            name,
            role_name,
            assignment_type,
            start,
            end,
            paid,
            on_call,
            order,
        ) in SHIFT_TEMPLATES:
            ShiftTemplate.objects.update_or_create(
                code=code,
                defaults={
                    "name": name,
                    "role": roles[role_name],
                    "assignment_type": assignment_type,
                    "start_time": start,
                    "end_time": end,
                    "scheduled_paid_hours": paid,
                    "on_call_hours": on_call,
                    "position_order": order,
                    "active": True,
                },
            )
        for role_name, confirmed_count, on_call_count in STAFFING_RULES:
            StaffingRule.objects.update_or_create(
                role=roles[role_name],
                minimum_guests=1,
                maximum_guests=100,
                defaults={
                    "confirmed_count": confirmed_count,
                    "on_call_count": on_call_count,
                    "active": True,
                },
            )
        self.stdout.write(
            self.style.SUCCESS(
                f"Scheduling configuration ready: {len(SHIFT_TEMPLATES)} shifts and "
                f"{len(STAFFING_RULES)} staffing rules."
            )
        )
