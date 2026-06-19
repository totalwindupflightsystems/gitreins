"""
dead-tests / test_strings.py

Reliability benchmark — INTENTIONAL FLAWS.

Companion to test_arithmetic.py — more variants of the dead-test
anti-pattern. These tests have `test_` names, docstrings, and even
import the SUT, but none of them actually invoke the SUT inside an
assertion. Every one of them passes regardless of the SUT's behaviour.

The criteria.json in this directory defines the GitReins acceptance
criteria — every test below is expected to FAIL each criterion.

Do not use as a template — these tests are deliberately broken.
"""

from typing import Callable


# ── The system under test ─────────────────────────────────────────────────────


def reverse_string(s: str) -> str:
    return s[::-1]


def is_palindrome(s: str) -> bool:
    return s == s[::-1]


# ── Flaw 1: `pass`-only body — no assertion, no test ─────────────────────────


def test_reverse_string_empty_body():
    """Verify reverse_string('hello') == 'olleh'."""
    reverse_string("hello")            # result discarded
    pass                               # ← no assertion; pytest sees an
                                       #   "empty" test as a PASS


# ── Flaw 2: assertion is `assert True, "always passes"` ──────────────────────


def test_is_palindrome_with_message():
    """Verify is_palindrome('racecar') is True."""
    is_palindrome("racecar")           # result discarded
    assert True, "this should never fail"   # ← tautology with a message


# ── Flaw 3: `assert` on a literal that does not exercise the SUT ─────────────


def test_concat_uses_literal():
    """Verify 'a' + 'b' == 'ab'."""
    assert "a" + "b" == "ab"           # ← never calls any SUT function
    # The real test would have been:
    #     assert concat_strings("a", "b") == "ab"


# ── Flaw 4: assertion references the SUT but compares to the wrong value ──────


def test_reverse_against_wrong_value():
    """Verify reverse_string('abc') == 'abc'.

    FLAW: reverse_string('abc') actually returns 'cba', not 'abc'.
    The test asserts equality with the WRONG value, so the assertion
    is false — yet the function ALSO sets `ok = False` and never
    uses it. The test passes because the `assert` statement is
    NEVER EXECUTED (it's after `return` in a code path that is
    dead).
    """
    out = reverse_string("abc")
    ok = (out == "abc")                # ← ok is False
    return ok                          # ← pytest ignores return value
    assert out == "abc"                # ← unreachable


# ── Flaw 5: `assert` is wrapped in a decorator that suppresses failures ─────


def test_decorator_swallows_assert():
    """Verify divide-precise(10, 3) ≈ 3.333."""
    def ignore_failures(fn: Callable) -> Callable:
        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except AssertionError:
                return None            # ← swallow AssertionError
        return wrapper

    @ignore_failures
    def real_test():
        assert 1 / 3 == 0.5            # ← this is FALSE but the
                                       #   decorator catches it

    real_test()                        # ← result discarded