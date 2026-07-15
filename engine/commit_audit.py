"""
Commit message auditor — LLM validates commit messages against staged diffs.

Runs as a Tier 2 pipeline stage. The LLM can optionally explore the codebase
with read_file / search_pattern tools when the diff alone is insufficient.

Config keys (under ``commit_audit`` in .gitreins/config.yaml):
  ``enabled``      — bool, default True
  ``mode``         — "warn" (default) | "block" | "suggest"
  ``strictness``   — "lenient" (default) | "standard" | "strict"
  ``max_iterations`` — int, default 3 (LLM exploration rounds)
  ``suggest_message`` — bool, default True (suggest better message on block/warn)
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from typing import Any

from engine.llm import LLMClient, LLMResponse

logger = logging.getLogger("gitreins.commit_audit")


# ── Trailer parsing (GR-064c) ────────────────────────────────────

# Standard git trailer format: ``Key: value`` on its own line at the
# end of the commit message body, after a blank-line separator from the
# body.  See https://git-scm.com/docs/git-interpret-trailers for the
# full grammar.  We deliberately support a minimal subset:
#   * Lines look like ``Token: value`` (Token followed by colon+space)
#   * The trailer block starts after the last blank line in the message
#   * Tokens may contain alphanumerics, dots, hyphens, slashes (the
#     ``gitreins.skip-tier2`` token is a non-standard but allowed form)

_TRAILER_PATTERN = re.compile(
    r"^(?P<key>[A-Za-z][A-Za-z0-9._-]*[A-Za-z0-9._/-]|[A-Za-z])\s*:\s*(?P<value>.+)$"
)


def parse_trailers(message: str) -> dict[str, str]:
    """Parse git-style trailers from a commit message body.

    Returns a dict mapping trailer key (lower-cased) to value.  Trailing
    whitespace and a trailing newline are tolerated.  Unknown / blank
    lines are ignored.  If the message has no trailer block, returns
    an empty dict.

    The trailer block is defined as the contiguous run of ``Key: value``
    lines at the end of the message after a blank-line separator from
    the body (this matches git's own ``git interpret-trailers``
    behaviour for the common case of footer trailers).
    """
    if not message:
        return {}

    # Normalise line endings, strip trailing whitespace per-line
    lines = message.replace("\r\n", "\n").rstrip().split("\n")

    # Walk backwards: collect trailer-like lines, stop at first non-trailer.
    # git's actual rules allow continuation lines (indented) inside the
    # block, but for our supported trailer (a single boolean toggle) we
    # require a contiguous run of ``Key: value`` lines at the end of the
    # message after a blank-line separator.
    trailers: dict[str, str] = {}
    body_done = False

    for line in reversed(lines):
        stripped = line.strip()
        if not stripped:
            # Blank line — keep walking back; the body must end before trailers.
            body_done = True
            continue
        if body_done:
            # We're past the blank-line separator — anything that isn't
            # a trailer-like line means there are no trailers.
            pass

        m = _TRAILER_PATTERN.match(line)
        if not m:
            if body_done:
                # Trailer block ended; this line is body or junk.
                break
            # No blank line yet — assume the entire end is trailer block.
            break

        key = m.group("key").strip().lower()
        value = m.group("value").strip()
        trailers[key] = value

    return trailers


def has_skip_tier2_trailer(message: str) -> bool:
    """Return True iff the commit message body contains the trailer
    ``gitreins.skip-tier2: true`` (case-insensitive trailer key, value
    parsed as truthy: ``true``/``yes``/``on``/``1``).

    Per GR-064c, this trailer lets users bypass Tier 2 LLM evaluation
    on a per-commit basis without modifying config or using a CLI flag.
    """
    if not message:
        return False
    trailers = parse_trailers(message)
    # Accept several possible token forms — be lenient.
    for key in ("gitreins.skip-tier2", "gitreins.skiptier2", "skip-tier2", "skiptier2"):
        if key in trailers:
            value = trailers[key].strip().lower()
            if value in ("true", "yes", "on", "1"):
                return True
    return False


# ── System prompt ───────────────────────────────────────────────

COMMIT_AUDIT_SYSTEM_PROMPT = """\
You are a commit message auditor for GitReins. Your job is to verify that a
proposed commit message accurately describes the staged changes in the diff.

