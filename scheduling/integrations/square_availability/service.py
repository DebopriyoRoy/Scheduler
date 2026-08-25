"""Service layer managing Square Production availability.

Handles availability fetching, completeness, and audit tracking.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, time, timedelta
from decimal import Decimal

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

        # Build lookup map: (employee_name, record_date) -> list[NormalizedAvailabilityRecord]
        record_map: dict[tuple[str, date], list[NormalizedAvailabilityRecord]] = defaultdict(list)
        for rec in records:
            record_map[(rec.employee_name.lower(), rec.date)].append(rec)

        # Resolve live event dates if not provided
        if not event_dates:
            event_dates = list(
                Show.objects.filter(date__range=(start_date, end_date), active=True)
                .values_list("date", flat=True)
                .distinct()
                .order_by("date")
            )
            if not event_dates:
                curr = start_date
                while curr <= end_date:
                    event_dates.append(curr)
                    curr += timedelta(days=1)

        total_combinations = len(active_employees) * len(event_dates)
        all_day_cnt = 0
        window_comb_cnt = 0
        window_rec_cnt = 0
        unavailable_cnt = 0
        unknown_cnt = 0

        matrix_cells: list[AvailabilityMatrixCell] = []

        # Upsert live availability into EmployeeAvailability model table
        from scheduling.models import AvailabilityType, EmployeeAvailability
        from scheduling.services.availability import LocalAvailabilityProvider

        for emp in active_employees:
            for ed in event_dates:
                EmployeeAvailability.objects.filter(employee=emp, date=ed).delete()
                recs = record_map.get((emp.display_name.lower(), ed), [])

                if not recs:
                    unknown_cnt += 1
                    EmployeeAvailability.objects.create(
                        employee=emp,
                        date=ed,
                        availability_type=AvailabilityType.UNKNOWN,
                        source="LIVE_SQUARE_PRODUCTION",
                    )
                    matrix_cells.append(
                        AvailabilityMatrixCell(
                            employee_name=emp.display_name,
                            event_date=ed,
                            record=None,
                            is_known=False,
                            state_display="UNKNOWN",
                        )
                    )
                else:
                    has_all_day = any(r.state == AvailabilityState.AVAILABLE_ALL_DAY for r in recs)
                    has_window = any(r.state == AvailabilityState.AVAILABLE_WINDOW for r in recs)
                    has_unavail = any(r.state == AvailabilityState.UNAVAILABLE for r in recs)

                    if has_all_day:
                        all_day_cnt += 1
                    elif has_window:
                        window_comb_cnt += 1
                        window_rec_cnt += len(recs)
                    elif has_unavail:
                        unavailable_cnt += 1
                    else:
                        unknown_cnt += 1

                    for rec in recs:
                        is_known = rec.state != AvailabilityState.UNKNOWN
                        if rec.state == AvailabilityState.AVAILABLE_ALL_DAY:
                            av_type = AvailabilityType.AVAILABLE_ALL_DAY
                            st, et = None, None
                            state_disp = "AVAILABLE_ALL_DAY"
                        elif rec.state == AvailabilityState.AVAILABLE_WINDOW:
                            av_type = AvailabilityType.AVAILABLE_WINDOW
                            st, et = rec.start_time, rec.end_time
                            if rec.start_time and rec.end_time:
                                state_disp = f"{rec.start_time:%H:%M}–{rec.end_time:%H:%M}"
                            else:
                                state_disp = "AVAILABLE_WINDOW"
                        elif rec.state == AvailabilityState.UNAVAILABLE:
                            av_type = AvailabilityType.UNAVAILABLE
                            st, et = None, None
                            state_disp = "UNAVAILABLE"
                        else:
                            av_type = AvailabilityType.UNKNOWN
                            st, et = None, None
                            state_disp = "UNKNOWN"

                        EmployeeAvailability.objects.create(
                            employee=emp,
                            date=ed,
                            availability_type=av_type,
                            start_time=st,
                            end_time=et,
                            source="LIVE_SQUARE_PRODUCTION",
                        )

                        matrix_cells.append(
                            AvailabilityMatrixCell(
                                employee_name=emp.display_name,
                                event_date=ed,
                                record=rec,
                                is_known=is_known,
                                state_display=state_disp,
                            )
                        )

        known_combinations = all_day_cnt + window_comb_cnt + unavailable_cnt
        if total_combinations > 0:
            pct_val = round((known_combinations / total_combinations) * 100.0, 1)
            completeness_pct = Decimal(str(pct_val))
        else:
            completeness_pct = Decimal("100.0")

        sync_run.total_employee_date_combinations = total_combinations
        sync_run.known_employee_date_combinations = known_combinations
        sync_run.unknown_employee_date_combinations = unknown_cnt
        sync_run.available_window_combinations = window_comb_cnt
        sync_run.available_window_records = window_rec_cnt
        sync_run.all_day_combinations = all_day_cnt
        sync_run.unavailable_combinations = unavailable_cnt
        sync_run.completeness_percentage = completeness_pct

        sync_run.unknown_count = unknown_cnt
        sync_run.status = "SUCCESS" if unknown_cnt == 0 else "PARTIAL"
        sync_run.completed_at = timezone.now()
        sync_run.save()

        # Compute slot-aware role capacity for each event date
        local_av_provider = LocalAvailabilityProvider()
        capacity_summaries: list[DateCapacitySummary] = []
        for ed in event_dates:
            show = Show.objects.filter(date=ed, active=True).first()
            show_title = show.title if show else "Show Date"

            lead_servers = 0
            servers_17_23 = 0
            on_call_servers = 0
            bartenders_15_21 = 0
            on_call_bartenders = 0
            bussers_18_2130 = 0
            fifty_fifty_18_2130 = 0

            for emp in active_employees:
                roles = set(
                    EmployeeRole.objects.filter(employee=emp, active=True).values_list(
                        "role__name", flat=True
                    )
                )

                if "Server" in roles:
                    if local_av_provider.check(emp, ed, time(15, 0), time(21, 30)).available:
                        lead_servers += 1
                    if local_av_provider.check(emp, ed, time(17, 0), time(23, 0)).available:
                        servers_17_23 += 1
                    if local_av_provider.check(emp, ed, time(17, 30), time(23, 0)).available:
                        on_call_servers += 1

                if "Bartender" in roles:
                    if local_av_provider.check(emp, ed, time(15, 0), time(21, 0)).available:
                        bartenders_15_21 += 1
                    if local_av_provider.check(emp, ed, time(17, 30), time(23, 0)).available:
                        on_call_bartenders += 1

                if "Busser" in roles:
                    if local_av_provider.check(emp, ed, time(18, 0), time(21, 30)).available:
                        bussers_18_2130 += 1

                if "50/50" in roles:
                    if local_av_provider.check(emp, ed, time(18, 0), time(21, 30)).available:
                        fifty_fifty_18_2130 += 1

            potential_shortage = (servers_17_23 < 2) or (bartenders_15_21 < 1)

            capacity_summaries.append(
                DateCapacitySummary(
                    event_date=ed,
                    show_title=show_title,
                    available_servers=servers_17_23,
                    available_bartenders=bartenders_15_21,
                    available_bussers=bussers_18_2130,
                    available_fifty_fifty=fifty_fifty_18_2130,
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
            known_combinations=known_combinations,
            unknown_combinations=unknown_cnt,
            completeness_pct=completeness_pct,
            matrix_cells=tuple(matrix_cells),
            capacity_summaries=tuple(capacity_summaries),
            olena_records=tuple(olena_recs),
            jackie_records=tuple(jackie_recs),
            yana_records=tuple(yana_recs),
            kate_records=tuple(kate_recs),
        )
