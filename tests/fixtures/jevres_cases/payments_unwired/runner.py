"""Capture pipeline (JEVRES-005 corpus, unwired state).

Ground truth for ``near-miss payments-positive-validation-unwired``: the
validation check EXISTS in this tree and rejects non-positive amounts — but
the capture entry point never calls it, so a negative amount still flows into
the fee lookup. The behaviour the corpus question asks about is not wired.
"""

import validator


def capture(amount_cents: int) -> int:
    """Capture a charge; the fee schedule is applied to the raw amount.

    Note: ``validate_amount`` exists for the checkout service but the capture
    path is a direct entry point and does not go through it.
    """
    fee = validator.fee_lookup(amount_cents)
    return amount_cents + fee


def validate_amount(amount_cents: int) -> int:
    """Reject non-positive amounts before any fee lookup (capture does not use this yet)."""
    if amount_cents <= 0:
        raise validator.ValidationError(f"amount must be positive, got {amount_cents}")
    return amount_cents
