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

import json
import logging
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("gitreins.review_profiles")

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_passes": self.max_passes,
            "max_llm_calls": self.max_llm_calls,
            "max_tokens": self.max_tokens,
            "time_budget_s": self.time_budget_s,
            "max_tool_calls": self.max_tool_calls,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> EffortLevel:
        if not d:
            return cls()
        known = {f for f in cls.__dataclass_fields__ if f != "label"}
        kwargs = {k: v for k, v in d.items() if k in known and v is not None}
        return cls(label=str(d.get("label", "custom")), **kwargs)

    def merged(self, override: dict[str, Any] | None) -> EffortLevel:
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

    def to_dict(self) -> dict[str, Any]:
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
    repo_default: dict[str, Any] | None = None,
    override: dict[str, Any] | None = None,
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


# ════════════════════════════════════════════════════════════════
# GR-171: the author-independent second-pass reviewer
# ════════════════════════════════════════════════════════════════
#
# The tier2 evaluator grades the task author's acceptance criteria. Whatever
# the author forgot to write down, the verdict cannot name. This pass is the
# check that runs BESIDE the criteria grading and does not consume the
# criteria at all — by construction: SecondPassReviewer has no criteria
# parameter, and its prompt is assembled from the diff alone.
#
# Borrowed discipline (board row GR-171, from the PR-Agent contrast): prefer
# NOT reporting over guessing, cap the findings, and state the detection rule
# behind each finding. Findings carry the module's canonical severity set;
# the ones a gate acts on are critical|high (BLOCKING_SEVERITIES).

#: Hard cap on the pass's findings — a reviewer that reports everything
#: reports nothing (same triage rule the QUICK profile applies).
MAX_SECOND_PASS_FINDINGS = 10

#: The severities a verdict gate acts on. Lower severities are recorded and
#: rendered, but advisory — a minor note must never block a merge by itself.
BLOCKING_SEVERITIES = ("critical", "high")

SECOND_PASS_SYSTEM_PROMPT = """\
You are an independent second-pass code reviewer. Your verdict does not replace the task's acceptance-criteria grading — it runs BESIDE it, and it does not see those criteria at all.

## YOUR INPUT

You receive the diff of the change under review, read against the repository it landed in. That is all you receive. Do not guess at what the task "probably asked for": judge the change itself.

## WHAT TO LOOK FOR

Review the diff for exactly these classes of defect:

1. Correctness — logic errors, inverted conditions, off-by-one, wrong variable, unreachable branch.
2. Missing wiring — a new symbol, flag, route or config key that nothing calls, reads or registers.
3. Missing failure-path tests — the happy path has a test, the failure path (error return, timeout, malformed input, retry exhaustion) has none.
4. Convention drift — the change does it differently from how the surrounding code already does it (naming, error style, layering).

## HARD RULE — DO NOT SPECULATE BEYOND EVIDENCE

Report a finding ONLY when you can point at the exact line or code shape that shows it. If you suspect a defect but cannot evidence it from the diff and the visible code, DO NOT report it — a silent non-finding is the correct answer to a guess. Prefer one correct, evidenced finding over five speculative ones.

## OUTPUT FORMAT

When done, output EXACTLY this JSON with no surrounding text, no markdown fences:

{"findings":[{"severity":"high","file":"engine/foo.py","line":42,"detection":"new config key `foo.bar` is never read anywhere in the repo","detail":"one-sentence evidence citation"}],"summary":"<one sentence>"}

Rules:
- "severity" is one of critical, high, medium, low, trivial, info, observation. critical = breaks existing behaviour; high = the change is wrong or unwired; lower = drift/smell.
- "file" is repo-relative; "line" is the 1-indexed line the finding is about (0 when the diff has no line to cite).
- "detection" names the RULE that fired — the checkable statement of why this is a finding, not prose about the code.
- At most {max_findings} findings. If the change is clean, return an empty findings list — that is a valid and expected answer.
- NEVER invent a file, line or behaviour you did not see."""


@dataclass(frozen=True)
class SecondPassFinding:
    """One machine-actable second-pass finding (GR-171 AC2).

    The fields the verdict gate (or any consumer of the persisted verdict
    record) acts on are ``severity``, the ``file``:``line`` pair and
    ``detection``; ``detail`` carries the human-readable evidence citation.
    """

    severity: str
    file: str
    line: int
    detection: str
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "file": self.file,
            "line": self.line,
            "detection": self.detection,
            "detail": self.detail,
        }


