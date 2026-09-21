"""Near-miss trap (JEVRES-005 corpus, untested state).

A test file that exists but covers the WRONG behaviour: it asserts the limit
constant, never the refusal. The corpus question asks about refusing the
sixth call — nothing here exercises that.
"""

import limiter


def check_limit_constant_is_five() -> None:
    """Asserts configuration, not the rolling-window refusal."""
    assert limiter.RateLimiter.WINDOW_SECONDS == 60.0
    gate = limiter.RateLimiter(limit=5)
    assert gate.limit == 5
