"""Read staff availability from the Square dashboard, for real.

Square publishes no availability API, and the provider that came before this one made
no network call at all: it generated records from a dict of windows transcribed by hand
into the source file. Every eligibility decision rested on that transcription. It is
how Kate's Thursday came to read 05:30 instead of 17:30, and why six staff looked
permanently unschedulable while management were scheduling them regularly - Square had
their hours all along.

This reads the availability grid the dashboard renders at
/dashboard/shifts/schedule/availability, using the stored session. One row per person,
one column per weekday, each cell either "Available" with a window, "Available All day",
or empty.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, time, timedelta

from scheduling.integrations.square_availability.base import (
    AvailabilityState,
    BaseAvailabilityProvider,
    NormalizedAvailabilityRecord,
)
from scheduling.integrations.square_availability.normalizer import build_normalized_record
from scheduling.integrations.square_session import (
    SquareSessionError,
    logged_in_context,
    session_status,
)
from scheduling.models import Employee, SquareEmployeeMapping

AVAILABILITY_URL = "https://app.squareup.com/dashboard/shifts/schedule/availability"
WEEKDAY_HEADERS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
PAGE_SETTLE_MS = 9000

# Square renders a window as two clock times joined by an en dash, and it drops
# whatever it considers redundant: the minutes when they are zero, and the meridiem on
# the start time when it matches the end. Every cell on the real dashboard looks like
# one of "5:30 – 11:59 pm", "1 – 10 pm", "4 – 8:30 pm", "7 – 11 pm", "11 am – 4 pm".
#
# The pattern that came before this one required ":MM" *and* a meridiem on both sides,
# so it matched none of those - not one real cell - and every windowed day was reported
# as UNKNOWN. Because UNKNOWN is indistinguishable from "Square holds nothing", the
# sync looked like it was working and the hours it appeared to return were really the
# hand-typed fallback dict, complete with its 05:30 transcription of Kate's Thursday.
CLOCK = r"(\d{1,2})(?::(\d{2}))?\s*(?:([ap])\.?\s*m\.?)?"
WINDOW = re.compile(CLOCK + r"\s*[–—-]\s*" + CLOCK, re.I)


def _to_24h(hour: int, minute: int, meridiem: str | None) -> time | None:
    """One clock reading to a time, or None if it is not a real one."""
    if not 0 <= minute <= 59:
        return None
    if meridiem is None:
        return time(hour, minute) if 0 <= hour <= 23 else None
    if not 1 <= hour <= 12:
        return None
    if meridiem.lower() == "a":
        return time(0 if hour == 12 else hour, minute)
    return time(12 if hour == 12 else hour + 12, minute)


def _resolve_window(match: re.Match) -> tuple[time, time] | None:
    """Both ends of one window, filling in whichever meridiem Square left out.

    An omitted meridiem is only ever omitted because it repeats the other end's, so
    borrowing is safe - and where borrowing would put the end before the start
    ("10 – 4 pm"), the start must be the other half of the day.
    """
    start_hour, start_min, start_mer, end_hour, end_min, end_mer = match.groups()
    start_hour, end_hour = int(start_hour), int(end_hour)
    start_min, end_min = int(start_min or 0), int(end_min or 0)

    if start_mer is None and end_mer is None:
        # Nothing to borrow. Only read it as a 24-hour clock if it cannot be a 12-hour
        # one; otherwise the day half is a genuine coin flip and guessing it wrong
        # either invents availability or destroys it.
        if start_hour <= 12 and end_hour <= 12:
            return None
        start, end = _to_24h(start_hour, start_min, None), _to_24h(end_hour, end_min, None)
    elif start_mer is None:
        start = _to_24h(start_hour, start_min, end_mer)
        end = _to_24h(end_hour, end_min, end_mer)
        if start and end and start >= end:
            start = _to_24h(start_hour, start_min, "a" if end_mer.lower() == "p" else "p")
    elif end_mer is None:
        start = _to_24h(start_hour, start_min, start_mer)
        end = _to_24h(end_hour, end_min, start_mer)
        if start and end and end <= start:
            end = _to_24h(end_hour, end_min, "p" if start_mer.lower() == "a" else "a")
    else:
        start = _to_24h(start_hour, start_min, start_mer)
        end = _to_24h(end_hour, end_min, end_mer)

    if start is None or end is None or end <= start:
        return None
    return start, end


@dataclass(frozen=True)
class WeeklyGrid:
    """Per person, per weekday, every availability cell Square rendered.

    A list rather than a single string because one person can hold several windows on
    the same weekday - Khrystyna works 11:00-16:00 and again 18:00-23:00 - and Square
    shows each as its own row.
    """

    rows: dict[str, dict[int, list[str]]]

    def names(self) -> list[str]:
        return sorted(self.rows)


def parse_cells(
    texts: Sequence[str],
) -> list[tuple[AvailabilityState, time | None, time | None]]:
    """Everything Square holds for one person on one weekday.

    Returns one entry per window, because a day with two windows is two facts and
    collapsing them loses one. An empty list of texts means Square holds nothing,
    which is UNKNOWN rather than "unavailable": both end unschedulable, but only one
    of them is worth asking a person about.
    """
    all_day = False
    unavailable = False
    windows: list[tuple[time, time]] = []

    for text in texts:
        cleaned = (text or "").strip()
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if "unavailable" in lowered or "not available" in lowered:
            unavailable = True
            continue
        if "all day" in lowered:
            all_day = True
            continue
        for match in WINDOW.finditer(cleaned):
            resolved = _resolve_window(match)
            if resolved:
                windows.append(resolved)

    # "All day" subsumes any window stated alongside it.
    if all_day:
        return [(AvailabilityState.AVAILABLE_ALL_DAY, None, None)]
    if windows:
        return [
            (AvailabilityState.AVAILABLE_WINDOW, start, end)
            for start, end in sorted(set(windows))
        ]
    if unavailable:
        return [(AvailabilityState.UNAVAILABLE, None, None)]
    return [(AvailabilityState.UNKNOWN, None, None)]


def parse_cell(cell: str) -> tuple[AvailabilityState, time | None, time | None]:
    """The first thing Square holds in one cell.

    A single-value view of parse_cells, kept for callers that can only carry one
    window. Anything that stores availability should use parse_cells instead, or it
    silently discards a person's second shift window.
    """
    return parse_cells([cell])[0]


def fetch_weekly_grid(headless: bool = True) -> WeeklyGrid:
    """Read the availability grid from the dashboard. Requires a stored session."""
    from playwright.sync_api import sync_playwright

    if not session_status().connected:
        raise SquareSessionError(
            "no Square dashboard session is stored. Connect to Square first."
        )

    with sync_playwright() as playwright:
        context = logged_in_context(playwright, headless=headless)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(AVAILABILITY_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(PAGE_SETTLE_MS)
            if "login" in page.url or "signin" in page.url:
                raise SquareSessionError(
                    "Square asked for a sign-in, so the stored session has expired. "
                    "Connect to Square again."
                )
            raw = page.eval_on_selector_all(
                "table tr, [role=row]",
                "els => els.map(e => Array.from("
                "e.querySelectorAll('td,th,[role=cell],[role=columnheader]')"
                ").map(c => c.innerText.trim()))",
            )
        finally:
            context.close()

    return build_grid(raw or [])


def _looks_like_a_name(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    if lowered in WEEKDAY_HEADERS or lowered in ("team member", "name"):
        return False
    if lowered.startswith(("available", "unavailable", "not available")):
        return False
    # A wrapped row can leave availability text where the name belongs. Treating that
    # as a person produces a phantom nobody can match.
    return not WINDOW.search(text)


def build_grid(raw: Sequence[Sequence[str]]) -> WeeklyGrid:
    """Turn the dashboard's rows into per-person, per-weekday cells.

    Square gives a person's first window a row carrying their name, and every further
    window a row with **no name cell at all** - not an empty one, absent. So a
    continuation row is one cell shorter and every weekday sits one position to the
    left. Reading both row shapes at fixed positions puts Yana's Friday evening on a
    Monday; the offset has to be derived from the row's own width.

    Skipping the nameless rows outright, as this did before, silently dropped a second
    window for everyone who has one.
    """
    header_index: dict[int, int] = {}
    rows: dict[str, dict[int, list[str]]] = {}
    current_name: str | None = None

    for cells in raw:
        if not cells or not any(cells):
            continue
        lowered = [c.lower() for c in cells]
        if any(header in lowered for header in WEEKDAY_HEADERS):
            # The header repeats as the grid scrolls. Capture the column order from
            # the first, then skip every occurrence.
            if not header_index:
                for position, value in enumerate(lowered):
                    if value in WEEKDAY_HEADERS:
                        header_index[position] = WEEKDAY_HEADERS.index(value)
            continue
        if not header_index:
            continue

        # Named rows carry one cell more than there are weekdays; continuation rows
        # carry exactly as many. Anything else is a layout this cannot read safely.
        offset = len(cells) - len(header_index)
        if offset not in (0, 1):
            continue

        if offset == 1:
            name = cells[0].strip()
            if not _looks_like_a_name(name):
                continue
            current_name = name
            rows.setdefault(current_name, {})
        elif current_name is None:
            continue

        for weekday in range(len(header_index)):
            position = offset + weekday
            text = cells[position].strip() if position < len(cells) else ""
            if text:
                rows[current_name].setdefault(weekday, []).append(text)

    if not rows:
        raise SquareSessionError(
            "the availability page did not render a grid. Square may have changed it, "
            "or the session may have expired."
        )
    return WeeklyGrid(rows=rows)


def match_employees(grid: WeeklyGrid) -> dict[str, Employee]:
    """Match Square's names to staff. Square shows full names; we often hold short ones."""
    staff = list(Employee.objects.filter(active=True))
    mapped = {
        m.employee_id: m
        for m in SquareEmployeeMapping.objects.filter(environment="production")
    }

    def normalise(value: str) -> str:
        return re.sub(r"\s+", " ", value.strip().lower())

    by_display = {normalise(e.display_name): e for e in staff}
    by_full = {normalise(f"{e.first_name} {e.last_name}"): e for e in staff if e.last_name}
    by_square = {}
    for employee in staff:
        mapping = mapped.get(employee.id)
        if mapping and (mapping.square_given_name or mapping.square_family_name):
            key = normalise(f"{mapping.square_given_name} {mapping.square_family_name}")
            by_square[key] = employee
    by_first = {}
    for employee in staff:
        by_first.setdefault(normalise(employee.first_name), employee)

    resolved: dict[str, Employee] = {}
    for name in grid.names():
        key = normalise(name)
        found = by_square.get(key) or by_display.get(key) or by_full.get(key)
        if found is None:
            found = by_first.get(key.split(" ")[0])
        if found is not None:
            resolved[name] = found
    return resolved


