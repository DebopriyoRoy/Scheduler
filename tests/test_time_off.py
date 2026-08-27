"""Time off, and the one thing that matters about it: only approved absences count.

A pending request is a question a manager has not answered. Blocking on it would
overrule them silently, and a roster built around a request that is later declined
is a roster built around a refusal that never happened.
"""

from datetime import date, time, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.urls import reverse

from scheduling.models import (
    AssignmentType,
    AvailabilityType,
    Employee,
    EmployeeAvailability,
    EmployeeTimeOff,
    Show,
    TimeOffStatus,
)
from scheduling.services.engine import SchedulingEngine


@pytest.fixture
def staff(db):
    call_command("seed_spirit_staff", verbosity=0)
    call_command("seed_scheduling_config", verbosity=0)
    return list(Employee.objects.filter(active=True))


@pytest.fixture
def show(staff):
    show = Show.objects.create(
        title="Time Off Test Show",
        date=date(2026, 9, 12),
        expected_guests=80,
    )
    for employee in staff:
        EmployeeAvailability.objects.create(
            employee=employee,
            date=show.date,
            availability_type=AvailabilityType.AVAILABLE_ALL_DAY,
        )
    return show


def _who_worked(run):
    return set(run.assignments.values_list("employee__display_name", flat=True))


@pytest.mark.django_db
def test_approved_time_off_keeps_somebody_off_the_schedule(staff, show):
    victim = Employee.objects.get(display_name="Olena")
    EmployeeTimeOff.objects.create(
        employee=victim,
        start_date=show.date,
        end_date=show.date,
        status=TimeOffStatus.APPROVED,
        reason="holiday",
    )

    run = SchedulingEngine().generate(show.date, show.date, allow_shortages=True)

    assert victim.display_name not in _who_worked(run)


@pytest.mark.django_db
def test_a_pending_request_does_not_block_scheduling(staff, show):
    """Unanswered is not the same as refused."""
    person = Employee.objects.get(display_name="Olena")
    EmployeeTimeOff.objects.create(
        employee=person,
        start_date=show.date,
        end_date=show.date,
        status=TimeOffStatus.PENDING,
        reason="asked for the day",
    )

    run = SchedulingEngine().generate(show.date, show.date, allow_shortages=True)

    assert person.display_name in _who_worked(run)


@pytest.mark.django_db
@pytest.mark.parametrize("status", [TimeOffStatus.DECLINED, TimeOffStatus.CANCELLED])
def test_a_declined_or_cancelled_request_does_not_block_scheduling(staff, show, status):
    person = Employee.objects.get(display_name="Olena")
    EmployeeTimeOff.objects.create(
        employee=person, start_date=show.date, end_date=show.date, status=status
    )

    run = SchedulingEngine().generate(show.date, show.date, allow_shortages=True)

    assert person.display_name in _who_worked(run)


@pytest.mark.django_db
def test_a_multi_day_absence_covers_every_day_in_its_range(staff, show):
    person = Employee.objects.get(display_name="Olena")
    EmployeeTimeOff.objects.create(
        employee=person,
        start_date=show.date - timedelta(days=2),
        end_date=show.date + timedelta(days=2),
        status=TimeOffStatus.APPROVED,
    )

    run = SchedulingEngine().generate(show.date, show.date, allow_shortages=True)

    assert person.display_name not in _who_worked(run)


@pytest.mark.django_db
def test_an_absence_that_ends_before_the_show_does_not_block_it(staff, show):
    person = Employee.objects.get(display_name="Olena")
    EmployeeTimeOff.objects.create(
        employee=person,
        start_date=show.date - timedelta(days=5),
        end_date=show.date - timedelta(days=1),
        status=TimeOffStatus.APPROVED,
    )

    run = SchedulingEngine().generate(show.date, show.date, allow_shortages=True)

    assert person.display_name in _who_worked(run)


