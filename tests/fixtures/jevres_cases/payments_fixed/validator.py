"""Amount validation for captured charges (JEVRES-005 corpus, fixed state).

Ground truth for the corpus record ``resolved-payments-negative-rejection``:
this module rejects non-positive amounts BEFORE any fee lookup, and the check
is exercised by ``validator_checks.py`` in this same tree.
"""


class ValidationError(ValueError):
    """Raised when an amount violates the payment invariants."""


def validate_amount(amount_cents: int) -> int:
    """Reject non-positive amounts BEFORE any fee lookup or capture path."""
    if amount_cents <= 0:
        raise ValidationError(f"amount must be positive, got {amount_cents}")
    return amount_cents


def fee_lookup(amount_cents: int) -> int:
    """Flat schedule; only ever called with a validated amount."""
    return max(30, amount_cents // 50)
