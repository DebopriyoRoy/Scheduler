"""How old the Square availability this app is working from is.

A roster built from last week's availability is built around people who may since
have booked the night off. Every read of Square is recorded as a
SquareAvailabilitySyncRun; this reads the newest one that produced data and says
whether it is recent enough to build a roster from.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.utils import timezone

from scheduling.models import SquareAvailabilitySyncRun

# Older than this and a roster run needs someone to say so on purpose.
STALE_AFTER = timedelta(hours=24)


@dataclass(frozen=True)
class Freshness:
    read_at: datetime | None
    run: SquareAvailabilitySyncRun | None
    stale: bool

    @property
    def never_read(self) -> bool:
        return self.read_at is None

    def age_text(self, now: datetime | None = None) -> str:
        if self.read_at is None:
            return "never read from Square"
        seconds = ((now or timezone.now()) - self.read_at).total_seconds()
        minutes = max(0, int(seconds // 60))
        if minutes < 1:
            return "read just now"
        if minutes < 60:
            return f"read {minutes} min ago"
        hours = minutes // 60
        if hours < 48:
            return f"read {hours} h ago"
        return f"read {hours // 24} days ago"


def availability_freshness(now: datetime | None = None) -> Freshness:
    now = now or timezone.now()
    run = (
        SquareAvailabilitySyncRun.objects.filter(
            status__in=[
                SquareAvailabilitySyncRun.SyncStatus.SUCCESS,
                SquareAvailabilitySyncRun.SyncStatus.PARTIAL,
            ]
        )
        .order_by("-started_at")
        .first()
    )
    if run is None:
        return Freshness(None, None, True)
    read_at = run.completed_at or run.started_at
    return Freshness(read_at, run, now - read_at > STALE_AFTER)
