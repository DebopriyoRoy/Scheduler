"""Playwright / Dashboard Rendered Provider for Square Production Availability."""

from collections.abc import Sequence
from datetime import date, time, timedelta
from typing import Any

from scheduling.integrations.square_availability.base import (
    AvailabilityState,
    BaseAvailabilityProvider,
    NormalizedAvailabilityRecord,
)
from scheduling.integrations.square_availability.exceptions import (
    AvailabilityNormalizationSuspectError,
)
from scheduling.integrations.square_availability.normalizer import build_normalized_record
from scheduling.models import Employee, SquareEmployeeMapping

# Weekly recurring availability rules per employee (0=Mon, ..., 6=Sun)
WEEKLY_AVAILABILITY_RULES: dict[str, dict[int, Any]] = {
    "Emily": {d: [("17:30", "23:00")] for d in range(7)},
    "Emily Talbot": {d: [("17:30", "23:00")] for d in range(7)},
    "Jackie Pynn": {
        2: [("14:00", "23:00")],
        3: [("14:00", "23:00")],
        4: [("14:00", "23:00")],
        5: [("14:00", "23:00")],
    },
    "Joleen Dickson": {d: [("16:00", "23:00")] for d in range(7)},
    # Corrected per live Square Availability page (was previously mistranscribed:
    # Thursday "05:30" should have read "17:30", and Saturday was missing).
    "Kate": {
        0: [("19:00", "23:00")],
        1: [("16:00", "20:30")],
        2: [("16:00", "20:30")],
        3: [("17:30", "21:30")],
        5: [("16:00", "20:30")],
    },
    "Kate Griffin": {
        0: [("19:00", "23:00")],
        1: [("16:00", "20:30")],
        2: [("16:00", "20:30")],
        3: [("17:30", "21:30")],
        5: [("16:00", "20:30")],
    },
    "Khrystyna": {
        0: [("11:00", "16:00"), ("18:00", "23:00")],
        1: [("11:00", "16:00"), ("18:00", "23:00")],
        2: [("11:00", "16:00"), ("18:00", "23:00")],
        3: [("11:00", "16:00"), ("18:00", "23:00")],
        4: [("11:00", "16:00"), ("18:00", "23:00")],
        5: [("10:00", "16:00"), ("18:00", "23:00")],
        6: [("10:00", "16:00"), ("18:00", "23:00")],
    },
    "Khrystyna Zavadetska": {
        0: [("11:00", "16:00"), ("18:00", "23:00")],
        1: [("11:00", "16:00"), ("18:00", "23:00")],
        2: [("11:00", "16:00"), ("18:00", "23:00")],
        3: [("11:00", "16:00"), ("18:00", "23:00")],
        4: [("11:00", "16:00"), ("18:00", "23:00")],
        5: [("10:00", "16:00"), ("18:00", "23:00")],
        6: [("10:00", "16:00"), ("18:00", "23:00")],
    },
    "Linda Penney": {5: [("16:00", "23:00")], 6: [("16:00", "23:00")]},
    "Maks Plsky": {d: [("18:00", "23:00")] for d in range(7)},
    # Per management direction (not a Square resync): Molly is available every
    # evening 6:00-10:30pm and is specifically well-suited to the on-call role.
    "Molly Rittwage": {d: [("18:00", "22:30")] for d in range(7)},
    "Neil Bobbit": {
        0: "AVAILABLE_ALL_DAY",
        1: "AVAILABLE_ALL_DAY",
        2: [("18:00", "23:00")],
        3: "AVAILABLE_ALL_DAY",
        4: "AVAILABLE_ALL_DAY",
        6: "AVAILABLE_ALL_DAY",
    },
    "Neil Bobbitt": {
        0: "AVAILABLE_ALL_DAY",
        1: "AVAILABLE_ALL_DAY",
        2: [("18:00", "23:00")],
        3: "AVAILABLE_ALL_DAY",
        4: "AVAILABLE_ALL_DAY",
        6: "AVAILABLE_ALL_DAY",
    },
    "Olena": {d: [("15:00", "23:30")] for d in range(7)},
    "Olena Martynova": {d: [("15:00", "23:30")] for d in range(7)},
    "Yana": {
        0: [("14:30", "00:00")],
        1: [("14:30", "00:00")],
        2: [("14:30", "00:00")],
        3: [("14:30", "00:00")],
        4: [("10:00", "15:00"), ("16:30", "21:30")],
        5: [("10:00", "15:00"), ("17:00", "22:00")],
        6: [("10:00", "15:00"), ("17:00", "23:00")],
    },
    "Yana Pasechniuk": {
        0: [("14:30", "00:00")],
        1: [("14:30", "00:00")],
        2: [("14:30", "00:00")],
        3: [("14:30", "00:00")],
        4: [("10:00", "15:00"), ("16:30", "21:30")],
        5: [("10:00", "15:00"), ("17:00", "22:00")],
        6: [("10:00", "15:00"), ("17:00", "23:00")],
    },
    "Brittany James": {},
    "Butros": {},
    "Butros Al-Deir": {},
    "Daniel": {},
    "Daniel Gordon": {},
    "Montana": {},
    "Montana Pynn": {},
    "Patrice": {},
    "Patrice Halley": {},
    "Svitlana": {},
    "Svitlana Al-Lahut": {},
}

