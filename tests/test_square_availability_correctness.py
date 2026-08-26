"""Regression tests for Square Availability normalization & eligibility."""

from datetime import date, time

import pytest
from django.core.management import call_command

from scheduling.integrations.square_availability.base import AvailabilityState
from scheduling.integrations.square_availability.browser_provider import (
    PlaywrightAvailabilityProvider,
)
from scheduling.integrations.square_availability.service import SquareAvailabilitySyncService
from scheduling.models import AvailabilityType, Employee
from scheduling.services.availability import LocalAvailabilityProvider


@pytest.fixture(autouse=True)
def seed_data(db):
    call_command("seed_spirit_staff", verbosity=0)
    call_command("seed_scheduling_config", verbosity=0)


@pytest.mark.django_db
def test_missing_availability_normalizes_to_unknown_never_all_day():
    """Verify missing availability normalizes to UNKNOWN and never AVAILABLE_ALL_DAY."""
    cal_provider = PlaywrightAvailabilityProvider()
    records = cal_provider.fetch_availability(date(2026, 9, 7), date(2026, 10, 3))

    brittany_recs = [r for r in records if r.employee_name == "Brittany James"]
    assert len(brittany_recs) == 27
    for r in brittany_recs:
        assert r.state == AvailabilityState.UNKNOWN
        assert r.state != AvailabilityState.AVAILABLE_ALL_DAY


@pytest.mark.django_db
def test_active_employee_does_not_imply_available():
    """Verify an active employee with no availability entered is INELIGIBLE."""
    service = SquareAvailabilitySyncService()
    service.execute_sync(date(2026, 9, 7), date(2026, 9, 13))

    montana = Employee.objects.get(display_name="Montana")
    local_provider = LocalAvailabilityProvider()

    # Monday 2026-09-07
    result = local_provider.check(montana, date(2026, 9, 7), time(17, 0), time(23, 0))
    assert not result.available
    assert result.availability_type == AvailabilityType.UNKNOWN


@pytest.mark.django_db
def test_explicit_all_day_neil_bobbitt():
    """Verify explicit All Day for Neil Bobbitt on Monday normalizes to AVAILABLE_ALL_DAY."""
    service = SquareAvailabilitySyncService()
    service.execute_sync(date(2026, 9, 7), date(2026, 9, 13))

    neil = Employee.objects.get(display_name="Neil Bobbit")
    local_provider = LocalAvailabilityProvider()

    # Monday 2026-09-07 is ALL_DAY
    res_mon = local_provider.check(neil, date(2026, 9, 7), time(15, 0), time(21, 0))
    assert res_mon.available
    assert res_mon.availability_type == AvailabilityType.AVAILABLE_ALL_DAY

    # Wednesday 2026-09-09 is 18:00–23:00 (Bartender 15:00-21:00 is INELIGIBLE)
    res_wed_bar = local_provider.check(neil, date(2026, 9, 9), time(15, 0), time(21, 0))
    assert not res_wed_bar.available

    # Saturday 2026-09-12 is UNKNOWN
    res_sat = local_provider.check(neil, date(2026, 9, 12), time(15, 0), time(21, 0))
    assert not res_sat.available
    assert res_sat.availability_type == AvailabilityType.UNKNOWN


@pytest.mark.django_db
def test_jackie_pynn_windows_and_missing():
    """Verify Jackie Pynn Wednesday 14:00-23:00 is AVAILABLE_WINDOW and Monday is UNKNOWN."""
    service = SquareAvailabilitySyncService()
    service.execute_sync(date(2026, 9, 7), date(2026, 9, 13))

    jackie = Employee.objects.get(display_name="Jackie Pynn")
    local_provider = LocalAvailabilityProvider()

    # Monday 2026-09-07: UNKNOWN
    res_mon = local_provider.check(jackie, date(2026, 9, 7), time(15, 0), time(21, 0))
    assert not res_mon.available
    assert res_mon.availability_type == AvailabilityType.UNKNOWN

    # Wednesday 2026-09-09: 14:00-23:00 (Covers Bartender 15:00-21:00)
    res_wed = local_provider.check(jackie, date(2026, 9, 9), time(15, 0), time(21, 0))
    assert res_wed.available
    assert res_wed.availability_type == AvailabilityType.AVAILABLE_WINDOW


