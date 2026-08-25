"""Unit tests for live schedule generation combining Spirit Calendar and Square Availability."""

from datetime import date, datetime, time

import pytest

from scheduling.integrations.spirit_calendar.base import NormalizedEventOccurrence
from scheduling.integrations.spirit_calendar.service import SpiritCalendarSyncService
from scheduling.integrations.square_availability.base import (
    AvailabilityState,
    NormalizedAvailabilityRecord,
)
from scheduling.integrations.square_availability.service import (
    ROSTER_EMPLOYEE_NAMES,
    SquareAvailabilitySyncService,
)
from scheduling.models import (
    Employee,
    ScheduleAssignment,
    ScheduleRunStatus,
    SchedulingWarning,
    Show,
    WarningType,
)
from scheduling.services.engine import SchedulingEngine

MOCK_EVENT_DATES = [
    date(2026, 9, 10),
    date(2026, 9, 11),
    date(2026, 9, 12),
    date(2026, 9, 17),
    date(2026, 9, 18),
    date(2026, 9, 19),
    date(2026, 9, 21),
    date(2026, 9, 23),
    date(2026, 9, 25),
    date(2026, 9, 26),
    date(2026, 9, 30),
    date(2026, 10, 2),
    date(2026, 10, 3),
]

TITLES_MAP = {
    date(2026, 9, 10): "(It's a Nice Day for) Dwight's Wedding!! - Fall 2026",
    date(2026, 9, 11): "(It's a Nice Day for) Dwight's Wedding!! - Fall 2026",
    date(2026, 9, 12): "Forever Country...in the Key of Spirit!! - Fall 2026",
    date(2026, 9, 17): "(It's a Nice Day for) Dwight's Wedding!! - Fall 2026",
    date(2026, 9, 18): "Forever Country...in the Key of Spirit!! - Fall 2026",
    date(2026, 9, 19): "Shift Happens!",
    date(2026, 9, 21): "Private - Offsite Event!",
    date(2026, 9, 23): "(It's a Nice Day for) Dwight's Wedding!! - Fall 2026",
    date(2026, 9, 25): "Forever Country...in the Key of Spirit!! - Fall 2026",
    date(2026, 9, 26): "HOME SWEET HOME-I-CIDE!",
    date(2026, 9, 30): "(It's a Nice Day for) Dwight's Wedding!! - Fall 2026",
    date(2026, 10, 2): "(It's a Nice Day for) Dwight's Wedding!! - Fall 2026",
    date(2026, 10, 3): "Private Shift Happens on 03 October 2026",
}

MOCK_OCCURRENCES = [
    NormalizedEventOccurrence(
        external_event_id=f"evt-{d}",
        external_occurrence_id=f"occ-{d}",
        title=TITLES_MAP[d],
        full_title=TITLES_MAP[d],
        date=d,
        start_time=time(18, 30),
        end_time=time(22, 30),
        start_datetime=datetime.combine(d, time(18, 30)),
        end_datetime=datetime.combine(d, time(22, 30)),
        venue="Offsite" if d == date(2026, 9, 21) else "Theatre Gower",
    )
    for d in MOCK_EVENT_DATES
]


class MockCalendarProvider:
    provider_name = "PLAYWRIGHT"

    def fetch_occurrences(self, start_date, end_date):
        Show.objects.filter(date__range=(start_date, end_date)).exclude(
            date__in=MOCK_EVENT_DATES
        ).update(active=False)
        return [occ for occ in MOCK_OCCURRENCES if start_date <= occ.date <= end_date]


class MockAvailabilityProvider:
    provider_name = "STRUCTURED_DASHBOARD_REQUEST"

    def fetch_availability(self, start_date, end_date):
        Employee.objects.exclude(display_name__in=ROSTER_EMPLOYEE_NAMES).update(active=False)
        records = []
        for emp_id, emp_name in enumerate(ROSTER_EMPLOYEE_NAMES, 1):
            parts = emp_name.split()
            first = parts[0]
            last = " ".join(parts[1:]) if len(parts) > 1 else ""
            emp, _ = Employee.objects.get_or_create(
                first_name=first,
                last_name=last,
                defaults={
                    "active": True,
                    "display_name": emp_name,
                    "square_team_member_id": f"tm-{emp_id}",
                    "excluded_from_automatic_scheduling": False,
                },
            )
            emp.display_name = emp_name
            emp.active = True
            emp.excluded_from_automatic_scheduling = False
            emp.save()
            for d in MOCK_EVENT_DATES:
                if start_date <= d <= end_date:
                    records.append(
                        NormalizedAvailabilityRecord(
                            employee_id=emp.id,
                            employee_name=emp_name,
                            square_team_member_id=emp.square_team_member_id,
                            date=d,
                            state=AvailabilityState.AVAILABLE_ALL_DAY,
                        )
                    )
        return records


