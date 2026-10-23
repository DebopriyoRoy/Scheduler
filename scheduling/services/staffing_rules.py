"""The live staffing rules: how many of each role a show needs, and when they come in.

Both used to be constants in code - the ratios in models.py, the call times in
engine.py - so changing "one bartender per 75 guests" or "the busser comes in at
6:30" meant a code change. They are now rows management edit on the Staffing rules
page, read every time a roster is built. scheduling.staffing_defaults holds the
starting values and is what "Reset to Spirit defaults" restores.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time

from django.db import transaction

from scheduling import staffing_defaults as defaults
from scheduling.models import (
    MAX_THEATRE_CAPACITY,
    AssignmentType,
    CallTime,
    Role,
    RoleStaffingRule,
    ShiftTemplate,
    StaffingRuleChange,
    Timetable,
)

# How each role's positions are named, confirmed then on call. The first position of a
# kind has its own code; the rest are numbered from 2.
POSITION_NAMING = {
    "Server Manager": {
        AssignmentType.CONFIRMED: ("server-manager", "server-manager-{n}", "Server Manager {n}"),
        AssignmentType.ON_CALL: (
            "on-call-server-manager",
            "on-call-server-manager-{n}",
            "On-call Server Manager {n}",
        ),
    },
    "Server": {
        AssignmentType.CONFIRMED: ("lead-server", "server-{n}", "Server {n}"),
        AssignmentType.ON_CALL: ("on-call-server", "on-call-server-{n}", "On-call Server {n}"),
    },
    "Bartender": {
        AssignmentType.CONFIRMED: ("bartender", "bartender-{n}", "Bartender {n}"),
        AssignmentType.ON_CALL: (
            "on-call-bartender",
            "on-call-bartender-{n}",
            "On-call Bartender {n}",
        ),
    },
    "Busser": {
        AssignmentType.CONFIRMED: ("busser", "busser-{n}", "Busser {n}"),
        AssignmentType.ON_CALL: ("on-call-busser", "on-call-busser-{n}", "On-call Busser {n}"),
    },
    "50/50": {
        AssignmentType.FIFTY_FIFTY: ("fifty-fifty", "fifty-fifty-{n}", "50/50 {n}"),
        AssignmentType.ON_CALL: (
            "on-call-fifty-fifty",
            "on-call-fifty-fifty-{n}",
            "On-call 50/50 {n}",
        ),
    },
}

CONFIRMED_TYPES = (AssignmentType.CONFIRMED, AssignmentType.FIFTY_FIFTY)


@dataclass(frozen=True)
class RoleNeed:
    role_name: str
    confirmed_count: int
    on_call_count: int


def _default_rule(role_name: str) -> RoleStaffingRule:
    minimum, per, stretch, maximum, base, steps = defaults.ROLE_RULES[role_name]
    return RoleStaffingRule(
        role=Role(name=role_name),
        minimum_confirmed=minimum,
        guests_per_person=per,
        stretch_guests=stretch,
        maximum_confirmed=maximum,
        on_call_base=base,
        on_call_steps=steps,
    )


def rules_by_role() -> dict[str, RoleStaffingRule]:
    """Every role's rule, in display order. Missing rows fall back to the defaults."""
    stored = {
        rule.role.name: rule
        for rule in RoleStaffingRule.objects.select_related("role").order_by(
            "display_order", "role__name"
        )
    }
    ordered: dict[str, RoleStaffingRule] = {}
    for name in defaults.ROLE_ORDER:
        ordered[name] = stored.pop(name, None) or _default_rule(name)
    ordered.update(stored)
    return ordered


def needs_for_guests(guests: int, rules: dict[str, RoleStaffingRule] | None = None):
    rules = rules or rules_by_role()
    needs = []
    for name, rule in rules.items():
        confirmed = rule.confirmed_for(guests)
        needs.append(RoleNeed(name, confirmed, rule.on_call_for(confirmed)))
    return needs


def describe_rule(rule: RoleStaffingRule) -> str:
    if rule.guests_per_person:
        text = (
            f"one per {rule.guests_per_person} guests (stretching {rule.stretch_guests}), "
            f"at least {rule.minimum_confirmed}"
        )
    else:
        text = f"always {rule.minimum_confirmed}"
    if rule.maximum_confirmed is not None and rule.guests_per_person:
        text += f", at most {rule.maximum_confirmed}"
    steps = ", ".join(str(s) for s in _steps(rule))
    if rule.on_call_base or steps:
        text += f"; {rule.on_call_base} on call"
        if steps:
            text += f", one more at {steps} confirmed"
    return text


def _steps(rule: RoleStaffingRule) -> list[int]:
    from scheduling.models import parse_on_call_steps

    try:
        return parse_on_call_steps(rule.on_call_steps)
    except ValueError:
        return []


def rules_summary() -> str:
    """One sentence for the roster screen: what crew the rules give."""
    parts = [f"{name}: {describe_rule(rule)}" for name, rule in rules_by_role().items()]
    return ". ".join(parts) + "."


# ------------------------------------------------------------------ call times


