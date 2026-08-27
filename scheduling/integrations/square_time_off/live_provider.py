"""Reading the Time off page out of the Square dashboard."""

from __future__ import annotations

from scheduling.integrations.square_session import (
    SquareSessionError,
    logged_in_context,
    session_status,
)

TIME_OFF_URL = "https://app.squareup.com/dashboard/shifts/schedule/time-off"

# The page renders rows as divs, not a table, and the class carries a build hash
# (_tableRow_af934a7) that changes whenever Square redeploys. Matching on the stable
# part of the name survives that; an exact class would break on their next release.
ROW_SELECTOR = 'div[class*="tableRow"]'

# Rows arrive through JavaScript well after domcontentloaded.
PAGE_SETTLE_MS = 9000


def fetch_time_off_rows(headless: bool = True) -> list[list[str]]:
    """Every row on the Time off page, as its lines of text."""
    from playwright.sync_api import sync_playwright

    if not session_status().connected:
        raise SquareSessionError(
            "no Square dashboard session is stored. Connect to Square first."
        )

    with sync_playwright() as playwright:
        context = logged_in_context(playwright, headless=headless)
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(TIME_OFF_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(PAGE_SETTLE_MS)
            if "login" in page.url or "signin" in page.url:
                raise SquareSessionError(
                    "Square asked for a sign-in, so the stored session has expired. "
                    "Connect to Square again."
                )
            rows = page.eval_on_selector_all(
                ROW_SELECTOR,
                "els => els.map(e => (e.innerText || '').split('\\n')"
                ".map(s => s.trim()).filter(Boolean))",
            )
        finally:
            context.close()
    return rows or []
