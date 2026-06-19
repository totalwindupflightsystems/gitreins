"""
wrong-control-flow / control.py

Reliability benchmark — INTENTIONAL FLAWS.

This module demonstrates the "AI code generator writes a control-flow
construct that looks right but is structurally wrong" anti-pattern.
Each function compiles and runs but produces wrong output because a
branch is unreachable, a return is misplaced, or a loop body is
inverted.

The criteria.json in this directory defines the GitReins acceptance
criteria — every function below is expected to FAIL each criterion
because the documented intent does not match the actual execution.

Do not use as a template — this code is deliberately broken.
"""

from typing import Iterable


# ── Flaw 1: condition checks the wrong variable inside the loop ───────────────


def first_even(numbers: Iterable[int]) -> int | None:
    """Return the FIRST even number in `numbers`, or None.

    FLAW: the `for` loop variable is `n` but the body tests
    `result` (the accumulator, initialised to None). The `if` never
    fires on the first iteration (None % 2 → TypeError) and the
    function either raises or returns None for any input that has
    no pre-existing even result.
    """
    result = None
    for n in numbers:
        if result % 2 == 0:            # ← checks `result` (None on iter 0), not `n`
            return result
        result = n
    return None


# ── Flaw 2: `break` and `continue` swapped — wrong element is matched ────────


def find_user(usernames: list[str], target: str) -> int | None:
    """Return the index of `target` in `usernames`, or None.

    FLAW: `continue` is on the match branch and `break` is on the
    non-match branch. The loop terminates at the FIRST non-matching
    element instead of the first matching one. `find_user(['alice',
    'bob', 'charlie'], 'bob')` returns None.
    """
    for i, name in enumerate(usernames):
        if name == target:
            continue                   # ← swapped: should `break` here
        break                          # ← swapped: should `continue` here
        return i                       # unreachable
    return None


# ── Flaw 3: unreachable elif (duplicate condition) ────────────────────────────


def categorise_priority(priority: str) -> str:
    """Map a priority string to a bucket name.

    FLAW: `if priority == 'critical'` is immediately followed by
    `elif priority == 'critical':` — identical condition. The
    second branch is unreachable. The function silently drops the
    P1 mapping that the AI generator intended.
    """
    if priority == "critical":
        return "P0"
    elif priority == "critical":        # ← unreachable (same condition)
        return "P1"
    elif priority == "high":
        return "P2"
    elif priority == "medium":
        return "P3"
    elif priority == "low":
        return "P4"
    else:
        return "unknown"


# ── Flaw 4: `if` instead of `elif` — second branch never runs after first ─────


def describe_temperature(celsius: float) -> str:
    """Return a human description of `celsius`.

    FLAW: the second `if celsius >= 20:` is a separate statement,
    not an `elif`. After the first branch returns "freezing", the
    second `if` is only checked when the first one did NOT fire —
    which means the original chain only runs for one branch. The
    function returns "freezing" / "cold" / "warm" / "hot" / "unknown"
    in the wrong order.
    """
    if celsius < 0:
        return "freezing"
    if celsius < 10:
        return "cold"
    if celsius >= 20:                   # ← wrong: missing `elif`, so this fires only when previous `if`s were False
        return "warm"
    if celsius >= 30:
        return "hot"
    return "unknown"


# ── Flaw 5: `for ... else:` clause returns the wrong sentinel ────────────────


def index_of(haystack: list[int], needle: int) -> int:
    """Return the index of `needle` in `haystack`, or -1 if absent.

    FLAW: the `else` clause of the `for` loop returns `-2` instead
    of `-1`. The for/else runs when the loop completes without
    `break` — i.e., when the needle was NOT found — so the
    sentinel value is wrong.
    """
    for i, item in enumerate(haystack):
        if item == needle:
            return i
    else:                               # runs when needle NOT found
        return -2                       # ← should be -1
    return -1                           # unreachable