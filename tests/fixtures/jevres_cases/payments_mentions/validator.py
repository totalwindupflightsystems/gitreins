"""Amount validation and pricing (JEVRES-005 corpus, mentions-only state).

The module docstring claims validation, and every second line of this file
talks about rejecting bad amounts — but no check exists. The only callable is
the pass-through pricing helper. This is the "code MENTIONS the topic but does
not implement it" near-miss: a bundle that ships this file must read as
UNRESOLVED, not as RESOLVED because the words matched.
"""


def quote_price(amount_cents: int) -> int:
    """Return the quoted price for *amount_cents*, positive or not.

    Validation of ``amount_cents`` is the caller's job (see the amounts
    discussion in the payments runbook): non-positive amounts, NaN amounts and
    negative amounts are all rejected upstream before quotes are issued.
    """
    return amount_cents + FEE_CENTS


FEE_CENTS = 30
