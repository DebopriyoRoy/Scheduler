from datetime import datetime

from django.core.management.base import BaseCommand, CommandError

from scheduling.importers.calendar import SpiritCalendarImporter


class Command(BaseCommand):
    help = "Import Spirit public-calendar events without deleting manual shows."

    def add_arguments(self, parser):
        parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
        parser.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")

    def handle(self, *args, **options):
        try:
            start_date = datetime.strptime(options["start"], "%Y-%m-%d").date()
            end_date = datetime.strptime(options["end"], "%Y-%m-%d").date()
            summary = SpiritCalendarImporter().import_range(start_date, end_date)
        except (ValueError, OSError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            self.style.SUCCESS(
                f"Calendar import complete: {summary.created} created, "
                f"{summary.updated} updated from {len(summary.sources_checked)} pages."
            )
        )