**Rules (based on strictness setting):**

*LENIENT:* Only flag truly misleading or empty messages.
*STANDARD:* Flag vague messages ("fix stuff"), missing scope, and incomplete descriptions.
*STRICT:* Enforce conventional commits format (type(scope): description),
require that every logical change group is mentioned.

**When the message is problematic:**
- Explain WHY it's wrong (be specific — cite the diff)
- Suggest a CORRECTED commit message that matches the diff

**When the message is good:**
- Output {"valid": true}

**Output Format (JSON only — no markdown fences, no extra text):**

For valid messages:
{"valid": true}

For invalid messages:
{"valid": false, "issues": ["issue 1", "issue 2"], "suggested_message": "type(scope): better description"}

The "issues" array explains what's wrong. Use imperative mood and precise file/function references from the diff."""


# ── Code review system prompt (GR-065: CodeRabbit-style review) ─

COMMIT_REVIEW_SYSTEM_PROMPT = """\
You are a senior code reviewer for GitReins. Your job is to review staged
changes in a git diff and identify issues — bugs, security vulnerabilities,
anti-patterns, style violations, and performance problems.

**Output Format (JSON only — no markdown fences, no extra text):**

For clean code:
{"valid": true, "summary": "No issues found."}

For code with issues:
{
  "valid": false,
  "summary": "Brief overall assessment (1 sentence).",
  "overall_score": 8.5,
  "issues": [
    {
      "file": "relative/path/file.py",
      "line": 42,
      "severity": "critical|high|medium|low|info",
      "score": 9.0,
      "category": "bugs|security|anti_patterns|style|performance",
      "title": "Short issue title (5-10 words)",
      "description": "What's wrong and why it matters.",
      "suggestion": "How to fix it — specific, actionable, with code if helpful."
    }
  ]
}

**CVE-Style Severity Scoring (1-10 numeric):**

Each issue MUST include a numeric ``score`` (1.0–10.0) reflecting real-world impact:

| Score Range | Severity Label | Description |
|:-----------:|:--------------:|-------------|
| 9.0–10.0 | critical | Remote code execution, data breach, auth bypass, production outage guaranteed. |
| 7.0–8.9  | high | User-visible bug, data corruption, security vulnerability with known exploit pattern, incident-likely. |
| 4.0–6.9  | medium | Code smell, anti-pattern, maintenance burden, potential future bug, tech debt. |
| 1.0–3.9  | low | Style nit, naming convention, minor readability improvement, informational. |
| 0.0       | info | Observation — not a problem, just something to be aware of. |

The ``overall_score`` MUST equal the highest single issue score in the report (worst case). If no issues: ``overall_score = 0.0``.

**Review Categories (which ones to check are passed in the prompt):**

*BUGS:*
- Logic errors, off-by-one, inverted conditions
- Missing null/None checks before dereference
- Incorrect exception handling (bare except, swallowing errors silently)
- Race conditions (shared mutable state without locks)
- Resource leaks (unclosed files, connections, sockets)
- Wrong argument order or type mismatches that would cause runtime errors
- Missing error propagation (returning nil/None instead of wrapping errors)

*SECURITY:*
- Hardcoded credentials, API keys, tokens, secrets
- SQL injection via string concatenation (use parameterized queries)
- Command injection via shell=True with user input
- Path traversal (unsanitized file paths from user input)
- Missing authentication or authorization checks
- Insecure random generation (using math/random instead of crypto)
- Missing input validation on user-supplied data
- Sensitive data logged or exposed in error messages
- Insecure deserialization (pickle, yaml.load without SafeLoader)
- Missing Content-Security-Policy or other security headers
- XXE vulnerabilities in XML parsing
- Open redirect via unvalidated URL parameters

*ANTI_PATTERNS:*
- God functions/methods (too long, too many responsibilities)
- Magic numbers without named constants
- Duplicate code that should be extracted
- Tight coupling between unrelated modules
- Premature optimization (complex code for unmeasured performance gain)
- Commented-out code left in production
- TODO/FIXME without a tracking issue reference
- Using mutable defaults in function signatures (Python: def f(x=[]))
- Mixing abstraction levels in the same function
- Catching Exception and continuing silently
- Inconsistent error handling patterns

*STYLE:*
- Naming conventions violated (snake_case vs camelCase, etc.)
- Inconsistent formatting (not matching project style)
- Missing or misleading docstrings/comments
- Overly complex one-liners (hard to read)
- Dead code (unreachable statements, unused variables after dead assignments)

*PERFORMANCE:*
- N+1 query patterns (query inside loop)
- Unnecessary allocations in hot paths
- Blocking I/O on async/event-loop threads
- Missing caching for expensive repeated operations
- Inefficient data structures (list for membership testing vs set)
- Large objects passed by value instead of reference
- Missing lazy evaluation (eager loading when streaming would work)
- Regex compiled inside a loop instead of once

**Severity Guidelines:**

- `critical`: Security vulnerability, data loss risk, or crash guaranteed in common paths. BLOCK merge.
- `high`: Bug with user-visible impact, likely to cause incidents. Should block merge.
- `medium`: Code smell or anti-pattern that will cause maintenance pain. Warn but allow.
- `low`: Style nit, naming convention, minor improvement. Informational.
- `info`: Observation — not a problem, just something to be aware of.

**Review Principles:**

1. Be specific. Reference exact file paths, line numbers, and code patterns.
2. Be actionable. Every issue must include a suggestion on how to fix it.
3. Be proportional. Flag real problems; don't nitpick for the sake of it.
4. Respect the severity filter. Don't report `low` issues in `critical-only` mode.
5. Consider the diff holistically. If a change looks incomplete (e.g., added a function but never called it), flag it.
6. Trust but verify. If the diff adds tests, check that they test meaningful behavior — not just test the mock.
7. No false positives. If you're unsure whether something is a bug, mention it as `info` with a caveat, not as `medium`.

**What NOT to flag:**
- Pre-existing issues in untouched code (focus on the diff only)
- Test fixture keys/configs that are clearly test-only (e.g., `sk-test-...`)
- Formatting differences that match the project's auto-formatter output
- Comments that add value (explain WHY, not WHAT)
- Reasonable design choices that differ from your preference but are not wrong"""


