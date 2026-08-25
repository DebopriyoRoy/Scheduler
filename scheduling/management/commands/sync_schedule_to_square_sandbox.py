from typing import Any

from django.core.management.base import BaseCommand, CommandError

from scheduling.models import ScheduleRun
from scheduling.services.square_sync import (
    SquareSyncError,
    sync_schedule_to_sandbox,
    validate_schedule_for_sync,
)


class Command(BaseCommand):
    help = "Validate and sync an approved local schedule version to Square Sandbox draft shifts."

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
            help=(
                "Execute the sync. Without this flag, the command executes a "
                "dry-run validation preview."
            ),
        )

    def handle(self, *args, **options: Any):
        schedule_id = options["schedule_id"]
        try:
            schedule_run = ScheduleRun.objects.get(pk=schedule_id)
        except ScheduleRun.DoesNotExist:
            raise CommandError(f"ScheduleRun with ID {schedule_id} does not exist.") from None

        validation = validate_schedule_for_sync(schedule_run)

        self.stdout.write(f"Schedule Run ID: #{schedule_run.id}")
        self.stdout.write(f"Schedule Period: {schedule_run.start_date} to {schedule_run.end_date}")
        self.stdout.write(f"Status: {schedule_run.get_status_display()}")
        self.stdout.write(
            f"Target Square Location: {validation.location_name} "
            f"({validation.location_id or 'NOT FOUND'})"
        )
        self.stdout.write(
            f"Total Assignments to Sync: {len(validation.assignments_payload)}"
        )

        if validation.errors:
            self.stdout.write(self.style.ERROR("\nValidation Errors:"))
            for err in validation.errors:
                self.stdout.write(self.style.ERROR(f"  - {err}"))

        if validation.warnings:
            self.stdout.write(self.style.WARNING("\nWarnings / Existing Shifts:"))
            for warn in validation.warnings:
                self.stdout.write(self.style.WARNING(f"  - {warn}"))

        if not validation.is_valid:
            raise CommandError(
                "Pre-sync validation failed. Correct the errors listed above before syncing."
            )

        if not options["confirm"]:
            self.stdout.write(self.style.WARNING("\nDRY RUN ONLY."))
            self.stdout.write(
                "Validation passed! Append --confirm to sync draft shifts to Square Sandbox."
            )
            return

        self.stdout.write("\nSyncing draft shifts to Square Sandbox...")
        try:
            result = sync_schedule_to_sandbox(
                schedule_run, location_id=validation.location_id
            )
        except SquareSyncError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            self.style.SUCCESS(
                f"\nSUCCESS: Synced {result['synced_count']} draft shifts to Square Sandbox."
            )
        )
        self.stdout.write(f"New Schedule Status: {schedule_run.get_status_display()}")
