"""Manual availability changes laid over Square.

The promise these protect: a manager's change wins over Square, survives a Square
refresh, and a change Square later contradicts is surfaced instead of silently lost
or silently applied.
"""

from datetime import date, time

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command

from scheduling.integrations.square_availability.service import SQUARE_AVAILABILITY_SOURCE
from scheduling.models import (
    AvailabilityOverride,
    AvailabilityType,
    Employee,
    EmployeeAvailability,
    EmployeeTimeOff,
    OverrideAction,
    Show,
    TimeOffSource,
    TimeOffStatus,
)
from scheduling.services.availability import LocalAvailabilityProvider
from scheduling.services.engine import SchedulingEngine
from scheduling.services.overrides import (
    conflicts_on,
    is_in_conflict,
    keep_override,
    record_override,
    square_view,
    use_square,
)

DAY = date(2026, 9, 12)


@pytest.fixture
def staff(db):
    call_command("seed_spirit_staff", verbosity=0)
    call_command("seed_scheduling_config", verbosity=0)
    return list(Employee.objects.filter(active=True))


@pytest.fixture
def show(staff):
    show = Show.objects.create(title="Override Test Show", date=DAY, expected_guests=80)
    for employee in staff:
        EmployeeAvailability.objects.create(
            employee=employee,
            date=DAY,
            availability_type=AvailabilityType.AVAILABLE_ALL_DAY,
            source=SQUARE_AVAILABILITY_SOURCE,
        )
    return show


@pytest.fixture
def olena(staff):
    return Employee.objects.get(display_name="Olena")


def _who_worked(run):
    return set(run.assignments.values_list("employee__display_name", flat=True))


def _square_says(employee, availability_type):
    EmployeeAvailability.objects.filter(employee=employee, date=DAY).delete()
    EmployeeAvailability.objects.create(
        employee=employee,
        date=DAY,
        availability_type=availability_type,
        source=SQUARE_AVAILABILITY_SOURCE,
    )


def _square_time_off(employee):
    return EmployeeTimeOff.objects.create(
        employee=employee,
        start_date=DAY,
        end_date=DAY,
        status=TimeOffStatus.APPROVED,
        source=TimeOffSource.SQUARE,
        reason="holiday",
    )


@pytest.mark.django_db
def test_a_removal_beats_square_all_day(show, olena):
    """The bug this layer exists for: a hand-entered "no" used to lose to Square."""
    record_override(olena, DAY, OverrideAction.REMOVE, "Training at Mile One")

    result = LocalAvailabilityProvider().check(olena, DAY, time(16, 0), time(23, 0))
    assert not result.available
    assert "Training at Mile One" in result.reasons[0]

    run = SchedulingEngine().generate(DAY, DAY, allow_shortages=True)
    assert "Olena" not in _who_worked(run)


@pytest.mark.django_db
def test_an_addition_beats_square_unavailable(show, olena):
    _square_says(olena, AvailabilityType.UNAVAILABLE)
    provider = LocalAvailabilityProvider()
    assert not provider.check(olena, DAY, time(16, 0), time(23, 0)).available

    record_override(olena, DAY, OverrideAction.ADD, "Called in, can cover")

    assert provider.check(olena, DAY, time(16, 0), time(23, 0)).available
    assert provider.fit(olena, DAY, time(16, 0), time(23, 0)).covers_full_shift


@pytest.mark.django_db
def test_an_added_window_counts_only_for_its_hours(show, olena):
    _square_says(olena, AvailabilityType.UNAVAILABLE)
    record_override(
        olena, DAY, OverrideAction.ADD, "Free after class",
        start_time=time(18, 0), end_time=time(23, 0),
    )
    provider = LocalAvailabilityProvider()
    assert provider.check(olena, DAY, time(18, 45), time(23, 0)).available
    assert not provider.check(olena, DAY, time(14, 0), time(21, 0)).available