def _to_time(value: str) -> time:
    return datetime.strptime(value, "%H:%M").time()


def call_time_for(timetable: str, code: str) -> tuple[time, time] | None:
    """The stored call time, or the default when management have stored none."""
    row = (
        CallTime.objects.filter(timetable=timetable, shift_template__code=code)
        .values_list("start_time", "end_time")
        .first()
    )
    if row:
        return row
    default = defaults.CALL_TIMES.get(timetable, {}).get(code)
    return (_to_time(default[0]), _to_time(default[1])) if default else None


# ------------------------------------------------------------------- positions


def _kind_templates(role: Role, assignment_type: str) -> list[ShiftTemplate]:
    return list(
        ShiftTemplate.objects.filter(role=role, assignment_type=assignment_type).order_by(
            "position_order", "pk"
        )
    )


@transaction.atomic
def ensure_positions(up_to_guests: int = MAX_THEATRE_CAPACITY) -> list[str]:
    """Create any position the rules can call for at this guest count but that is missing.

    A new position copies the last one of its kind - its hours, its call times - so a
    seventh server comes in with the sixth. Returns the names created.
    """
    created: list[str] = []
    for need in needs_for_guests(up_to_guests):
        naming = POSITION_NAMING.get(need.role_name)
        role = Role.objects.filter(name=need.role_name).first()
        if not naming or role is None:
            continue
        for assignment_type, (first_code, code_pattern, name_pattern) in naming.items():
            wanted = (
                need.confirmed_count if assignment_type in CONFIRMED_TYPES else need.on_call_count
            )
            existing = _kind_templates(role, assignment_type)
            for template in existing[:wanted]:
                if not template.active:
                    template.active = True
                    template.save(update_fields=["active"])
            # A role's first on-call position copies its first confirmed one.
            first_of_role = (
                ShiftTemplate.objects.filter(role=role)
                .exclude(assignment_type=AssignmentType.ON_CALL)
                .order_by("position_order", "pk")
                .first()
            )
            for number in range(len(existing) + 1, wanted + 1):
                previous = existing[-1] if existing else first_of_role
                on_call = assignment_type == AssignmentType.ON_CALL
                code = first_code if number == 1 else code_pattern.format(n=number)
                if ShiftTemplate.objects.filter(code=code).exists():
                    continue
                template = ShiftTemplate.objects.create(
                    code=code,
                    name=name_pattern.format(n=number),
                    role=role,
                    assignment_type=assignment_type,
                    start_time=previous.start_time if previous else time(17, 30),
                    end_time=previous.end_time if previous else time(23, 0),
                    scheduled_paid_hours=(
                        0 if on_call else (previous.scheduled_paid_hours if previous else 0)
                    ),
                    on_call_hours=(previous.on_call_hours if previous else 0) if on_call else 0,
                    position_order=(previous.position_order + 1) if previous else 90,
                    active=True,
                )
                for timetable in Timetable.values:
                    source = previous and CallTime.objects.filter(
                        timetable=timetable, shift_template=previous
                    ).first()
                    if source:
                        CallTime.objects.get_or_create(
                            timetable=timetable,
                            shift_template=template,
                            defaults={"start_time": source.start_time, "end_time": source.end_time},
                        )
                existing.append(template)
                created.append(template.name)
    return created


# -------------------------------------------------------------------- defaults


@transaction.atomic
def install_defaults(user=None, overwrite: bool = False) -> None:
    """Give every role a rule and every position its call times.

    Without overwrite this only fills gaps, so it is safe to run on an installation
    management have already tuned. With overwrite it is "Reset to Spirit defaults".
    """
    for order, name in enumerate(defaults.ROLE_ORDER):
        role, _ = Role.objects.get_or_create(name=name)
        minimum, per, stretch, maximum, base, steps = defaults.ROLE_RULES[name]
        values = {
            "minimum_confirmed": minimum,
            "guests_per_person": per,
            "stretch_guests": stretch,
            "maximum_confirmed": maximum,
            "on_call_base": base,
            "on_call_steps": steps,
            "display_order": order,
            "updated_by": user,
        }
        if overwrite:
            RoleStaffingRule.objects.update_or_create(role=role, defaults=values)
        else:
            RoleStaffingRule.objects.get_or_create(role=role, defaults=values)

    for timetable, times in defaults.CALL_TIMES.items():
        for code, (start, end) in times.items():
            template = ShiftTemplate.objects.filter(code=code).first()
            if template is None:
                continue
            values = {"start_time": _to_time(start), "end_time": _to_time(end), "updated_by": user}
            if overwrite:
                CallTime.objects.update_or_create(
                    timetable=timetable, shift_template=template, defaults=values
                )
            else:
                CallTime.objects.get_or_create(
                    timetable=timetable, shift_template=template, defaults=values
                )
    ensure_positions()


def log_change(user, summary: str) -> StaffingRuleChange:
    return StaffingRuleChange.objects.create(
        changed_by=user if getattr(user, "is_authenticated", False) else None,
        summary=summary,
    )
