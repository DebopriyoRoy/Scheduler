"""Parse the availability cells Square actually renders.

Every string below was copied from the live dashboard grid. The pattern these replace
required ":MM" and a meridiem on both sides of the dash, which no real cell has, so it
matched nothing and reported every windowed day as UNKNOWN - indistinguishable from
"Square holds nothing for this person". The sync therefore looked healthy while
returning no hours at all, and the fallback dict's values stood in for them.

These are regression tests in the strict sense: each one fails against that pattern.
"""

from datetime import time

import pytest

from scheduling.integrations.square_availability.base import AvailabilityState
from scheduling.integrations.square_availability.live_provider import parse_cell


@pytest.mark.parametrize(
    ("cell", "start", "end"),
    [
        # Minutes dropped when zero, meridiem only on the end - the common case.
        ("Available\n1 – 10 pm", time(13, 0), time(22, 0)),
        ("Available\n2 – 11 pm", time(14, 0), time(23, 0)),
        ("Available\n7 – 11 pm", time(19, 0), time(23, 0)),
        ("Available\n4 – 8:30 pm", time(16, 0), time(20, 30)),
        ("Available\n5:30 – 11:59 pm", time(17, 30), time(23, 59)),
        # Kate's Thursday. The hand-typed dict recorded this as 05:30 and the engine
        # believed it; borrowing the end's meridiem is what makes it 17:30.
        ("Available\n5:30 – 9:30 pm", time(17, 30), time(21, 30)),
        # Both meridiems present.
        ("Available\n11 am – 4 pm", time(11, 0), time(16, 0)),
        # Borrowing "pm" would put the start after the end, so the start must be am.
        ("Available\n10 – 4 pm", time(10, 0), time(16, 0)),
        # Hyphen and em dash occur as well as the en dash.
        ("Available\n4 - 8:30 pm", time(16, 0), time(20, 30)),
        ("Available\n4 — 8:30 pm", time(16, 0), time(20, 30)),
        # Square writes noon and midnight as 12.
        ("Available\n12 – 5 pm", time(12, 0), time(17, 0)),
    ],
)
def test_real_dashboard_windows_parse(cell, start, end):
    state, got_start, got_end = parse_cell(cell)
    assert state == AvailabilityState.AVAILABLE_WINDOW
    assert (got_start, got_end) == (start, end)


def test_all_day_is_not_a_window():
    assert parse_cell("Available\nAll day")[0] == AvailabilityState.AVAILABLE_ALL_DAY


def test_unavailable_is_distinct_from_unknown():
    assert parse_cell("Unavailable")[0] == AvailabilityState.UNAVAILABLE


def test_empty_cell_is_unknown_not_unavailable():
    """Square holding nothing is not the same as someone refusing the day.

    Both end up unschedulable, but only one of them is worth asking a person about.
    """
    assert parse_cell("")[0] == AvailabilityState.UNKNOWN
    assert parse_cell("   ")[0] == AvailabilityState.UNKNOWN


def test_split_day_collapses_to_the_longest_window():
    """One record holds one window, so a split day has to lose something.

    Losing the shorter half understates availability. Spanning 10:00-21:00 would
    invent three hours in the middle that the person never offered.
    """
    state, start, end = parse_cell("Available\n10 – 2 pm, 5 – 9 pm")
    assert state == AvailabilityState.AVAILABLE_WINDOW
    assert (start, end) == (time(10, 0), time(14, 0))


def test_ambiguous_bare_window_is_not_guessed():
    """With no meridiem anywhere, the day half is a coin flip; guessing it either
    invents an evening or destroys one."""
    assert parse_cell("Available\n4 – 8")[0] == AvailabilityState.UNKNOWN


def test_nonsense_times_do_not_become_windows():
    for cell in ("Available\n25 – 30 pm", "Available\n4:75 – 8:30 pm"):
        assert parse_cell(cell)[0] != AvailabilityState.AVAILABLE_WINDOW
