"""Named review profiles and effort levels for the GitReins code review (GR-148).

A *profile* is a review POLICY: which checks run, what rubric the reviewer
follows, what output contract each pass must satisfy, and how many passes run.
An *effort level* is a BUDGET: iterations, tokens, wall-clock time, and tool
calls. They are orthogonal dimensions — ``deep`` with a tiny effort budget
returns partial results early, and ``quick`` never runs more than one pass
regardless of budget.

Precedence for effort (highest wins):
    1. per-invocation override (CLI flag / explicit argument)
    2. repo default (config ``commit_audit.review_effort``)
    3. profile default

No profile runs unbounded: every profile carries a hard cap on passes, LLM
calls, tokens, and wall-clock time. When a cap is hit mid-review the result is
returned as PARTIAL with a machine-readable reason — never silently truncated.

Severity compatibility (documented mapping for existing consumers):
    The canonical severity set is critical|high|medium|low|trivial|info|
    observation. ``trivial`` is distinct (cosmetic-only). Legacy consumers of
    the pre-GR-148 five-value set map: trivial -> info (info/observation stay
    info). See SEVERITY_COMPAT_MAP.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

VALID_PROFILES = ("quick", "standard", "deep")

# Canonical severities the reviewer may emit.
VALID_SEVERITIES = ("critical", "high", "medium", "low", "trivial", "info", "observation")

# Mapping to the pre-GR-148 consumer set (critical|high|medium|low|info).
# trivial is a cosmetic nit: it maps DOWN to info for legacy parsers; info and
# observation both stay info. Nothing maps upward, so a legacy consumer can
# never miss a critical/high/medium/low finding.
SEVERITY_COMPAT_MAP = {
    "critical": "critical",
    "high": "high",
    "medium": "medium",
    "low": "low",
    "trivial": "info",
    "info": "info",
    "observation": "info",
}


def compat_severity(severity: str) -> str:
    """Map a canonical severity to the legacy five-value consumer set."""
    return SEVERITY_COMPAT_MAP.get(severity, "info")


@dataclass
class EffortLevel:
    """Hard budget for a review run. Every field caps real consumption."""

    max_passes: int = 1
    max_llm_calls: int = 1
    max_tokens: int = 2048
    time_budget_s: float = 120.0
    max_tool_calls: int = 0
    label: str = "default"

    def to_dict(self) -> dict:
        return {
            "max_passes": self.max_passes,
            "max_llm_calls": self.max_llm_calls,
            "max_tokens": self.max_tokens,
            "time_budget_s": self.time_budget_s,
            "max_tool_calls": self.max_tool_calls,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> EffortLevel:
        if not d:
            return cls()
        known = {f for f in cls.__dataclass_fields__ if f != "label"}
        kwargs = {k: v for k, v in d.items() if k in known and v is not None}
        return cls(label=str(d.get("label", "custom")), **kwargs)

    def merged(self, override: dict | None) -> EffortLevel:
        """Return a copy with per-invocation overrides applied (highest precedence)."""
        if not override:
            return self
        merged = self.to_dict()
        merged.update({k: v for k, v in override.items() if v is not None})
        return EffortLevel.from_dict(merged)


@dataclass
class ReviewProfile:
    """A named review policy (rubric + checks + output contract + passes)."""

    name: str
    description: str
    rubric: str
    checks: dict[str, bool]
    severity_filter: str  # critical-only | standard | all
    passes: tuple[str, ...]  # subset of ("findings", "estimates", "remediation")
    effort: EffortLevel

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "rubric": self.rubric,
            "checks": dict(self.checks),
            "severity_filter": self.severity_filter,
            "passes": list(self.passes),
            "effort": self.effort.to_dict(),
        }


# -- Built-in profiles --

QUICK = ReviewProfile(
    name="quick",
    description=(
        "Diff-focused, single-pass triage. High-impact findings only "
        "(critical/high); no estimates, no remediation brief."
    ),
    rubric=(
        "Report ONLY issues that would block a merge or cause a production "
        "incident (critical/high). Skip style, nitpicks, and observations. "
        "One pass over the diff; do not request extra context."
    ),
    checks={
        "bugs": True,
        "security": True,
        "anti_patterns": False,
        "style": False,
        "performance": False,
    },
    severity_filter="critical-only",
    passes=("findings",),
    effort=EffortLevel(
        max_passes=1,
        max_llm_calls=1,
        max_tokens=1024,
        time_budget_s=60.0,
        max_tool_calls=0,
        label="quick",
    ),
)

STANDARD = ReviewProfile(
    name="standard",
    description=(
        "Current default behavior: one findings pass plus a fix-estimate pass, "
        "severity filter 'standard'."
    ),
    rubric=(
        "Report critical, high, and medium issues across the active checks. "
        "Findings pass first; then estimate fix effort per finding."
    ),
    checks={
        "bugs": True,
        "security": True,
        "anti_patterns": True,
        "style": False,
        "performance": False,
    },
    severity_filter="standard",
    passes=("findings", "estimates"),
    effort=EffortLevel(
        max_passes=2,
        max_llm_calls=2,
        max_tokens=2048,
        time_budget_s=120.0,
        max_tool_calls=0,
        label="standard",
    ),
)

DEEP = ReviewProfile(
    name="deep",
    description=(
        "Multi-pass review: findings pass, per-finding fix estimates, and an "
        "optional agent-ready remediation brief. Broader context, larger hard "
        "budget. Returns partial results with a reason when the cap is hit."
    ),
    rubric=(
        "Report all severities including low and observations. Pass 1: "
        "evidence-backed findings across the diff and reachable context. "
        "Pass 2: per-finding fix estimates with confidence and assumptions. "
        "Pass 3: an agent-ready remediation brief with acceptance checks. "
        "Never modify code — the brief is a description, not an action."
    ),
    checks={
        "bugs": True,
        "security": True,
        "anti_patterns": True,
        "style": True,
        "performance": True,
    },
    severity_filter="all",
    passes=("findings", "estimates", "remediation"),
    effort=EffortLevel(
        max_passes=3,
        max_llm_calls=4,
        max_tokens=4096,
        time_budget_s=300.0,
        max_tool_calls=8,
        label="deep",
    ),
)

PROFILES: dict[str, ReviewProfile] = {p.name: p for p in (QUICK, STANDARD, DEEP)}


def get_profile(name: str | None) -> ReviewProfile:
    """Resolve a profile by name; unknown/None -> standard."""
    if not name:
        return PROFILES["standard"]
    if name not in PROFILES:
        raise ValueError(
            f"Unknown review profile {name!r}; valid profiles: {', '.join(VALID_PROFILES)}"
        )
    return PROFILES[name]


def resolve_effort(
    profile: ReviewProfile,
    repo_default: dict | None = None,
    override: dict | None = None,
) -> EffortLevel:
    """Resolve the effort budget: override > repo default > profile default.

    Args:
        profile: the selected profile (supplies the base/default budget).
        repo_default: repo config ``commit_audit.review_effort`` dict (may be None).
        override: per-invocation override dict (may be None).

    Returns:
        The resolved EffortLevel actually in force for this run.
    """
    base = profile.effort
    if repo_default:
        base = base.merged(repo_default)
    return base.merged(override)


# -- Pass-2 fix estimate contract (GR-148) --

FIX_ESTIMATE_SIZES = ("XS", "S", "M", "L", "XL")

FIX_ESTIMATE_PROMPT = """\
For EACH finding from pass 1, output a fix estimate as JSON:
{"estimates": [
  {
    "file": "...", "line": 42,
    "size": "XS|S|M|L|XL",
    "time_range": "e.g. '15-30 minutes' or '2-4 hours'",
    "confidence": "high|medium|low",
    "assumptions": ["assumption 1", ...],
    "likely_scope": ["files/areas the fix would touch"],
    "likely_tests": ["tests to add or update"],
    "reason": "REQUIRED if size is unknown — why the estimate cannot be made"
  }, ...]}