class LiveSquareAvailabilityProvider(BaseAvailabilityProvider):
    """Availability as Square actually holds it."""

    def __init__(self, headless: bool = True):
        self.headless = headless
        self.unmatched_names: list[str] = []

    @property
    def provider_name(self) -> str:
        return "LIVE_SQUARE_DASHBOARD"

    @property
    def is_live(self) -> bool:
        return True

    def fetch_availability(
        self, start_date: date, end_date: date, team_member_ids: Sequence[str] | None = None
    ) -> list[NormalizedAvailabilityRecord]:
        grid = fetch_weekly_grid(headless=self.headless)
        employees = match_employees(grid)
        self.unmatched_names = [n for n in grid.names() if n not in employees]

        mappings = {
            m.employee_id: m.square_team_member_id
            for m in SquareEmployeeMapping.objects.filter(environment="production")
        }

        records: list[NormalizedAvailabilityRecord] = []
        for square_name, employee in employees.items():
            week = grid.rows[square_name]
            current = start_date
            while current <= end_date:
                # One record per window. A person with both a lunch and an evening
                # window gets two, and the eligibility check passes if either covers
                # the shift.
                for state, start_time, end_time in parse_cells(week.get(current.weekday(), [])):
                    records.append(
                        build_normalized_record(
                            employee_id=employee.id,
                            employee_name=employee.display_name,
                            square_team_member_id=mappings.get(employee.id, ""),
                            record_date=current,
                            state=state,
                            start_time=start_time,
                            end_time=end_time,
                            source_provider=self.provider_name,
                            source_environment="PRODUCTION",
                        )
                    )
                current += timedelta(days=1)
        return records
