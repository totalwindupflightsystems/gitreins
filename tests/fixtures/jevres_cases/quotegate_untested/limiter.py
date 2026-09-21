"""Rolling-minute quote gate (JEVRES-005 corpus, untested state).

Ground truth for ``near-miss quotegate-rolling-window-untested``: the limiter
is fully implemented — sixth call inside the window refused, window rolls —
but this tree contains NO test, check or probe for that behaviour.
"""

import time
from collections import deque


class RateLimiter:
    """Allow at most ``limit`` calls per rolling 60-second window."""

    WINDOW_SECONDS = 60.0

    def __init__(self, limit: int = 5) -> None:
        self.limit = limit
        self._hits: deque[float] = deque()

    def allow(self, now: float | None = None) -> bool:
        """Record a hit and return False once the window is full."""
        moment = time.monotonic() if now is None else now
        while self._hits and moment - self._hits[0] >= self.WINDOW_SECONDS:
            self._hits.popleft()
        if len(self._hits) >= self.limit:
            return False
        self._hits.append(moment)
        return True


class QuoteAPI:
    """Public surface; the limiter is created at import time."""

    def __init__(self) -> None:
        self.limiter = RateLimiter(limit=5)

    def quote(self, amount_cents: int) -> str:
        if not self.limiter.allow():
            raise RuntimeError("quote rate limit exceeded")
        return f"quote:{amount_cents}"
