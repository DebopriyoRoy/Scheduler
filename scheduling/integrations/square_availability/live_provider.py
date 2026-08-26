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
from datetime import date, datetime, time, timedelta

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
    """One row per person: weekday index -> cell text as Square rendered it."""

    rows: dict[str, dict[int, str]]

    def names(self) -> list[str]:
        return sorted(self.rows)


def parse_cell(cell: str) -> tuple[AvailabilityState, time | None, time | None]:
    """Turn one grid cell into a state and, where given, a window.

    An empty cell means Square holds nothing for that weekday. That is UNKNOWN, not
    "unavailable": the two are different, and treating a gap in the record as a refusal
    would quietly make people unschedulable.
    """
    text = (cell or "").strip()
    if not text:
        return AvailabilityState.UNKNOWN, None, None

    lowered = text.lower()
    if "unavailable" in lowered or "not available" in lowered:
        return AvailabilityState.UNAVAILABLE, None, None

    # "All day" is checked before the window pattern: the words carry no digits, but
    # a cell can hold both, and the explicit statement should win.
    if "all day" in lowered:
        return AvailabilityState.AVAILABLE_ALL_DAY, None, None

    windows = [w for w in (_resolve_window(m) for m in WINDOW.finditer(text)) if w]
    if windows:
        # A split day ("10 – 2 pm, 5 – 9 pm") has to collapse to one window, because
        # one record holds one. Take the longest: it is the most useful of the two and
        # never claims a minute Square did not give, whereas spanning them end to end
        # would invent availability across the gap.
        start, end = max(windows, key=lambda w: (datetime.combine(date.min, w[1])
                                                 - datetime.combine(date.min, w[0])))
        return AvailabilityState.AVAILABLE_WINDOW, start, end

    if "available" in lowered:
        # Marked available, but Square gave no window we could read. Say so rather
        # than inventing one.
        return AvailabilityState.UNKNOWN, None, None
    return AvailabilityState.UNKNOWN, None, None


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

    header_index: dict[int, int] = {}
    rows: dict[str, dict[int, str]] = {}
    for cells in raw or []:
        if not cells or not any(cells):
            continue
        lowered = [c.lower() for c in cells]
        if not header_index and any(h in lowered for h in WEEKDAY_HEADERS):
            for position, value in enumerate(lowered):
                if value in WEEKDAY_HEADERS:
                    header_index[position] = WEEKDAY_HEADERS.index(value)
            continue
        if not header_index:
            continue
        name = cells[0].strip()
        if not name or name.lower() in WEEKDAY_HEADERS:
            continue
        # A wrapped row can leave a cell of availability text where the name belongs.
        # Treating that as a person produces a phantom nobody can match.
        if WINDOW.search(name) or name.lower().startswith(("available", "unavailable")):
            continue
        rows[name] = {
            weekday: (cells[position] if position < len(cells) else "")
            for position, weekday in header_index.items()
        }

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
                state, start_time, end_time = parse_cell(week.get(current.weekday(), ""))
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
