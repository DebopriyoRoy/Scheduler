"""Exceptions for Spirit Calendar Integration."""


class SpiritCalendarError(Exception):
    """Base exception for calendar sync failures."""


class SpiritCalendarAPIError(SpiritCalendarError):
    """Raised when primary API/XHR provider fails or is incomplete."""


class SpiritCalendarBrowserError(SpiritCalendarError):
    """Raised when Playwright browser provider fails."""


class SpiritCalendarCompletenessError(SpiritCalendarError):
    """Raised when extracted events count does not match rendered events count."""
