"""Base provider interface and data structures for Square availability."""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from enum import StrEnum


class AvailabilityState(StrEnum):
    AVAILABLE_ALL_DAY = "AVAILABLE_ALL_DAY"
    AVAILABLE_WINDOW = "AVAILABLE_WINDOW"
    UNAVAILABLE = "UNAVAILABLE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class NormalizedAvailabilityRecord:
    """Represents a normalized employee availability record for a single date."""

    employee_id: int
    employee_name: str
    square_team_member_id: str
    date: date
    state: AvailabilityState
    start_time: time | None = None
    end_time: time | None = None
    source_provider: str = "LIVE_SQUARE_PRODUCTION"
    source_environment: str = "PRODUCTION"
    source_url: str = "https://app.squareup.com/dashboard/shifts/schedule/availability"
    retrieved_at: datetime | None = None
    source_hash: str = ""

    def is_eligible_for_shift(self, shift_start_time: time, shift_end_time: time) -> bool:
        """Determines shift coverage eligibility according to Section 16 rules."""
        if self.state == AvailabilityState.UNKNOWN or self.state == AvailabilityState.UNAVAILABLE:
            return False
        
        if self.state == AvailabilityState.AVAILABLE_ALL_DAY:
            return True
            
        if self.state == AvailabilityState.AVAILABLE_WINDOW:
            if not self.start_time or not self.end_time:
                return False
            # Check full coverage
            return self.start_time <= shift_start_time and shift_end_time <= self.end_time
            
        return False


class BaseAvailabilityProvider(ABC):
    """Abstract base provider for reading Square employee availability."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Returns provider engine name."""

    @abstractmethod
    def fetch_availability(
        self, start_date: date, end_date: date, team_member_ids: Sequence[str] | None = None
    ) -> Sequence[NormalizedAvailabilityRecord]:
        """Fetch and return normalized availability records within the requested date range."""