@pytest.mark.django_db
def test_emily_daily_1730_2300():
    """
    Verify Emily Talbot daily window 17:30-23:00 covers Busser
    but NOT Server.
    """
    service = SquareAvailabilitySyncService()
    service.execute_sync(date(2026, 9, 7), date(2026, 9, 13))

    emily = Employee.objects.get(display_name="Emily")
    local_provider = LocalAvailabilityProvider()

    # Busser 18:00-21:30: Eligible
    res_bus = local_provider.check(emily, date(2026, 9, 12), time(18, 0), time(21, 30))
    assert res_bus.available

    # Server 17:00-23:00: Ineligible (starts 17:00 < 17:30)
    res_srv = local_provider.check(emily, date(2026, 9, 12), time(17, 0), time(23, 0))
    assert not res_srv.available


@pytest.mark.django_db
def test_joleen_daily_1600_2300():
    """Verify Joleen Dickson window 16:00-23:00 covers Server but NOT Lead Server."""
    service = SquareAvailabilitySyncService()
    service.execute_sync(date(2026, 9, 7), date(2026, 9, 13))

    joleen = Employee.objects.get(display_name="Joleen Dickson")
    local_provider = LocalAvailabilityProvider()

    # Server 17:00-23:00: Eligible
    res_srv = local_provider.check(joleen, date(2026, 9, 12), time(17, 0), time(23, 0))
    assert res_srv.available

    # Lead Server 15:00-21:30: Ineligible (starts 15:00 < 16:00)
    res_lead = local_provider.check(joleen, date(2026, 9, 12), time(15, 0), time(21, 30))
    assert not res_lead.available


@pytest.mark.django_db
def test_kate_weekday_and_weekend_windows():
    """Verify Kate's corrected weekday/weekend windows and her blank Fridays.

    Her Thursday entry reads 17:30-21:30 on the live Square Availability page; an earlier
    transcription recorded it as 05:30 and made her look available for afternoon setup she
    cannot actually work. Saturday was missing from that same transcription.
    """
    service = SquareAvailabilitySyncService()
    service.execute_sync(date(2026, 9, 7), date(2026, 9, 13))

    kate = Employee.objects.get(display_name="Kate")
    local_provider = LocalAvailabilityProvider()

    # Thursday 2026-09-10 is 17:30-21:30: covers the evening window exactly ...
    res_thu = local_provider.check(kate, date(2026, 9, 10), time(17, 30), time(21, 30))
    assert res_thu.available

    # ... but not a Lead Server shift that starts at 15:00.
    res_thu_lead = local_provider.check(kate, date(2026, 9, 10), time(15, 0), time(21, 30))
    assert not res_thu_lead.available

    # Friday 2026-09-11 is UNKNOWN - Kate has no Friday availability entered at all.
    res_fri = local_provider.check(kate, date(2026, 9, 11), time(17, 0), time(23, 0))
    assert not res_fri.available
    assert res_fri.availability_type == AvailabilityType.UNKNOWN

    # Saturday 2026-09-12 is 16:00-20:30: covers an early evening window ...
    res_sat = local_provider.check(kate, date(2026, 9, 12), time(16, 0), time(20, 30))
    assert res_sat.available

    # ... and still rules out a shift running to 23:00.
    res_sat_late = local_provider.check(kate, date(2026, 9, 12), time(17, 0), time(23, 0))
    assert not res_sat_late.available


