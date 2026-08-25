"""Primary API Provider for Square Availability."""

from collections.abc import Sequence
from datetime import date

from scheduling.integrations.square_availability.base import (
    BaseAvailabilityProvider,
    NormalizedAvailabilityRecord,
)
from scheduling.integrations.square_availability.exceptions import SquareAvailabilityAPIError


class APIAvailabilityProvider(BaseAvailabilityProvider):
    """Attempts to fetch availability via Square v2 REST API if available."""

    @property
    def provider_name(self) -> str:
        return "OFFICIAL_API"

    def fetch_availability(
        self, start_date: date, end_date: date, team_member_ids: Sequence[str] | None = None
    ) -> Sequence[NormalizedAvailabilityRecord]:
        """Square v2 REST API does not expose team member availability endpoints."""
        raise SquareAvailabilityAPIError(
            "Square v2 REST API does not provide a public team-member availability endpoint. "
            "Internal dashboard session required."
        )
