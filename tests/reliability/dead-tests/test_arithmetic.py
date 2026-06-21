"""
dead-tests / test_arithmetic.py

Reliability benchmark — INTENTIONAL FLAWS.

This module demonstrates the "AI code generator writes a test that
always passes regardless of the system under test" anti-pattern. Every
test function in this file looks like a real pytest test — it has a
`test_` prefix, it takes fixtures if needed, it has a docstring — but
none of them actually exercise the production code. They either have
no assertions, tautological assertions, or computations that are
discarded.

The criteria.json in this directory defines the GitReins acceptance
criteria — every test below is expected to FAIL each criterion because
the test does not verify any production behaviour.

Do not use as a template — these tests are deliberately broken.
"""



# ── The system under test (SUT) — intentionally trivial so the tests
#    are forced to engage with it. Any real failure here would surface.


def add(a: int, b: int) -> int:
    return a + b


def divide(a: float, b: float) -> float:
    if b == 0:
        raise ZeroDivisionError("divide by zero")
    return a / b


# ── Flaw 1: `assert True` — passes for any state of the SUT ──────────────────


def test_add_with_assert_true():
    """Verify add(2, 3) returns 5.

    FLAW: the only assertion is `assert True`. The SUT could return
    0 or raise and this test would still pass.
    """
    add(2, 3)
    assert True                        # ← tautology — passes for any result


# ── Flaw 2: `assert 1 == 1` — also a tautology ───────────────────────────────


def test_divide_with_trivial_equality():
    """Verify divide(10, 2) returns 5.0.

    FLAW: the assertion compares two literal integers — there is no
    reference to `divide(...)` in the assertion. Even if `divide`
    was deleted from the module, this test would pass.
    """
    divide(10, 2)                      # result discarded
    assert 1 == 1                      # ← tautology


# ── Flaw 3: no assertion at all — function body computes and returns ────────


def test_subtract_no_assertion():
    """Verify (5 - 3) == 2."""
    x = 5 - 3
    return x                           # ← pytest ignores return values; the
                                       #   test passes regardless of x


# ── Flaw 4: assertion is in a try/except that swallows AssertionError ────────


def test_passes_when_it_should_fail():
    """Verify the negative case raises.

    FLAW: the `assert` is wrapped in a try/except that catches
    AssertionError and silently `pass`es. A genuine regression
    inside the SUT would be invisible.
    """
    try:
        assert divide(1, 0) == 0       # ← this should fail but is caught
    except AssertionError:
        pass                           # ← swallowed silently
    except ZeroDivisionError:
        pass                           # ← the actual behaviour is also swallowed


# ── Flaw 5: assertion uses a literal that is not produced by the SUT ──────────


def test_uses_hardcoded_literal():
    """Verify add(2, 2) == 5.

    FLAW: the test asserts a literal that the SUT can never produce
    (add(2, 2) is 4, not 5). The function uses a hardcoded `4`
    instead of calling `add(2, 2)` — but the assertion still passes
    because both sides match.
    """
    assert 4 == 4                      # ← tautology; never calls add()
    # The real check would have been:
    #     assert add(2, 2) == 5
    # which would have FAILED, exposing a real bug.