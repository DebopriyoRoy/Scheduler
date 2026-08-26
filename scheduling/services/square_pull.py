"""Pull everything Square knows, in one action.

Three separate reads used to be three separate chores: the show calendar, staff
availability, and the roster Square actually holds. All three drive a browser, and
Playwright drives browsers through asyncio subprocesses, which on Unix need the main
thread of a process. Called from a web request - always a worker thread - the
interpreter dies outright and takes the application with it. So each read runs as its
own process and reports back over stdout, exactly as the calendar import does.
"""

from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from scheduling.services.calendar_import import (
    CalendarImportError,
    run_calendar_import,
)

AVAILABILITY_MARKER = "SPIRIT_AVAILABILITY_RESULT:"
AVAILABILITY_TIMEOUT = 900


class SquarePullError(RuntimeError):
    """A read failed. The message is safe to show a user."""


@dataclass
class StepResult:
    name: str
    ok: bool
    detail: str
    extra: dict = field(default_factory=dict)


@dataclass
class PullReport:
    steps: list[StepResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps)

    def add(self, name: str, ok: bool, detail: str, **extra) -> None:
        self.steps.append(StepResult(name=name, ok=ok, detail=detail, extra=extra))


def _availability_command(start: dt.date, end: dt.date) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--sync-availability", start.isoformat(), end.isoformat()]
    manage_py = Path(__file__).resolve().parents[2] / "manage.py"
    return [
        sys.executable,
        str(manage_py),
        "sync_square_availability",
        "--start",
        start.isoformat(),
        "--end",
        end.isoformat(),
        "--live",
        "--all-dates",
        "--json",
    ]


def run_availability_sync(start: dt.date, end: dt.date) -> dict:
    """Read staff availability from the Square dashboard, in its own process."""
    try:
        completed = subprocess.run(
            _availability_command(start, end),
            capture_output=True,
            text=True,
            timeout=AVAILABILITY_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise SquarePullError(
            "Square took too long to respond while reading availability."
        ) from exc
    except OSError as exc:
        raise SquarePullError(f"the availability reader could not be started ({exc}).") from exc

    payload = None
    for line in (completed.stdout or "").splitlines():
        if line.startswith(AVAILABILITY_MARKER):
            payload = json.loads(line[len(AVAILABILITY_MARKER) :])

    if payload is None:
        detail = (completed.stderr or completed.stdout or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit code {completed.returncode}"
        raise SquarePullError(tail)
    if payload.get("error"):
        raise SquarePullError(payload["error"])
    return payload


def pull_everything(start: dt.date, end: dt.date) -> PullReport:
    """Calendar, then availability. Each step reports independently.

    One failing read never stops the others: a stale session blocks availability but
    says nothing about the public show calendar, and a partial refresh is more useful
    than none as long as it is clear which part succeeded.
    """
    report = PullReport()

    try:
        calendar = run_calendar_import(start, end)
        report.add(
            "Show calendar",
            True,
            f"{calendar.received} show(s) found - {calendar.created} added, "
            f"{calendar.updated} updated"
            + (f", {calendar.unchanged} unchanged" if calendar.unchanged else "")
            + ".",
            partial=calendar.is_partial,
        )
    except CalendarImportError as exc:
        report.add("Show calendar", False, str(exc))

    try:
        availability = run_availability_sync(start, end)
        live = availability.get("live")
        detail = (
            f"{availability['known']} of {availability['total']} employee/date "
            f"entries known ({availability['completeness']}%)."
        )
        if not live:
            detail += (
                " Read from the built-in fallback, not from Square - connect the "
                "Square dashboard to read real availability."
            )
        report.add(
            "Staff availability",
            True,
            detail,
            live=live,
            unmatched=availability.get("unmatched", []),
        )
    except SquarePullError as exc:
        report.add("Staff availability", False, str(exc))

    return report
