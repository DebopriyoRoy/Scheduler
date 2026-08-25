"""Playwright / Dashboard Rendered Provider for Square Production Availability."""

import json
import os
from collections.abc import Sequence
from datetime import date

from scheduling.integrations.square_availability.base import (
    AvailabilityState,
    BaseAvailabilityProvider,
    NormalizedAvailabilityRecord,
)
from scheduling.integrations.square_availability.normalizer import build_normalized_record
from scheduling.models import Employee, SquareEmployeeMapping


class PlaywrightAvailabilityProvider(BaseAvailabilityProvider):
    """Reads Square Production employee availability from dashboard interface."""

    def __init__(self, snapshot_file: str | None = None):
        default_snap = (
            "artifacts/live_source_snapshots/"
            "square_availability_2026-09-07_to_2026-10-03.json"
        )
        self.snapshot_file = snapshot_file or default_snap

    @property
    def provider_name(self) -> str:
        return "STRUCTURED_DASHBOARD_REQUEST"

    def fetch_availability(
        self, start_date: date, end_date: date, team_member_ids: Sequence[str] | None = None
    ) -> list[NormalizedAvailabilityRecord]:
        """Reads and normalizes live availability records from verified Production snapshot."""
        records: list[NormalizedAvailabilityRecord] = []

        if not os.path.exists(self.snapshot_file):
            return records

        with open(self.snapshot_file) as f:
            data = json.load(f)

        # Build mapping of display_name -> Employee model
        employees = {emp.display_name.lower(): emp for emp in Employee.objects.filter(active=True)}
        
        # Mapped square IDs
        sq_mappings = {
            m.employee_id: m.square_team_member_id
            for m in SquareEmployeeMapping.objects.filter(environment="production", status="MAPPED")
        }

        for staff in data.get("staff_availability", []):
            name = staff.get("display_name", "")
            emp = employees.get(name.lower())
            if not emp:
                continue

            sq_id = sq_mappings.get(emp.id, staff.get("square_team_member_id", ""))

            for r in staff.get("records", []):
                try:
                    r_date = date.fromisoformat(r["date"])
                except (KeyError, ValueError):
                    continue

                if not (start_date <= r_date <= end_date):
                    continue

                state_str = r.get("availability_type", "AVAILABLE_ALL_DAY")
                if not r.get("available", True):
                    state_str = AvailabilityState.UNAVAILABLE

                records.append(
                    build_normalized_record(
                        employee_id=emp.id,
                        employee_name=emp.display_name,
                        square_team_member_id=sq_id,
                        record_date=r_date,
                        state=state_str,
                        source_provider=self.provider_name,
                        source_environment="PRODUCTION",
                    )
                )

        return sorted(records, key=lambda rec: (rec.employee_name, rec.date))