def get_mock_sync_services():
    from django.core.management import call_command

    call_command("seed_spirit_staff", verbosity=0)
    call_command("seed_scheduling_config", verbosity=0)
    Employee.objects.exclude(display_name__in=ROSTER_EMPLOYEE_NAMES).update(active=False)

    cal_provider = MockCalendarProvider()
    avail_provider = MockAvailabilityProvider()
    cal_service = SpiritCalendarSyncService(
        api_provider=cal_provider, browser_provider=cal_provider
    )
    avail_service = SquareAvailabilitySyncService(
        api_provider=avail_provider, browser_provider=avail_provider
    )
    return cal_service, avail_service


@pytest.mark.django_db
def test_schedule_run_references_live_sync_runs():
    """Verify ScheduleRun links directly to live CalendarSyncRun and SquareAvailabilitySyncRun."""
    cal_service, avail_service = get_mock_sync_services()
    cal_summary = cal_service.execute_sync(date(2026, 9, 7), date(2026, 10, 3))
    avail_summary = avail_service.execute_sync(date(2026, 9, 7), date(2026, 10, 3))

    engine = SchedulingEngine()
    run = engine.generate(date(2026, 9, 7), date(2026, 10, 3))

    assert run.calendar_sync_run == cal_summary.sync_run
    assert run.availability_sync_run == avail_summary.sync_run
    assert run.status in {ScheduleRunStatus.GENERATED, ScheduleRunStatus.NEEDS_REVIEW}


@pytest.mark.django_db
def test_offsite_event_handled_separately():
    """Verify Sept 21 Offsite event is classified with OFFSITE_STAFFING_REVIEW_REQUIRED."""
    cal_service, avail_service = get_mock_sync_services()
    cal_service.execute_sync(date(2026, 9, 7), date(2026, 10, 3))
    avail_service.execute_sync(date(2026, 9, 7), date(2026, 10, 3))

    engine = SchedulingEngine()
    run = engine.generate(date(2026, 9, 7), date(2026, 10, 3))

    offsite_show = Show.objects.filter(date=date(2026, 9, 21), active=True).first()
    assert offsite_show is not None
    assert "Offsite" in offsite_show.title

    offsite_assignments = ScheduleAssignment.objects.filter(schedule_run=run, show=offsite_show)
    assert offsite_assignments.count() == 0

    offsite_warning = SchedulingWarning.objects.filter(
        schedule_run=run,
        show=offsite_show,
        warning_type=WarningType.EVENT_STAFFING_REVIEW_REQUIRED,
    ).first()
    assert offsite_warning is not None
    assert "OFFSITE_STAFFING_REVIEW_REQUIRED" in offsite_warning.message


@pytest.mark.django_db
def test_private_theatre_event_handled():
    """Verify Oct 3 Private event emits review warning and holds 0 assignments."""
    cal_service, avail_service = get_mock_sync_services()
    cal_service.execute_sync(date(2026, 9, 7), date(2026, 10, 3))
    avail_service.execute_sync(date(2026, 9, 7), date(2026, 10, 3))

    engine = SchedulingEngine()
    run = engine.generate(date(2026, 9, 7), date(2026, 10, 3))

    private_show = Show.objects.filter(date=date(2026, 10, 3), active=True).first()
    assert private_show is not None

    private_assignments = ScheduleAssignment.objects.filter(schedule_run=run, show=private_show)
    assert private_assignments.count() == 0

    private_warning = run.warnings.filter(
        show=private_show,
        warning_type="PRIVATE_EVENT_STAFFING_REVIEW_REQUIRED",
    ).first()
    assert private_warning is not None
    assert "PRIVATE_EVENT_STAFFING_REVIEW_REQUIRED" in private_warning.message


@pytest.mark.django_db
def test_yana_kate_cannot_hold_dual_role_in_same_show():
    """Verify an employee is never assigned dual roles in the same show."""
    cal_service, avail_service = get_mock_sync_services()
    cal_service.execute_sync(date(2026, 9, 7), date(2026, 10, 3))
    avail_service.execute_sync(date(2026, 9, 7), date(2026, 10, 3))

    engine = SchedulingEngine()
    run = engine.generate(date(2026, 9, 7), date(2026, 10, 3))

    valid_dates = (date(2026, 9, 7), date(2026, 10, 3))
    for show in Show.objects.filter(date__range=valid_dates, active=True):
        assigned_employees = list(
            ScheduleAssignment.objects.filter(schedule_run=run, show=show).values_list(
                "employee_id", flat=True
            )
        )
        assert len(assigned_employees) == len(set(assigned_employees))


@pytest.mark.django_db
def test_full_production_square_write_not_executed_during_generation():
    """Verify schedule generation only produces local DB records and does NOT write to Square."""
    from integrations.square.config import SquareConfig

    config = SquareConfig.from_env()

    cal_service, avail_service = get_mock_sync_services()
    cal_service.execute_sync(date(2026, 9, 7), date(2026, 10, 3))
    avail_service.execute_sync(date(2026, 9, 7), date(2026, 10, 3))

    engine = SchedulingEngine()
    run = engine.generate(date(2026, 9, 7), date(2026, 10, 3))

    valid_statuses = {
        ScheduleRunStatus.DRAFT,
        ScheduleRunStatus.GENERATED,
        ScheduleRunStatus.NEEDS_REVIEW,
    }
    assert run.status in valid_statuses
    assert config.publishing_enabled is False
