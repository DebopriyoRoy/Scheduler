"""The raffle seat may not take someone the floor cannot be covered without.

50/50 is settled first because it is the most constrained slot on the board - only two
people hold the role. Both of them also serve. Across one real fortnight that emptied
the floor: Yana took five 50/50 seats and served on none, while three server and five
on-call server positions went unfilled, because the raffle had booked her before they
were settled.

Ordering alone cannot fix it. Settling 50/50 last starves the rotation instead - both
sellers get taken as servers on every show with a seat free and nobody sells tickets,
which is what the Yana/Kate rotation tests catch. The rule is a reservation: a
candidate is withheld from the raffle when losing them would leave the show's required
positions with fewer people than seats.
"""

from datetime import date, time

import pytest
from django.core.management import call_command

from scheduling.models import (
    AssignmentType,
    AvailabilityType,
    Employee,
    EmployeeAvailability,
    FiftyFiftyRotationConfig,
    Show,
)
from scheduling.services.allocator import ESSENTIAL_ROLES, role_tier
from scheduling.services.engine import SchedulingEngine, shift_window_for


@pytest.fixture
def configured_staff(db):
    call_command("seed_spirit_staff")
    call_command("seed_scheduling_config")
    return list(Employee.objects.filter(active=True))


def _show(when: date) -> Show:
    return Show.objects.create(
        title="Forever Country", date=when,
        start_time=time(18, 30), end_time=time(22, 30), expected_guests=80,
    )


def _available(employees, *dates):
    for employee in employees:
        for when in dates:
            EmployeeAvailability.objects.create(
                employee=employee, date=when,
                availability_type=AvailabilityType.AVAILABLE_ALL_DAY,
            )


def test_role_tiers_are_explicit():
    assert ESSENTIAL_ROLES == {"Server", "Bartender", "Server Manager"}
    assert role_tier("Server") == role_tier("Bartender") == 0
    assert role_tier("Busser") == 1
    assert role_tier("50/50") == 2


def test_the_raffle_is_filled_when_the_floor_can_spare_someone(configured_staff):
    """With everyone free there are servers to spare, so 50/50 still gets sold.

    The reservation must not become a blanket refusal - that would be the same bug
    from the other direction.
    """
    when = date(2026, 9, 12)
    _show(when)
    _available(configured_staff, when)
    FiftyFiftyRotationConfig.objects.create(
        seed_employee=Employee.objects.get(display_name="Yana")
    )

    run = SchedulingEngine().generate(when, when)

    assert run.assignments.filter(assignment_type=AssignmentType.FIFTY_FIFTY).exists()


def test_the_raffle_gives_way_when_the_floor_is_short(configured_staff):
    """Only the two sellers are free, and the floor needs them both.

    Before the reservation, the raffle was settled first and took one of them; a
    server seat then went unfilled while a 50/50 seat was staffed.
    """
    when = date(2026, 9, 12)
    show = _show(when)
    sellers = list(Employee.objects.filter(employee_roles__role__name="50/50", active=True))
    assert len(sellers) >= 2, "the fixture roster should hold two 50/50 sellers"
    _available(sellers, when)

    run = SchedulingEngine().generate(when, when, allow_shortages=True)

    fifty = run.assignments.filter(assignment_type=AssignmentType.FIFTY_FIFTY)
    served = run.assignments.filter(show=show, role__name="Server")
    assert served.count() >= 1, "the floor should be staffed before the raffle"
    assert not fifty.exists(), "the raffle took someone the floor could not spare"


def test_nobody_works_two_positions_at_one_show(configured_staff):
    """The reservation must not open a door to double-booking."""
    when = date(2026, 9, 12)
    show = _show(when)
    _available(configured_staff, when)

    run = SchedulingEngine().generate(when, when)

    seen = [a.employee_id for a in run.assignments.filter(show=show)]
    assert len(seen) == len(set(seen))


def test_the_raffle_window_follows_the_show_it_belongs_to(configured_staff):
    """50/50 sells from doors, so its call time is a property of the show type.

    It was a single hardcoded 18:00 for every show, which is half an hour early for an
    ordinary evening whose doors open at 6:30.
    """
    from scheduling.models import ShiftTemplate

    template = ShiftTemplate.objects.get(code="fifty-fifty", active=True)
    ordinary = _show(date(2026, 10, 9))
    dwights = Show.objects.create(
        title="(It's a Nice Day for) Dwight's Wedding!!", date=date(2026, 10, 7),
        start_time=time(18, 30), end_time=time(22, 0), expected_guests=80,
    )

    start, end = shift_window_for(ordinary, template)
    assert (start.time(), end.time()) == (time(18, 30), time(21, 30))

    start, end = shift_window_for(dwights, template)
    assert (start.time(), end.time()) == (time(18, 0), time(21, 30))


@pytest.mark.parametrize(
    "title",
    [
        "HOME SWEET HOME-I-CIDE!",
        "Forever Country…in the Key of Spirit!! - Fall 2026",
        "Shift Happens!",
        "Private Shift Happens on 03 October 2026",
    ],
)
def test_the_regular_shows_keep_house_timing_whatever_the_clock_says(configured_staff, title):
    """Doors 6:30, dinner 7, curtain 8 - recognised by name, not by start time.

    Matching on the clock alone is fragile. Dwight's is the proof: the same production
    is entered against the doors on some dates and the curtain on others. A regular
    show whose calendar entry drifts off 18:30 would silently fall back to offsets
    derived from whatever times it did carry, re-timing the whole crew.
    """
    from scheduling.models import ShiftTemplate

    drifted = Show.objects.create(
        title=title, date=date(2026, 10, 23),
        start_time=time(19, 0), end_time=time(23, 0), expected_guests=80,
    )
    template = ShiftTemplate.objects.get(code="server-2", active=True)

    start, end = shift_window_for(drifted, template)

    assert (start.time(), end.time()) == (time(16, 0), time(21, 30))


def test_dwights_wedding_still_wins_over_the_regular_timetable(configured_staff):
    """Its own timetable takes precedence, including on the private dates."""
    from scheduling.models import ShiftTemplate

    show = Show.objects.create(
        title="PRIVATE EVENT - (It's a Nice Day for) Dwight's Wedding!!",
        date=date(2026, 10, 21),
        start_time=time(18, 0), end_time=time(22, 0), expected_guests=80,
    )
    template = ShiftTemplate.objects.get(code="server-2", active=True)

    start, end = shift_window_for(show, template)

    assert (start.time(), end.time()) == (time(15, 30), time(21, 30))


def test_a_show_on_neither_timetable_still_derives_its_own_window(configured_staff):
    """A private booking at eight must not be forced onto an evening timetable."""
    from scheduling.models import ShiftTemplate

    show = Show.objects.create(
        title="Private @ Gower October 24 2026", date=date(2026, 10, 24),
        start_time=time(20, 0), end_time=time(23, 50), expected_guests=80,
    )
    template = ShiftTemplate.objects.get(code="bartender", active=True)

    start, end = shift_window_for(show, template)

    assert (start.time(), end.time()) == (time(19, 0), time(0, 5))
