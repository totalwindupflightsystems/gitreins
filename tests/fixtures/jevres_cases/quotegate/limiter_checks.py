"""Acceptance checks for the quote gate (JEVRES-005 corpus, implemented state).

Second half of the ``resolved`` ground truth: the sixth ``allow()`` inside the
window is refused, and the window genuinely rolls. Never collected by pytest
(the filename does not match ``test_*.py``).
"""

import limiter


def check_sixth_call_inside_the_window_is_refused() -> None:
    gate = limiter.RateLimiter(limit=5)
    hits = [gate.allow(now=float(tick)) for tick in range(5)]
    assert all(hits), "the first five calls inside the window must pass"
    assert gate.allow(now=5.5) is False, "the sixth call must be refused"
    assert gate.allow(now=61.0) is True, "after the window rolls the gate reopens"
