"""Client-side request budget for the documented status.tw limit (100 requests / 60 s per IP)."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Awaitable, Callable

WINDOW_SECONDS = 60.0


class RequestBudget:
    """Sliding-window limiter: at most ``max_requests`` per 60 s, plus server-imposed pauses."""

    def __init__(
        self,
        max_requests: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if max_requests < 1:
            raise ValueError("max_requests must be >= 1")
        self.max_requests = max_requests
        self._clock = clock
        self._sleep = sleep
        self._sent: deque[float] = deque()
        self._blocked_until = 0.0
        self._lock = asyncio.Lock()

    def block_for(self, seconds: float) -> None:
        """Pause all requests (e.g. after a 429 or when the server reports 0 remaining)."""
        self._blocked_until = max(self._blocked_until, self._clock() + max(0.0, seconds))

    def wait_time(self) -> float:
        """Seconds until the next request may be sent (0 if immediately)."""
        now = self._clock()
        while self._sent and now - self._sent[0] >= WINDOW_SECONDS:
            self._sent.popleft()
        wait = max(0.0, self._blocked_until - now)
        if len(self._sent) >= self.max_requests:
            wait = max(wait, WINDOW_SECONDS - (now - self._sent[0]))
        return wait

    async def acquire(self) -> None:
        async with self._lock:
            while (wait := self.wait_time()) > 0:
                await self._sleep(wait)
            self._sent.append(self._clock())
