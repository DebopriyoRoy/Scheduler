"""Management command to sync authoritative Spirit live show calendar events."""

from datetime import date

from django.core.management.base import BaseCommand, CommandError

from scheduling.integrations.spirit_calendar.service import SpiritCalendarSyncService


class Command(BaseCommand):
    help = "Syncs event occurrences directly from authoritative live Spirit show calendar."

    def add_arguments(self, parser):
        parser.add_argument(
            "--start",
            type=str,
            default="2026-09-07",
            help="Start date (YYYY-MM-DD), default: 2026-09-07",
        )
        parser.add_argument(
            "--end",
            type=str,
            default="2026-10-03",
            help="End date (YYYY-MM-DD), default: 2026-10-03",
        )
        parser.add_argument(
            "--provider",
            type=str,
            choices=["API_XHR", "PLAYWRIGHT"],
            default=None,
            help="Force specific provider engine",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Preview sync without saving to database",
        )

    def handle(self, *args, **options):
        try:
            start_date = date.fromisoformat(options["start"])
            end_date = date.fromisoformat(options["end"])
        except ValueError as exc:
            raise CommandError(f"Invalid date format. Use YYYY-MM-DD. Error: {exc}") from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"Starting Live Calendar Sync for {start_date} to {end_date}..."
            )
        )

        service = SpiritCalendarSyncService()

        try:
            summary = service.execute_sync(
                start_date=start_date,
                end_date=end_date,
                force_provider=options.get("provider"),
                dry_run=options.get("dry_run"),
            )

            run = summary.sync_run

            self.stdout.write(
                self.style.SUCCESS(
                    f"\nCALENDAR SYNC COMPLETED ({run.status})\n"
                    f"Provider: {run.provider}\n"
                    f"Rendered Count: {run.rendered_count}\n"
                    f"Extracted Count: {run.extracted_count}\n"
                    f"Difference: {run.difference}\n"
                    f"Events Created: {run.events_created}\n"
                    f"Events Updated: {run.events_updated}\n"
                    f"Events Unchanged: {run.events_unchanged}\n"
                )
            )

            for row in summary.preview_rows:
                occ = row.occurrence
                self.stdout.write(
                    f"[{row.action}] {occ.date} ({occ.date.strftime('%a')}) | {occ.full_title} | "
                    f"{occ.start_time.strftime('%H:%M')}–{occ.end_time.strftime('%H:%M')} | "
                    f"Venue: {occ.venue} | Private: {occ.is_private} | Offsite: {occ.is_offsite}"
                )

        except Exception as exc:
            raise CommandError(f"Calendar Sync Command Failed: {exc}") from exc
