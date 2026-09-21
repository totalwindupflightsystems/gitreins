"""Fee schedule (JEVRES-005 corpus, unwired state)."""

FEE_CENTS = 30


def fee_lookup(amount_cents: int) -> int:
    """Flat schedule plus the per-hundred rate."""
    return FEE_CENTS + (amount_cents // 100)


class ValidationError(ValueError):
    """Raised by ``runner.validate_amount`` for non-positive amounts."""
