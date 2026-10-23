"""Editable staffing rules: crew size per role and call times, changed in the app."""

from datetime import date, time

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.urls import reverse
from django.utils import timezone

from scheduling.models import (
    AvailabilityType,
    CallTime,
    Employee,
    EmployeeAvailability,
    Role,
    RoleStaffingRule,
    ShiftTemplate,
    Show,
    StaffingRuleChange,
    Timetable,
)
from scheduling.services.engine import SchedulingEngine, shift_window_for
from scheduling.services.requirements import staffing_requirements_for
from scheduling.services.staffing_rules import ensure_positions, needs_for_guests

DAY = date(2026, 11, 20)


@pytest.fixture
def configured(db):
    call_command("seed_spirit_staff", verbosity=0)
    call_command("seed_scheduling_config", verbosity=0)


@pytest.fixture
def client_in(client, db):
    user = get_user_model().objects.create_user(username="mgr", password="safe-test-password")
    client.force_login(user)
    return client


def _crew(guests):
    return {n.role_name: (n.confirmed_count, n.on_call_count) for n in needs_for_guests(guests)}


def _rule(name):
    return RoleStaffingRule.objects.get(role__name=name)


def _show(guests=80, **extra):
    values = {"title": "Forever Country", "date": DAY, "expected_guests": guests,
              "start_time": time(18, 30)}
    values.update(extra)
    return Show.objects.create(**values)


def _window(show, code):
    start, end = shift_window_for(show, ShiftTemplate.objects.get(code=code))
    return timezone.localtime(start).time(), timezone.localtime(end).time()


# ----------------------------------------------------------------- crew size


@pytest.mark.django_db
def test_the_standard_crew_at_75_to_80_guests(configured):
    for guests in (75, 80):
        assert _crew(guests) == {
            "Server Manager": (1, 0),
            "Server": (3, 1),
            "Bartender": (1, 1),
            "Busser": (1, 0),
            "50/50": (1, 0),
        }


@pytest.mark.django_db
def test_the_crew_grows_with_the_guest_count(configured):
    assert _crew(105)["Server"] == (4, 1)
    assert _crew(130)["Server"] == (5, 2)
    assert _crew(155)["Bartender"] == (2, 1)
    assert _crew(160)["Bartender"] == (3, 2)
    assert _crew(175) == {
        "Server Manager": (1, 0),
        "Server": (7, 3),
        "Bartender": (3, 2),
        "Busser": (2, 0),
        "50/50": (1, 0),
    }


@pytest.mark.django_db
def test_changing_a_rule_changes_the_next_roster(configured):
    rule = _rule("Server")
    rule.guests_per_person = 20
    rule.save()
    assert _crew(100)["Server"][0] == 5  # ceil(95 / 20)


@pytest.mark.django_db
def test_a_fixed_crew_ignores_the_guest_count(configured):
    rule = _rule("50/50")
    assert rule.guests_per_person == 0
    assert _crew(75)["50/50"] == _crew(175)["50/50"] == (1, 0)


@pytest.mark.django_db
def test_a_dynamic_50_50(configured):
    rule = _rule("50/50")
    rule.guests_per_person = 100
    rule.save()
    assert _crew(80)["50/50"][0] == 1
    assert _crew(150)["50/50"][0] == 2


@pytest.mark.django_db
def test_the_maximum_caps_the_crew(configured):
    rule = _rule("Server")
    rule.maximum_confirmed = 5
    rule.save()
    assert _crew(175)["Server"][0] == 5


@pytest.mark.django_db
def test_on_call_steps(configured):
    rule = _rule("Bartender")
    rule.on_call_steps = "2, 3"
    rule.save()
    assert _crew(80)["Bartender"] == (1, 1)
    assert _crew(150)["Bartender"] == (2, 2)
    assert _crew(160)["Bartender"] == (3, 3)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "field, value",
    [("stretch_guests", 30), ("maximum_confirmed", 1), ("on_call_steps", "7, 5"),
     ("on_call_steps", "five")],
)
def test_bad_rules_are_refused(configured, field, value):
    rule = _rule("Server")
    setattr(rule, field, value)
    with pytest.raises(ValidationError):
        rule.full_clean()


