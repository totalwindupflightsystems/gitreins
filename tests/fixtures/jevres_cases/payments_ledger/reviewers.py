"""Reviewer registry (JEVRES-005 corpus, budget-starved state).

Second file the budget-starved question requires: it maps each ledger
reviewer to their team. After the ~124k-char ledger clips to the bundle
ceiling, this file cannot also fit — the packer drops it by name, which is
what sets ``budget_exhausted`` on the verdict.
"""

REVIEWER_TEAMS = {
    "s.okafor": "disputes",
    "j.marchetti": "settlements",
    "p.nakamura": "risk",
    "l.ferreira": "settlements",
    "t.abara": "risk",
    "k.dunne": "disputes",
}


def team_for(reviewer: str) -> str:
    """Team of *reviewer*; unknown reviewers have no team."""
    return REVIEWER_TEAMS.get(reviewer, "")
