"""Service layer managing Square Production availability.

Handles availability fetching, completeness, and audit tracking.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from django.db import transaction
from django.utils import timezone

from scheduling.integrations.square_availability.api_provider import APIAvailabilityProvider
from scheduling.integrations.square_availability.base import (
    AvailabilityState,
    NormalizedAvailabilityRecord,
)
from scheduling.integrations.square_availability.browser_provider import (
    PlaywrightAvailabilityProvider,
)
from scheduling.integrations.square_availability.exceptions import SquareAvailabilityAPIError
from scheduling.models import (
    Employee,
    EmployeeRole,
    Show,
    SquareAvailabilitySyncRun,
)

ROSTER_EMPLOYEE_NAMES = [
    "Joleen Dickson",
    "Jackie Pynn",
    "Olena",
    "Yana",
    "Kate",
    "Molly Rittwage",
    "Linda Penney",
    "Svitlana",
    "Daniel",
    "Butros",
    "Patrice",
    "Montana",
    "Neil Bobbit",
    "Brittany James",
    "Khrystyna",
    "Emily",
    "Maks Plsky",
]


@dataclass
class DateCapacitySummary:
    event_date: date
    show_title: str
    available_servers: int
    available_bartenders: int
    available_bussers: int
    available_fifty_fifty: int
    potential_shortage: bool


@dataclass
class AvailabilityMatrixCell:
    employee_name: str
    event_date: date
    record: NormalizedAvailabilityRecord | None
    is_known: bool
    state_display: str


@dataclass
class AvailabilityAnalysisSummary:
    sync_run: SquareAvailabilitySyncRun
    total_requested: int
    total_found: int
    total_combinations: int
    known_combinations: int
    unknown_combinations: int
    completeness_pct: float
    matrix_cells: tuple[AvailabilityMatrixCell, ...]
    capacity_summaries: tuple[DateCapacitySummary, ...]
    olena_records: tuple[NormalizedAvailabilityRecord, ...]
    jackie_records: tuple[NormalizedAvailabilityRecord, ...]
    yana_records: tuple[NormalizedAvailabilityRecord, ...]
    kate_records: tuple[NormalizedAvailabilityRecord, ...]


class SquareAvailabilitySyncService:
    """Manages reading and completeness validation of Square Production availability."""

    def __init__(self, api_provider=None, browser_provider=None):
        self.api_provider = api_provider or APIAvailabilityProvider()
        self.browser_provider = browser_provider or PlaywrightAvailabilityProvider()

    def fetch_with_fallback(
        self, start_date: date, end_date: date
    ) -> tuple[Sequence[NormalizedAvailabilityRecord], str]:
        """Attempts API extraction first, falls back to browser/snapshot extraction."""
        try:
            records = self.api_provider.fetch_availability(start_date, end_date)
            if records:
                return records, self.api_provider.provider_name
        except SquareAvailabilityAPIError:
            pass

        records = self.browser_provider.fetch_availability(start_date, end_date)
        return records, self.browser_provider.provider_name

    @transaction.atomic
    def execute_sync(
        self, start_date: date, end_date: date, event_dates: Sequence[date] | None = None
    ) -> AvailabilityAnalysisSummary:
        """Executes read-only availability sync and completeness validation."""
        sync_run = SquareAvailabilitySyncRun.objects.create(
            environment="PRODUCTION",
            start_date=start_date,
            end_date=end_date,
            provider="STRUCTURED_DASHBOARD_REQUEST",
            status="RUNNING",
        )

        # Retrieve active scheduling employees
        active_employees = list(
            Employee.objects.filter(
                active=True,
                excluded_from_automatic_scheduling=False,
                display_name__in=ROSTER_EMPLOYEE_NAMES,
            ).order_by("display_name")
        )

        sync_run.employees_requested = len(ROSTER_EMPLOYEE_NAMES)
        sync_run.employees_found = len(active_employees)

        records, provider_used = self.fetch_with_fallback(start_date, end_date)
        sync_run.provider = provider_used
        sync_run.records_received = len(records)

        # Build lookup map: (employee_name, record_date) -> record
        record_map: dict[tuple[str, date], NormalizedAvailabilityRecord] = {
            (rec.employee_name.lower(), rec.date): rec for rec in records
        }

        # Resolve live event dates if not provided
        if not event_dates:
            event_dates = list(
                Show.objects.filter(date__range=(start_date, end_date), active=True)
                .values_list("date", flat=True)
                .distinct()
                .order_by("date")
            )

        total_combinations = len(active_employees) * len(event_dates)
        known_cnt = 0
        unknown_cnt = 0

        matrix_cells: list[AvailabilityMatrixCell] = []

        for emp in active_employees:
            for ed in event_dates:
                rec = record_map.get((emp.display_name.lower(), ed))
                if rec and rec.state != AvailabilityState.UNKNOWN:
                    known_cnt += 1
                    is_known = True
                    state_disp = rec.state.name if hasattr(rec.state, "name") else str(rec.state)
                else:
                    unknown_cnt += 1
                    is_known = False
                    state_disp = "UNKNOWN"

                matrix_cells.append(
                    AvailabilityMatrixCell(
                        employee_name=emp.display_name,
                        event_date=ed,
                        record=rec,
                        is_known=is_known,
                        state_display=state_disp,
                    )
                )

        sync_run.unknown_count = unknown_cnt
        completeness_pct = (
            round((known_cnt / total_combinations) * 100.0, 1) if total_combinations > 0 else 100.0
        )
        sync_run.status = "SUCCESS" if unknown_cnt == 0 else "PARTIAL"
        sync_run.completed_at = timezone.now()
        sync_run.save()

        # Compute role-aware capacity for each event date
        capacity_summaries: list[DateCapacitySummary] = []
        for ed in event_dates:
            show = Show.objects.filter(date=ed, active=True).first()
            show_title = show.title if show else "Show Date"

            # Count available staff by role capability
            av_servers = 0
            av_bartenders = 0
            av_bussers = 0
            av_fifty = 0

            for emp in active_employees:
                rec = record_map.get((emp.display_name.lower(), ed))
                is_av = rec and rec.state in (
                    AvailabilityState.AVAILABLE_ALL_DAY,
                    AvailabilityState.AVAILABLE_WINDOW,
                )
                if is_av:
                    roles = set(
                        EmployeeRole.objects.filter(employee=emp, active=True).values_list(
                            "role__name", flat=True
                        )
                    )
                    if "Server" in roles:
                        av_servers += 1
                    if "Bartender" in roles:
                        av_bartenders += 1
                    if "Busser" in roles:
                        av_bussers += 1
                    if "50/50" in roles:
                        av_fifty += 1

            potential_shortage = (av_servers < 4) or (av_bartenders < 2) or (av_bussers < 1)

            capacity_summaries.append(
                DateCapacitySummary(
                    event_date=ed,
                    show_title=show_title,
                    available_servers=av_servers,
                    available_bartenders=av_bartenders,
                    available_bussers=av_bussers,
                    available_fifty_fifty=av_fifty,
                    potential_shortage=potential_shortage,
                )
            )

        # Special staff lists
        def get_staff_records(staff_name: str) -> list[NormalizedAvailabilityRecord]:
            return [
                rec
                for rec in records
                if rec.employee_name.lower() == staff_name.lower() and rec.date in event_dates
            ]

        olena_recs = get_staff_records("Olena")
        jackie_recs = get_staff_records("Jackie Pynn")
        yana_recs = get_staff_records("Yana")
        kate_recs = get_staff_records("Kate")

        return AvailabilityAnalysisSummary(
            sync_run=sync_run,
            total_requested=len(ROSTER_EMPLOYEE_NAMES),
            total_found=len(active_employees),
            total_combinations=total_combinations,
            known_combinations=known_cnt,
            unknown_combinations=unknown_cnt,
            completeness_pct=completeness_pct,
            matrix_cells=tuple(matrix_cells),
            capacity_summaries=tuple(capacity_summaries),
            olena_records=tuple(olena_recs),
            jackie_records=tuple(jackie_recs),
            yana_records=tuple(yana_recs),
            kate_records=tuple(kate_recs),
        )