def second_pass_finding_from_dict(data: Any) -> tuple[SecondPassFinding | None, str | None]:
    """Validate one parsed finding. Returns (finding, None) or (None, reason).

    A finding that fails validation is DROPPED, never coerced: severity must
    be a canonical word (the module's own set), file and detection must be
    non-empty, line must be a non-negative integer. Drops are logged by the
    caller so a model that keeps emitting malformed findings is visible
    instead of silently shrinking the findings list.
    """
    if not isinstance(data, dict):
        return None, f"not an object ({type(data).__name__})"
    severity = str(data.get("severity", "") or "").strip().lower()
    if severity not in VALID_SEVERITIES:
        return None, f"severity {severity!r} not in {VALID_SEVERITIES}"
    file = str(data.get("file", "") or "").strip()
    if not file:
        return None, "empty file"
    raw_line = data.get("line", 0)
    if isinstance(raw_line, bool) or not isinstance(raw_line, int) or raw_line < 0:
        return None, f"line {raw_line!r} is not a non-negative integer"
    detection = str(data.get("detection", "") or "").strip()
    if not detection:
        return None, "empty detection"
    detail = str(data.get("detail", "") or "").strip()
    return (
        SecondPassFinding(
            severity=severity, file=file, line=raw_line, detection=detection, detail=detail
        ),
        None,
    )


def blocking_second_pass_findings(review: Any) -> list[SecondPassFinding]:
    """The findings a gate acts on: every critical or high finding.

    Accepts a :class:`SecondPassResult`, its ``to_dict()`` shape, or a
    persisted verdict record's ``review`` block. Lower severities are
    advisory and never block.
    """
    raw: Any = None
    if isinstance(review, SecondPassResult):
        raw = review.findings
    elif isinstance(review, dict):
        findings = review.get("findings")
        if isinstance(findings, list):
            raw = findings
    if raw is None:
        return []
    out: list[SecondPassFinding] = []
    for item in raw:
        if isinstance(item, SecondPassFinding):
            if item.severity in BLOCKING_SEVERITIES:
                out.append(item)
        elif isinstance(item, dict):
            finding, _ = second_pass_finding_from_dict(item)
            if finding is not None and finding.severity in BLOCKING_SEVERITIES:
                out.append(finding)
    return out


def self_review_flag(reviewer_email: str, author_email: str) -> bool | None:
    """True/False when both identities are known, None when either is not.

    AC3: a verdict may claim "the reviewer differs from the author" only when
    BOTH identities were actually read. A missing identity is unknown, and
    unknown is never recorded as independent.
    """
    reviewer = reviewer_email.strip().casefold()
    author = author_email.strip().casefold()
    if not reviewer or not author:
        return None
    return reviewer == author


def collect_review_identities(workdir: str) -> dict[str, Any]:
    """Reviewer (judging environment) vs commit-author (HEAD) git identities.

    The reviewer is the ``user.email``/``user.name`` of the checkout the judge
    runs in — the identity that would own a follow-up commit. The author is
    the author of HEAD, the change under review. Every lookup failure degrades
    that single field to "" and never raises: an identity report must not be
    able to fail a judge run.
    """
    out: dict[str, Any] = {
        "reviewer_email": "",
        "reviewer_name": "",
        "commit_author_email": "",
        "commit_author_name": "",
        "self_review": None,
    }
    for key, args in (
        ("reviewer_email", ["git", "config", "user.email"]),
        ("reviewer_name", ["git", "config", "user.name"]),
        ("commit_author_email", ["git", "log", "-1", "--format=%ae", "HEAD"]),
        ("commit_author_name", ["git", "log", "-1", "--format=%an", "HEAD"]),
    ):
        try:
            result = subprocess.run(  # noqa: S607 - git resolved via PATH by design
                args,
                capture_output=True,
                text=True,
                timeout=5,
                cwd=workdir,
                check=False,
            )
            if result.returncode == 0:
                out[key] = result.stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            logger.debug("identity lookup failed for %s", key, exc_info=True)
    out["self_review"] = self_review_flag(
        str(out["reviewer_email"]), str(out["commit_author_email"])
    )
    return out