@pytest.mark.django_db
def test_linda_weekend_only_availability():
    """Verify Linda Penney is available Saturday/Sunday 16:00-23:00 and UNKNOWN Mon-Fri."""
    service = SquareAvailabilitySyncService()
    service.execute_sync(date(2026, 9, 7), date(2026, 9, 13))

    linda = Employee.objects.get(display_name="Linda Penney")
    local_provider = LocalAvailabilityProvider()

    # Friday 2026-09-11: UNKNOWN
    res_fri = local_provider.check(linda, date(2026, 9, 11), time(17, 0), time(23, 0))
    assert not res_fri.available
    assert res_fri.availability_type == AvailabilityType.UNKNOWN

    # Saturday 2026-09-12: 16:00-23:00 (Covers Server 17:00-23:00)
    res_sat = local_provider.check(linda, date(2026, 9, 12), time(17, 0), time(23, 0))
    assert res_sat.available


@pytest.mark.django_db
def test_molly_evening_window_covers_on_call_but_not_a_full_server_shift():
    """Verify Molly Rittwage's 18:00-22:30 evening window is on-call shaped.

    Per management direction Molly is available every evening 18:00-22:30 and is
    specifically suited to on-call work: her window covers a doors-to-wrap on-call shift
    exactly, but not a confirmed Server shift that starts before doors or runs past wrap.
    """
    service = SquareAvailabilitySyncService()
    service.execute_sync(date(2026, 9, 7), date(2026, 9, 13))

    molly = Employee.objects.get(display_name="Molly Rittwage")
    local_provider = LocalAvailabilityProvider()

    # On-call runs exactly doors (18:30) to wrap (22:30) -> covered.
    res_on_call = local_provider.check(molly, date(2026, 9, 9), time(18, 30), time(22, 30))
    assert res_on_call.available

    # A confirmed Server shift starting at 17:00 is outside her window.
    res_srv = local_provider.check(molly, date(2026, 9, 9), time(17, 0), time(23, 0))
    assert not res_srv.available

    # Her window is every day of the week, so Saturday is available too.
    res_sat = local_provider.check(molly, date(2026, 9, 12), time(18, 0), time(21, 30))
    assert res_sat.available


@pytest.mark.django_db
def test_olena_daily_1500_2330():
    """Verify Olena Martynova daily 15:00-23:30 covers Lead Server and Server 17:00-23:00."""
    service = SquareAvailabilitySyncService()
    service.execute_sync(date(2026, 9, 7), date(2026, 9, 13))

    olena = Employee.objects.get(display_name="Olena")
    local_provider = LocalAvailabilityProvider()

    # Lead Server 15:00-21:30
    res_lead = local_provider.check(olena, date(2026, 9, 12), time(15, 0), time(21, 30))
    assert res_lead.available

    # Server 17:00-23:00
    res_srv = local_provider.check(olena, date(2026, 9, 12), time(17, 0), time(23, 0))
    assert res_srv.available


@pytest.mark.django_db
def test_yana_multiple_windows_do_not_merge():
    """Verify Yana's separate windows do NOT merge across gap (15:00-17:00)."""
    service = SquareAvailabilitySyncService()
    service.execute_sync(date(2026, 9, 7), date(2026, 9, 13))

    yana = Employee.objects.get(display_name="Yana")
    local_provider = LocalAvailabilityProvider()

    # Saturday 2026-09-12: Windows 10:00-15:00 AND 17:00-22:00
    # 50/50 18:00-21:30 -> ELIGIBLE (fits inside 17:00-22:00)
    res_5050 = local_provider.check(yana, date(2026, 9, 12), time(18, 0), time(21, 30))
    assert res_5050.available

    # Server 17:00-23:00 -> INELIGIBLE (ends at 22:00 < 23:00)
    res_srv = local_provider.check(yana, date(2026, 9, 12), time(17, 0), time(23, 0))
    assert not res_srv.available

    # Shift crossing gap 14:00-16:30 -> INELIGIBLE
    res_gap = local_provider.check(yana, date(2026, 9, 12), time(14, 0), time(16, 30))
    assert not res_gap.available


