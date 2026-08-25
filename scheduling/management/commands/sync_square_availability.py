"""Management command to read and validate Square Production employee availability."""

from datetime import date

from django.core.management.base import BaseCommand, CommandError

from scheduling.integrations.square_availability.service import SquareAvailabilitySyncService


class Command(BaseCommand):
    help = "Reads and validates Square Production employee availability in a READ ONLY mode."

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

    def handle(self, *args, **options):
        try:
            start_date = date.fromisoformat(options["start"])
            end_date = date.fromisoformat(options["end"])
        except ValueError as exc:
            raise CommandError(f"Invalid date format. Use YYYY-MM-DD. Error: {exc}") from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"Starting Square Availability Fetch for {start_date} to {end_date}..."
            )
        )

        service = SquareAvailabilitySyncService()

        try:
            summary = service.execute_sync(start_date=start_date, end_date=end_date)
            run = summary.sync_run

            self.stdout.write(
                self.style.SUCCESS(
                    f"\nSQUARE AVAILABILITY SYNC COMPLETED ({run.status})\n"
                    f"Environment: {run.environment}\n"
                    f"Provider: {run.provider}\n"
                    f"Employees Requested: {summary.total_requested}\n"
                    f"Employees Mapped/Found: {summary.total_found}\n"
                    f"Total Combinations: {summary.total_combinations}\n"
                    f"Known Combinations: {summary.known_combinations}\n"
                    f"Unknown Combinations: {summary.unknown_combinations}\n"
                    f"Completeness: {summary.completeness_pct}%\n"
                )
            )

        except Exception as exc:
            raise CommandError(f"Square Availability Command Failed: {exc}") from exc