@pytest.mark.django_db
def test_an_override_survives_square_rewriting_its_rows(show, olena):
    """A refresh deletes and rewrites every row it owns; the override is not one of them."""
    record_override(olena, DAY, OverrideAction.REMOVE, "Swapped with Megan")
    EmployeeAvailability.objects.filter(source=SQUARE_AVAILABILITY_SOURCE).delete()
    _square_says(olena, AvailabilityType.AVAILABLE_ALL_DAY)

    assert AvailabilityOverride.objects.filter(employee=olena, active=True).exists()
    assert not LocalAvailabilityProvider().check(olena, DAY, time(16, 0), time(23, 0)).available


@pytest.mark.django_db
def test_a_new_change_retires_the_old_one_rather_than_deleting_it(show, olena):
    first = record_override(olena, DAY, OverrideAction.REMOVE, "Training day")
    second = record_override(olena, DAY, OverrideAction.ADD, "Training moved")

    first.refresh_from_db()
    assert not first.active
    assert first.retired_reason == "Replaced by a newer change."
    assert second.active
    assert AvailabilityOverride.objects.filter(employee=olena, date=DAY).count() == 2


@pytest.mark.django_db
def test_a_reason_is_required(show, olena):
    with pytest.raises(ValidationError):
        record_override(olena, DAY, OverrideAction.REMOVE, "  ")


@pytest.mark.django_db
def test_a_removal_cannot_be_partial(show, olena):
    with pytest.raises(ValidationError):
        record_override(
            olena, DAY, OverrideAction.REMOVE, "Dentist in the morning",
            start_time=time(9, 0), end_time=time(12, 0),
        )


@pytest.mark.django_db
def test_square_state_is_snapshotted_when_the_change_is_made(show, olena):
    _square_says(olena, AvailabilityType.UNAVAILABLE)
    override = record_override(olena, DAY, OverrideAction.ADD, "Called in")
    assert override.square_state_at_creation == "Not available"
    assert square_view(olena, DAY).summary == "Not available"


@pytest.mark.django_db
def test_an_addition_made_knowing_about_time_off_overrules_it(show, olena):
    _square_time_off(olena)
    override = record_override(olena, DAY, OverrideAction.ADD, "Cancelled her trip, can work")

    assert override.time_off_at_creation
    assert not is_in_conflict(override)
    run = SchedulingEngine().generate(DAY, DAY, allow_shortages=True)
    assert "Olena" in _who_worked(run)


@pytest.mark.django_db
def test_time_off_approved_after_an_addition_is_a_conflict_that_blocks(show, olena):
    override = record_override(olena, DAY, OverrideAction.ADD, "Swapped with Megan")
    _square_time_off(olena)

    assert is_in_conflict(override)
    assert conflicts_on(DAY) == [override]
    # Unsettled, the time off wins: guessing wrong puts someone on a night they booked off.
    run = SchedulingEngine().generate(DAY, DAY, allow_shortages=True)
    assert "Olena" not in _who_worked(run)


@pytest.mark.django_db
def test_keeping_the_change_settles_the_conflict_in_its_favour(show, olena):
    override = record_override(olena, DAY, OverrideAction.ADD, "Swapped with Megan")
    _square_time_off(olena)

    keep_override(override)

    assert not is_in_conflict(override)
    assert conflicts_on(DAY) == []
    run = SchedulingEngine().generate(DAY, DAY, allow_shortages=True)
    assert "Olena" in _who_worked(run)


@pytest.mark.django_db
def test_using_square_retires_the_change(show, olena):
    override = record_override(olena, DAY, OverrideAction.ADD, "Swapped with Megan")
    _square_time_off(olena)

    use_square(override)

    override.refresh_from_db()
    assert not override.active
    assert conflicts_on(DAY) == []
    run = SchedulingEngine().generate(DAY, DAY, allow_shortages=True)
    assert "Olena" not in _who_worked(run)


@pytest.mark.django_db
def test_an_override_makes_an_unknown_date_known(show, olena):
    EmployeeAvailability.objects.filter(employee=olena, date=DAY).delete()
    engine = SchedulingEngine()
    before = engine._missing_availability_count([show])

    record_override(olena, DAY, OverrideAction.ADD, "Told the office she is free")

    assert engine._missing_availability_count([show]) == before - 1
