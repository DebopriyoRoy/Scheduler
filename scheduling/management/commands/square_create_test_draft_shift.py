from typing import Any

from django.core.management.base import BaseCommand, CommandError

from integrations.square import SquareClient, SquareConfig
from integrations.square.exceptions import SquareIntegrationError
from integrations.square.services import DraftShiftRequest, create_sandbox_draft_shift


class Command(BaseCommand):
    help = "Dry-run or explicitly create one draft scheduled shift in Square Sandbox."

    def add_arguments(self, parser):
        parser.add_argument("--team-member-id", required=True)
        parser.add_argument("--job-id", required=True)
        parser.add_argument("--location-id", required=True)
        parser.add_argument("--start", required=True)
        parser.add_argument("--end", required=True)
        parser.add_argument(
            "--confirm",
            action="store_true",
            help="Create the Sandbox draft. Without this flag, the command is a dry run.",
        )

    def handle(self, *args, **options: Any):
        try:
            config = SquareConfig.from_env()
            config.require_sandbox()
            request = DraftShiftRequest(
                team_member_id=options["team_member_id"],
                job_id=options["job_id"],
                location_id=options["location_id"],
                start_at=options["start"],
                end_at=options["end"],
            )
            request.validate()
        except SquareIntegrationError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write("Environment: SANDBOX")
        self.stdout.write(f"Team member ID: {request.team_member_id}")
        self.stdout.write(f"Job ID: {request.job_id}")
        self.stdout.write(f"Location ID: {request.location_id}")
        self.stdout.write(f"Start: {request.start_at}")
        self.stdout.write(f"End: {request.end_at}")

        if not options["confirm"]:
            self.stdout.write(self.style.WARNING("DRY RUN"))
            self.stdout.write("No Square changes made.")
            return

        try:
            scheduled_shift = create_sandbox_draft_shift(SquareClient(config), request)
        except SquareIntegrationError as exc:
            raise CommandError(str(exc)) from exc

        details = scheduled_shift.get("draft_shift_details", {})
        self.stdout.write(self.style.SUCCESS("Square Sandbox draft shift created."))
        self.stdout.write(f"Square scheduled shift ID: {scheduled_shift.get('id', '(missing ID)')}")
        self.stdout.write("Status: DRAFT")
        self.stdout.write(f"Employee ID: {details.get('team_member_id', request.team_member_id)}")
        self.stdout.write(f"Job ID: {details.get('job_id', request.job_id)}")
        self.stdout.write(f"Location ID: {details.get('location_id', request.location_id)}")
        self.stdout.write(f"Start: {details.get('start_at', request.start_at)}")
        self.stdout.write(f"End: {details.get('end_at', request.end_at)}")
        self.stdout.write("Publication: NOT PUBLISHED")