# Approved time-off requests, keyed by employee name, that override the weekly
# recurring rule above for specific dates regardless of day-of-week.
DATE_OVERRIDES: dict[str, dict[date, str]] = {
    "Kate": {
        date(2026, 9, 25): "UNAVAILABLE",
        date(2026, 9, 26): "UNAVAILABLE",
        date(2026, 9, 27): "UNAVAILABLE",
    },
    "Kate Griffin": {
        date(2026, 9, 25): "UNAVAILABLE",
        date(2026, 9, 26): "UNAVAILABLE",
        date(2026, 9, 27): "UNAVAILABLE",
    },
}


class PlaywrightAvailabilityProvider(BaseAvailabilityProvider):
    """Reads Square Production employee availability from dashboard interface."""

    def __init__(self, snapshot_file: str | None = None):
        self.snapshot_file = snapshot_file

    @property
    def provider_name(self) -> str:
        return "MANUAL_VERIFIED_SQUARE_AVAILABILITY_SNAPSHOT"

    @property
    def is_live(self) -> bool:
        """False. This provider does not contact Square.

        Named for an intention that was never implemented: it makes no network call at
        all, and generates records from WEEKLY_AVAILABILITY_RULES above - a set of
        windows transcribed by hand. Every eligibility decision the engine makes rests
        on that transcription, which is how Kate's Thursday came to read 05:30 instead
        of 17:30, and why six staff appear permanently unschedulable. Replacing it
        needs a signed-in dashboard session; see scheduling.integrations.square_session.
        """
        return False

    def fetch_availability(
        self, start_date: date, end_date: date, team_member_ids: Sequence[str] | None = None
    ) -> list[NormalizedAvailabilityRecord]:
        """Generate records from the hand-transcribed rules. Not read from Square."""
        records: list[NormalizedAvailabilityRecord] = []

        active_employees = list(Employee.objects.filter(active=True))

        sq_mappings = {
            m.employee_id: m.square_team_member_id
            for m in SquareEmployeeMapping.objects.filter(
                environment="production", status="MAPPED"
            )
        }

        curr = start_date
        while curr <= end_date:
            dow = curr.weekday()
            for emp in active_employees:
                sq_id = sq_mappings.get(emp.id, emp.square_team_member_id or f"tm-{emp.id}")

                # Approved time off overrides the weekly rule for this exact date.
                overrides_for_emp = DATE_OVERRIDES.get(
                    emp.display_name,
                    DATE_OVERRIDES.get(f"{emp.first_name} {emp.last_name}".strip(), {}),
                )
                if curr in overrides_for_emp:
                    records.append(
                        build_normalized_record(
                            employee_id=emp.id,
                            employee_name=emp.display_name,
                            square_team_member_id=sq_id,
                            record_date=curr,
                            state=AvailabilityState.UNAVAILABLE,
                            source_provider=self.provider_name,
                            source_environment="PRODUCTION",
                        )
                    )
                    continue

                # Find rules key
                rules_for_emp = WEEKLY_AVAILABILITY_RULES.get(
                    emp.display_name,
                    WEEKLY_AVAILABILITY_RULES.get(
                        f"{emp.first_name} {emp.last_name}".strip(), {}
                    ),
                )

                if dow not in rules_for_emp:
                    # Missing availability entered -> UNKNOWN
                    records.append(
                        build_normalized_record(
                            employee_id=emp.id,
                            employee_name=emp.display_name,
                            square_team_member_id=sq_id,
                            record_date=curr,
                            state=AvailabilityState.UNKNOWN,
                            source_provider=self.provider_name,
                            source_environment="PRODUCTION",
                        )
                    )
                else:
                    rule = rules_for_emp[dow]
                    if rule == "AVAILABLE_ALL_DAY":
                        records.append(
                            build_normalized_record(
                                employee_id=emp.id,
                                employee_name=emp.display_name,
                                square_team_member_id=sq_id,
                                record_date=curr,
                                state=AvailabilityState.AVAILABLE_ALL_DAY,
                                source_provider=self.provider_name,
                                source_environment="PRODUCTION",
                            )
                        )
                    elif isinstance(rule, list):
                        for start_str, end_str in rule:
                            st = time.fromisoformat(start_str)
                            et = time.fromisoformat(end_str)
                            records.append(
                                build_normalized_record(
                                    employee_id=emp.id,
                                    employee_name=emp.display_name,
                                    square_team_member_id=sq_id,
                                    record_date=curr,
                                    state=AvailabilityState.AVAILABLE_WINDOW,
                                    start_time=st,
                                    end_time=et,
                                    source_provider=self.provider_name,
                                    source_environment="PRODUCTION",
                                )
                            )

            curr += timedelta(days=1)

        # Validation Guard: Prevent false fallback reporting
        all_day_count = sum(1 for r in records if r.state == AvailabilityState.AVAILABLE_ALL_DAY)
        total_employees = len(active_employees)
        # If more than 20% of employees normalize to ALL_DAY, flag suspect
        days_cnt = (end_date - start_date + timedelta(days=1)).days
        max_implausible_all_day = total_employees * days_cnt * 0.3
        if all_day_count > max_implausible_all_day:
            raise AvailabilityNormalizationSuspectError(
                f"AVAILABILITY_NORMALIZATION_SUSPECT: Found {all_day_count} "
                f"AVAILABLE_ALL_DAY records, exceeding threshold ({max_implausible_all_day:.0f})."
            )

        return sorted(
            records,
            key=lambda rec: (rec.employee_name, rec.date, rec.start_time or time(0, 0)),
        )
