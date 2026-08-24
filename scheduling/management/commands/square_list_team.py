from django.core.management.base import BaseCommand, CommandError

from integrations.square import SquareClient, SquareConfig, SquareEnvironment
from integrations.square.exceptions import SquareIntegrationError


class Command(BaseCommand):
    help = "List active team members from Square Sandbox."

    def handle(self, *args, **options):
        try:
            config = SquareConfig.from_env()
            if config.environment is not SquareEnvironment.SANDBOX:
                raise CommandError("Phase 1 team discovery is sandbox-only.")
            members = SquareClient(config).search_team_members(active_only=True)
        except SquareIntegrationError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write("Given name | Family name | Square team member ID | Status")
        for member in members:
            self.stdout.write(
                f"{member.get('given_name', '')} | {member.get('family_name', '')} | "
                f"{member.get('id', '(missing ID)')} | {member.get('status', 'UNKNOWN')}"
            )
        self.stdout.write(self.style.SUCCESS(f"Active Sandbox team members: {len(members)}"))
