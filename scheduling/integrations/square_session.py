"""A reusable logged-in Square dashboard session.

Square publishes no API for team-member availability - the REST v2 provider says so
outright - so availability can only be read the way a person reads it, from the
dashboard. That needs a login, and a login is the one thing this application must never
hold: the sign-in happens in a real browser window, against Square's own page, and only
the resulting session is kept.

Nothing here ever sees or stores a password. Playwright persists the session Square
issues afterwards, in the application's own data folder, and later syncs reuse it
headlessly until Square expires it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

DASHBOARD_HOME = "https://app.squareup.com/dashboard/"
LOGIN_URL = "https://app.squareup.com/login"
# Where Square shows staff availability. Confirmed during the interactive connect, and
# stored, because Square moves these paths between dashboard revisions.
DEFAULT_AVAILABILITY_URL = "https://app.squareup.com/dashboard/team/availability"
SESSION_MARKER = "session.json"


class SquareSessionError(RuntimeError):
    """No usable session. The message is safe to show a user."""


def session_dir() -> Path:
    """Where the browser profile lives - beside the database, not inside the app."""
    configured = os.getenv("SPIRIT_SQUARE_SESSION_DIR")
    if configured:
        return Path(configured)
    return Path.home() / "Library" / "Application Support" / "Spirit Scheduler" / "square-session"


@dataclass(frozen=True)
class SessionStatus:
    connected: bool
    detail: str
    availability_url: str = DEFAULT_AVAILABILITY_URL


def _marker_path() -> Path:
    return session_dir() / SESSION_MARKER


def session_status() -> SessionStatus:
    """Whether a session has been captured, without opening a browser.

    Deliberately cheap and offline: this is read on page loads, and Square's own
    expiry is discovered when a sync actually runs rather than guessed at here.
    """
    marker = _marker_path()
    if not marker.exists():
        return SessionStatus(False, "Not connected. Sign in to Square to enable syncing.")
    try:
        saved = json.loads(marker.read_text())
    except (OSError, ValueError):
        return SessionStatus(False, "The saved session could not be read. Sign in again.")
    return SessionStatus(
        True,
        f"Connected as {saved.get('account', 'your Square account')} "
        f"on {saved.get('connected_at', 'an earlier date')}.",
        saved.get("availability_url", DEFAULT_AVAILABILITY_URL),
    )


def record_session(account: str, availability_url: str) -> None:
    directory = session_dir()
    directory.mkdir(parents=True, exist_ok=True)
    from django.utils import timezone

    _marker_path().write_text(
        json.dumps(
            {
                "account": account,
                "availability_url": availability_url,
                "connected_at": timezone.localtime().strftime("%d %b %Y"),
            }
        )
    )
    _marker_path().chmod(0o600)
    try:
        directory.chmod(0o700)  # the profile carries Square's session cookies
    except OSError:
        pass


def forget_session() -> None:
    """Remove the stored session. Signing out of Square is a separate act, there."""
    import shutil

    shutil.rmtree(session_dir(), ignore_errors=True)


def open_dashboard_for_login(timeout_seconds: int = 600) -> SessionStatus:
    """Open a real browser at Square's login and wait for the user to sign in.

    Runs headed on purpose: the person signs in to Square directly, including any
    two-factor step, and this process only observes when the dashboard has loaded.
    """
    from playwright.sync_api import sync_playwright

    directory = session_dir()
    directory.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(directory / "profile"),
            headless=False,
            viewport={"width": 1440, "height": 900},
        )
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(LOGIN_URL, wait_until="domcontentloaded")
        try:
            page.wait_for_url("**/dashboard/**", timeout=timeout_seconds * 1000)
        except Exception:
            context.close()
            raise SquareSessionError(
                "sign-in was not completed. The window closed or timed out before the "
                "Square dashboard loaded."
            ) from None

        account = ""
        try:
            account = page.title().split("|")[-1].strip()
        except Exception:  # noqa: BLE001 - cosmetic only
            account = ""
        context.close()

    record_session(account or "Square", DEFAULT_AVAILABILITY_URL)
    return session_status()


def logged_in_context(playwright, headless: bool = True):
    """A browser context carrying the stored session, for a headless sync.

    Raises rather than falling back to a signed-out browser: silently returning an
    empty availability set would look exactly like every member of staff being
    unavailable, and the engine would quietly schedule nobody.
    """
    directory = session_dir()
    if not _marker_path().exists():
        raise SquareSessionError(
            "no Square dashboard session is stored. Use Connect to Square first."
        )
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(directory / "profile"),
        headless=headless,
        viewport={"width": 1440, "height": 900},
    )
