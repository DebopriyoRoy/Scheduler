"""A run generated over dates a live roster already covers must say so.

Nobody can work two shifts at once, so anyone already booked on an approved or synced
schedule is correctly refused a second overlapping one. But that refusal only ever
appeared as a line inside each unfilled position, behind a "Why?" link. Run #33 was
generated across dates run #30 already held in Square and came back with forty-six
shortages, every server column empty, and nothing on the page explaining why - which
reads as a broken engine rather than a booked one.

The overlap is a property of the whole run, so it is reported once, at the top.
"""

from datetime import date, time, timedelta

import pytest
from django.contrib.auth import get_user_model
from django.urls import reverse

from scheduling.models import (
    Employee,
    ScheduleRun,
    ScheduleRunStatus,
    SchedulingWarning,
    Show,
    WarningType,
)
from scheduling.services.engine import SchedulingEngine


@pytest.fixture
def seeded(db):
    from django.core.management import call_command

    call_command("seed_spirit_staff")
    call_command("seed_scheduling_config")


def _show(when: date) -> Show:
    return Show.objects.create(
        title="Forever Country",
        date=when,
        start_time=time(18, 30),
        end_time=time(22, 30),
        expected_guests=80,
    )


def _live_run(status, start: date, end: date) -> ScheduleRun:
    return ScheduleRun.objects.create(start_date=start, end_date=end, status=status)


@pytest.mark.parametrize(
    "status",
    [ScheduleRunStatus.SYNCED_TO_SQUARE, ScheduleRunStatus.APPROVED],
)
def test_overlapping_live_roster_is_reported_once(seeded, status):
    start = date(2026, 9, 14)
    _show(start + timedelta(days=2))
    other = _live_run(status, start - timedelta(days=7), start + timedelta(days=14))

    run = SchedulingEngine().generate(start, start + timedelta(days=6), allow_shortages=True)

    warnings = SchedulingWarning.objects.filter(
        schedule_run=run, warning_type=WarningType.OVERLAPPING_ROSTER
    )
    assert warnings.count() == 1
    message = warnings.first().message
    assert f"#{other.pk}" in message
    assert "two shifts at once" in message


def test_a_superseded_run_is_not_reported(seeded):
    """A superseded run books nobody, so naming it would be noise."""
    start = date(2026, 9, 14)
    _show(start + timedelta(days=2))
    _live_run(
        ScheduleRunStatus.SUPERSEDED_SOURCE_DATA,
        start - timedelta(days=7),
        start + timedelta(days=14),
    )

    run = SchedulingEngine().generate(start, start + timedelta(days=6), allow_shortages=True)

    assert not SchedulingWarning.objects.filter(
        schedule_run=run, warning_type=WarningType.OVERLAPPING_ROSTER
    ).exists()


def test_no_overlap_produces_no_warning(seeded):
    start = date(2026, 9, 14)
    _show(start + timedelta(days=2))
    # Live, but finishing well before this run begins.
    _live_run(
        ScheduleRunStatus.SYNCED_TO_SQUARE, start - timedelta(days=60), start - timedelta(days=30)
    )

    run = SchedulingEngine().generate(start, start + timedelta(days=6), allow_shortages=True)

    assert not SchedulingWarning.objects.filter(
        schedule_run=run, warning_type=WarningType.OVERLAPPING_ROSTER
    ).exists()


def test_the_overlap_is_shown_at_the_top_of_the_schedule_page(seeded, client):
    """It has WARNING severity, so it would never appear in the blocking-errors list.

    That list only carries ERRORs. Without its own banner this warning exists in the
    database and nowhere a manager will ever look.
    """
    start = date(2026, 9, 14)
    _show(start + timedelta(days=2))
    other = _live_run(
        ScheduleRunStatus.SYNCED_TO_SQUARE, start - timedelta(days=7), start + timedelta(days=14)
    )
    run = SchedulingEngine().generate(start, start + timedelta(days=6), allow_shortages=True)

    user = get_user_model().objects.create_user(username="mgr", password="safe-test-password")
    client.force_login(user)
    html = client.get(reverse("schedule_detail", args=[run.pk])).content.decode()

    assert "These dates are already rostered elsewhere" in html
    assert f"#{other.pk}" in html


def test_the_engine_still_refuses_to_double_book(seeded):
    """The warning explains the refusal; it must not soften it.

    Someone already on a synced roster must stay unavailable here - a warning that
    quietly let the engine book them twice would be far worse than silence.
    """
    from decimal import Decimal

    from scheduling.models import Role, ScheduleAssignment, ShiftTemplate

    start = date(2026, 9, 14)
    show = _show(start + timedelta(days=2))
    other = _live_run(
        ScheduleRunStatus.SYNCED_TO_SQUARE, start - timedelta(days=7), start + timedelta(days=14)
    )
    employee = Employee.objects.filter(employee_roles__role__name="Server").first()
    role = Role.objects.get(name="Server")
    template = ShiftTemplate.objects.filter(role=role, active=True).first()
    ScheduleAssignment.objects.create(
        schedule_run=other,
        show=show,
        employee=employee,
        role=role,
        shift_template=template,
        start_datetime=show.date_start_datetime()
        if hasattr(show, "date_start_datetime")
        else __import__("django.utils.timezone", fromlist=["now"]).now(),
        end_datetime=__import__("django.utils.timezone", fromlist=["now"]).now()
        + timedelta(hours=6),
        scheduled_paid_hours=Decimal("6.00"),
        on_call_hours=Decimal("0.00"),
    )

    run = SchedulingEngine().generate(start, start + timedelta(days=6), allow_shortages=True)

    overlapping = ScheduleAssignment.objects.filter(schedule_run=run, employee=employee)
    for assignment in overlapping:
        clash = ScheduleAssignment.objects.filter(
            schedule_run=other,
            employee=employee,
            start_datetime__lt=assignment.end_datetime,
            end_datetime__gt=assignment.start_datetime,
        )
        assert not clash.exists(), "the engine double-booked someone already rostered"
