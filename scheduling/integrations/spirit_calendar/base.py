"""Base provider interfaces and normalized data structures for calendar sync."""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time


@dataclass(frozen=True)
class NormalizedEventOccurrence:
    """Represents a single, normalized show/event occurrence extracted from the live calendar."""

    external_event_id: str
    external_occurrence_id: str
    title: str
    full_title: str
    date: date
    start_time: time
    end_time: time
    start_datetime: datetime
    end_datetime: datetime
    timezone_name: str = "America/St_Johns"
    venue: str = "Theatre Gower"
    category: str = "General"
    event_url: str = ""
    is_private: bool = False
    is_offsite: bool = False
    is_cancelled: bool = False
    source_provider: str = "LIVE_SPIRIT_CALENDAR"
    source_url: str = "https://spiritofnewfoundland.com/show-calendar/"
    retrieved_at: datetime | None = None
    source_hash: str = ""


class BaseCalendarProvider(ABC):
    """Abstract base provider for extracting live calendar events."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Returns the provider name string."""

    @abstractmethod
    def fetch_occurrences(
        self, start_date: date, end_date: date
    ) -> Sequence[NormalizedEventOccurrence]:
        """Fetch and return normalized event occurrences within the date range."""
