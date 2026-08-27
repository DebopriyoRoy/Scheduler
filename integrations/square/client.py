import logging
import re
from collections.abc import Mapping
from typing import Any

import requests

from .config import SquareConfig
from .exceptions import SquareAPIError, SquareConnectionError, SquarePublishedShiftError

logger = logging.getLogger(__name__)


def redact_secrets(value: object, secrets: tuple[str, ...] = ()) -> str:
    text = str(value)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return re.sub(r"(?i)Bearer\s+[^\s,;]+", "Bearer [REDACTED]", text)


class SquareClient:
    def __init__(
        self,
        config: SquareConfig | None = None,
        *,
        session: requests.Session | None = None,
    ):
        self.config = config or SquareConfig.from_env()
        self.session = session or requests.Session()

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.access_token}",
            "Content-Type": "application/json",
            "Square-Version": self.config.api_version,
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.config.base_url}{path}"
        try:
            response = self.session.request(
                method,
                url,
                headers=self._headers,
                json=dict(json) if json is not None else None,
                params=dict(params) if params is not None else None,
                timeout=self.config.request_timeout_seconds,
            )
        except requests.RequestException as exc:
            safe_error = redact_secrets(
                exc,
                (self.config.sandbox_access_token, self.config.production_access_token),
            )
            logger.warning("Square request failed: %s", safe_error)
            raise SquareConnectionError(
                "Unable to reach Square. Check the network and Sandbox configuration."
            ) from exc

        try:
            payload = response.json() if response.content else {}
        except ValueError as exc:
            raise SquareConnectionError(
                f"Square returned an unreadable response (HTTP {response.status_code})."
            ) from exc

        if response.status_code >= 400 or payload.get("errors"):
            error_text = redact_secrets(
                self._format_api_errors(payload.get("errors", [])),
                (self.config.sandbox_access_token, self.config.production_access_token),
            )
            raise SquareAPIError(
                f"Square API request failed (HTTP {response.status_code}): {error_text}",
                status_code=response.status_code,
            )
        return payload

    @staticmethod
    def _format_api_errors(errors: object) -> str:
        if not isinstance(errors, list):
            return "Unknown Square API error."
        messages: list[str] = []
        for error in errors:
            if not isinstance(error, dict):
                continue
            code = str(error.get("code", "UNKNOWN"))
            detail = str(error.get("detail", "Request rejected."))
            messages.append(f"{code}: {detail}")
        return "; ".join(messages) or "Unknown Square API error."

    def test_connection(self) -> list[dict[str, Any]]:
        return self.list_locations()

    def list_locations(self) -> list[dict[str, Any]]:
        payload = self._request("GET", "/v2/locations")
        return list(payload.get("locations", []))

    def search_team_members(self, *, active_only: bool = True) -> list[dict[str, Any]]:
        members: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            request_body: dict[str, Any] = {"limit": 200}
            if active_only:
                request_body["query"] = {"filter": {"status": "ACTIVE"}}
            if cursor:
                request_body["cursor"] = cursor
            payload = self._request("POST", "/v2/team-members/search", json=request_body)
            members.extend(payload.get("team_members", []))
            cursor = payload.get("cursor")
            if not cursor:
                return members

    def list_jobs(self) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params = {"cursor": cursor} if cursor else None
            payload = self._request("GET", "/v2/team-members/jobs", params=params)
            jobs.extend(payload.get("jobs", []))
            cursor = payload.get("cursor")
            if not cursor:
                return jobs

    def search_scheduled_shifts(
        self,
        query: Mapping[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        shifts: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            request_body: dict[str, Any] = {}
            if query:
                request_body["query"] = dict(query)
            if cursor:
                request_body["cursor"] = cursor
            payload = self._request(
                "POST",
                "/v2/labor/scheduled-shifts/search",
                json=request_body,
            )
            shifts.extend(payload.get("scheduled_shifts", []))
            cursor = payload.get("cursor")
            if not cursor:
                return shifts

    def create_draft_shift(
        self,
        *,
        idempotency_key: str,
        team_member_id: str,
        job_id: str,
        location_id: str,
        start_at: str,
        end_at: str,
        notes: str = "Spirit scheduling Phase 1 Sandbox test",
    ) -> dict[str, Any]:
        self.config.assert_write_allowed()
        payload = self._request(
            "POST",
            "/v2/labor/scheduled-shifts",
            json={
                "idempotency_key": idempotency_key,
                "scheduled_shift": {
                    "draft_shift_details": {
                        "team_member_id": team_member_id,
                        "job_id": job_id,
                        "location_id": location_id,
                        "start_at": start_at,
                        "end_at": end_at,
                        "notes": notes,
                    }
                },
            },
        )
        return dict(payload.get("scheduled_shift", {}))

    def get_scheduled_shift(self, shift_id: str) -> dict[str, Any]:
        payload = self._request("GET", f"/v2/labor/scheduled-shifts/{shift_id}")
        return dict(payload.get("scheduled_shift", {}))

    def update_draft_shift(
        self,
        shift_id: str,
        *,
        version: int,
        draft_shift_details: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Update an existing *draft* shift in place.

        Correcting a field on an already-created draft otherwise requires deleting and
        recreating it, and this client has no delete. Callers pass the shift's full
        draft_shift_details with only the intended field changed, so anything not being
        corrected is echoed back exactly as Square already holds it. `version` carries
        Square's optimistic-concurrency token: the update is rejected rather than
        silently overwriting if the shift changed since it was read.

        This writes drafts only. assert_publishing_disabled() keeps it that way.
        """
        self.config.assert_write_allowed()
        self.config.assert_publishing_disabled()
        payload = self._request(
            "PUT",
            f"/v2/labor/scheduled-shifts/{shift_id}",
            json={
                "scheduled_shift": {
                    "version": version,
                    "draft_shift_details": dict(draft_shift_details),
                }
            },
        )
        return dict(payload.get("scheduled_shift", {}))

    def delete_draft_shift(self, shift_id: str) -> dict[str, Any]:
        """Remove a shift from Square permanently.

        Square publishes no DELETE for scheduled shifts. A shift is removed by
        updating it with draft_shift_details.is_deleted = true, which Square treats
        as a hard delete *only while the shift has never been published*. A published
        shift is merely marked by the same call and needs PublishScheduledShift to
        finalise, which this integration refuses to call - so those are raised back
        rather than left half-deleted and looking gone when they are not.

        The current version and details are read first: Square rejects the update if
        the version is stale, which is what stops this from clobbering a shift a
        manager edited in the meantime.
        """
        self.config.assert_write_allowed()
        self.config.assert_publishing_disabled()

        shift = self.get_scheduled_shift(shift_id)
        if shift.get("published_shift_details"):
            raise SquarePublishedShiftError(
                f"Shift {shift_id} is published in Square and cannot be removed from here. "
                "Delete it in the Square dashboard instead."
            )

        details = dict(shift.get("draft_shift_details") or {})
        details["is_deleted"] = True
        return self.update_draft_shift(
            shift_id, version=int(shift.get("version") or 0), draft_shift_details=details
        )

