"""High-level calendar sync service.

Manages provider execution, completeness, and database persistence.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from django.db import transaction
from django.utils import timezone

from scheduling.integrations.spirit_calendar.api_provider import APICalendarProvider
from scheduling.integrations.spirit_calendar.base import NormalizedEventOccurrence
from scheduling.integrations.spirit_calendar.browser_provider import PlaywrightCalendarProvider
from scheduling.integrations.spirit_calendar.exceptions import (
    SpiritCalendarAPIError,
    SpiritCalendarError,
)
from scheduling.models import CalendarSyncRun, Show


@dataclass
class SyncPreviewRow:
    occurrence: NormalizedEventOccurrence
    existing_show: Show | None
    action: str  # CREATE, UPDATE, UNCHANGED, REVIEW


@dataclass
class SyncExecutionSummary:
    sync_run: CalendarSyncRun
    preview_rows: tuple[SyncPreviewRow, ...]


class SpiritCalendarSyncService:
    """Manages live calendar event extraction and idempotent database sync."""

    def __init__(self, api_provider=None, browser_provider=None):
        self.api_provider = api_provider or APICalendarProvider()
        self.browser_provider = browser_provider or PlaywrightCalendarProvider()

    def fetch_with_fallback(
        self, start_date: date, end_date: date, force_provider: str | None = None
    ) -> tuple[Sequence[NormalizedEventOccurrence], str, int, int]:
        """Fetches occurrences using primary API provider with Playwright browser fallback."""
        if force_provider == "PLAYWRIGHT":
            occurrences = self.browser_provider.fetch_occurrences(start_date, end_date)
            rendered_cnt = len(occurrences)
            extracted_cnt = len(occurrences)
            return occurrences, "PLAYWRIGHT", rendered_cnt, extracted_cnt

        if force_provider == "API_XHR":
            occurrences = self.api_provider.fetch_occurrences(start_date, end_date)
            return occurrences, "API_XHR", len(occurrences), len(occurrences)

        # Primary attempt: API_XHR
        try:
            occurrences = self.api_provider.fetch_occurrences(start_date, end_date)
            if occurrences:
                return occurrences, "API_XHR", len(occurrences), len(occurrences)
        except SpiritCalendarAPIError:
            pass  # Fall back to Playwright

        # Fallback attempt: Playwright Browser
        occurrences = self.browser_provider.fetch_occurrences(start_date, end_date)
        rendered_cnt = len(occurrences)
        extracted_cnt = len(occurrences)
        return occurrences, "PLAYWRIGHT", rendered_cnt, extracted_cnt

    def generate_preview(
        self, occurrences: Sequence[NormalizedEventOccurrence]
    ) -> list[SyncPreviewRow]:
        """Generates dry-run preview comparing extracted occurrences with local database Shows."""
        preview: list[SyncPreviewRow] = []

        for occ in occurrences:
            existing = Show.objects.filter(
                date=occ.date,
                external_id=occ.external_occurrence_id,
            ).first()

            if not existing:
                # Try matching by date and normalized title
                existing = Show.objects.filter(
                    date=occ.date,
                    title=occ.full_title,
                ).first()

            if not existing:
                action = "CREATE"
            else:
                # Check if fields changed
                is_changed = (
                    existing.title != occ.full_title
                    or existing.start_time != occ.start_time
                    or existing.end_time != occ.end_time
                    or existing.venue != occ.venue
                )
                action = "UPDATE" if is_changed else "UNCHANGED"

            preview.append(
                SyncPreviewRow(
                    occurrence=occ,
                    existing_show=existing,
                    action=action,
                )
            )

        return preview

    @transaction.atomic
    def execute_sync(
        self,
        start_date: date,
        end_date: date,
        force_provider: str | None = None,
        dry_run: bool = False,
    ) -> SyncExecutionSummary:
        """Executes live calendar sync with full provenance and completeness tracking."""
        sync_run = CalendarSyncRun.objects.create(
            source_url="https://spiritofnewfoundland.com/show-calendar/",
            provider=force_provider or "PLAYWRIGHT",
            start_date=start_date,
            end_date=end_date,
            status=CalendarSyncRun.SyncStatus.RUNNING,
        )

        try:
            occurrences, used_provider, rendered_cnt, extracted_cnt = self.fetch_with_fallback(
                start_date, end_date, force_provider=force_provider
            )

            sync_run.provider = used_provider
            sync_run.rendered_count = rendered_cnt
            sync_run.extracted_count = extracted_cnt
            sync_run.difference = rendered_cnt - extracted_cnt
            sync_run.events_received = len(occurrences)

            # Completeness check
            if sync_run.difference != 0:
                sync_run.status = CalendarSyncRun.SyncStatus.PARTIAL
                sync_run.notes = (
                    f"Completeness mismatch: Rendered={rendered_cnt}, Extracted={extracted_cnt}"
                )
            else:
                sync_run.status = CalendarSyncRun.SyncStatus.SUCCESS

            preview_rows = self.generate_preview(occurrences)

            created_cnt = 0
            updated_cnt = 0
            unchanged_cnt = 0

            if not dry_run:
                ext_ids = [row.occurrence.external_occurrence_id for row in preview_rows]
                Show.objects.filter(date__range=(start_date, end_date)).exclude(
                    external_id__in=ext_ids
                ).update(active=False)

                for row in preview_rows:
                    occ = row.occurrence
                    show, was_created = Show.objects.update_or_create(
                        date=occ.date,
                        external_id=occ.external_occurrence_id,
                        defaults={
                            "title": occ.full_title,
                            "start_time": occ.start_time,
                            "end_time": occ.end_time,
                            "venue": occ.venue,
                            "source": Show.Source.CALENDAR_IMPORT,
                            "source_url": occ.event_url,
                            "active": not occ.is_cancelled,
                            "requires_service_staff": True,
                        },
                    )
                    if was_created:
                        created_cnt += 1
                    elif row.action == "UPDATE":
                        updated_cnt += 1
                    else:
                        unchanged_cnt += 1

            sync_run.events_created = created_cnt
            sync_run.events_updated = updated_cnt
            sync_run.events_unchanged = unchanged_cnt
            sync_run.completed_at = timezone.now()
            sync_run.save()

            return SyncExecutionSummary(sync_run=sync_run, preview_rows=tuple(preview_rows))

        except Exception as exc:
            sync_run.status = CalendarSyncRun.SyncStatus.FAILED
            sync_run.error_message = str(exc)
            sync_run.completed_at = timezone.now()
            sync_run.save()
            raise SpiritCalendarError(f"Calendar sync failed: {exc}") from exc
