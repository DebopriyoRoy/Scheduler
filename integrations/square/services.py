import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from .client import SquareClient
from .exceptions import SquareConfigurationError


@dataclass(frozen=True)
class DraftShiftRequest:
    team_member_id: str
    job_id: str
    location_id: str
    start_at: str
    end_at: str

    def validate(self) -> None:
        try:
            start = datetime.fromisoformat(self.start_at)
            end = datetime.fromisoformat(self.end_at)
        except ValueError as exc:
            raise SquareConfigurationError(
                "Start and end must be ISO 8601 timestamps with UTC offsets."
            ) from exc
        if start.tzinfo is None or end.tzinfo is None:
            raise SquareConfigurationError("Start and end timestamps must include UTC offsets.")
        if end <= start:
            raise SquareConfigurationError("Shift end must be after shift start.")
        for label, value in (
            ("team member ID", self.team_member_id),
            ("job ID", self.job_id),
            ("location ID", self.location_id),
        ):
            if not value.strip():
                raise SquareConfigurationError(f"The {label} cannot be empty.")

    @property
    def idempotency_key(self) -> str:
        canonical = "|".join(
            (
                self.team_member_id,
                self.job_id,
                self.location_id,
                self.start_at,
                self.end_at,
            )
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:40]
        return f"spirit-phase1-{digest}"


def create_sandbox_draft_shift(
    client: SquareClient,
    request: DraftShiftRequest,
) -> dict[str, Any]:
    request.validate()
    client.config.require_sandbox()
    return client.create_draft_shift(
        idempotency_key=request.idempotency_key,
        team_member_id=request.team_member_id,
        job_id=request.job_id,
        location_id=request.location_id,
        start_at=request.start_at,
        end_at=request.end_at,
    )
