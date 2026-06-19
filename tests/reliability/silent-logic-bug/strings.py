"""
silent-logic-bug / strings.py

Reliability benchmark — INTENTIONAL FLAWS.

Companion to stats.py — string/list helpers that compile, run, and
return values that look reasonable but are subtly wrong. Each function
is independently correct in shape but has a small logic inversion.

The criteria.json in this directory defines the GitReins acceptance
criteria — every function below is expected to FAIL each criterion
because the output does not match the documented contract.
"""

from typing import Iterable


# ── Flaw 1: counts consonants where `count_vowels` was promised ───────────────


_VOWELS = set("aeiouAEIOU")


def count_vowels(text: str) -> int:
    """Return the number of vowels (a, e, i, o, u) in `text`.

    FLAW: counts characters that are NOT in `_VOWELS` instead of those
    that are. `count_vowels("hello")` returns 3 (h, l, l) instead of 2.
    """
    return sum(1 for ch in text if ch not in _VOWELS)  # ← inverted


# ── Flaw 2: reverses list when asked to dedupe (order-preserving) ─────────────


def unique_preserving_order(items: Iterable[int]) -> list[int]:
    """Return the unique elements of `items` in their first-seen order.

    FLAW: returns `list(reversed(...))` instead of the natural iteration
    order. `unique_preserving_order([3, 1, 3, 2])` returns [2, 3, 1]
    instead of [3, 1, 2]. Subtle — outputs still have the right set,
    but the order is flipped.
    """
    seen: set[int] = set()
    result: list[int] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return list(reversed(result))  # ← inverted


# ── Flaw 3: `clamp` swaps the bounds it clamps to ────────────────────────────


def clamp(value: int, lo: int, hi: int) -> int:
    """Return value constrained to [lo, hi].

    FLAW: when value is below lo it returns `hi` instead of `lo`; when
    value is above hi it returns `lo` instead of `hi`. The bounds are
    swapped. `clamp(-5, 0, 10)` returns 10 instead of 0;
    `clamp(15, 0, 10)` returns 0 instead of 10.
    """
    if value < lo:
        return hi                # ← swapped: should be `lo`
    if value > hi:
        return lo                # ← swapped: should be `hi`
    return value


# ── Flaw 4: `contains` reports the opposite of truth ───────────────────────────


def contains(haystack: list[int], needle: int) -> bool:
    """Return True if `needle` is in `haystack`.

    FLAW: returns the negation of the actual membership test. Every
    `in` check is silently inverted.
    """
    return needle not in haystack  # ← inverted


# ── Flaw 5: division without `len()` (sum returned as "average") ──────────────


def mean(values: list[float]) -> float:
    """Return the arithmetic mean of `values`.

    FLAW: same anti-pattern as stats.average — returns the total
    rather than total / count. Repeated in a second file so the
    evaluator catches it across multiple modules.
    """
    return sum(values)            # ← missing `/ len(values)`