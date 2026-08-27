"""Turning a scraped Time off row into dates, a status and a reason.

Square renders the page as divs, so a row arrives as the list of text lines it
contained, e.g.:

    ["KG", "Kate Griffin", "Aug 27-Sep 2, 2026", "All day", "7d",
     "Out of town", "Approved", "7d", "0.00h"]

Fields are located by what they look like rather than by index. A row with no
reason has one fewer line, and reading positionally would then take the status as
the reason and shift every column after it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Square writes "Approved", "Declined" and "Requested"; the model calls the last PENDING.
STATUS_WORDS = {
    "approved": "APPROVED",
    "declined": "DECLINED",
    "requested": "PENDING",
    "pending": "PENDING",
    "cancelled": "CANCELLED",
    "canceled": "CANCELLED",
}

_DASH = r"[-‐-―]"
# "Aug 27-Sep 2, 2026" | "Sep 3-4, 2026" | "Sep 1, 2026"
_RANGE = re.compile(
    rf"^([A-Za-z]{{3,}})\s+(\d{{1,2}})\s*(?:{_DASH}\s*(?:([A-Za-z]{{3,}})\s+)?(\d{{1,2}}))?,\s*(\d{{4}})$"
)
_DURATION = re.compile(r"^\d+(\.\d+)?\s*[dh]$", re.IGNORECASE)
_TIME_WINDOW = re.compile(
    r"\d{1,2}:\d{2}\s*(?:[ap]m)?\s*" + _DASH + r"\s*\d{1,2}:\d{2}\s*(?:[ap]m)?",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class TimeOffRow:
    employee_name: str
    start_date: date
    end_date: date
    status: str
    reason: str
    requested_time: str
    approved_all_day: str
    approved_partial: str
    all_day: bool


def parse_date_range(text: str) -> tuple[date, date] | None:
    """Square prints the year once, at the end, and the month only when it changes."""
    match = _RANGE.match(text.strip())
    if not match:
        return None
    start_month_name, start_day, end_month_name, end_day, year = match.groups()
    start_month = MONTHS.get(start_month_name[:3].lower())
    if start_month is None:
        return None
    year = int(year)
    if end_day is None:
        start = date(year, start_month, int(start_day))
        return start, start

    end_month = MONTHS.get((end_month_name or start_month_name)[:3].lower())
    if end_month is None:
        return None
    end = date(year, end_month, int(end_day))
    # The printed year belongs to the end of the range, so a range that crosses New
    # Year starts in the previous one - "Dec 30-Jan 2, 2027" begins in 2026.
    start_year = year - 1 if start_month > end_month else year
    return date(start_year, start_month, int(start_day)), end


def parse_row(lines: list[str]) -> TimeOffRow | None:
    """One scraped row, or None when it is not a time-off row at all."""
    cleaned = [line.strip() for line in lines if line and line.strip()]
    if len(cleaned) < 4:
        return None

    date_index = next(
        (i for i, line in enumerate(cleaned) if parse_date_range(line)), None
    )
    if date_index is None or date_index == 0:
        return None
    start_date, end_date = parse_date_range(cleaned[date_index])

    status_index = next(
        (
            i
            for i in range(date_index + 1, len(cleaned))
            if cleaned[i].strip().lower() in STATUS_WORDS
        ),
        None,
    )
    if status_index is None:
        return None
    status = STATUS_WORDS[cleaned[status_index].strip().lower()]

    # The name is the line before the dates; a leading avatar initial is not a name.
    employee_name = cleaned[date_index - 1]

    # Between the dates and the status sit: the all-day/partial marker, the requested
    # duration, and the reason. Durations and the marker are recognisable, so whatever
    # is left over is the reason.
    middle = cleaned[date_index + 1 : status_index]
    all_day = any(line.lower() == "all day" for line in middle)
    durations = [line for line in middle if _DURATION.match(line)]
    reason_parts = [
        line
        for line in middle
        if line.lower() != "all day" and not _DURATION.match(line) and not _TIME_WINDOW.search(line)
    ]

    tail = cleaned[status_index + 1 :]
    return TimeOffRow(
        employee_name=employee_name,
        start_date=start_date,
        end_date=end_date,
        status=status,
        reason=" ".join(reason_parts).strip(),
        requested_time=durations[0] if durations else "",
        approved_all_day=tail[0] if len(tail) > 0 else "",
        approved_partial=tail[1] if len(tail) > 1 else "",
        all_day=all_day,
    )


def parse_rows(rows: list[list[str]]) -> list[TimeOffRow]:
    parsed = [parse_row(row) for row in rows]
    return [row for row in parsed if row is not None]
