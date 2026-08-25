from typing import Any

from django.core.management.base import BaseCommand, CommandError

from integrations.square import SquareClient, SquareConfig, SquareEnvironment
from integrations.square.exceptions import SquareIntegrationError


class Command(BaseCommand):
    help = "Test configured Square token and list locations (READ ONLY)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--environment",
            choices=["sandbox", "production"],
            default=None,
            help="Specify environment to test. Defaults to SQUARE_ENVIRONMENT from .env.",
        )

    def handle(self, *args, **options: Any):
        try:
            config = SquareConfig.from_env()
            target_env = options["environment"]
            if target_env:
                config = SquareConfig(
                    environment=SquareEnvironment(target_env),
                    sandbox_access_token=config.sandbox_access_token,
                    production_access_token=config.production_access_token,
                    location_id=config.location_id,
                    api_version=config.api_version,
                    request_timeout_seconds=config.request_timeout_seconds,
                    production_writes_enabled=config.production_writes_enabled,
                    production_pilot_verified=config.production_pilot_verified,
                    publishing_enabled=config.publishing_enabled,
                )

            locations = SquareClient(config).test_connection()
        except SquareIntegrationError as exc:
            raise CommandError(str(exc)) from exc

        env_label = config.environment.value.upper()
        self.stdout.write(self.style.SUCCESS(f"Square {env_label} Connection: SUCCESS"))
        self.stdout.write(f"Environment: {env_label}")
        if not locations:
            self.stdout.write(f"No {env_label} locations were returned.")
            return

        self.stdout.write(f"Found {len(locations)} location(s):")
        for location in locations:
            self.stdout.write(
                f"  - {location.get('name', '(unnamed)')} | "
                f"ID: {location.get('id', '(missing ID)')} | "
                f"Status: {location.get('status', 'UNKNOWN')}"
            )