COMMIT_AUDIT_USER_PROMPT = """\
## STRICTNESS: {strictness}
## MODE: {mode}

## PROPOSED COMMIT MESSAGE
{message}

## STAGED DIFF
{diff}

## INSTRUCTIONS
{instructions}"""


INSTRUCTIONS_LENIENT = """\
Check if the commit message is completely wrong or empty. Only flag messages
that would actively mislead someone reading the git history.

Allow: short messages, informal messages, single-word fixes.
Reject only: empty messages, messages describing changes not in the diff."""

INSTRUCTIONS_STANDARD = """\
Check if the commit message describes what actually changed. Flag messages that
are too vague ("fix stuff", "update code") or that omit significant changes.

A good message: summarizes the change, uses imperative mood, mentions the area affected.
Allow: reasonable shorthand. Reject: generic placeholder messages."""

INSTRUCTIONS_STRICT = """\
Enforce conventional commits format: type(scope): description.
- type: feat, fix, refactor, docs, test, chore, perf, ci, build, revert
- scope: optional but encouraged (file, module, component)
- description: imperative, lowercase, no period at end

Every logical change group in the diff must be reflected in the message.
Reject: anything that doesn't follow conventional commits, or that omits
changes visible in the diff."""

INSTRUCTIONS_BY_STRICTNESS = {
    "lenient": INSTRUCTIONS_LENIENT,
    "standard": INSTRUCTIONS_STANDARD,
    "strict": INSTRUCTIONS_STRICT,
}


