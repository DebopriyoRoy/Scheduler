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
        parser.add_argument(
            "--live",
            action="store_true",
            help="Read Square itself using the stored dashboard session.",
        )
        parser.add_argument(
            "--all-dates",
            action="store_true",
            help="Refresh every date in the range, not only show dates.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit a single machine-readable result line for the application.",
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

        service = (
            SquareAvailabilitySyncService.with_live_provider()
            if options.get("live")
            else SquareAvailabilitySyncService()
        )
        event_dates = None
        if options.get("all_dates"):
            # Only the dates a sync touches get refreshed, so a show-dates-only run
            # leaves everything else holding whatever it held before.
            import datetime as _dt

            span = (end_date - start_date).days
            event_dates = [start_date + _dt.timedelta(days=i) for i in range(span + 1)]

        try:
            summary = service.execute_sync(
                start_date=start_date, end_date=end_date, event_dates=event_dates
            )
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

            if options.get("json"):
                self._emit_json(summary, service)

        except Exception as exc:
            if options.get("json"):
                self._emit_json(None, service, error=str(exc))
                return
            raise CommandError(f"Square Availability Command Failed: {exc}") from exc

    def _emit_json(self, summary, service, error: str = "") -> None:
        """One parseable line for scheduling.services.square_pull to read back."""
        import json

        from scheduling.services.square_pull import AVAILABILITY_MARKER

        if error:
            payload = {"error": error}
        else:
            payload = {
                "provider": service.browser_provider.provider_name,
                "live": bool(getattr(service.browser_provider, "is_live", False)),
                "total": summary.total_combinations,
                "known": summary.known_combinations,
                "unknown": summary.unknown_combinations,
                "completeness": float(summary.completeness_pct),
                "unmatched": list(getattr(service.browser_provider, "unmatched_names", []))[:40],
            }
        self.stdout.write(AVAILABILITY_MARKER + json.dumps(payload))
