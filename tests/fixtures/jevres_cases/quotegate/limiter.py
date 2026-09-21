"""Rolling-minute quote gate (JEVRES-005 corpus, implemented state).

Ground truth for ``resolved-quotegate-window``: ``RateLimiter.allow`` refuses
the sixth call inside a rolling 60-second window, and
``limiter_checks.py`` in this same tree exercises exactly that.
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
