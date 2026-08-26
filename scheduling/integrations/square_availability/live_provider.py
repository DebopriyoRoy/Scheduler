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

# "5:30 pm – 11:59 pm", with an en dash, occasionally a hyphen.
WINDOW = re.compile(
    r"(\d{1,2}:\d{2}\s*[ap]\.?m\.?)\s*[–\-—]\s*(\d{1,2}:\d{2}\s*[ap]\.?m\.?)", re.I
)


@dataclass(frozen=True)
class WeeklyGrid:
    """One row per person: weekday index -> cell text as Square rendered it."""

    rows: dict[str, dict[int, str]]

    def names(self) -> list[str]:
        return sorted(self.rows)


def _parse_clock(text: str) -> time | None:
    cleaned = text.strip().replace(".", "").upper().replace(" ", "")
    for fmt in ("%I:%M%p", "%H:%M"):
        try:
            return datetime.strptime(cleaned, fmt).time()
        except ValueError:
            continue
    return None


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

    match = WINDOW.search(text)
    if match:
        start, end = _parse_clock(match.group(1)), _parse_clock(match.group(2))
        if start and end:
            return AvailabilityState.AVAILABLE_WINDOW, start, end

    if "all day" in lowered:
        return AvailabilityState.AVAILABLE_ALL_DAY, None, None
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
