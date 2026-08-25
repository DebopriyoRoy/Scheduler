"""Exceptions for Square Production Availability Integration."""


class SquareAvailabilityError(Exception):
    """Base exception for availability integration errors."""


class SquareAvailabilityAPIError(SquareAvailabilityError):
    """Raised when API extraction fails."""


class SquareAvailabilityBrowserError(SquareAvailabilityError):
    """Raised when browser extraction fails."""


class SquareAvailabilityCompletenessError(SquareAvailabilityError):
    """Raised when availability completeness check fails."""
