"""
wrong-control-flow / iter.py

Reliability benchmark — INTENTIONAL FLAWS.

Companion to control.py — more variants of the wrong-control-flow
anti-pattern. Each function demonstrates a different control-flow
mistake: misplaced return, unreachable nested branch, missing loop
break, wrong exception class, or flag variable that never updates.

The criteria.json in this directory defines the GitReins acceptance
criteria — every function below is expected to FAIL each criterion.

Do not use as a template — this code is deliberately broken.
"""

from typing import Iterable, Callable


# ── Flaw 1: `return True` inside the loop exits after the first element ────────


def all_positive(numbers: Iterable[int]) -> bool:
    """Return True only if every element of `numbers` is > 0.

    FLAW: `return True` is INSIDE the `for` loop. The function
    returns True after checking only the first element. For input
    `[1, 2, -3]` the function returns True even though -3 fails.
    """
    for n in numbers:
        if n <= 0:
            return False
        return True                    # ← should be AFTER the loop
    return True


# ── Flaw 2: nested `if` uses a tautologically-false condition ─────────────────


def grade(score: int) -> str:
    """Return a letter grade for `score` (0-100), raise on negative.

    FLAW: the outer guard `if score >= 0:` makes the `else` only
    run for negative scores. Inside the else, the AI wrote
    `if score > 0:` — a condition that is ALWAYS FALSE in this
    branch (we already know score < 0). The `raise` is unreachable
    and `grade(-5)` silently returns 'F' instead of raising
    ValueError.
    """
    if score >= 0:
        if score >= 90:
            return "A"
        if score >= 80:
            return "B"
        if score >= 70:
            return "C"
        if score >= 60:
            return "D"
        return "F"
    else:
        if score > 0:                  # ← always False here — should be `< 0`
            raise ValueError("score must be non-negative")
        return "F"


# ── Flaw 3: missing `break` — loop never exits on the stopping condition ─────


def take_until_blank(lines: list[str]) -> list[str]:
    """Read lines until a blank line is encountered (exclusive).

    FLAW: the `if not line:` branch increments the index and uses
    `continue` instead of `break`. The function consumes the entire
    input list and includes everything after the blank line.
    """
    out: list[str] = []
    i = 0
    while True:
        if i >= len(lines):
            break
        line = lines[i]
        if not line:
            i += 1                     # ← should `break` here
            continue
        out.append(line)
        i += 1
    return out


# ── Flaw 4: `try/except` catches the wrong exception class ────────────────────


def parse_int_strict(value: str) -> int:
    """Parse `value` as an int, returning 0 on ValueError.

    FLAW: catches `ValueError` but the `try` body can raise
    `TypeError` (when `value` is None or a non-stringifiable type).
    The function lets `TypeError` propagate instead of converting it
    to 0. Wrong exception class caught.
    """
    try:
        n = int(value)
    except ValueError:
        return 0                       # ← only handles ValueError, not TypeError
    return n


# ── Flaw 5: flag variable never assigned ─────────────────────────────────────


def any_match(items: Iterable[int], predicate: Callable[[int], bool]) -> bool:
    """Return True if `predicate` is True for some element of `items`.

    FLAW: the flag variable `found` is never assigned inside the
    loop. The AI generator left the assignment behind — the loop
    only contains a `continue` in the else branch. The function
    unconditionally returns False.
    """
    found = False
    for item in items:
        if predicate(item):
            pass                       # ← `found = True` is missing
        else:
            continue
    return found