@pytest.mark.django_db
def test_missing_positions_are_created_with_the_last_ones_times(configured):
    rule = _rule("Server")
    rule.guests_per_person = 15
    rule.save()

    created = ensure_positions(175)

    assert "Server 12" in created  # ceil(170 / 15) = 12
    server_12 = ShiftTemplate.objects.get(code="server-12")
    server_7 = ShiftTemplate.objects.get(code="server-7")
    for timetable in Timetable.values:
        a = CallTime.objects.get(timetable=timetable, shift_template=server_12)
        b = CallTime.objects.get(timetable=timetable, shift_template=server_7)
        assert (a.start_time, a.end_time) == (b.start_time, b.end_time)


@pytest.mark.django_db
def test_requirements_and_positions_line_up_after_a_rule_change(configured):
    rule = _rule("Busser")
    rule.on_call_base = 1
    rule.save()
    ensure_positions()
    from scheduling.services.requirements import templates_for_requirement

    for requirement in staffing_requirements_for(_show(guests=175))[0]:
        needed = requirement.confirmed_count + requirement.on_call_count
        assert len(templates_for_requirement(requirement)) == needed, requirement.role_name


@pytest.mark.django_db
def test_the_engine_staffs_the_extra_positions(configured):
    rule = _rule("Server")
    rule.guests_per_person = 15
    rule.save()
    show = _show(guests=170)
    for employee in Employee.objects.filter(active=True):
        EmployeeAvailability.objects.create(
            employee=employee, date=DAY, availability_type=AvailabilityType.AVAILABLE_ALL_DAY
        )
    run = SchedulingEngine().generate(DAY, DAY, allow_shortages=True)
    planned = run.assignments.count() + run.warnings.filter(
        warning_type__icontains="SHORTAGE", show=show
    ).count()
    assert planned >= 11 + 1 + 3  # servers, manager, on call at the very least
    assert not run.warnings.filter(warning_type="ROLE_CONFIGURATION_ERROR").exists()


# ---------------------------------------------------------------- call times


@pytest.mark.django_db
def test_the_house_call_times_for_a_half_six_show(configured):
    show = _show()
    assert _window(show, "server-manager") == (time(14, 0), time(21, 0))
    assert _window(show, "lead-server") == (time(15, 0), time(21, 0))
    assert _window(show, "server-2") == (time(16, 0), time(21, 30))
    assert _window(show, "server-3") == (time(17, 30), time(23, 0))
    assert _window(show, "on-call-server") == (time(18, 15), time(23, 0))
    assert _window(show, "busser") == (time(18, 30), time(23, 0))
    assert _window(show, "fifty-fifty") == (time(17, 45), time(20, 30))
    assert _window(show, "bartender") == (time(16, 0), time(21, 30))
    assert _window(show, "on-call-bartender") == (time(17, 30), time(23, 0))


@pytest.mark.django_db
def test_an_edited_call_time_is_what_the_engine_uses(configured):
    row = CallTime.objects.get(timetable=Timetable.REGULAR, shift_template__code="busser")
    row.start_time = time(18, 0)
    row.save()
    assert _window(_show(), "busser") == (time(18, 0), time(23, 0))


@pytest.mark.django_db
def test_reseeding_never_undoes_managements_changes(configured):
    rule = _rule("Server")
    rule.guests_per_person = 22
    rule.save()
    call_command("seed_scheduling_config", verbosity=0)
    assert _rule("Server").guests_per_person == 22


# ---------------------------------------------------------------- the page


@pytest.mark.django_db
def test_the_page_requires_login(client):
    assert client.get(reverse("staffing_rules")).status_code == 302


@pytest.mark.django_db
def test_the_page_shows_rules_ladder_and_call_times(configured, client_in):
    body = client_in.get(reverse("staffing_rules"), {"guests": 80}).content.decode()
    assert "Crew size by role" in body
    assert "What the rules give" in body
    assert "Call times" in body
    assert "18:30" in body  # busser in


def _rules_post(**overrides):
    data = {"action": "rules", "rules-TOTAL_FORMS": 0, "rules-INITIAL_FORMS": 0}
    rules = list(RoleStaffingRule.objects.order_by("display_order", "role__name"))
    data["rules-TOTAL_FORMS"] = data["rules-INITIAL_FORMS"] = len(rules)
    for i, rule in enumerate(rules):
        values = {
            "id": rule.pk,
            "minimum_confirmed": rule.minimum_confirmed,
            "guests_per_person": rule.guests_per_person,
            "stretch_guests": rule.stretch_guests,
            "maximum_confirmed": rule.maximum_confirmed or "",
            "on_call_base": rule.on_call_base,
            "on_call_steps": rule.on_call_steps,
        }
        values.update(overrides.get(rule.role.name, {}))
        for key, value in values.items():
            data[f"rules-{i}-{key}"] = value
    return data


