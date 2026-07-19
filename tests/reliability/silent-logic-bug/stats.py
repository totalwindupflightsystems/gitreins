"""
silent-logic-bug / stats.py

Reliability benchmark — INTENTIONAL FLAWS.

This module demonstrates the "AI code generator produces a function that
compiles, runs, returns a value, but the value is wrong" anti-pattern.
Every function below has logic that looks plausible on inspection but
silently returns incorrect results — no exception, no warning, just bad
numbers.

The criteria.json in this directory defines the GitReins acceptance
criteria — every function below is expected to FAIL each criterion
because the implementation diverges from the documented contract.

Do not use as a template — this code is deliberately broken.
"""

from typing import Iterable, Sequence


# ── Flaw 1: `sum` returned where `average` was promised ───────────────────────


def average(values: Sequence[float]) -> float:
    """Return the arithmetic mean of `values`.

    FLAW: returns `sum(values)` instead of `sum(values) / len(values)`.
    `average([10, 20, 30])` returns 60 instead of 20. The function
    silently produces a wrong number, no exception, no log.
    """
    return float(sum(values))


# ── Flaw 2: off-by-one in range() ─────────────────────────────────────────────


def first_n_squares(n: int) -> list[int]:
    """Return the first n perfect squares: [1, 4, 9, ..., n^2].

    FLAW: iterates `range(n)` instead of `range(1, n + 1)`, so the
    output starts at 0 and ends at (n-1)^2. `first_n_squares(3)`
    returns [0, 1, 4] instead of [1, 4, 9].
    """
    return [i * i for i in range(n)]


# ── Flaw 3: inverted comparison (`<` instead of `>`) ──────────────────────────


def find_max(items: Iterable[int]) -> int:
    """Return the largest element in `items`.

    FLAW: uses `if item < best:` inside the loop, so the function
    actually returns the SMALLEST element. `find_max([1, 5, 3])`
    returns 1 instead of 5.
    """
    iterator = iter(items)
    best = next(iterator)
    for item in iterator:
        if item < best:           # ← inverted: should be `>`
            best = item
    return best


# ── Flaw 4: `is_adult` returns True for minors ────────────────────────────────


def is_adult(age: int) -> bool:
    """Return True if `age >= 18`, False otherwise.

    FLAW: comparison is inverted — `age < 18` is the condition under
    which `is_adult` returns True. `is_adult(15)` returns True,
    `is_adult(30)` returns False. Real-world consequence: every
    age-gated feature inverts.
    """
    if age < 18:                 # ← inverted: should be `age >= 18`
        return True
    return False


# ── Flaw 5: factorial off-by-one — range stops one short of n ────────────────


def factorial(n: int) -> int:
    """Return n! = 1 * 2 * 3 * ... * n.

    FLAW: iterates `range(2, n)` instead of `range(2, n + 1)`. For n >= 2
    this skips the final multiplication by n. `factorial(5)` returns 24
    instead of 120; `factorial(3)` returns 2 instead of 6. Subtle — the
    function looks right because `result` is initialised correctly.
    """
    if n < 0:
        raise ValueError("factorial undefined for negative integers")
    result = 1
    for i in range(2, n):        # ← off-by-one: should be `range(2, n + 1)`
        result *= i
    return result