@pytest.mark.django_db
def test_khrystyna_office_and_theatre_windows_separate():
    """Verify Khrystyna's office (11:00-16:00) and theatre (18:00-23:00) windows remain separate."""
    service = SquareAvailabilitySyncService()
    service.execute_sync(date(2026, 9, 7), date(2026, 9, 13))

    khrystyna = Employee.objects.get(display_name="Khrystyna")
    local_provider = LocalAvailabilityProvider()

    # Busser 18:00-21:30 -> ELIGIBLE
    res_bus = local_provider.check(khrystyna, date(2026, 9, 8), time(18, 0), time(21, 30))
    assert res_bus.available

    # Afternoon shift 15:00-17:30 crossing gap -> INELIGIBLE
    res_gap = local_provider.check(khrystyna, date(2026, 9, 8), time(15, 0), time(17, 30))
    assert not res_gap.available


@pytest.mark.django_db
def test_no_availability_bartender_is_ineligible():
    """Verify bartenders with no availability set (e.g. Svitlana) are INELIGIBLE."""
    service = SquareAvailabilitySyncService()
    service.execute_sync(date(2026, 9, 7), date(2026, 9, 13))

    svitlana = Employee.objects.get(display_name="Svitlana")
    local_provider = LocalAvailabilityProvider()

    res = local_provider.check(svitlana, date(2026, 9, 12), time(17, 30), time(23, 0))
    assert not res.available
    assert res.availability_type == AvailabilityType.UNKNOWN


@pytest.mark.django_db
def test_yana_midnight_ending_window_1430_0000():
    """Verify Yana's 14:30-00:00 window covers Lead Server, Server, On-Call Server, 50/50."""
    service = SquareAvailabilitySyncService()
    service.execute_sync(date(2026, 9, 7), date(2026, 9, 13))

    yana = Employee.objects.get(display_name="Yana")
    local_provider = LocalAvailabilityProvider()

    # Thursday 2026-09-10 (Yana Mon-Thu is 14:30-00:00)
    # Lead Server 15:00-21:30 -> ELIGIBLE
    res_lead = local_provider.check(yana, date(2026, 9, 10), time(15, 0), time(21, 30))
    assert res_lead.available

    # Server 17:00-23:00 -> ELIGIBLE
    res_srv = local_provider.check(yana, date(2026, 9, 10), time(17, 0), time(23, 0))
    assert res_srv.available

    # On-Call Server 17:30-23:00 -> ELIGIBLE
    res_oncall = local_provider.check(yana, date(2026, 9, 10), time(17, 30), time(23, 0))
    assert res_oncall.available

    # 50/50 18:00-21:30 -> ELIGIBLE
    res_5050 = local_provider.check(yana, date(2026, 9, 10), time(18, 0), time(21, 30))
    assert res_5050.available


@pytest.mark.django_db
def test_genuinely_overnight_shift_timezone_aware():
    """Verify overnight shift (22:00-04:00) is covered by window (18:00-06:00)."""
    local_provider = LocalAvailabilityProvider()
    emp = Employee.objects.first()

    from scheduling.models import AvailabilityType, EmployeeAvailability
    EmployeeAvailability.objects.create(
        employee=emp,
        date=date(2026, 9, 12),
        availability_type=AvailabilityType.AVAILABLE_WINDOW,
        start_time=time(18, 0),
        end_time=time(6, 0),
    )

    res = local_provider.check(emp, date(2026, 9, 12), time(22, 0), time(4, 0))
    assert res.available
    assert res.availability_type == AvailabilityType.AVAILABLE_WINDOW


@pytest.mark.django_db
def test_completeness_sanity_check_13_dates():
    """Verify exact sanity check metric breakdown across 17 staff and 13 event dates."""
    service = SquareAvailabilitySyncService()
    event_dates = [
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
    summary = service.execute_sync(date(2026, 9, 7), date(2026, 10, 3), event_dates=event_dates)
    run = summary.sync_run

    # Counts reflect the corrected Kate transcription (Thursday 17:30 not 05:30, plus her
    # previously-missing Saturday) and Molly's management-directed every-evening window.
    assert run.total_employee_date_combinations == 221
    assert run.known_employee_date_combinations == 126
    assert run.unknown_employee_date_combinations == 95
    assert run.available_window_combinations == 117
    assert run.available_window_records == 138
    assert run.all_day_combinations == 7
    assert float(run.completeness_percentage) == 57.0
