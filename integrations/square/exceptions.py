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
    """Raised whenever the application attempts an unauthorized Square production write."""


class SquareProductionWritesDisabledError(SquareProductionWriteBlocked):
    """Raised when SQUARE_PRODUCTION_WRITES_ENABLED is False."""


class SquarePilotNotVerifiedError(SquareIntegrationError):
    """Raised when full production sync is attempted before pilot verification."""


class SquarePublishingDisabledError(SquareIntegrationError):
    """Raised if any attempt is made to publish shifts via the application."""



class SquarePublishedShiftError(SquareIntegrationError):
    """Raised when a shift cannot be removed because Square has it published.

    Square deletes a draft outright when it is updated with is_deleted, but a
    published shift is only *marked* by the same call and needs a publish to
    finalise - which this integration never does. Half-deleting is worse than
    refusing, so these are reported back to the manager to remove in Square.
    """
