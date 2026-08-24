from dataclasses import dataclass

from scheduling.models import AssignmentType, ShiftTemplate, Show, StaffingRule


@dataclass(frozen=True)
class StaffingRequirement:
    role_name: str
    confirmed_count: int
    on_call_count: int


def staffing_requirements_for(show: Show) -> tuple[list[StaffingRequirement], bool]:
    guest_count = show.planning_guest_count
    rules = list(
        StaffingRule.objects.filter(
            active=True,
            minimum_guests__lte=guest_count,
            maximum_guests__gte=guest_count,
        ).select_related("role")
    )
    outside_rules = False
    if not rules:
        outside_rules = True
        maximum = (
            StaffingRule.objects.filter(active=True)
            .order_by("-maximum_guests")
            .values_list("maximum_guests", flat=True)
            .first()
        )
        if maximum is not None:
            rules = list(
                StaffingRule.objects.filter(active=True, maximum_guests=maximum).select_related(
                    "role"
                )
            )
    requirements = [
        StaffingRequirement(rule.role.name, rule.confirmed_count, rule.on_call_count)
        for rule in rules
        if rule.role.name != "50/50" or show.requires_50_50
    ]
    return requirements, outside_rules


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
