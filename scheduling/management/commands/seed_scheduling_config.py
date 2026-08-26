from datetime import time
from functools import reduce
from operator import or_

from django.core.management.base import BaseCommand
from django.db import transaction
from django.db.models import Q

from scheduling.models import AssignmentType, Role, ShiftTemplate, StaffingRule

SHIFT_TEMPLATES = (
    (
        "lead-server",
        "Server 1",
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
    # Surge positions. These stay dormant at the 75-80 guest buffer and only become
    # live slots once the guest count climbs the staffing ladder below (Christmas and
    # other full-house nights). position_order keeps them behind the standing crew so
    # templates_for_requirement() fills core positions first.
    ("server-4", "Server 4", "Server", AssignmentType.CONFIRMED, time(17), time(23), 6, 0, 32),
    ("server-5", "Server 5", "Server", AssignmentType.CONFIRMED, time(17), time(23), 6, 0, 34),
    ("server-6", "Server 6", "Server", AssignmentType.CONFIRMED, time(17), time(23), 6, 0, 36),
    ("server-7", "Server 7", "Server", AssignmentType.CONFIRMED, time(17), time(23), 6, 0, 38),
    (
        "on-call-server-2",
        "On-call Server 2",
        "Server",
        AssignmentType.ON_CALL,
        time(17, 30),
        time(23),
        0,
        5.5,
        42,
    ),
    (
        "on-call-server-3",
        "On-call Server 3",
        "Server",
        AssignmentType.ON_CALL,
        time(17, 30),
        time(23),
        0,
        5.5,
        44,
    ),
    (
        "bartender-2",
        "Bartender 2",
        "Bartender",
        AssignmentType.CONFIRMED,
        time(15),
        time(21),
        6,
        0,
        52,
    ),
    (
        "bartender-3",
        "Bartender 3",
        "Bartender",
        AssignmentType.CONFIRMED,
        time(15),
        time(21),
        6,
        0,
        54,
    ),
    (
        "on-call-bartender-2",
        "On-call Bartender 2",
        "Bartender",
        AssignmentType.ON_CALL,
        time(17, 30),
        time(23),
        0,
        5.5,
        62,
    ),
    (
        "busser-2",
        "Busser 2",
        "Busser",
        AssignmentType.CONFIRMED,
        time(18),
        time(21, 30),
        3.5,
        0,
        72,
    ),
)


# Retained only so an existing installation's rows can be retired. Staffing counts are
# no longer read from bands: they are computed from the coverage ratios in
# scheduling.services.requirements, which express the five-guest buffer that bands
# could not without a row for every five-guest step.
STAFFING_LADDER = (
    (75, 99, {"Server": (3, 1), "Bartender": (1, 1), "Busser": (1, 0), "50/50": (1, 0)}),
    (100, 124, {"Server": (4, 1), "Bartender": (2, 1), "Busser": (1, 0), "50/50": (1, 0)}),
    (125, 149, {"Server": (5, 2), "Bartender": (2, 2), "Busser": (2, 0), "50/50": (1, 0)}),
    (150, 175, {"Server": (6, 3), "Bartender": (3, 2), "Busser": (2, 0), "50/50": (1, 0)}),
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
        # Retire any ladder band that is no longer part of the approved shape (for
        # example the original flat 1-100 band) so a stale rule cannot shadow the new
        # ladder in staffing_requirements_for().
        keep_bands = {(low, high) for low, high, _ in STAFFING_LADDER}
        stale = StaffingRule.objects.filter(active=True).exclude(
            reduce(
                or_,
                (Q(minimum_guests=low, maximum_guests=high) for low, high in keep_bands),
            )
        )
        retired = stale.update(active=False)

        rule_count = 0
        for minimum_guests, maximum_guests, role_counts in STAFFING_LADDER:
            for role_name, (confirmed_count, on_call_count) in role_counts.items():
                StaffingRule.objects.update_or_create(
                    role=roles[role_name],
                    minimum_guests=minimum_guests,
                    maximum_guests=maximum_guests,
                    defaults={
                        "confirmed_count": confirmed_count,
                        "on_call_count": on_call_count,
                        "active": True,
                    },
                )
                rule_count += 1
        self.stdout.write(
            self.style.SUCCESS(
                f"Scheduling configuration ready: {len(SHIFT_TEMPLATES)} shift templates, "
                f"{rule_count} staffing rules across {len(STAFFING_LADDER)} guest bands "
                f"({retired} stale rule(s) retired)."
            )
        )
