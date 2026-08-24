class SquareIntegrationError(Exception):
    """Base exception for safe, user-facing Square integration failures."""


class SquareConfigurationError(SquareIntegrationError):
    """Raised when Square environment configuration is invalid or incomplete."""


class SquareConnectionError(SquareIntegrationError):
    """Raised when Square cannot be reached or returns an invalid response."""


class SquareAPIError(SquareIntegrationError):
    """Raised when Square returns an API error response."""

    def __init__(self, message: str, *, status_code: int | None = None):
        self.status_code = status_code
        super().__init__(message)


class SquareProductionWriteBlocked(SquareIntegrationError):
    """Raised whenever the application attempts a Square production write."""
