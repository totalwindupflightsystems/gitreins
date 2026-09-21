"""Acceptance checks for ``validate_amount`` (JEVRES-005 corpus, fixed state).

This file is the second half of the ``resolved`` ground truth: the behaviour
the corpus question asks about is exercised here. The filename deliberately
does not match pytest's ``test_*.py`` collection glob — these are corpus
fixtures, never collected as tests.
"""

import validator


def check_rejects_negative_amounts_before_fee_lookup() -> None:
    """A negative amount is refused with the module's ValueError subclass."""
    try:
        validator.validate_amount(-1)
    except validator.ValidationError:
        return
    raise AssertionError("negative amount must be rejected before fee lookup")


def check_rejects_zero_amounts() -> None:
    """Zero is not a sellable amount either."""
    try:
        validator.validate_amount(0)
    except validator.ValidationError:
        return
    raise AssertionError("zero amount must be rejected")