@pytest.mark.django_db
def test_a_morning_absence_does_not_block_an_evening_show(staff, show):
    """Partial time off blocks only the hours it actually covers."""
    person = Employee.objects.get(display_name="Olena")
    EmployeeTimeOff.objects.create(
        employee=person,
        start_date=show.date,
        end_date=show.date,
        start_time=time(9, 0),
        end_time=time(12, 0),
        status=TimeOffStatus.APPROVED,
        reason="appointment",
    )

    run = SchedulingEngine().generate(show.date, show.date, allow_shortages=True)

    assert person.display_name in _who_worked(run)


@pytest.mark.django_db
def test_a_partial_absence_overlapping_the_shift_does_block_it(staff, show):
    person = Employee.objects.get(display_name="Olena")
    EmployeeTimeOff.objects.create(
        employee=person,
        start_date=show.date,
        end_date=show.date,
        start_time=time(17, 0),
        end_time=time(23, 30),
        status=TimeOffStatus.APPROVED,
    )

    run = SchedulingEngine().generate(show.date, show.date, allow_shortages=True)

    assert person.display_name not in _who_worked(run)


@pytest.mark.django_db
def test_the_reason_given_names_time_off_so_the_roster_explains_itself(staff, show):
    from scheduling.models import Role, ShiftTemplate
    from scheduling.services.eligibility import EligibilityService

    person = Employee.objects.get(display_name="Olena")
    EmployeeTimeOff.objects.create(
        employee=person,
        start_date=show.date,
        end_date=show.date,
        status=TimeOffStatus.APPROVED,
        reason="wedding",
    )
    run = SchedulingEngine().generate(show.date, show.date, allow_shortages=True)
    template = ShiftTemplate.objects.get(code="lead-server")
    start, end = SchedulingEngine()._datetimes(show, template)

    result = EligibilityService().evaluate(
        person, Role.objects.get(name="Server"), show, template, run, start, end
    )

    assert not result.eligible
    assert any("time off" in reason.lower() for reason in result.reasons)
    assert any("wedding" in reason for reason in result.reasons)


@pytest.mark.django_db
def test_the_employees_page_lists_upcoming_time_off(staff, show):
    person = Employee.objects.get(display_name="Olena")
    EmployeeTimeOff.objects.create(
        employee=person,
        start_date=date.today() + timedelta(days=3),
        end_date=date.today() + timedelta(days=4),
        status=TimeOffStatus.APPROVED,
        reason="family",
    )
    user = get_user_model().objects.create_user(username="mgr", password="safe-test-password")

    from django.test import Client

    client = Client()
    client.force_login(user)
    body = client.get(reverse("employees")).content.decode()

    assert "Time Off" in body
    assert "family" in body


@pytest.mark.django_db
def test_approving_from_the_page_starts_blocking_the_schedule(staff, show):
    person = Employee.objects.get(display_name="Olena")
    entry = EmployeeTimeOff.objects.create(
        employee=person,
        start_date=show.date,
        end_date=show.date,
        status=TimeOffStatus.PENDING,
    )
    user = get_user_model().objects.create_user(username="mgr", password="safe-test-password")

    from django.test import Client

    client = Client()
    client.force_login(user)

    before = SchedulingEngine().generate(show.date, show.date, allow_shortages=True)
    assert person.display_name in _who_worked(before)

    client.post(reverse("time_off_approve", args=[entry.pk]), follow=True)
    entry.refresh_from_db()
    assert entry.status == TimeOffStatus.APPROVED

    after = SchedulingEngine().generate(show.date, show.date, allow_shortages=True)
    assert person.display_name not in _who_worked(after)


@pytest.mark.django_db
def test_an_absence_never_silently_drops_the_shift_it_blocks(staff, show):
    """Somebody else must pick it up, or it must be reported as a shortage."""
    victim = Employee.objects.get(display_name="Olena")
    EmployeeTimeOff.objects.create(
        employee=victim,
        start_date=show.date,
        end_date=show.date,
        status=TimeOffStatus.APPROVED,
    )

    run = SchedulingEngine().generate(show.date, show.date, allow_shortages=True)

    confirmed_servers = run.assignments.filter(
        role__name="Server", assignment_type=AssignmentType.CONFIRMED
    ).count()
    shortages = run.warnings.filter(warning_type__contains="SHORTAGE").count()
    assert confirmed_servers == 3 or shortages > 0