REVIEW_USER_PROMPT = """\
## COMMIT MESSAGE
{message}

## STAGED DIFF
{diff}

## REVIEW CONFIGURATION
Active checks: {checks}
Severity filter: {severity}
Fix suggestions: {fix}

## INSTRUCTIONS
Review the staged diff for issues. Check ONLY the categories listed above.
Respect the severity filter. {fix}"""


# ── Audit tools — same pattern as evaluator tools ────────────────

COMMIT_AUDIT_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the working directory to understand context beyond the diff.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to the file."},
                    "offset": {"type": "integer", "description": "Line offset (1-indexed)."},
                    "limit": {"type": "integer", "description": "Max lines (default 200)."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_pattern",
            "description": "Search for patterns in the codebase to check if related files were missed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for."},
                    "path": {"type": "string", "description": "Directory to search in (default: .)."},
                    "file_glob": {"type": "string", "description": "Filter by file glob (e.g. '*.py')."},
                },
                "required": ["pattern"],
            },
        },
    },
]


# ── Result dataclasses ───────────────────────────────────────────

@dataclass
class CommitAuditResult:
    """Result of a commit message audit."""

    valid: bool
    """True if the message accurately describes the diff."""

    issues: list[str] = field(default_factory=list)
    """What's wrong with the message (empty if valid)."""

    suggested_message: str = ""
    """A corrected commit message (empty if valid)."""

    iterations_used: int = 0
    """How many LLM iterations were consumed."""

    review_issues: list = field(default_factory=list)
    """Full ReviewIssue dicts from CodeRabbit-style review (GR-065).
    Each dict has: file, line, severity, category, title, description, suggestion."""

    review_summary: str = ""
    """Summary from the review (GR-065)."""

    @property
    def action(self) -> str:
        """The action the hook should take: 'pass', 'warn', or 'block'."""
        if self.valid:
            return "pass"
        return "block"  # block maps to reject; mode determines warn vs block upstream


@dataclass
class ReviewIssue:
    """A single issue found during code review (GR-065, GR-066)."""
    file: str
    line: int
    severity: str   # critical|high|medium|low|info
    category: str   # bugs|security|anti_patterns|style|performance
    title: str
    description: str = ""
    suggestion: str = ""
    score: float = 0.0  # GR-066: CVE-style 1-10 score

    @classmethod
    def from_dict(cls, d: dict) -> "ReviewIssue":
        return cls(
            file=d.get("file", ""),
            line=int(d.get("line", 0)),
            severity=d.get("severity", "info"),
            category=d.get("category", "style"),
            title=d.get("title", ""),
            description=d.get("description", ""),
            suggestion=d.get("suggestion", ""),
            score=float(d.get("score", 0.0)),
        )


@dataclass
class CommitReviewResult:
    """Result of a CodeRabbit-style commit review (GR-065, GR-066)."""
    valid: bool
    summary: str = ""
    issues: list[ReviewIssue] = field(default_factory=list)
    message_valid: bool = True
    message_issues: list[str] = field(default_factory=list)
    suggested_message: str = ""
    iterations_used: int = 0
    overall_score: float = 0.0  # GR-066: worst issue score, 0 if no issues


# ═════════════════════════════════════════════════════════════════
# CommitAuditor
# ═════════════════════════════════════════════════════════════════

