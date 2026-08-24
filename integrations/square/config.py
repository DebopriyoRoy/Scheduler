import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from dotenv import load_dotenv

from .exceptions import SquareConfigurationError, SquareProductionWriteBlocked


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

        return cls(
            environment=environment,
            sandbox_access_token=os.getenv("SQUARE_SANDBOX_ACCESS_TOKEN", "").strip(),
            production_access_token=os.getenv("SQUARE_PRODUCTION_ACCESS_TOKEN", "").strip(),
            location_id=os.getenv("SQUARE_LOCATION_ID", "").strip(),
            api_version=os.getenv("SQUARE_API_VERSION", "2026-08-19").strip(),
            request_timeout_seconds=timeout,
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
        token = (
            self.sandbox_access_token
            if self.environment is SquareEnvironment.SANDBOX
            else self.production_access_token
        )
        return bool(token)

    def require_sandbox(self) -> None:
        if self.environment is not SquareEnvironment.SANDBOX:
            raise SquareProductionWriteBlocked(
                "Phase 1 is sandbox-only. Change SQUARE_ENVIRONMENT to sandbox."
            )

    def assert_write_allowed(self) -> None:
        if self.environment is SquareEnvironment.PRODUCTION:
            raise SquareProductionWriteBlocked(
                "Square production writes are disabled for Phase 1."
            )

