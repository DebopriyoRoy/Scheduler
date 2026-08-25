"""Primary API/XHR Provider for Spirit Live Show Calendar."""

from collections.abc import Sequence
from datetime import date

import requests

from scheduling.integrations.spirit_calendar.base import (
    BaseCalendarProvider,
    NormalizedEventOccurrence,
)
from scheduling.integrations.spirit_calendar.exceptions import SpiritCalendarAPIError


class APICalendarProvider(BaseCalendarProvider):
    """Attempts to fetch event occurrences via public REST / XHR endpoints."""

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36 "
                    "SpiritScheduler/2.0"
                ),
                "Accept": "application/json",
            }
        )

    @property
    def provider_name(self) -> str:
        return "API_XHR"

    def fetch_occurrences(
        self, start_date: date, end_date: date
    ) -> Sequence[NormalizedEventOccurrence]:
        """Fetch event occurrences from public REST API if available."""
        # Test public EventIn REST API endpoints
        endpoint = "https://spiritofnewfoundland.com/wp-json/eventin/v2/events"
        try:
            response = self.session.get(endpoint, timeout=10)
            if response.status_code != 200:
                raise SpiritCalendarAPIError(
                    f"API Endpoint '{endpoint}' returned HTTP status {response.status_code} "
                    "(Authentication / Nonce required for structured API feed)."
                )
            payload = response.json()
            if not isinstance(payload, list) and not isinstance(payload, dict):
                raise SpiritCalendarAPIError(f"Unexpected JSON structure from '{endpoint}'.")
            
            # If endpoint is public and returns events, parse occurrences
            occurrences: list[NormalizedEventOccurrence] = []
            # ... API parsing logic if public API is reachable ...
            return occurrences
        except Exception as exc:
            if isinstance(exc, SpiritCalendarAPIError):
                raise
            raise SpiritCalendarAPIError(f"API Provider failed: {exc}") from exc
