"""A person can hold more than one availability window on the same weekday.

Square gives the first window a row carrying the person's name, and every further
window a row with **no name cell at all** - not an empty one, absent. The reader used
to skip nameless rows, so a second window was discarded before anything downstream saw
it. Khrystyna lost her 18:00-23:00 evening and looked like daytime-only staff who
could never work a show; Yana lost her Friday, Saturday and Sunday evenings.

The rows below are copied verbatim from the live dashboard. The shorter shape is the
point: a continuation row has seven cells to a named row's eight, so every weekday
sits one position to the left. Reading both at fixed positions puts Yana's Friday
evening on a Monday, which is worse than dropping it.
"""

from datetime import time

import pytest

from scheduling.integrations.square_availability.base import AvailabilityState
from scheduling.integrations.square_availability.live_provider import (
    build_grid,
    parse_cells,
)

HEADER = [
    "Team member", "Monday", "Tuesday", "Wednesday",
    "Thursday", "Friday", "Saturday", "Sunday",
]

# Eight cells: name plus seven weekdays.
KHRYSTYNA = [
    "Khrystyna Zavadetska",
    "Available\n11:00 am – 4:00 pm", "Available\n11:00 am – 4:00 pm",
    "Available\n11:00 am – 4:00 pm", "Available\n11:00 am – 4:00 pm",
    "Available\n11:00 am – 4:00 pm", "Available\n10:00 am – 4:00 pm",
    "Available\n10:00 am – 4:00 pm",
]
# Seven cells: no name cell at all.
KHRYSTYNA_SECOND = ["Available\n6:00 pm – 11:00 pm"] * 7

YANA = [
    "Yana Pasechniuk",
    "Available\n2:30 pm – 11:59 pm", "Available\n2:30 pm – 11:59 pm",
    "Available\n2:30 pm – 11:59 pm", "Available\n2:30 pm – 11:59 pm",
    "Available\n10:00 am – 3:00 pm", "Available\n10:00 am – 3:00 pm",
    "Available\n10:00 am – 3:00 pm",
]
YANA_SECOND = [
    "", "", "", "",
    "Available\n4:30 pm – 9:30 pm",
    "Available\n5:00 pm – 10:00 pm",
    "Available\n5:00 pm – 11:00 pm",
]

MON, TUE, WED, THU, FRI, SAT, SUN = range(7)


@pytest.fixture
def grid():
    return build_grid([HEADER, KHRYSTYNA, KHRYSTYNA_SECOND, YANA, YANA_SECOND])


def _windows(grid, name, weekday):
    return [
        (start, end)
        for state, start, end in parse_cells(grid.rows[name].get(weekday, []))
        if state == AvailabilityState.AVAILABLE_WINDOW
    ]


def test_a_nameless_row_belongs_to_the_person_above_it(grid):
    assert grid.names() == ["Khrystyna Zavadetska", "Yana Pasechniuk"]


def test_both_of_khrystynas_windows_survive(grid):
    """The evening window is the one that decides whether she can work a show."""
    assert _windows(grid, "Khrystyna Zavadetska", MON) == [
        (time(11, 0), time(16, 0)),
        (time(18, 0), time(23, 0)),
    ]


def test_khrystynas_second_window_covers_every_weekday(grid):
    for weekday in range(7):
        assert (time(18, 0), time(23, 0)) in _windows(
            grid, "Khrystyna Zavadetska", weekday
        ), f"evening window missing on weekday {weekday}"


def test_yanas_extra_evenings_land_on_the_right_days(grid):
    """The shorter row shifts every column left by one.

    Reading it at the named row's positions would file these under Monday, Tuesday
    and Wednesday - present in the data, and wrong in a way nothing would flag.
    """
    assert _windows(grid, "Yana Pasechniuk", FRI) == [
        (time(10, 0), time(15, 0)),
        (time(16, 30), time(21, 30)),
    ]
    assert _windows(grid, "Yana Pasechniuk", SAT) == [
        (time(10, 0), time(15, 0)),
        (time(17, 0), time(22, 0)),
    ]
    assert _windows(grid, "Yana Pasechniuk", SUN) == [
        (time(10, 0), time(15, 0)),
        (time(17, 0), time(23, 0)),
    ]


def test_yanas_untouched_days_gain_nothing(grid):
    """The continuation row is blank Monday to Thursday; those days keep one window."""
    for weekday in (MON, TUE, WED, THU):
        assert _windows(grid, "Yana Pasechniuk", weekday) == [
            (time(14, 30), time(23, 59))
        ]


def test_a_blank_weekday_is_unknown_not_unavailable(grid):
    """Kate has no Friday row at all. Square holding nothing is not a refusal."""
    grid_with_gap = build_grid(
        [HEADER, ["Kate Griffin", "Available\n7:00 pm – 11:00 pm", "", "", "", "", "", ""]]
    )
    states = [s for s, _, _ in parse_cells(grid_with_gap.rows["Kate Griffin"].get(FRI, []))]
    assert states == [AvailabilityState.UNKNOWN]


def test_all_day_beats_a_window_stated_alongside_it():
    states = parse_cells(["Available\nAll day", "Available\n6:00 pm – 11:00 pm"])
    assert states == [(AvailabilityState.AVAILABLE_ALL_DAY, None, None)]


def test_repeated_header_rows_do_not_become_people():
    """The header re-renders as the grid scrolls; it is not a member of staff."""
    grid = build_grid([HEADER, KHRYSTYNA, HEADER, YANA])
    assert grid.names() == ["Khrystyna Zavadetska", "Yana Pasechniuk"]


def test_a_nameless_row_before_any_person_is_ignored():
    """Nothing to attach it to, so it must not attach to whoever comes next."""
    grid = build_grid([HEADER, ["Available\n6:00 pm – 11:00 pm"] * 7, KHRYSTYNA])
    assert grid.names() == ["Khrystyna Zavadetska"]
    assert _windows(grid, "Khrystyna Zavadetska", MON) == [(time(11, 0), time(16, 0))]
