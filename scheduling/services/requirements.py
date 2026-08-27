from dataclasses import dataclass

from scheduling.models import (
    GUESTS_PER_BARTENDER,
    GUESTS_PER_BUSSER,
    GUESTS_PER_SERVER,
    AssignmentType,
    ShiftTemplate,
    Show,
    staff_for_guests,
)


@dataclass(frozen=True)
class StaffingRequirement:
    role_name: str
    confirmed_count: int
    on_call_count: int


def on_call_servers_for(confirmed: int) -> int:
    """Standby floor cover. One is the standing cushion; a full house wants more."""
    if confirmed >= 7:
        return 3
    if confirmed >= 5:
        return 2
    return 1


def on_call_bartenders_for(confirmed: int) -> int:
    """Standby bar cover, behind the confirmed bartenders already rostered."""
    return 2 if confirmed >= 3 else 1


def staffing_requirements_for(show: Show) -> tuple[list[StaffingRequirement], bool]:
    """How many of each role this show needs, from the guest count.

    Counts are calculated from the coverage ratios rather than looked up in bands:
    one server per 25 guests, one bartender per 75, one busser per 100, each block
    stretching five guests further before another person is added. Bands could not
    express that tolerance without a row for every five-guest step, and the ratio is
    the rule management actually apply.

    The second return value reports a guest count beyond the venue's capacity, which
    is a data problem worth surfacing rather than silently staffing.
    """
    guests = show.planning_guest_count

    servers = staff_for_guests(guests, GUESTS_PER_SERVER)
    bartenders = staff_for_guests(guests, GUESTS_PER_BARTENDER)
    bussers = staff_for_guests(guests, GUESTS_PER_BUSSER)

    # The 50/50 is part of the standard crew, not an extra somebody opts into. It was
    # gated on Show.requires_50_50, which defaults to False and which the calendar
    # import never sets - so every show pulled from the live calendar arrived without
    # one and the role went unstaffed across the entire roster (95 of 96 active shows).
    # The rule belongs here, beside the rest of the crew, not in a per-show flag that
    # nothing maintains.
    requirements = [
        StaffingRequirement("Server", servers, on_call_servers_for(servers)),
        StaffingRequirement("Bartender", bartenders, on_call_bartenders_for(bartenders)),
        StaffingRequirement("Busser", bussers, 0),
        StaffingRequirement("50/50", 1, 0),
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