@dataclass
class SecondPassResult:
    """The second pass's outcome, in the shape a verdict record persists."""

    findings: list[SecondPassFinding] = field(default_factory=list)
    reviewer_email: str = ""
    reviewer_name: str = ""
    commit_author_email: str = ""
    commit_author_name: str = ""
    # True = the reviewer IS the commit author (flagged, AC3); False = they
    # differ; None = at least one identity was unreadable.
    self_review: bool | None = None
    summary: str = ""
    # Set when the pass itself failed (no diff, transport, unparseable reply)
    # so a consumer can tell "the reviewer found nothing" from "the reviewer
    # never reported".
    error: str | None = None
    # GR-180: the reflection pass's drop record ({dropped, threshold,
    # dropped_findings[, error]}), set only when reflection ran AND it dropped
    # something. Absent — never an empty dict — otherwise, so every pre-GR-180
    # (and every no-drop) review dict serializes byte-identically.
    reflection: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "reviewer_email": self.reviewer_email,
            "reviewer_name": self.reviewer_name,
            "commit_author_email": self.commit_author_email,
            "commit_author_name": self.commit_author_name,
            "self_review": self.self_review,
            "findings": [f.to_dict() for f in self.findings],
            "summary": self.summary,
        }
        if self.error is not None:
            d["error"] = self.error
        if self.reflection is not None:
            d["reflection"] = self.reflection
        return d


