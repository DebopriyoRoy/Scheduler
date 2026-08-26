"""Playwright Rendered Browser Provider for Spirit Live Show Calendar."""

import asyncio
import datetime
import os
import re
from collections.abc import Sequence
from datetime import date, time
from pathlib import Path

from playwright.async_api import async_playwright

from scheduling.integrations.spirit_calendar.base import (
    BaseCalendarProvider,
    NormalizedEventOccurrence,
)
from scheduling.integrations.spirit_calendar.exceptions import SpiritCalendarBrowserError
from scheduling.integrations.spirit_calendar.normalizer import build_normalized_occurrence

CALENDAR_URL = "https://spiritofnewfoundland.com/show-calendar/"
EVENT_SELECTOR = ".fc-event, .fc-daygrid-event, .etn-event-item, a.fc-event"
HEADER_SELECTOR = ".fc-toolbar-title, .fc-header-title, .calendar-title"
NEXT_SELECTOR = ".fc-next-button, button.fc-next-button"
PREV_SELECTOR = ".fc-prev-button, button.fc-prev-button"

# A backstop, not a plan: navigation stops as soon as the requested months have been
# read. This only bounds the damage if the header ever becomes unreadable, so no date
# range can spin forever.
MAX_MONTH_STEPS = 24

# The calendar re-renders locally on a month click - no network round trip - so it
# settles in about a tenth of a second. This is the ceiling before giving up, not a
# wait: the code waits for the heading to actually change.
MONTH_RENDER_TIMEOUT_MS = 15000


