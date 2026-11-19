"""Small in-memory rate limiter (fixed window, per client IP).

Good enough for one web process. It is per-process, so it would under-count across
several instances; move it to shared storage if the app is ever scaled out.
"""

import math
import time
from collections.abc import Callable

from fastapi import HTTPException, Request, status

_MAX_TRACKED_KEYS = 50_000


class RateLimiter:
    def __init__(
        self, limit: int, window_seconds: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.limit = limit
        self.window = window_seconds
        self._clock = clock
        self._hits: dict[str, tuple[float, int]] = {}  # key -> (window start, count)

    def retry_after(self, key: str) -> float | None:
        """Record a hit. Returns None if allowed, else seconds until the window resets."""
        now = self._clock()
        started, count = self._hits.get(key, (now, 0))
        if now - started >= self.window:
            started, count = now, 0
        if count >= self.limit:
            return self.window - (now - started)
        if len(self._hits) >= _MAX_TRACKED_KEYS and key not in self._hits:
            self._drop_expired(now)
        self._hits[key] = (started, count + 1)
        return None

    def _drop_expired(self, now: float) -> None:
        self._hits = {
            key: (started, count)
            for key, (started, count) in self._hits.items()
            if now - started < self.window
        }
        if len(self._hits) >= _MAX_TRACKED_KEYS:  # still full of live keys: start over
            self._hits = {}


def enforce(limiter: RateLimiter, request: Request) -> None:
    client = request.client.host if request.client else "unknown"
    wait = limiter.retry_after(client)
    if wait is not None:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many requests",
            headers={"Retry-After": str(math.ceil(wait))},
        )
