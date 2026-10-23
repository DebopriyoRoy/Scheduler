"""Step 3: which staffing rules a show gets, named before a roster exists."""

from datetime import date, time

import pytest

from scheduling.models import Show
from scheduling.services.show_profiles import ProfileKey, profile_for

DAY = date(2026, 11, 20)


def _show(title, guests=150, start=time(18, 30), end=time(22, 30), venue="Theatre Gower"):
    return Show(
        title=title, date=DAY, expected_guests=guests,
        start_time=start, end_time=end, venue=venue,
    )


@pytest.mark.parametrize(
    "show, key, staffed",
    [
        (_show("Ugly Stick Workshop"), ProfileKey.WORKSHOP, False),
        (_show("Offsite Christmas Party"), ProfileKey.OFFSITE, False),
        (_show("Kitchen Party", venue="Offsite - Delta"), ProfileKey.OFFSITE, False),
        (_show("The Blizzard of Oz", guests=62), ProfileKey.BELOW_MINIMUM, False),
        (_show("Private Event - Law Firm"), ProfileKey.PRIVATE, True),
        (_show("Dwight's Wedding", start=time(18, 0)), ProfileKey.DWIGHTS, True),
        (_show("Shift Happens", start=time(18, 0)), ProfileKey.REGULAR_EVENING, True),
        (_show("The Blizzard of Oz"), ProfileKey.REGULAR_EVENING, True),
        (
            _show("Christmas Lunch", start=time(12, 0), end=time(14, 30)),
            ProfileKey.OWN_TIMES,
            True,
        ),
    ],
    ids=lambda v: getattr(v, "title", v),
)
def test_each_kind_of_show_gets_its_rules(show, key, staffed):
    profile = profile_for(show)
    assert profile.key == key
    assert profile.staffed is staffed


def test_a_workshop_wins_even_when_it_is_small():
    """Checked in the engine's order: a 20-person workshop is a workshop, not a small show."""
    assert profile_for(_show("Ugly Stick Workshop", guests=20)).key == ProfileKey.WORKSHOP


def test_a_show_with_no_guest_count_is_planned_at_the_buffer():
    profile = profile_for(_show("Shift Happens", guests=None))
    assert profile.key == ProfileKey.REGULAR_EVENING


def test_own_timetable_says_which_times_it_uses():
    profile = profile_for(_show("Christmas Lunch", start=time(12, 0), end=time(14, 30)))
    assert "12:00" in profile.timetable and "14:30" in profile.timetable
