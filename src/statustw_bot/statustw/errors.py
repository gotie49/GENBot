"""Error hierarchy of the status.tw client.

Every error except :class:`PlayerNotFound` means "status unknown" for the monitor,
never "offline".
"""

from __future__ import annotations


class StatusTwError(Exception):
    """Base class for all status.tw client errors."""


class StatusTwUnavailable(StatusTwError):
    """Network error, timeout or 5xx response (after retries)."""


class StatusTwRateLimited(StatusTwError):
    """HTTP 429 from status.tw (after retries)."""

    def __init__(self, retry_after: float) -> None:
        super().__init__(f"rate limited by status.tw, retry after {retry_after:.0f}s")
        self.retry_after = retry_after


class StatusTwInvalidResponse(StatusTwError):
    """The response did not match the documented schema, or an unexpected 4xx status."""


class PlayerNotFound(StatusTwError):
    """`GET /player/name/{name}` answered 404: status.tw has never seen this name."""

    def __init__(self, name: str) -> None:
        super().__init__(f"status.tw has never seen a player named {name!r}")
        self.name = name
