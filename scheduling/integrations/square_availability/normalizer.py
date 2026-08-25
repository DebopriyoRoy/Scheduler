"""Normalizer functions for Square Production employee availability."""

import hashlib
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from django.utils import timezone

from scheduling.integrations.square_availability.base import (
    AvailabilityState,
    NormalizedAvailabilityRecord,
)

ST_JOHNS_TZ = ZoneInfo("America/St_Johns")


def generate_availability_hash(
    square_team_member_id: str,
    record_date: date,
    state: str,
    start_time: time | None,
    end_time: time | None,
) -> str:
    """Generate SHA-256 hash for availability snapshot provenance."""
    st_str = start_time.strftime("%H:%M") if start_time else "NONE"
    et_str = end_time.strftime("%H:%M") if end_time else "NONE"
    raw = f"{square_team_member_id}|{record_date.isoformat()}|{state}|{st_str}|{et_str}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def build_normalized_record(
    employee_id: int,
    employee_name: str,
    square_team_member_id: str,
    record_date: date,
    state: AvailabilityState | str,
    start_time: time | None = None,
    end_time: time | None = None,
    source_provider: str = "LIVE_SQUARE_PRODUCTION",
    source_environment: str = "PRODUCTION",
    source_url: str = "https://app.squareup.com/dashboard/shifts/schedule/availability",
    retrieved_at: datetime | None = None,
) -> NormalizedAvailabilityRecord:
    """Builds a NormalizedAvailabilityRecord instance."""
    if isinstance(state, str):
        try:
            state = AvailabilityState(state)
        except ValueError:
            state = AvailabilityState.UNKNOWN

    if retrieved_at is None:
        retrieved_at = timezone.now()

    source_hash = generate_availability_hash(
        square_team_member_id, record_date, str(state), start_time, end_time
    )

    return NormalizedAvailabilityRecord(
        employee_id=employee_id,
        employee_name=employee_name,
        square_team_member_id=square_team_member_id,
        date=record_date,
        state=state,
        start_time=start_time,
        end_time=end_time,
        source_provider=source_provider,
        source_environment=source_environment,
        source_url=source_url,
        retrieved_at=retrieved_at,
        source_hash=source_hash,
    )
