"""A manager kept out of the ordinary rota must still be able to hold their own job.

The Server Manager position exists for exactly the people who are excluded from
serving. Eligibility already exempts that one role from the manager exclusion - but
availability is only synced for the roster, and the roster was selected with
`excluded_from_automatic_scheduling=False`. So a manager's hours were never read, the
one position they are eligible for could never be filled, and every show reported a
Server Manager shortage. An exclusion from serving had quietly become an exclusion
from managing.

Holding a role is now the test, not the flag.
"""

from datetime import date, time

import pytest
from django.core.management import call_command

from scheduling.integrations.square_availability.service import roster_employees
from scheduling.models import (
    AvailabilityType,
    Employee,
    EmployeeAvailability,
    EmployeeRole,
    Role,
    Show,
)
from scheduling.services.eligibility import EligibilityService
from scheduling.services.engine import SchedulingEngine


@pytest.fixture
def configured(db):
    call_command("seed_spirit_staff")
    call_command("seed_scheduling_config")


@pytest.fixture
def manager(configured):
    # The staff seed already carries her, so this reuses that record rather than
    # creating a second Deborah and colliding on the unique display name.
    person, _ = Employee.objects.get_or_create(
        display_name="Deborah Sweetapple",
        defaults={"first_name": "Deborah", "last_name": "Sweetapple", "active": True},
    )
    person.active = True
    person.excluded_from_automatic_scheduling = True
    person.save()
    EmployeeRole.objects.update_or_create(
        employee=person,
        role=Role.objects.get(name="Server Manager"),
        defaults={"capability_level": 5, "active": True},
    )
    return person


def test_an_excluded_manager_holding_a_role_is_on_the_availability_roster(manager):
    """Without this their hours are never read and the position cannot be filled."""
    assert roster_employees().filter(pk=manager.pk).exists()


def test_someone_with_no_role_stays_off_the_roster(configured):
    """Kitchen, cleaners and the owner are on the books and are not staff to roster."""
    chef = Employee.objects.create(
        first_name="Colleen", last_name="O'Reilly", display_name="Colleen O'Reilly", active=True
    )
    assert not roster_employees().filter(pk=chef.pk).exists()


def test_the_manager_is_eligible_for_their_own_role_only(manager):
    """The exemption is scoped: they manage, they never serve."""
    from scheduling.models import ScheduleRun, ShiftTemplate

    show = Show.objects.create(
        title="Forever Country", date=date(2026, 10, 9),
        start_time=time(18, 30), end_time=time(22, 30), expected_guests=80,
    )
    EmployeeAvailability.objects.create(
        employee=manager, date=show.date, availability_type=AvailabilityType.AVAILABLE_ALL_DAY
    )
    run = ScheduleRun.objects.create(start_date=show.date, end_date=show.date)
    service = EligibilityService()

    manager_template = ShiftTemplate.objects.get(code="server-manager", active=True)
    start, end = SchedulingEngine()._datetimes(show, manager_template)
    assert service.evaluate(
        manager, manager_template.role, show, manager_template, run, start, end
    ).eligible

    server_template = ShiftTemplate.objects.get(code="server-2", active=True)
    start, end = SchedulingEngine()._datetimes(show, server_template)
    result = service.evaluate(
        manager, server_template.role, show, server_template, run, start, end
    )
    assert not result.eligible


def test_the_manager_position_gets_filled(manager):
    """End to end: a show should come out with the manager on it, not a shortage."""
    show = Show.objects.create(
        title="Forever Country", date=date(2026, 10, 9),
        start_time=time(18, 30), end_time=time(22, 30), expected_guests=80,
    )
    for employee in Employee.objects.filter(active=True):
        EmployeeAvailability.objects.create(
            employee=employee, date=show.date,
            availability_type=AvailabilityType.AVAILABLE_ALL_DAY,
        )

    run = SchedulingEngine().generate(show.date, show.date, allow_shortages=True)

    assert run.assignments.filter(role__name="Server Manager", employee=manager).exists()
    assert not run.warnings.filter(warning_type="SERVER_MANAGER_SHORTAGE").exists()
