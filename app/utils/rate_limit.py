from __future__ import annotations

import threading
import time


class MinIntervalRateLimiter:
    """Simple process-local limiter that spaces API calls evenly."""

    def __init__(self, calls_per_second: float) -> None:
        if calls_per_second <= 0:
            raise ValueError("calls_per_second must be positive.")
        self._min_interval = 1.0 / calls_per_second
        self._lock = threading.Lock()
        self._next_allowed_at = 0.0

    def acquire(self) -> None:
        with self._lock:
            now = time.monotonic()
            wait_seconds = self._next_allowed_at - now
            if wait_seconds > 0:
                time.sleep(wait_seconds)
                now = time.monotonic()
            self._next_allowed_at = now + self._min_interval