@pytest.mark.django_db
def test_saving_a_rule_from_the_page_is_logged(configured, client_in):
    response = client_in.post(
        reverse("staffing_rules"), _rules_post(Server={"guests_per_person": 22})
    )
    assert response.status_code == 302
    assert _rule("Server").guests_per_person == 22
    change = StaffingRuleChange.objects.get()
    assert "Server: guests per person 25 → 22" in change.summary
    assert change.changed_by.username == "mgr"


@pytest.mark.django_db
def test_a_bad_rule_is_refused_on_the_page(configured, client_in):
    response = client_in.post(
        reverse("staffing_rules"), _rules_post(Server={"stretch_guests": 40})
    )
    assert response.status_code == 200
    assert _rule("Server").stretch_guests == 5
    assert not StaffingRuleChange.objects.exists()


@pytest.mark.django_db
def test_saving_call_times_from_the_page(configured, client_in):
    rows = list(
        CallTime.objects.filter(timetable=Timetable.REGULAR, shift_template__active=True)
        .order_by("shift_template__position_order", "shift_template__pk")
    )
    prefix = "t-REGULAR"
    data = {"action": "times", "timetable": "REGULAR",
            f"{prefix}-TOTAL_FORMS": len(rows), f"{prefix}-INITIAL_FORMS": len(rows)}
    for i, row in enumerate(rows):
        start = "18:00" if row.shift_template.code == "busser" else f"{row.start_time:%H:%M}"
        data.update({f"{prefix}-{i}-id": row.pk, f"{prefix}-{i}-start_time": start,
                     f"{prefix}-{i}-end_time": f"{row.end_time:%H:%M}"})
    response = client_in.post(reverse("staffing_rules"), data)
    assert response.status_code == 302
    busser = CallTime.objects.get(timetable=Timetable.REGULAR, shift_template__code="busser")
    assert busser.start_time == time(18, 0)
    assert "Busser: in 18:30 → 18:00" in StaffingRuleChange.objects.get().summary


@pytest.mark.django_db
def test_reset_puts_the_defaults_back(configured, client_in):
    rule = _rule("Server")
    rule.guests_per_person = 10
    rule.save()
    client_in.post(reverse("staffing_rules"), {"action": "reset"})
    assert _rule("Server").guests_per_person == 25
    assert "Reset" in StaffingRuleChange.objects.get().summary


@pytest.mark.django_db
def test_the_nav_links_to_the_rules(configured, client_in):
    assert reverse("staffing_rules").encode() in client_in.get(reverse("dashboard")).content


@pytest.mark.django_db
def test_every_role_has_a_rule_after_setup(configured):
    names = set(RoleStaffingRule.objects.values_list("role__name", flat=True))
    assert {"Server Manager", "Server", "Bartender", "Busser", "50/50"} <= names


@pytest.mark.django_db
def test_without_stored_rules_the_defaults_apply(db):
    """Before setup has run, the engine still sizes crews by the defaults."""
    assert not RoleStaffingRule.objects.exists()
    assert not Role.objects.exists()
    assert _crew(80)["Server"] == (3, 1)


@pytest.mark.django_db
def test_the_review_page_shows_positions_beyond_the_standard_nine(configured, client_in):
    """Server 4+ and Bartender 2+ were booked on big nights but never shown in the table."""
    show = _show(guests=160)
    for employee in Employee.objects.filter(active=True):
        EmployeeAvailability.objects.create(
            employee=employee, date=DAY, availability_type=AvailabilityType.AVAILABLE_ALL_DAY
        )
    run = SchedulingEngine().generate(DAY, DAY, allow_shortages=True)
    extra = run.assignments.filter(show=show).exclude(
        shift_template__code__in=[
            "server-manager", "lead-server", "server-2", "server-3", "on-call-server",
            "bartender", "on-call-bartender", "busser", "fifty-fifty",
        ]
    )
    assert extra.exists()
    body = client_in.get(reverse("schedule_detail", args=[run.pk])).content.decode()
    assert "More crew" in body
    for assignment in extra:
        assert assignment.shift_template.name in body
        assert assignment.employee.display_name in body