class CommitAuditor:
    """LLM-powered commit message validator.

    Can optionally explore the codebase with read_file / search_pattern
    tools when the staged diff alone isn't enough context.
    """

    def __init__(
        self,
        llm: LLMClient,
        workdir: str = ".",
        *,
        strictness: str = "standard",
        max_iterations: int = 3,
        suggest_message: bool = True,
        review_mode: str = "message",
        review_checks: dict | None = None,
        review_severity: str = "standard",
        review_suggest_fix: bool = True,
        review_score_threshold: float = 8.0,
        review_score_offset: float = 1.0,
    ):
        self.llm = llm
        self.workdir = os.path.abspath(workdir)
        self.strictness = strictness
        self.max_iterations = max_iterations
        self.suggest_message = suggest_message
        self.review_mode = review_mode
        self.review_checks = review_checks or {
            "bugs": True, "security": True, "anti_patterns": True,
            "style": False, "performance": False,
        }
        self.review_severity = review_severity
        self.review_suggest_fix = review_suggest_fix
        self.review_score_threshold = review_score_threshold
        self.review_score_offset = review_score_offset

    # ── Public API ──────────────────────────────────────────────

    def audit(
        self,
        message: str,
        diff: str | None = None,
    ) -> CommitAuditResult:
        """Audit a commit message against a staged diff.

        Args:
            message: The proposed commit message.
            diff: The staged diff (git diff --cached). If None, captured automatically.

        Returns:
            CommitAuditResult with valid/issue/suggested_message.
        """
        if self.review_mode != "message":
            # Code review mode — returns CommitReviewResult
            return self._run_review(message, diff)

        if diff is None:
            diff = self._capture_diff()

        if not diff.strip():
            return CommitAuditResult(valid=True)

        if not message.strip():
            return CommitAuditResult(
                valid=False,
                issues=["Empty commit message."],
                suggested_message=self._generate_fallback_message(diff) if self.suggest_message else "",
            )

        # Fast path: single LLM call (no tools needed for simple diffs)
        result = self._single_pass(message, diff)

        if result is not None:
            return result

        # Tool-enabled path: LLM wants to explore before judging
        return self._tool_loop(message, diff)

    # ── Review mode (GR-065: CodeRabbit-style) ──────────────────

    def review(
        self,
        message: str,
        diff: str | None = None,
    ) -> "CommitReviewResult":
        """Run a CodeRabbit-style commit review."""
        if diff is None:
            diff = self._capture_diff()
        if not diff.strip():
            return CommitReviewResult(valid=True, summary="No changes to review.")

        # Build review prompt
        active_checks = [k for k, v in self.review_checks.items() if v]
        checks_str = ", ".join(active_checks)

        severity_map = {
            "critical-only": "Only report critical and high severity issues. Skip medium, low, and info.",
            "standard": "Report critical, high, and medium severity issues. Skip low and info.",
            "all": "Report all issues regardless of severity.",
        }
        severity_instr = severity_map.get(self.review_severity, severity_map["standard"])

        fix_instr = (
            "Include specific, actionable fix suggestions for every issue."
            if self.review_suggest_fix else "Do NOT include fix suggestions — just identify issues."
        )

        review_prompt = REVIEW_USER_PROMPT.format(
            message=message,
            diff=diff[:15000],
            checks=checks_str,
            severity=severity_instr,
            fix=fix_instr,
        )

        if self.review_mode == "agent":
            return self._review_tool_loop(message, diff, review_prompt)

        # Single-call review
        return self._review_single_call(review_prompt)

    def _review_single_call(self, review_prompt: str) -> "CommitReviewResult":
        """Single LLM call for code review."""
        try:
            response = self.llm.chat(
                messages=[
                    {"role": "system", "content": COMMIT_REVIEW_SYSTEM_PROMPT},
                    {"role": "user", "content": review_prompt},
                ],
                temperature=0.1,
                max_tokens=2048,
            )
        except Exception as e:
            logger.warning("Code review LLM call failed: %s", e)
            return CommitReviewResult(
                valid=True, summary=f"Review unavailable: {e}",
                iterations_used=1,
            )

        return self._parse_review_result(response, iteration=1)

    def _review_tool_loop(self, message: str, diff: str, review_prompt: str) -> "CommitReviewResult":
        """Multi-turn review with tool access."""
        # Stub for agent mode — reuses existing tool loop pattern
        messages: list[dict] = [
            {"role": "system", "content": COMMIT_REVIEW_SYSTEM_PROMPT},
            {"role": "user", "content": review_prompt},
        ]

        for iteration in range(1, self.max_iterations + 1):
            try:
                response = self.llm.chat(
                    messages=messages,
                    tools=COMMIT_AUDIT_TOOLS,
                    temperature=0.1,
                    max_tokens=2048,
                )
            except Exception as e:
                return CommitReviewResult(
                    valid=True,
                    summary=f"Review error on iteration {iteration}: {e}",
                    iterations_used=iteration,
                )

            if not response.tool_calls:
                if response.content:
                    return self._parse_review_result(response, iteration=iteration)
                return CommitReviewResult(valid=True, summary="No issues found.", iterations_used=iteration)

            assistant_msg = {
                "role": "assistant",
                "content": response.content,
                "tool_calls": [
                    {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                    for tc in response.tool_calls
                ],
            }
            messages.append(assistant_msg)

            for tc in response.tool_calls:
                tool_result = self._execute_tool(tc)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": tool_result})

        return CommitReviewResult(
            valid=True,
            summary=f"Review exhausted {self.max_iterations} iterations without verdict",
            iterations_used=self.max_iterations,
        )

    def _parse_review_result(self, response: LLMResponse, iteration: int = 1) -> "CommitReviewResult":
        """Parse the structured review JSON from the LLM."""
        content = (response.content or "").strip()
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            # Try to extract the first complete JSON object (non-greedy)
            match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", content)
            if match:
                try:
                    data = json.loads(match.group(0))
                except json.JSONDecodeError:
                    return CommitReviewResult(
                        valid=True, summary=f"Could not parse review: {content[:200]}",
                        iterations_used=iteration,
                    )
            else:
                return CommitReviewResult(
                    valid=True, summary=f"Could not parse review: {content[:200]}",
                    iterations_used=iteration,
                )

        issues = [ReviewIssue.from_dict(i) for i in data.get("issues", [])]
        overall_score = float(data.get("overall_score", 0.0))
        if not overall_score and issues:
            overall_score = max(i.score for i in issues)
        return CommitReviewResult(
            valid=data.get("valid", True),
            summary=data.get("summary", ""),
            issues=issues,
            message_valid=data.get("message_valid", True),
            message_issues=data.get("message_issues", []),
            suggested_message=data.get("suggested_message", ""),
            iterations_used=iteration,
            overall_score=overall_score,
        )

    def _run_review(self, message: str, diff: str | None = None) -> CommitAuditResult:
        """Bridge: run review mode and convert to CommitAuditResult for compatibility."""
        rev = self.review(message, diff)
        return CommitAuditResult(
            valid=rev.valid and rev.message_valid,
            issues=[f"[{i.severity}][{i.category}] {i.file}:{i.line}: {i.title} (score: {i.score:.1f})" for i in rev.issues],
            suggested_message=rev.suggested_message,
            iterations_used=rev.iterations_used,
            review_issues=[
                {
                    "file": i.file,
                    "line": i.line,
                    "severity": i.severity,
                    "category": i.category,
                    "title": i.title,
                    "description": i.description,
                    "suggestion": i.suggestion,
                    "score": i.score,
                }
                for i in rev.issues
            ],
            review_summary=rev.summary,
        )

    # ── Fast path: one LLM call ─────────────────────────────────

    def _single_pass(self, message: str, diff: str) -> CommitAuditResult | None:
        """Try a single LLM call. Returns None if the LLM wants tools."""
        instructions = INSTRUCTIONS_BY_STRICTNESS.get(
            self.strictness, INSTRUCTIONS_STANDARD
        )

        prompt = COMMIT_AUDIT_USER_PROMPT.format(
            strictness=self.strictness.upper(),
            mode="audit",
            message=message,
            diff=diff[:15000],  # cap diff — enough for most commits
            instructions=instructions,
        )

        try:
            response = self.llm.chat(
                messages=[
                    {"role": "system", "content": COMMIT_AUDIT_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                tools=COMMIT_AUDIT_TOOLS,  # offer tools — LLM only uses if needed
                temperature=0.1,
                max_tokens=1024,
            )
        except Exception as e:
            logger.warning("Commit audit LLM call failed: %s", e)
            return CommitAuditResult(valid=True, issues=[f"LLM unavailable: {e}"])

        if response.tool_calls:
            return None  # LLM wants tools — escalate to tool loop

        if response.content:
            return self._parse_result(response, iteration=1)

        return CommitAuditResult(valid=True)  # empty response = pass

    # ── Tool loop: LLM explores then judges ─────────────────────

    def _tool_loop(self, message: str, diff: str) -> CommitAuditResult:
        """Run a tool-enabled evaluation loop (max N iterations)."""
        instructions = INSTRUCTIONS_BY_STRICTNESS.get(
            self.strictness, INSTRUCTIONS_STANDARD
        )

        prompt = COMMIT_AUDIT_USER_PROMPT.format(
            strictness=self.strictness.upper(),
            mode="audit",
            message=message,
            diff=diff[:15000],
            instructions=instructions,
        )

        messages: list[dict] = [
            {"role": "system", "content": COMMIT_AUDIT_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]

        for iteration in range(1, self.max_iterations + 1):
            try:
                response = self.llm.chat(
                    messages=messages,
                    tools=COMMIT_AUDIT_TOOLS,
                    temperature=0.1,
                    max_tokens=1024,
                )
            except Exception as e:
                logger.warning("Commit audit LLM call failed on iteration %d: %s", iteration, e)
                return CommitAuditResult(
                    valid=True,
                    issues=[f"LLM error on iteration {iteration}: {e}"],
                    iterations_used=iteration,
                )

            # No tool calls → verdict
            if not response.tool_calls:
                if response.content:
                    return self._parse_result(response, iteration=iteration)
                return CommitAuditResult(valid=True, iterations_used=iteration)

            # Add assistant message
            assistant_msg = {
                "role": "assistant",
                "content": response.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in response.tool_calls
                ],
            }
            messages.append(assistant_msg)

            # Execute tool calls
            for tc in response.tool_calls:
                tool_result = self._execute_tool(tc)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": tool_result,
                })

        # Exhausted iterations without verdict
        return CommitAuditResult(
            valid=True,  # err on the side of passing
            issues=[f"Audit exhausted {self.max_iterations} iterations without verdict"],
            iterations_used=self.max_iterations,
        )

    # ── Tool execution ──────────────────────────────────────────

    def _execute_tool(self, tc: Any) -> str:
        """Execute a tool call and return the result as a string."""
        name = tc.name
        args = tc.arguments

        if name == "read_file":
            return self._tool_read_file(
                args.get("path", ""),
                args.get("offset", 1),
                args.get("limit", 200),
            )

        if name == "search_pattern":
            return self._tool_search_pattern(
                args.get("pattern", ""),
                args.get("path", "."),
                args.get("file_glob", None),
            )

        return json.dumps({"error": f"Unknown tool: {name}"})

    def _tool_read_file(self, path: str, offset: int = 1, limit: int = 200) -> str:
        """Read a file from the working directory."""
        full_path = os.path.join(self.workdir, path)
        if not os.path.isfile(full_path):
            return json.dumps({"error": f"File not found: {path}"})
        try:
            with open(full_path, "r") as f:
                lines = f.readlines()
            total = len(lines)
            start = max(0, offset - 1)
            end = min(total, start + limit)
            content = "".join(lines[start:end])
            header = f"// {path} (lines {start+1}-{end}/{total})\n"
            return header + content
        except Exception as e:
            return json.dumps({"error": str(e)})

    def _tool_search_pattern(
        self, pattern: str, path: str = ".", file_glob: str | None = None
    ) -> str:
        """Search for a regex pattern in the codebase."""
        import re as _re
        search_dir = os.path.join(self.workdir, path)
        if not os.path.isdir(search_dir):
            return json.dumps({"error": f"Directory not found: {path}"})

        results: list[str] = []
        try:
            compiled = _re.compile(pattern)
        except _re.error as e:
            return json.dumps({"error": f"Invalid regex: {e}"})

        for root, dirs, files in os.walk(search_dir):
            # Skip hidden dirs and venvs
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in ("venv", "node_modules", "__pycache__")]
            for fname in files:
                if file_glob:
                    import fnmatch
                    if not fnmatch.fnmatch(fname, file_glob):
                        continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r") as f:
                        for i, line in enumerate(f, 1):
                            if compiled.search(line):
                                rel = os.path.relpath(fpath, self.workdir)
                                results.append(f"{rel}:{i}: {line.rstrip()}")
                                if len(results) >= 20:
                                    break
                except Exception:
                    pass
                if len(results) >= 20:
                    break
            if len(results) >= 20:
                break

        if not results:
            return json.dumps({"matches": 0, "hint": "No matches found"})

        return f"Found {len(results)} matches:\n" + "\n".join(results)

    # ── Diff capture ─────────────────────────────────────────────

    def _capture_diff(self) -> str:
        """Capture the staged diff from git."""
        try:
            result = subprocess.run(
                ["git", "diff", "--cached"],
                capture_output=True, text=True, timeout=60,
                cwd=self.workdir,
            )
            if result.returncode == 0:
                return result.stdout
            # No HEAD yet — try diff against empty tree
            result = subprocess.run(
                ["git", "diff", "--cached", "4b825dc642cb6eb9a060e54bf899d92e65bfb3a0"],
                capture_output=True, text=True, timeout=60,
                cwd=self.workdir,
            )
            return result.stdout
        except Exception:
            # Fallback: list all staged files
            try:
                result = subprocess.run(
                    ["git", "diff", "--cached", "--stat"],
                    capture_output=True, text=True, timeout=60,
                    cwd=self.workdir,
                )
                return result.stdout
            except Exception:
                return ""

    # ── Result parsing ───────────────────────────────────────────

    def _parse_result(self, response: LLMResponse, iteration: int = 1) -> CommitAuditResult:
        """Parse the LLM's JSON verdict."""
        content = (response.content or "").strip()

        # Strip markdown fences if present
        if content.startswith("```"):
            content = re.sub(r"^```(?:json)?\s*", "", content)
            content = re.sub(r"\s*```$", "", content)

        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            # Try to extract JSON from mixed content
            match = re.search(r"\{[\s\S]*\}", content)
            if match:
                try:
                    data = json.loads(match.group(0))
                except json.JSONDecodeError:
                    return CommitAuditResult(
                        valid=True,
                        issues=[f"Could not parse audit response: {content[:200]}"],
                        iterations_used=iteration,
                    )
            else:
                return CommitAuditResult(
                    valid=True,
                    issues=[f"Could not parse audit response: {content[:200]}"],
                    iterations_used=iteration,
                )

        return CommitAuditResult(
            valid=data.get("valid", True),
            issues=data.get("issues", []),
            suggested_message=data.get("suggested_message", ""),
            iterations_used=iteration,
        )

    # ── Fallback message generation ──────────────────────────────

    def _generate_fallback_message(self, diff: str) -> str:
        """Generate a minimal commit message from diff stats (no LLM)."""
        try:
            result = subprocess.run(
                ["git", "diff", "--cached", "--stat"],
                capture_output=True, text=True, timeout=60,
                cwd=self.workdir,
            )
            stat = result.stdout.strip()
            if stat:
                # Extract first changed file as hint
                lines = stat.split("\n")
                last_line = lines[-1] if lines else ""
                return f"chore: update {last_line.split('|')[0].strip() if '|' in last_line else 'files'}"
        except Exception:
            pass
        return "chore: update files"
