"""Which staffing rules a show gets - step 3 of building a roster.

The engine has always treated shows differently by kind: a workshop gets no floor
staff, an offsite event waits for a manager, a show under 75 guests does not run,
Dwight's Wedding keeps its own call times. Those decisions were made inline as the
engine walked the calendar, so nothing could tell a person *which* treatment a show
would get before a roster existed. This names them, in one place, in the order the
engine applies them. The engine asks this module; the roster screens show its answer.
"""

from __future__ import annotations

from dataclasses import dataclass

from scheduling.models import (
    GUESTS_PER_BARTENDER,
    GUESTS_PER_BUSSER,
    GUESTS_PER_SERVER,
    MINIMUM_VIABLE_GUESTS,
    Show,
)


class ProfileKey:
    WORKSHOP = "workshop"
    OFFSITE = "offsite"
    BELOW_MINIMUM = "below_minimum"
    PRIVATE = "private"
    DWIGHTS = "dwights"
    REGULAR_EVENING = "regular_evening"
    OWN_TIMES = "own_times"


RATIO_LADDER = (
    f"One server per {GUESTS_PER_SERVER} guests, one bartender per {GUESTS_PER_BARTENDER}, "
    f"one busser per {GUESTS_PER_BUSSER}, each stretching five guests before another is "
    "added; one Server Manager and one 50/50 on every show."
)


@dataclass(frozen=True)
class ShowProfile:
    key: str
    label: str
    staffed: bool
    explanation: str
    timetable: str = ""

    @property
    def needs_manager(self) -> bool:
        """Shows the engine leaves for a person to staff."""
        return not self.staffed


def _title(show: Show) -> str:
    return (show.title or "").casefold()


def is_workshop(show: Show) -> bool:
    return "workshop" in _title(show)


def is_offsite(show: Show) -> bool:
    return "offsite" in _title(show) or "offsite" in (show.venue or "").casefold()


def is_private(show: Show) -> bool:
    return "private" in _title(show) and not is_offsite(show)


def profile_for(show: Show) -> ShowProfile:
    """The rule set this show is staffed by, checked in the engine's own order."""
    # Late import: the engine imports this module.
    from scheduling.services.engine import is_dwights_wedding, is_standard_evening_show

    guests = show.planning_guest_count

    if is_workshop(show):
        return ShowProfile(
            ProfileKey.WORKSHOP,
            "Workshop — not staffed",
            False,
            "A workshop or class, not a dinner-theatre show, so it gets no floor or bar "
            "staff.",
        )
    if is_offsite(show):
        return ShowProfile(
            ProfileKey.OFFSITE,
            "Offsite event — staffed by a manager",
            False,
            "Offsite events are staffed by hand; the engine flags them for review.",
        )
    if guests < MINIMUM_VIABLE_GUESTS:
        return ShowProfile(
            ProfileKey.BELOW_MINIMUM,
            f"Under {MINIMUM_VIABLE_GUESTS} guests — no roster",
            False,
            f"{guests} guests is under the {MINIMUM_VIABLE_GUESTS}-guest minimum a show "
            "runs at. Management decides whether it is cancelled or the guests move.",
        )

    if is_dwights_wedding(show):
        timetable = "Dwight’s Wedding call times (doors 6:00 pm)"
    elif is_standard_evening_show(show):
        timetable = "Regular house call times (doors 6:30 pm)"
    else:
        timetable = (
            f"Times worked out from this show’s own start ({show.start_time:%H:%M}) and "
            f"finish ({show.end_time:%H:%M})"
        )

    if is_private(show):
        return ShowProfile(
            ProfileKey.PRIVATE,
            "Private event — standard theatre staffing",
            True,
            RATIO_LADDER,
            timetable,
        )
    if is_dwights_wedding(show):
        return ShowProfile(
            ProfileKey.DWIGHTS, "Dwight’s Wedding", True, RATIO_LADDER, timetable
        )
    if is_standard_evening_show(show):
        return ShowProfile(
            ProfileKey.REGULAR_EVENING, "Regular evening show", True, RATIO_LADDER, timetable
        )
    return ShowProfile(
        ProfileKey.OWN_TIMES, "Show on its own timetable", True, RATIO_LADDER, timetable
    )
