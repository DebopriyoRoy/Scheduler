from typing import Any

from django.core.management.base import BaseCommand, CommandError

from scheduling.models import ScheduleRun
from scheduling.services.square_production_sync import (
    SquareProductionSyncError,
    preview_production_sync,
)


class Command(BaseCommand):
    help = "READ ONLY preview of proposed schedule assignments against Square Production."

    def add_arguments(self, parser):
        parser.add_argument(
            "schedule_id",
            type=int,
            help="ID of the approved ScheduleRun to preview.",
        )
        parser.add_argument(
            "--environment",
            choices=["production", "sandbox"],
            default="production",
            help="Target environment (default: production).",
        )

    def handle(self, *args, **options: Any):
        schedule_id = options["schedule_id"]
        try:
            schedule_run = ScheduleRun.objects.get(pk=schedule_id)
        except ScheduleRun.DoesNotExist:
            raise CommandError(f"ScheduleRun #{schedule_id} does not exist.") from None

        try:
            preview = preview_production_sync(schedule_run)
        except SquareProductionSyncError as exc:
            raise CommandError(str(exc)) from exc

        msg = f"Square Production Preview for Schedule #{schedule_run.id}"
        self.stdout.write(self.style.SUCCESS(msg))

        self.stdout.write(f"Schedule Period: {schedule_run.start_date} to {schedule_run.end_date}")
        self.stdout.write(f"Status: {schedule_run.get_status_display()}")
        self.stdout.write(
            f"Target Location: {preview.location_name} ({preview.location_id or 'UNMAPPED'})"
        )
        self.stdout.write(f"Total Assignments: {len(preview.rows)}")
        self.stdout.write(
            f"Summary: Ready: {preview.ready_count} | "
            f"Already Exists: {preview.already_exists_count} | "
            f"Conflicts: {preview.conflict_count} | "
            f"Blocked: {preview.blocked_count}"
        )

        if preview.errors:
            self.stdout.write(self.style.ERROR("\nValidation Errors:"))
            for err in preview.errors:
                self.stdout.write(self.style.ERROR(f"  - {err}"))

        if preview.warnings:
            self.stdout.write(self.style.WARNING("\nWarnings:"))
            for warn in preview.warnings:
                self.stdout.write(self.style.WARNING(f"  - {warn}"))

        self.stdout.write("\nDetailed Assignment Classifications:")
        for r in preview.rows:
            is_ready = r.result_status == "READY_TO_CREATE"
            status_style = self.style.SUCCESS if is_ready else self.style.WARNING
            self.stdout.write(
                f"  [{r.show_date}] {r.show_title} | {r.employee_name} ({r.role_name}) -> "
                f"{status_style(r.result_status)} ({r.reason})"
            )