Rules:
- size/time_range are ESTIMATES, never measured durations. Always label them
  as estimates in any rendering.
- If you cannot estimate a finding, omit size and set "reason".
- confidence reflects how much you know about the surrounding code."""

REMEDIATION_PROMPT = """\
Write an agent-ready remediation brief for the findings above. Output JSON:
{"remediation_brief": {
  "summary": "1-3 sentence overview",
  "steps": ["ordered remediation steps"],
  "acceptance_checks": ["verifiable checks / tests that prove the fix"],
  "notes": "risks, ordering constraints, or things an agent must NOT do"
}}

The brief is a DESCRIPTION only. Never modify code or claim code was changed."""


@dataclass
class BudgetState:
    """Tracks consumption against the resolved effort budget during a run."""

    effort: EffortLevel
    llm_calls: int = 0
    tool_calls: int = 0
    _start: float = field(default_factory=time.monotonic)

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self._start

        return (
            self.llm_calls < self.effort.max_llm_calls
            and self.elapsed_s < self.effort.time_budget_s
        )
        return (
            self.llm_calls < self.effort.max_llm_calls
            and self.elapsed_s < self.effort.time_budget_s
        )

    def charges_tool(self) -> bool:
        return (
            self.tool_calls < self.effort.max_tool_calls
            and self.elapsed_s < self.effort.time_budget_s
        )

    def exhausted_reason(self) -> str | None:
        """Name the FIRST budget dimension that is exhausted, or None.

        A dimension with a zero cap is "unused" (no tools configured), not
        exhausted — only positive caps can be hit.
        """
        if self.llm_calls >= self.effort.max_llm_calls:
            return f"llm-call budget exhausted ({self.llm_calls}/{self.effort.max_llm_calls})"
        if self.effort.max_tool_calls and self.tool_calls >= self.effort.max_tool_calls:
            return f"tool-call budget exhausted ({self.tool_calls}/{self.effort.max_tool_calls})"
        if self.elapsed_s >= self.effort.time_budget_s:
            return (
                f"time budget exhausted ({self.elapsed_s:.0f}s >= {self.effort.time_budget_s:.0f}s)"
            )
        return None
