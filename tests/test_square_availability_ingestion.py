"""Unit tests for Square Production employee availability integration."""

from datetime import date, time

import pytest

from scheduling.integrations.square_availability.base import (
    AvailabilityState,
)
from scheduling.integrations.square_availability.normalizer import build_normalized_record
from scheduling.integrations.square_availability.service import SquareAvailabilitySyncService
from scheduling.models import SquareAvailabilitySyncRun


def test_availability_shift_coverage_eligibility():
    """Verify full shift coverage eligibility and partial overlap rejection (Section 16)."""
    # 1. Available all day -> eligible
    rec_all_day = build_normalized_record(
        employee_id=1,
        employee_name="Jackie Pynn",
        square_team_member_id="TM123",
        record_date=date(2026, 9, 12),
        state=AvailabilityState.AVAILABLE_ALL_DAY,
    )
    assert rec_all_day.is_eligible_for_shift(time(17, 0), time(23, 0)) is True

    # 2. Available window 14:00 - 23:30 -> shift 17:00 - 23:00 is eligible
    rec_window = build_normalized_record(
        employee_id=1,
        employee_name="Jackie Pynn",
        square_team_member_id="TM123",
        record_date=date(2026, 9, 12),
        state=AvailabilityState.AVAILABLE_WINDOW,
        start_time=time(14, 0),
        end_time=time(23, 30),
    )
    assert rec_window.is_eligible_for_shift(time(17, 0), time(23, 0)) is True

    # 3. Partial overlap: window 18:00 - 22:00 -> shift 17:00 - 23:00 is NOT eligible
    rec_partial = build_normalized_record(
        employee_id=1,
        employee_name="Jackie Pynn",
        square_team_member_id="TM123",
        record_date=date(2026, 9, 12),
        state=AvailabilityState.AVAILABLE_WINDOW,
        start_time=time(18, 0),
        end_time=time(22, 0),
    )
    assert rec_partial.is_eligible_for_shift(time(17, 0), time(23, 0)) is False


def test_unknown_availability_is_not_eligible():
    """Verify CRITICAL UNKNOWN RULE: UNKNOWN does NOT mean available (Section 11)."""
    rec_unknown = build_normalized_record(
        employee_id=1,
        employee_name="Jackie Pynn",
        square_team_member_id="TM123",
        record_date=date(2026, 9, 12),
        state=AvailabilityState.UNKNOWN,
    )
    assert rec_unknown.is_eligible_for_shift(time(17, 0), time(23, 0)) is False


@pytest.mark.django_db
def test_square_availability_sync_provenance_and_audit():
    """Verify read-only availability sync and provenance tracking.

    The roster is seeded here on purpose. total_requested used to be the length of a
    hardcoded name list, so this assertion held at seventeen even against an empty
    database - it was checking a constant, not a sync. It now counts the staff who
    actually hold a role, so the seed is what makes the number mean anything.
    """
    from django.core.management import call_command

    call_command("seed_spirit_staff")

    service = SquareAvailabilitySyncService()
    summary = service.execute_sync(date(2026, 9, 7), date(2026, 10, 3))

    assert summary.sync_run.environment == "PRODUCTION"
    assert summary.total_requested == 18

    # Completeness is a real measurement now. It read 100% before only because an
    # empty roster made the denominator zero - the fixture provider has never held
    # hours for every member of staff, and saying so is the entire point of the
    # figure.
    assert 0 < summary.completeness_pct < 100
    assert summary.unknown_combinations > 0
    assert (
        summary.known_combinations + summary.unknown_combinations
        == summary.total_combinations
    )
    assert SquareAvailabilitySyncRun.objects.count() >= 1
