import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from django.db import transaction

from scheduling.models import Show

CALENDAR_URL = "https://spiritofnewfoundland.com/show-calendar/"
KNOWN_EVENT_PAGES = (
    "https://spiritofnewfoundland.com/shows/forever-countryin-the-key-of-spirit/",
    "https://spiritofnewfoundland.com/shift-happens/",
)
MONTH_PATTERN = (
    r"January|February|March|April|May|June|July|August|September|October|November|December"
)
DATE_PATTERN = re.compile(rf"\b({MONTH_PATTERN})\s+(\d{{1,2}}),\s+(20\d{{2}})\b", re.IGNORECASE)
TIME_RANGE_PATTERN = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*([ap]m)\s*(?:-|–|—|to)\s*"
    r"(\d{1,2})(?::(\d{2}))?\s*([ap]m)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ImportedEvent:
    external_id: str
    title: str
    date: date
    start_time: time
    end_time: time
    venue: str
    source_url: str


@dataclass(frozen=True)
class ImportSummary:
    created: int
    updated: int
    events: tuple[ImportedEvent, ...]
    sources_checked: tuple[str, ...]


def _event_id(url: str, title: str, event_date: date) -> str:
    digest = hashlib.sha256(f"{url}|{title}|{event_date.isoformat()}".encode()).hexdigest()[:24]
    return f"spirit-calendar-{digest}"


def _parse_iso_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _iter_json_objects(value):
    if isinstance(value, list):
        for item in value:
            yield from _iter_json_objects(item)
    elif isinstance(value, dict):
        yield value
        for child in value.values():
            if isinstance(child, (dict, list)):
                yield from _iter_json_objects(child)


def events_from_html(html: str, source_url: str) -> list[ImportedEvent]:
    soup = BeautifulSoup(html, "html.parser")
    events: list[ImportedEvent] = []
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        for item in _iter_json_objects(payload):
            event_types = item.get("@type", [])
            if isinstance(event_types, str):
                event_types = [event_types]
            if "Event" not in event_types or not item.get("startDate"):
                continue
            try:
                start = _parse_iso_datetime(item["startDate"])
                end = _parse_iso_datetime(item.get("endDate", item["startDate"]))
            except (TypeError, ValueError):
                continue
            location = item.get("location", {})
            venue = (
                location.get("name", "Theatre Gower")
                if isinstance(location, dict)
                else str(location)
            )
            title = BeautifulSoup(str(item.get("name", "Spirit Show")), "html.parser").get_text(
                " ", strip=True
            )
            url = str(item.get("url") or source_url)
            events.append(
                ImportedEvent(
                    external_id=_event_id(url, title, start.date()),
                    title=title,
                    date=start.date(),
                    start_time=start.time().replace(tzinfo=None),
                    end_time=end.time().replace(tzinfo=None),
                    venue=venue or "Theatre Gower",
                    source_url=url,
                )
            )
    if events:
        return events

    title_element = soup.find("h1") or soup.find("title")
    title = title_element.get_text(" ", strip=True) if title_element else "Spirit Show"
    title = re.sub(r"\s*[|–-]\s*Spirit of Newfoundland.*$", "", title, flags=re.IGNORECASE)
    text = soup.get_text(" ", strip=True)
    time_match = TIME_RANGE_PATTERN.search(text)
    start_time, end_time = time(18, 30), time(22, 30)
    if time_match:
        start_time = datetime.strptime(
            f"{time_match.group(1)}:{time_match.group(2) or '00'} {time_match.group(3)}",
            "%I:%M %p",
        ).time()
        end_time = datetime.strptime(
            f"{time_match.group(4)}:{time_match.group(5) or '00'} {time_match.group(6)}",
            "%I:%M %p",
        ).time()
    seen_dates: set[date] = set()
    for match in DATE_PATTERN.finditer(text):
        parsed_date = datetime.strptime(" ".join(match.groups()), "%B %d %Y").date()
        if parsed_date in seen_dates:
            continue
        seen_dates.add(parsed_date)
        events.append(
            ImportedEvent(
                external_id=_event_id(source_url, title, parsed_date),
                title=title,
                date=parsed_date,
                start_time=start_time,
                end_time=end_time,
                venue="Theatre Gower",
                source_url=source_url,
            )
        )
    return events


class SpiritCalendarImporter:
    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": "SpiritScheduler/2.0 calendar importer"})

    def discover_urls(self) -> list[str]:
        urls = {CALENDAR_URL, *KNOWN_EVENT_PAGES}
        response = self.session.get(CALENDAR_URL, timeout=20)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
        for anchor in soup.select("a[href]"):
            url = urljoin(CALENDAR_URL, anchor["href"])
            if url.startswith("https://spiritofnewfoundland.com/") and (
                "/shows/" in url or "show" in anchor.get_text(" ", strip=True).casefold()
            ):
                urls.add(url.split("#", 1)[0])
        return sorted(urls)

    @transaction.atomic
    def import_range(self, start_date: date, end_date: date) -> ImportSummary:
        if end_date < start_date:
            raise ValueError("End date must not precede the start date.")
        urls = self.discover_urls()
        parsed: dict[str, ImportedEvent] = {}
        for url in urls:
            response = self.session.get(url, timeout=20)
            response.raise_for_status()
            for event in events_from_html(response.text, url):
                if start_date <= event.date <= end_date:
                    parsed[event.external_id] = event
        created = 0
        updated = 0
        events = sorted(
            parsed.values(),
            key=lambda item: (item.date, item.start_time, item.title),
        )
        for event in events:
            _, was_created = Show.objects.update_or_create(
                external_id=event.external_id,
                defaults={
                    "title": event.title,
                    "date": event.date,
                    "start_time": event.start_time,
                    "end_time": event.end_time,
                    "venue": event.venue,
                    "source": Show.Source.CALENDAR_IMPORT,
                    "source_url": event.source_url,
                    "active": True,
                },
            )
            created += int(was_created)
            updated += int(not was_created)
        return ImportSummary(created, updated, tuple(parsed.values()), tuple(urls))
