from typing import Any

from django.core.management.base import BaseCommand, CommandError

from scheduling.models import ScheduleRun
from scheduling.services.square_production_sync import (
    SquareProductionSyncError,
    sync_full_production_schedule,
)


class Command(BaseCommand):
    help = "Sync an approved local schedule to Square Production as unpublished draft shifts."

    def add_arguments(self, parser):
        parser.add_argument(
            "--schedule-id",
            type=int,
            required=True,
            help="ID of the approved ScheduleRun to sync.",
        )
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Execute the sync. Defaults to dry-run preview if omitted.",
        )
        parser.add_argument(
            "--confirmation-phrase",
            type=str,
            default="",
            help="Must equal 'CREATE SQUARE DRAFTS' when --confirm is supplied.",
        )

    def handle(self, *args, **options: Any):
        schedule_id = options["schedule_id"]
        try:
            schedule_run = ScheduleRun.objects.get(pk=schedule_id)
        except ScheduleRun.DoesNotExist:
            raise CommandError(f"ScheduleRun #{schedule_id} does not exist.") from None

        if not options["confirm"]:
            self.stdout.write(self.style.WARNING("DRY RUN ONLY."))
            self.stdout.write(
                "Run 'python manage.py square_sync_preview <id>' to view detailed classifications."
            )
            self.stdout.write(
                "To sync Production drafts, append --confirm --confirmation-phrase "
                "\"CREATE SQUARE DRAFTS\"."
            )

            return

        try:
            result = sync_full_production_schedule(
                schedule_run,
                confirmation_phrase=options["confirmation_phrase"],
            )
        except SquareProductionSyncError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"SUCCESS: Synced {result['created_count']} draft shift(s) to Square Production."
            )
        )
        if result["failed_count"] > 0:
            self.stdout.write(
                self.style.ERROR(
                    f"WARNING: {result['failed_count']} shift(s) failed to sync. "
                    "Review logs for details."
                )
            )

