"""Management's staff list by department, which is not the same thing as a rota role."""

import re

import pytest
from django.core.management import call_command

from scheduling.models import Department, Employee, EmployeeDepartment

EXPECTED = {
    "Marketing/Screech in": 3,
    "Bar Manager": 1,
    "Bartenders at Spirit": 5,
    "ACC Bartenders": 6,
    "Server Manager": 1,
    "Server": 13,
    "Flyers/Screech in Helper": 4,
    "50/50": 3,
    "Kitchen": 9,
    "Office/reservations": 6,
    "Cleaners/ Maintenance": 3,
    "Tech": 1,
}


@pytest.fixture
def departments(db):
    call_command("seed_spirit_staff")
    call_command("seed_departments")


def test_the_written_structure_is_reproduced_exactly(departments):
    actual = {
        d.name: d.memberships.count() for d in Department.objects.all()
    }
    assert actual == EXPECTED


def test_departments_keep_the_order_they_were_written_in(departments):
    assert [d.name for d in Department.objects.all()] == list(EXPECTED)


def test_one_person_belongs_to_several_departments(departments):
    """Yana is in five of them. That is the staff list, not duplicated data."""
    yana = Employee.objects.get(display_name="Yana Pasechniuk")
    names = {m.department.name for m in yana.department_memberships.all()}
    assert names == {
        "Marketing/Screech in",
        "Server",
        "Flyers/Screech in Helper",
        "50/50",
        "Office/reservations",
    }


@pytest.mark.django_db
def test_a_department_is_not_a_scheduling_role():
    """Neil is in the Server department but tends bar in the rota.

    Built from scratch rather than the dev seed, whose roster spells some names
    differently from the live one ("Neil Bobbit" against "Neil Bobbitt").
    """
    from scheduling.models import EmployeeRole, Role

    neil = Employee.objects.create(display_name="Neil Bobbitt", first_name="Neil", active=True)
    bartender, _ = Role.objects.get_or_create(name="Bartender")
    EmployeeRole.objects.create(employee=neil, role=bartender, active=True, capability_level=4)
    call_command("seed_departments")

    neil.refresh_from_db()
    assert "Server" in {m.department.name for m in neil.department_memberships.all()}
    assert "Bartender" in {r.role.name for r in neil.employee_roles.filter(active=True)}
    # Being in the Server department does not make him a rota Server.
    assert "Server" not in {r.role.name for r in neil.employee_roles.filter(active=True)}


def test_staff_this_app_never_rosters_are_created_unschedulable(departments):
    """Kitchen and office staff belong on the list without entering the rota.

    Creating them as ordinary employees would put them in the scheduling pool and have
    them counted as people whose availability is missing, which is noise about staff
    this application does not schedule.
    """
    for name in ["Ash Lundrigan", "Zachary Samson", "Keith Power", "Colton Tucker"]:
        person = Employee.objects.get(display_name=name)
        assert person.excluded_from_automatic_scheduling is True
        assert not person.employee_roles.filter(active=True).exists()


def test_reseeding_changes_nothing(departments):
    before = EmployeeDepartment.objects.count()
    call_command("seed_departments")
    assert EmployeeDepartment.objects.count() == before


@pytest.mark.django_db
def test_the_page_filters_to_one_department(client, departments, django_user_model):
    user = django_user_model.objects.create_user(username="mgr", password="x")
    client.force_login(user)

    everyone = client.get("/employees/")
    assert everyone.status_code == 200
    assert b"Kitchen" in everyone.content

    kitchen = client.get("/employees/", {"department": "Kitchen"})
    assert kitchen.status_code == 200
    body = kitchen.content.decode()
    assert "Charlie Barron" in body
    # Exactly one department section is rendered, and it is the Kitchen. Checking the
    # headings rather than a name: names crop up elsewhere on this page (the time-off
    # panel, the missing-availability notice) and would make a plain absence check lie.
    headings = re.findall(r'<th colspan="\d+" class="py-2">\s*([^<]+?)\s*<span', body)
    assert headings == ["Kitchen"]
