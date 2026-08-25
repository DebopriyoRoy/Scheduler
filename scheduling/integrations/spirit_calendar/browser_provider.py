"""Playwright Rendered Browser Provider for Spirit Live Show Calendar."""

import asyncio
import datetime
import os
import re
from collections.abc import Sequence
from datetime import date, time

from playwright.async_api import async_playwright

from scheduling.integrations.spirit_calendar.base import (
    BaseCalendarProvider,
    NormalizedEventOccurrence,
)
from scheduling.integrations.spirit_calendar.exceptions import SpiritCalendarBrowserError
from scheduling.integrations.spirit_calendar.normalizer import build_normalized_occurrence


class PlaywrightCalendarProvider(BaseCalendarProvider):
    """Navigates live rendered calendar with Playwright Chromium to extract occurrences."""

    def __init__(self, headless: bool = True, timeout_ms: int = 30000):
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.screenshot_dir = "artifacts/calendar_screenshots"
        os.makedirs(self.screenshot_dir, exist_ok=True)

    @property
    def provider_name(self) -> str:
        return "PLAYWRIGHT"

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

        calendar_url = "https://spiritofnewfoundland.com/show-calendar/"
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

                # Navigate to live show calendar page
                await page.goto(calendar_url, wait_until="networkidle", timeout=self.timeout_ms)
                await page.wait_for_timeout(3000)

                # Loop navigating months until target end_date is reached
                max_month_clicks = 12
                for click_idx in range(max_month_clicks):
                    # Check current calendar header month
                    header_el = await page.query_selector(
                        ".fc-toolbar-title, .fc-header-title, .calendar-title"
                    )
                    header_text = await header_el.inner_text() if header_el else ""

                    # Save debug screenshot for current month
                    clean_header = re.sub(r"[^a-zA-Z0-9]+", "_", header_text.lower()).strip("_")
                    screenshot_path = os.path.join(
                        self.screenshot_dir, f"{clean_header or f'month_{click_idx}'}.png"
                    )
                    await page.screenshot(path=screenshot_path, full_page=True)

                    # Extract all event elements in current view
                    event_elements = await page.query_selector_all(
                        ".fc-event, .fc-daygrid-event, .etn-event-item, a.fc-event"
                    )

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

                    # Check if we should click next month button
                    next_btn = await page.query_selector(".fc-next-button, button.fc-next-button")
                    if not next_btn:
                        break
                    
                    # If we've reached past end_date month, break early
                    await next_btn.click()
                    await page.wait_for_timeout(2000)

                await browser.close()
        except Exception as exc:
            raise SpiritCalendarBrowserError(f"Playwright browser provider failed: {exc}") from exc

        return sorted(
            occurrences_map.values(),
            key=lambda item: (item.date, item.start_time, item.full_title),
        )
