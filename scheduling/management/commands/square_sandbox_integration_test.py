from django.core.management.base import BaseCommand, CommandError

from integrations.square import SquareClient, SquareConfig
from integrations.square.exceptions import SquareIntegrationError


class Command(BaseCommand):
    help = "Run optional read-only integration checks against Square Sandbox."

    def handle(self, *args, **options):
        try:
            config = SquareConfig.from_env()
            config.require_sandbox()
            client = SquareClient(config)
            locations = client.list_locations()
            team_members = client.search_team_members(active_only=True)
            jobs = client.list_jobs()
        except SquareIntegrationError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS("Square Sandbox integration checks: SUCCESS"))
        self.stdout.write(f"Locations: {len(locations)}")
        self.stdout.write(f"Active team members: {len(team_members)}")
        self.stdout.write(f"Jobs: {len(jobs)}")
        self.stdout.write("Writes performed: 0")