class PlaywrightCalendarProvider(BaseCalendarProvider):
    """Navigates live rendered calendar with Playwright Chromium to extract occurrences."""

    def __init__(
        self,
        headless: bool = True,
        timeout_ms: int = 30000,
        capture_screenshots: bool = False,
    ):
        self.headless = headless
        self.timeout_ms = timeout_ms
        # Off by default. A full-page PNG per month cost more than reading every event
        # on the page, for output nobody looks at unless something has gone wrong.
        self.screenshot_dir = self._resolve_screenshot_dir() if capture_screenshots else None

    @staticmethod
    def _resolve_screenshot_dir() -> str | None:
        """A writable place for the per-month screenshots, or None to skip them.

        These are diagnostic only. The path used to be the relative string
        "artifacts/calendar_screenshots", created eagerly in __init__, so whenever the
        working directory was not writable - which is always true for an installed Mac
        app, whose bundle is read-only - constructing the provider raised
        "Read-only file system: 'artifacts'" and no import could run at all. A missing
        screenshot directory must never be able to stop a calendar import.
        """
        target = os.getenv("SPIRIT_ARTIFACTS_DIR")
        candidate = (
            Path(target) / "calendar_screenshots"
            if target
            else Path("artifacts") / "calendar_screenshots"
        )
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".writable"
            probe.touch()
            probe.unlink()
        except OSError:
            return None
        return str(candidate)

    @property
    def provider_name(self) -> str:
        return "PLAYWRIGHT"

    @staticmethod
    def _month_ordinal(year: int, month: int) -> int:
        """Months as a single comparable number, so ranges span year boundaries."""
        return year * 12 + month

    @classmethod
    def _parse_header_month(cls, text: str) -> int | None:
        """Read "October 2026" from the calendar heading as a month ordinal."""
        match = re.search(r"([A-Za-z]{3,9})\s+(20\d{2})", text or "")
        if not match:
            return None
        try:
            month = datetime.datetime.strptime(match.group(1)[:3], "%b").month
        except ValueError:
            return None
        return cls._month_ordinal(int(match.group(2)), month)

    async def _current_month(self, page) -> int | None:
        element = await page.query_selector(HEADER_SELECTOR)
        if element is None:
            return None
        return self._parse_header_month(await element.inner_text())

    async def _step_month(self, page, selector: str) -> bool:
        """Move one month and wait for the heading to actually change.

        This replaced a flat two-second sleep after every click. The wait is both far
        quicker - the calendar re-renders in about 0.1s - and more dependable, because
        a fixed sleep is simultaneously too long on a fast connection and too short on
        a slow one.
        """
        header = await page.query_selector(HEADER_SELECTOR)
        before = (await header.inner_text()).strip() if header else ""
        button = await page.query_selector(selector)
        if button is None:
            return False
        await button.click()
        try:
            await page.wait_for_function(
                "prev => { const e = document.querySelector("
                f"'{HEADER_SELECTOR}'"
                "); return e && e.innerText.trim() !== prev; }",
                arg=before,
                timeout=MONTH_RENDER_TIMEOUT_MS,
            )
        except Exception:
            return False
        return True

    def fetch_occurrences(
        self, start_date: date, end_date: date
    ) -> Sequence[NormalizedEventOccurrence]:
        """Synchronous wrapper around async Playwright execution."""
        return asyncio.run(self._async_fetch_occurrences(start_date, end_date))

    async def _async_fetch_occurrences(
        self, start_date: date, end_date: date
    ) -> list[NormalizedEventOccurrence]:
        if end_date < start_date:
            raise ValueError("End date must not precede start date.")

        calendar_url = CALENDAR_URL
        occurrences_map: dict[str, NormalizedEventOccurrence] = {}

        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=self.headless)
                context = await browser.new_context(
                    viewport={"width": 1600, "height": 1200},
                    user_agent=(
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
                    ),
                )
                page = await context.new_page()

                # Wait for the calendar itself rather than for the network to fall
                # quiet. "networkidle" waits out analytics, fonts and trackers that
                # have nothing to do with the events, and cost several seconds.
                await page.goto(
                    calendar_url, wait_until="domcontentloaded", timeout=self.timeout_ms
                )
                await page.wait_for_selector(EVENT_SELECTOR, timeout=self.timeout_ms)

                first_month = self._month_ordinal(start_date.year, start_date.month)
                last_month = self._month_ordinal(end_date.year, end_date.month)
                steps = 0

                # The calendar always opens on the current month, and no URL parameter
                # moves it, so walk to the range. Previously this clicked forward a
                # fixed twelve times whatever was asked for - importing one month read
                # eleven irrelevant ones.
                while steps < MAX_MONTH_STEPS:
                    current = await self._current_month(page)
                    if current is None or current == first_month:
                        break
                    direction = NEXT_SELECTOR if current < first_month else PREV_SELECTOR
                    if not await self._step_month(page, direction):
                        break
                    steps += 1

                while steps < MAX_MONTH_STEPS:
                    header_el = await page.query_selector(HEADER_SELECTOR)
                    header_text = await header_el.inner_text() if header_el else ""

                    # Diagnostic only, and off unless explicitly requested.
                    if self.screenshot_dir:
                        clean_header = re.sub(
                            r"[^a-zA-Z0-9]+", "_", header_text.lower()
                        ).strip("_")
                        screenshot_path = os.path.join(
                            self.screenshot_dir, f"{clean_header or f'month_{steps}'}.png"
                        )
                        try:
                            await page.screenshot(path=screenshot_path, full_page=True)
                        except OSError:
                            self.screenshot_dir = None

                    # Extract all event elements in current view
                    event_elements = await page.query_selector_all(EVENT_SELECTOR)

                    for el in event_elements:
                        raw_text = await el.inner_text()
                        href = await el.get_attribute("href") or calendar_url
                        
                        # Parse date from text (format: "to September 30, 2026...")
                        date_match = re.search(r"to\s+([A-Za-z]+\s+\d{1,2},\s+20\d{2})", raw_text)
                        if not date_match:
                            continue
                        
                        try:
                            parsed_date = datetime.datetime.strptime(
                                date_match.group(1), "%B %d, %Y"
                            ).date()
                        except ValueError:
                            continue

                        # Filter occurrences strictly within requested date range
                        if not (start_date <= parsed_date <= end_date):
                            continue

                        # Parse times
                        time_match = re.search(
                            r"(\d{1,2}:\d{2}\s*[ap]m)\s*-\s*(\d{1,2}:\d{2}\s*[ap]m)",
                            raw_text,
                            re.IGNORECASE,
                        )
                        if time_match:
                            start_t = datetime.datetime.strptime(
                                time_match.group(1).upper(), "%I:%M %p"
                            ).time()
                            end_t = datetime.datetime.strptime(
                                time_match.group(2).upper(), "%I:%M %p"
                            ).time()
                        else:
                            start_t, end_t = time(18, 30), time(22, 30)

                        # Extract title and venue
                        parts = [p.strip() for p in raw_text.split("\n") if p.strip()]
                        raw_title = parts[0] if parts else "Spirit Show"
                        
                        venue = "Theatre Gower"
                        for part in parts:
                            if "gower" in part.lower() or "st. john" in part.lower():
                                venue = part
                                break

                        occurrence = build_normalized_occurrence(
                            title=raw_title,
                            event_date=parsed_date,
                            start_time=start_t,
                            end_time=end_t,
                            venue=venue,
                            event_url=href,
                            source_provider=self.provider_name,
                            source_url=calendar_url,
                        )
                        
                        occurrences_map[occurrence.external_occurrence_id] = occurrence

                    # Stop as soon as the requested months have been read. This check
                    # was described in a comment here but never actually written, so
                    # every import walked the full twelve months regardless.
                    current = await self._current_month(page)
                    if current is None or current >= last_month:
                        break
                    if not await self._step_month(page, NEXT_SELECTOR):
                        break
                    steps += 1

                await browser.close()
        except Exception as exc:
            raise SpiritCalendarBrowserError(f"Playwright browser provider failed: {exc}") from exc

        return sorted(
            occurrences_map.values(),
            key=lambda item: (item.date, item.start_time, item.full_title),
        )
