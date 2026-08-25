import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from dotenv import load_dotenv

from .exceptions import (
    SquareConfigurationError,
    SquarePilotNotVerifiedError,
    SquareProductionWriteBlocked,
    SquareProductionWritesDisabledError,
    SquarePublishingDisabledError,
)


class SquareEnvironment(StrEnum):
    SANDBOX = "sandbox"
    PRODUCTION = "production"


@dataclass(frozen=True)
class SquareConfig:
    environment: SquareEnvironment
    sandbox_access_token: str = ""
    production_access_token: str = ""
    location_id: str = ""
    api_version: str = "2026-08-19"
    request_timeout_seconds: int = 15
    production_writes_enabled: bool = False
    production_pilot_verified: bool = False
    publishing_enabled: bool = False

    @classmethod
    def from_env(cls) -> "SquareConfig":
        load_dotenv(Path.cwd() / ".env")
        environment_value = os.getenv("SQUARE_ENVIRONMENT", "sandbox").strip().lower()
        try:
            environment = SquareEnvironment(environment_value)
        except ValueError as exc:
            raise SquareConfigurationError(
                "SQUARE_ENVIRONMENT must be either 'sandbox' or 'production'."
            ) from exc

        try:
            timeout = int(os.getenv("SQUARE_REQUEST_TIMEOUT_SECONDS", "15"))
        except ValueError as exc:
            raise SquareConfigurationError(
                "SQUARE_REQUEST_TIMEOUT_SECONDS must be a whole number."
            ) from exc
        if timeout < 1:
            raise SquareConfigurationError(
                "SQUARE_REQUEST_TIMEOUT_SECONDS must be greater than zero."
            )

        prod_writes = (
            os.getenv("SQUARE_PRODUCTION_WRITES_ENABLED", "false").strip().lower()
            in ("true", "1")
        )
        pilot_verified = (
            os.getenv("SQUARE_PRODUCTION_PILOT_VERIFIED", "false").strip().lower()
            in ("true", "1")
        )
        publishing = (
            os.getenv("SQUARE_PUBLISHING_ENABLED", "false").strip().lower()
            in ("true", "1")
        )

        return cls(
            environment=environment,
            sandbox_access_token=os.getenv("SQUARE_SANDBOX_ACCESS_TOKEN", "").strip(),
            production_access_token=os.getenv("SQUARE_PRODUCTION_ACCESS_TOKEN", "").strip(),
            location_id=os.getenv("SQUARE_LOCATION_ID", "").strip(),
            api_version=os.getenv("SQUARE_API_VERSION", "2026-08-19").strip(),
            request_timeout_seconds=timeout,
            production_writes_enabled=prod_writes,
            production_pilot_verified=pilot_verified,
            publishing_enabled=publishing,
        )

    @property
    def base_url(self) -> str:
        if self.environment is SquareEnvironment.SANDBOX:
            return "https://connect.squareupsandbox.com"
        return "https://connect.squareup.com"

    @property
    def access_token(self) -> str:
        token = (
            self.sandbox_access_token
            if self.environment is SquareEnvironment.SANDBOX
            else self.production_access_token
        )
        if not token:
            variable = (
                "SQUARE_SANDBOX_ACCESS_TOKEN"
                if self.environment is SquareEnvironment.SANDBOX
                else "SQUARE_PRODUCTION_ACCESS_TOKEN"
            )
            raise SquareConfigurationError(f"{variable} is not configured.")
        return token

    @property
    def token_is_configured(self) -> bool:
        return bool(
            self.sandbox_access_token
            if self.environment is SquareEnvironment.SANDBOX
            else self.production_access_token
        )

    def require_sandbox(self) -> None:
        if self.environment is not SquareEnvironment.SANDBOX:
            raise SquareProductionWriteBlocked(
                "Operation requires SQUARE_ENVIRONMENT=sandbox. "
                "Square Sandbox integration is sandbox-only."
            )

    def assert_write_allowed(self) -> None:
        self.assert_publishing_disabled()
        if self.environment is SquareEnvironment.PRODUCTION:
            if not self.production_writes_enabled:
                raise SquareProductionWritesDisabledError(
                    "Square production writes are disabled. "
                    "Set SQUARE_PRODUCTION_WRITES_ENABLED=true."
                )

    def assert_pilot_verified(self) -> None:
        if not self.production_pilot_verified:
            raise SquarePilotNotVerifiedError(
                "Full Production synchronization requires pilot verification. "
                "Set SQUARE_PRODUCTION_PILOT_VERIFIED=true or verify pilot in management UI."
            )

    def assert_publishing_disabled(self) -> None:
        if self.publishing_enabled:
            raise SquarePublishingDisabledError(
                "Automatic publishing is strictly prohibited. "
                "SQUARE_PUBLISHING_ENABLED must remain false."
            )
