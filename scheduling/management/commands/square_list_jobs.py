from django.core.management.base import BaseCommand, CommandError

from integrations.square import SquareClient, SquareConfig, SquareEnvironment
from integrations.square.exceptions import SquareIntegrationError

EXPECTED_JOBS = {"Server", "Bartender", "Busser", "50/50"}


class Command(BaseCommand):
    help = "List jobs from Square Sandbox and report required Spirit jobs that are absent."

    def handle(self, *args, **options):
        try:
            config = SquareConfig.from_env()
            if config.environment is not SquareEnvironment.SANDBOX:
                raise CommandError("Phase 1 job discovery is sandbox-only.")
            jobs = SquareClient(config).list_jobs()
        except SquareIntegrationError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write("Job title | Square job ID")
        for job in jobs:
            self.stdout.write(f"{job.get('title', '(untitled)')} | {job.get('id', '(missing ID)')}")

        found = {str(job.get("title", "")).casefold() for job in jobs}
        missing = sorted(job for job in EXPECTED_JOBS if job.casefold() not in found)
        if missing:
            self.stdout.write(self.style.WARNING(f"Missing expected jobs: {', '.join(missing)}"))
        else:
            self.stdout.write(self.style.SUCCESS("All expected Spirit jobs were found."))