def _extract_json_object(content: str) -> dict[str, Any] | None:
    """The first complete JSON object in *content*, or None.

    Same discipline as the evaluator's verdict parse: raw_decode consumes
    exactly one value, so trailing commentary or an echoed example cannot
    poison the parse.
    """
    cleaned = content.strip()
    start = cleaned.find("{")
    if start < 0:
        return None
    try:
        data, _end = json.JSONDecoder().raw_decode(cleaned, start)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def collect_review_diff(workdir: str) -> str:
    """The diff under review: working tree vs HEAD, else the last commit.

    Shared by the second-pass reviewer and the GR-180 reflection pass so both
    reads resolve the SAME change. Mirrors the evaluator's context rule
    (GR-GAP-046): a judge run after a committed change has an empty
    ``git diff HEAD``, so anchor on the last commit instead of reviewing an
    empty diff.
    """
    try:
        result = subprocess.run(  # noqa: S607 - git resolved via PATH by design
            ["git", "diff", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=workdir,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        if result.returncode != 0:
            return ""
    except (OSError, subprocess.TimeoutExpired):
        return ""
    try:
        shown = subprocess.run(  # noqa: S607 - git resolved via PATH by design
            ["git", "show", "--format=", "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=workdir,
            check=False,
        )
        return shown.stdout.strip() if shown.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


class SecondPassReviewer:
    """The GR-171 reviewer: diff in, structured findings out, criteria never.

    There is deliberately NO way to hand this class a task's acceptance
    criteria — the prompt is built from the diff alone, so the pass cannot
    quietly become a re-grade of the author's rubric. One LLM call, no tools.
    """

    def __init__(
        self,
        llm: Any,
        workdir: str,
        max_findings: int = MAX_SECOND_PASS_FINDINGS,
    ) -> None:
        self.llm = llm
        self.workdir = workdir
        self.max_findings = max(1, int(max_findings))

    # ── Prompt assembly (diff-only — no task criteria anywhere) ──────────

    def _system_prompt(self) -> str:
        return SECOND_PASS_SYSTEM_PROMPT.replace("{max_findings}", str(self.max_findings))

    def _collect_diff(self) -> str:
        """The diff under review — :func:`collect_review_diff` (GR-180 shares it)."""
        return collect_review_diff(self.workdir)

    def _build_user_prompt(self, diff_text: str) -> str:
        return (
            "## DIFF UNDER REVIEW (your only input — no task criteria are provided)\n\n"
            f"{diff_text}\n\n"
            "Review this diff per your instructions and output the JSON findings."
        )

    # ── Parsing ──────────────────────────────────────────────────────────

    def _parse_findings(self, content: str) -> tuple[list[SecondPassFinding], str, list[str]]:
        """(valid findings, summary, drop reasons) from one LLM reply."""
        data = _extract_json_object(content)
        if data is None:
            return [], "", [f"no parseable JSON object in reply: {content[:200]!r}"]
        findings: list[SecondPassFinding] = []
        drops: list[str] = []
        for index, raw in enumerate(data.get("findings") or []):
            finding, reason = second_pass_finding_from_dict(raw)
            if finding is None:
                drops.append(f"finding {index}: {reason}")
                continue
            findings.append(finding)
        if len(findings) > self.max_findings:
            drops.append(
                f"findings capped at {self.max_findings} "
                f"(dropped {len(findings) - self.max_findings})"
            )
            findings = findings[: self.max_findings]
        return findings, str(data.get("summary", "") or ""), drops

    # ── The pass itself ──────────────────────────────────────────────────

    def review(self, diff_text: str | None = None) -> SecondPassResult:
        """Run the independent pass over *diff_text* (or the tree's own diff).

        Identities (reviewer vs commit author) are recorded from git on every
        run — including a failed one — so the verdict can always answer
        "who reviewed, and was it the author?" (AC3). A review failure is
        recorded on the result as ``error``; it never raises, because a
        second pass must not be able to fail the judge run it accompanies.
        """
        identities = collect_review_identities(self.workdir)
        result = SecondPassResult(
            reviewer_email=str(identities["reviewer_email"]),
            reviewer_name=str(identities["reviewer_name"]),
            commit_author_email=str(identities["commit_author_email"]),
            commit_author_name=str(identities["commit_author_name"]),
            self_review=identities["self_review"],
        )
        diff = diff_text if diff_text is not None else self._collect_diff()
        if not diff:
            result.error = "no diff to review (clean tree, no commit anchor)"
            return result
        messages = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": self._build_user_prompt(diff)},
        ]
        try:
            response = self.llm.chat(messages, max_tokens=4096)
        except Exception as e:
            logger.warning("Second-pass reviewer LLM call failed: %s", e)
            result.error = f"LLM call failed: {e}"
            return result
        content = getattr(response, "content", "") or ""
        findings, summary, drops = self._parse_findings(content)
        for reason in drops:
            logger.warning("Second-pass reviewer dropped %s", reason)
        result.findings = findings
        result.summary = summary
        if not findings and drops and drops[0].startswith("no parseable"):
            result.error = f"unparseable reviewer reply: {drops[0]}"
        return result


# ════════════════════════════════════════════════════════════════
# GR-180: the reflection pass — vet the reviewer's own findings
# ════════════════════════════════════════════════════════════════
#
# The second pass (above) produces the candidate findings a verdict carries.
# Whatever its model hallucinated — a real-sounding defect about a line that is
# not in the diff, a plausible claim with no evidence behind it — rides into
# the verdict record as if it were verified. The reflection pass is the check
# ON the reviewer: each candidate finding is scored, read against the same
# diff, for correctness (is the claimed defect real?) and evidence (does the
# citation actually show it?), and anything below ``review.reflection_threshold``
# is DROPPED before the verdict record is written.
#
# Fail-closed by construction: a finding whose score is missing, malformed or
# unparseable is dropped, and a reflection whose LLM call fails or whose whole
# reply cannot be parsed drops EVERY candidate — a broken filter never
# promotes. The drop record ({dropped, threshold, dropped_findings, error})
# rides the review block into the persisted verdict, so a drop is visible with
# its reason, never silent.

#: Findings scoring at or above this on BOTH dimensions survive; below it
#: (either dimension) they are dropped with the failing dimension named.
#: Overridable per repo via ``review.reflection_threshold`` in
#: .gitreins/config.yaml — this constant is that setting's default.
DEFAULT_REFLECTION_THRESHOLD = 0.6

REFLECTION_SYSTEM_PROMPT = """\
You are a reflection pass auditing another reviewer's candidate findings before they are written into a verdict. For EACH candidate finding, score two independent dimensions and nothing else — you are a filter, not a second reviewer, and you never add, remove or rewrite findings.

## YOUR INPUT

You receive the diff under review and the candidate findings (each with an index, a severity, a file:line, a detection rule and a detail). That is all.

## SCORE EACH FINDING (0.0–1.0)

- "correctness" — is the claimed defect REAL in this diff? 1.0 = the exact code shape the detection names is visible in the diff and the claim about it is right. 0.0 = the claim contradicts the code.
- "evidence" — does the finding cite evidence that actually shows the defect? The file:line it names must exist in this diff and show what the detection claims. A plausible-sounding claim about a line that is not in the diff, or a detection rule the diff cannot confirm, scores LOW evidence.

Score the two dimensions independently: a real defect whose citation does not hold scores high correctness and LOW evidence.

## OUTPUT FORMAT

When done, output EXACTLY this JSON with no surrounding text, no markdown fences:

{"scores":[{"index":0,"correctness":0.9,"evidence":0.8,"reason":"one line: what you checked"},{"index":1,"correctness":0.2,"evidence":0.1,"reason":"the named line is not in this diff"}],"summary":"<one sentence>"}

Rules:
- Exactly one score entry per candidate finding, keyed by its given "index".
- A finding you cannot assess scores 0.0 on the dimension you cannot assess — you may NOT skip an entry or leave it unscored.
- NEVER invent findings or scores beyond the candidates given."""


@dataclass(frozen=True)
class ReflectionScore:
    """One candidate finding's reflection scores (GR-180).

    ``correctness`` and ``evidence`` are 0.0–1.0; ``reason`` is the scorer's
    one-line account of what it checked (recorded with the drop when the
    scores fall below the threshold).
    """

    correctness: float
    evidence: float
    reason: str = ""


def reflection_score_from_dict(data: Any) -> tuple[ReflectionScore | None, str | None]:
    """Validate one parsed score entry. Returns (score, None) or (None, reason).

    Fail-closed like :func:`second_pass_finding_from_dict`: a score missing a
    dimension, carrying a non-numeric value, or falling outside [0.0, 1.0]
    validates to None — the caller DROPS the finding, never guesses a
    passing value for it.
    """
    if not isinstance(data, dict):
        return None, f"not an object ({type(data).__name__})"
    values: dict[str, float] = {}
    for name in ("correctness", "evidence"):
        raw = data.get(name)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return None, f"{name} {raw!r} is not a number"
        value = float(raw)
        if not 0.0 <= value <= 1.0:
            return None, f"{name} {value!r} outside [0.0, 1.0]"
        values[name] = value
    reason = str(data.get("reason", "") or "").strip()
    return (
        ReflectionScore(
            correctness=values["correctness"], evidence=values["evidence"], reason=reason
        ),
        None,
    )


@dataclass
class DroppedFinding:
    """One candidate finding the reflection dropped, with the reason (GR-180).

    ``finding`` is kept whole so the verdict record can show exactly WHAT was
    dropped — a silent shrink of the findings list is the failure mode this
    pass exists to prevent.
    """

    index: int
    finding: SecondPassFinding
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "severity": self.finding.severity,
            "file": self.finding.file,
            "line": self.finding.line,
            "detection": self.finding.detection,
            "reason": self.reason,
        }


@dataclass
class ReflectionResult:
    """The reflection pass's outcome, in the shape a verdict record persists."""

    kept: list[SecondPassFinding] = field(default_factory=list)
    dropped: list[DroppedFinding] = field(default_factory=list)
    threshold: float = DEFAULT_REFLECTION_THRESHOLD
    # Set when the reflection itself failed (transport, unparseable reply) —
    # the fail-closed contract means the error explains a total drop.
    error: str | None = None

    def to_dict(self) -> dict[str, Any] | None:
        """The verdict-record block, or None when nothing was dropped.

        Absence contract (GR-180): a reflection that dropped nothing — or
        never ran — adds no key, so every pre-GR-180 review dict serializes
        byte-identically. Drops are never silent: every dropped finding is
        listed beside the reason it fell.
        """
        if not self.dropped and self.error is None:
            return None
        d: dict[str, Any] = {
            "dropped": len(self.dropped),
            "threshold": self.threshold,
            "dropped_findings": [entry.to_dict() for entry in self.dropped],
        }
        if self.error is not None:
            d["error"] = self.error
        return d


class FindingReflector:
    """The GR-180 reflection pass: candidate findings in, scored survivors out.

    One LLM call, no tools — the same discipline as SecondPassReviewer, one
    layer up: it never adds findings, and every candidate that fails to earn
    a valid score at or above the threshold on BOTH dimensions is dropped
    with a recorded reason. Deterministic enforcement: the threshold
    comparison happens HERE, in code, never in the model.
    """

    def __init__(
        self,
        llm: Any,
        workdir: str,
        threshold: float = DEFAULT_REFLECTION_THRESHOLD,
    ) -> None:
        self.llm = llm
        self.workdir = workdir
        # Config garbage must not crash the judge run or widen the filter:
        # an unusable threshold falls back to the default (which still vets)
        # and says so in the log.
        if isinstance(threshold, bool):
            logger.warning(
                "reflection_threshold %r is not a number — using default %.2f",
                threshold,
                DEFAULT_REFLECTION_THRESHOLD,
            )
            threshold = DEFAULT_REFLECTION_THRESHOLD
        try:
            self.threshold = float(threshold)
        except (TypeError, ValueError):
            logger.warning(
                "reflection_threshold %r is not a number — using default %.2f",
                threshold,
                DEFAULT_REFLECTION_THRESHOLD,
            )
            self.threshold = DEFAULT_REFLECTION_THRESHOLD

    # ── Prompt assembly ──────────────────────────────────────────────────

    def _system_prompt(self) -> str:
        return REFLECTION_SYSTEM_PROMPT

    def _build_user_prompt(self, findings: list[SecondPassFinding], diff_text: str) -> str:
        rows = "\n".join(
            f"- index={i} severity={f.severity} file={f.file}:{f.line} "
            f"detection: {f.detection} detail: {f.detail or '(none)'}"
            for i, f in enumerate(findings)
        )
        return (
            "## DIFF UNDER REVIEW\n\n"
            f"{diff_text}\n\n"
            "## CANDIDATE FINDINGS TO SCORE\n\n"
            f"{rows}\n\n"
            "Score each finding per your instructions and output the JSON scores."
        )

    # ── Parsing ──────────────────────────────────────────────────────────

    def _parse_scores(self, content: str) -> tuple[dict[int, Any] | None, str | None]:
        """(index → raw score entry) from one reflection reply, or (None, reason).

        ``_extract_json_object`` consumes exactly one JSON value, so trailing
        commentary cannot poison the parse (the module's standing discipline).
        Entries whose index is not a plain integer are skipped here and
        surface later as that finding's per-finding fail-closed drop.
        """
        data = _extract_json_object(content)
        if data is None:
            return None, f"no parseable JSON object in reply: {content[:200]!r}"
        rows = data.get("scores")
        if not isinstance(rows, list):
            return None, "reply carries no 'scores' list"
        by_index: dict[int, Any] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            index = row.get("index")
            if isinstance(index, bool) or not isinstance(index, int):
                continue
            by_index[index] = row
        return by_index, None

    # ── The pass itself ──────────────────────────────────────────────────

    def reflect(
        self, findings: list[SecondPassFinding], diff_text: str | None = None
    ) -> ReflectionResult:
        """Score *findings* against the diff; keep only what clears the threshold.

        Fail-closed everywhere: a finding with no score entry, an unusable
        score, or a score below the threshold on either dimension is DROPPED
        with the reason recorded; an LLM failure or an unparseable reply drops
        EVERY candidate and names the failure on the result. Never raises —
        the reflection must not be able to fail the judge run it vets, and it
        must never promote a finding it could not score.
        """
        result = ReflectionResult(threshold=self.threshold)
        if not findings:
            return result  # nothing to vet — no LLM call, no drops, no record
        diff = diff_text if diff_text is not None else collect_review_diff(self.workdir)
        messages = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": self._build_user_prompt(findings, diff)},
        ]
        try:
            response = self.llm.chat(messages, max_tokens=4096)
        except Exception as e:
            logger.warning(
                "Reflection pass LLM call failed: %s — dropping %d finding(s) fail-closed",
                e,
                len(findings),
            )
            result.error = f"reflection LLM call failed: {e}"
            result.dropped = [
                DroppedFinding(index=i, finding=f, reason=f"reflection LLM call failed: {e}")
                for i, f in enumerate(findings)
            ]
            return result
        content = getattr(response, "content", "") or ""
        by_index, parse_error = self._parse_scores(content)
        if by_index is None:
            logger.warning(
                "Reflection reply unparseable — dropping %d finding(s) fail-closed",
                len(findings),
            )
            result.error = f"unparseable reflection reply: {parse_error}"
            result.dropped = [
                DroppedFinding(index=i, finding=f, reason=str(parse_error))
                for i, f in enumerate(findings)
            ]
            return result
        for i, finding in enumerate(findings):
            raw = by_index.get(i)
            if raw is None:
                result.dropped.append(
                    DroppedFinding(
                        index=i,
                        finding=finding,
                        reason=f"reflection returned no score for finding {i}",
                    )
                )
                continue
            score, why = reflection_score_from_dict(raw)
            if score is None:
                result.dropped.append(
                    DroppedFinding(
                        index=i,
                        finding=finding,
                        reason=f"unusable reflection score for finding {i}: {why}",
                    )
                )
                continue
            below = [
                name
                for name, value in (
                    ("correctness", score.correctness),
                    ("evidence", score.evidence),
                )
                if value < self.threshold
            ]
            if below:
                result.dropped.append(
                    DroppedFinding(
                        index=i,
                        finding=finding,
                        reason=(
                            f"{' and '.join(below)} below threshold "
                            f"{self.threshold:.2f} (correctness {score.correctness:.2f}, "
                            f"evidence {score.evidence:.2f})"
                        ),
                    )
                )
                continue
            result.kept.append(finding)
        for dropped in result.dropped:
            logger.warning("Reflection dropped finding %d: %s", dropped.index, dropped.reason)
        return result
