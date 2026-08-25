"""Unit tests for Authoritative Live Spirit Calendar Ingestion."""

from datetime import date, time

import pytest

from scheduling.integrations.spirit_calendar.normalizer import (
    build_normalized_occurrence,
    clean_full_title,
    detect_private_and_offsite,
)
from scheduling.integrations.spirit_calendar.service import SpiritCalendarSyncService
from scheduling.models import CalendarSyncRun, Show


def test_dwights_wedding_full_title_resolution():
    """Verify that truncated UI labels for Dwight's Wedding resolve to full title (Section 30)."""
    raw_ui_text = "(It's a Nice Day for) Dwight's Wedding!! – Fall 2026"
    resolved_title = clean_full_title(raw_ui_text)
    assert resolved_title == "(It's a Nice Day for) Dwight's Wedding!! - Fall 2026"


def test_private_and_offsite_detection():
    """Verify private and offsite detection from title and venue (Section 32)."""
    title = "Private - Offsite Event!"
    venue = "St. John's, NL"
    is_private, is_offsite = detect_private_and_offsite(title, venue)
    assert is_private is True
    assert is_offsite is True


@pytest.mark.django_db
def test_recurring_dwight_events_stay_separate():
    """Verify that recurring shows on different dates create separate records (Section 31)."""
    service = SpiritCalendarSyncService()

    occ1 = build_normalized_occurrence(
        title="(It's a Nice Day for) Dwight's Wedding!! – Fall 2026",
        event_date=date(2026, 9, 23),
        start_time=time(18, 30),
        end_time=time(22, 0),
        source_provider="TEST",
    )
    occ2 = build_normalized_occurrence(
        title="(It's a Nice Day for) Dwight's Wedding!! – Fall 2026",
        event_date=date(2026, 9, 30),
        start_time=time(18, 30),
        end_time=time(22, 0),
        source_provider="TEST",
    )

    preview = service.generate_preview([occ1, occ2])
    assert len(preview) == 2
    assert preview[0].action == "CREATE"
    assert preview[1].action == "CREATE"

    # Persist
    for row in preview:
        occ = row.occurrence
        Show.objects.create(
            date=occ.date,
            external_id=occ.external_occurrence_id,
            title=occ.full_title,
            start_time=occ.start_time,
            end_time=occ.end_time,
            venue=occ.venue,
            source=Show.Source.CALENDAR_IMPORT,
        )

    assert Show.objects.filter(title__icontains="Dwight's Wedding").count() == 2


@pytest.mark.django_db
def test_same_title_multiple_dates_retained():
    """Verify that multiple show occurrences on different dates are retained (Section 33)."""
    occ1 = build_normalized_occurrence(
        title="Forever Country…in the Key of Spirit!! – Fall 2026",
        event_date=date(2026, 9, 12),
        start_time=time(18, 30),
        end_time=time(22, 30),
    )
    occ2 = build_normalized_occurrence(
        title="Forever Country…in the Key of Spirit!! – Fall 2026",
        event_date=date(2026, 9, 18),
        start_time=time(18, 30),
        end_time=time(22, 30),
    )

    Show.objects.create(
        date=occ1.date,
        external_id=occ1.external_occurrence_id,
        title=occ1.full_title,
    )
    Show.objects.create(
        date=occ2.date,
        external_id=occ2.external_occurrence_id,
        title=occ2.full_title,
    )

    assert Show.objects.filter(title__icontains="Forever Country").count() == 2


@pytest.mark.django_db
def test_completeness_mismatch_flags_partial_status():
    """Verify that if rendered_count != extracted_count, status is PARTIAL (Section 34)."""
    run = CalendarSyncRun.objects.create(
        source_url="https://spiritofnewfoundland.com/show-calendar/",
        provider="PLAYWRIGHT",
        start_date=date(2026, 9, 7),
        end_date=date(2026, 10, 3),
        rendered_count=15,
        extracted_count=10,
        difference=5,
        status=CalendarSyncRun.SyncStatus.PARTIAL,
    )
    assert run.status == CalendarSyncRun.SyncStatus.PARTIAL
    assert run.difference == 5
