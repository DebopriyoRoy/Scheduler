from dataclasses import dataclass

from scheduling.models import (
    AssignmentType,
    ShiftTemplate,
    Show,
)
from scheduling.services.staffing_rules import needs_for_guests


@dataclass(frozen=True)
class StaffingRequirement:
    role_name: str
    confirmed_count: int
    on_call_count: int


def staffing_requirements_for(show: Show) -> tuple[list[StaffingRequirement], bool]:
    """How many of each role this show needs, from its guest count and the live rules.

    The rules are management's, edited on the Staffing rules page: a minimum per role,
    one more person per so many guests (each person stretching a few guests further
    before another is added), an optional maximum, and on-call cover that grows with
    the confirmed crew. At 75-80 guests the defaults give the standard crew: 3 servers
    + 1 on call, 1 bartender + 1 on call, 1 busser, 1 50/50 and the Server Manager.

    The Server Manager and the 50/50 are part of every show's crew, not extras: the
    50/50 used to hang off Show.requires_50_50, which the calendar import never set,
    and 95 of 96 real shows went without one.

    The second return value reports a guest count beyond the venue's capacity, which
    is a data problem worth surfacing rather than silently staffing.
    """
    guests = show.planning_guest_count
    requirements = [
        StaffingRequirement(need.role_name, need.confirmed_count, need.on_call_count)
        for need in needs_for_guests(guests)
    ]
    return requirements, guests > show.capacity


def templates_for_requirement(requirement: StaffingRequirement) -> list[ShiftTemplate]:
    confirmed = list(
        ShiftTemplate.objects.filter(
            active=True,
            role__name=requirement.role_name,
        )
        .exclude(assignment_type=AssignmentType.ON_CALL)
        .order_by("position_order")[: requirement.confirmed_count]
    )
    on_call = list(
        ShiftTemplate.objects.filter(
            active=True,
            role__name=requirement.role_name,
            assignment_type=AssignmentType.ON_CALL,
        ).order_by("position_order")[: requirement.on_call_count]
    )
    return confirmed + on_call
