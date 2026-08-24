from django.core.management.base import BaseCommand, CommandError

from integrations.square import SquareClient, SquareConfig, SquareEnvironment
from integrations.square.exceptions import SquareIntegrationError


class Command(BaseCommand):
    help = "Test the configured Square Sandbox token and list locations."

    def handle(self, *args, **options):
        try:
            config = SquareConfig.from_env()
            if config.environment is not SquareEnvironment.SANDBOX:
                raise CommandError("Phase 1 connection testing is sandbox-only.")
            locations = SquareClient(config).test_connection()
        except SquareIntegrationError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(self.style.SUCCESS("Square connection: SUCCESS"))
        if not locations:
            self.stdout.write("No Sandbox locations were returned.")
            return
        for location in locations:
            self.stdout.write(
                f"{location.get('name', '(unnamed)')} | "
                f"{location.get('id', '(missing ID)')} | "
                f"{location.get('status', 'UNKNOWN')}"
            )

