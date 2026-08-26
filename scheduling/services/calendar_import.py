"""Run the live calendar import in its own process.

Reading the show calendar drives a headless browser, and Playwright does that through
asyncio subprocesses. On Unix those need the main thread of the process: called from a
web request - which is always a worker thread - the child-process machinery has no
watcher attached and the interpreter dies outright, taking the whole application with
it rather than raising something catchable.

So the import runs as a separate process and reports back over stdout. That also keeps
a slow or wedged browser from occupying a request thread, and means a crash in the
browser can never take the application down.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

RESULT_MARKER = "SPIRIT_CALENDAR_RESULT:"
TIMEOUT_SECONDS = 600


class CalendarImportError(RuntimeError):
    """The import could not be completed. The message is safe to show a user."""


@dataclass(frozen=True)
class CalendarImportResult:
    received: int
    created: int
    updated: int
    unchanged: int
    status: str
    rendered: int
    extracted: int
    notes: str

    @property
    def is_partial(self) -> bool:
        return self.status == "PARTIAL"


def _command(start: date, end: date) -> list[str]:
    """The command that performs the import, frozen app or source checkout."""
    if getattr(sys, "frozen", False):
        # Re-invoke this same application binary in calendar-sync mode; there is no
        # separate Python interpreter to call inside a packaged .app.
        return [sys.executable, "--sync-calendar", start.isoformat(), end.isoformat()]

    manage_py = Path(__file__).resolve().parents[2] / "manage.py"
    return [
        sys.executable,
        str(manage_py),
        "sync_spirit_calendar",
        "--start",
        start.isoformat(),
        "--end",
        end.isoformat(),
        "--json",
    ]


def run_calendar_import(start: date, end: date) -> CalendarImportResult:
    try:
        completed = subprocess.run(
            _command(start, end),
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise CalendarImportError(
            "the calendar took too long to respond. Check the internet connection "
            "and try a shorter date range."
        ) from exc
    except OSError as exc:
        raise CalendarImportError(f"the import process could not be started ({exc}).") from exc

    payload = None
    for line in (completed.stdout or "").splitlines():
        if line.startswith(RESULT_MARKER):
            payload = json.loads(line[len(RESULT_MARKER) :])

    if payload is None:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit code {completed.returncode}"
        if "executable doesn't exist" in tail.lower() or "playwright install" in tail.lower():
            tail = (
                "the browser engine is still downloading. It fetches itself shortly "
                "after the application starts - wait a moment and try again."
            )
        raise CalendarImportError(tail)

    if payload.get("error"):
        raise CalendarImportError(payload["error"])

    return CalendarImportResult(
        received=payload.get("received", 0),
        created=payload.get("created", 0),
        updated=payload.get("updated", 0),
        unchanged=payload.get("unchanged", 0),
        status=payload.get("status", ""),
        rendered=payload.get("rendered", 0),
        extracted=payload.get("extracted", 0),
        notes=payload.get("notes", ""),
    )
