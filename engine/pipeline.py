"""
Pipeline Engine — Configurable evaluation pipelines.

Pipelines are defined in .gitreins/config.yaml as a list of stages.
Each stage can be sequential or parallel. Results pipe between stages.

Key features:
    - Nested lists = parallel groups (items in a parallel list run concurrently)
    - Flat lists = sequential stages
    - Conditional execution (skip AI if scripts pass)
    - Result piping (failures from Tier 1 feed into Tier 2 AI context)
    - Script stages, AI evaluation stages, output stages

YAML schema:

pipeline:
  stages:
    - id: tier1
      parallel: true
      on: [pre-commit, pre-eval]   # When to run
      steps:
        - id: secrets
          type: script
          # DF-012: gitleaks alone is not trustworthy (its default rules
          # miss sk-/ghp_ patterns) — cross-check with the built-in scanner.
          run: "gitleaks detect --source . --no-git --no-banner && built-in cross-check"
          on_fail: continue          # continue | block | skip_remaining

        - id: lint
          type: script
          run: "ruff check ."

    - id: tier2
      type: ai_eval
      condition: "stage.tier1.any_failed"
      max_iterations: -1  # Defer to evaluator config
      tools: [read_file, run_command, search_pattern, read_diff, sandbox]
      prompt_template: |
        Evaluate task completeness.
        Tier 1 results: {{ stage.tier1 }}
        Criteria: {{ task.criteria }}

    - id: verdict
      type: output

    - id: fix_verification          # GR-235: appended to the default pipeline
      type: fix_verification        # after tier2 — deterministic, no LLM call
      condition: "true"
"""

import concurrent.futures
import hashlib
import json
import logging
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

import yaml

from engine import command_hygiene, lang_detect, scanner_nice, usage
from engine.evidence_bounds import (
    _ERROR_TEST_LINE,
    MAX_STEP_EVIDENCE_CHARS as _MAX_STEP_EVIDENCE_CHARS,
    _bound_step_evidence,
)
from engine.guard_manager import (
    GITLEAKS_MAX_PROCS,
    HARNESS_STATE_DIRS,
    _pytest_no_tests_benign,
    _pytest_runner_missing_hint,
    _resolve_test_command,
    harness_scan_gitleaks_config,
)
from engine.types import (
    _FAILED_TEST_LINE,
    _SECRETS_FINDING_LINE,
    _first_nonblank_line,
    parse_first_failing_test,
    pytest_outcome,
    strip_ansi,
)

# Re-exported for this module's callers/tests (tests/test_pipeline.py and
# tests/test_pytest_outcome.py import it from here). The rebind is deliberate:
# pyflakes reports a bare re-import as unused — it accepts only a module-level
# binding or ``__all__``, and does not honour ``noqa`` comments (a
# flake8/ruff-only concept).
MAX_STEP_EVIDENCE_CHARS = _MAX_STEP_EVIDENCE_CHARS

if TYPE_CHECKING:
    from engine.evaluator import VerdictItem

logger = logging.getLogger("gitreins.pipeline")

# DF-GITREINS-POC-8 / DF-GITREINS-POC-19: the evidence budget and the
# line-bound bounder live in engine/evidence_bounds.py (imported above) so
# pipeline steps, worktree fleet runs and disposable QA runs share ONE
# implementation instead of three copies that drift apart.

# INT-FLAKE-2: script steps whose command is a pytest invocation get their
# outcome classified (engine.types.pytest_outcome) instead of leaving a bare
# exit code in the verdict. Matches `pytest`, `python -m pytest`, `uv run
# pytest`, and a venv console script — the whole `\bpytest\b` word.
_PYTEST_INVOCATION = re.compile(r"\bpytest\b")

# DF-GITREINS-POC-49: the skip reason a benign pytest exit-5 step records. The
# exact wording engine/guard_manager.py:2098 puts on its GuardResult, so the two
# surfaces report the same tree with the same words.
_PYTEST_SKIP_NO_TESTS = "no tests collected"

# TRUST-001: a step that SKIPS (e.g. the linter is not on PATH) prints this
# marker and exits 0, so the stage can record the gate it never graded instead
# of reading as a graded pass. Format on the line:
#   <marker> <step-id>=<short reason>
SKIP_SENTINEL = "GITREINS_SKIP:"

# DF-GITREINS-POC-61: the env var this module's Tier 1 tests step stamps into
# its own CHILD environment (never into this process — see _run_script_step's
# per-step `env` merge). tests/conftest.py imports this name: when the child
# sees it set to "1" the `live`-marked (real-egress) tests are skipped, so a
# skipif-guarded host-global smoke test can never grade a judge run. It is
# deliberately NOT set by the guard: `gitreins guard` and manual `pytest` runs
# still execute the live test (flock-serialized).
TIER1_ENV_VAR = "GITREINS_TIER1"


def _verdict_item_data(item: "VerdictItem") -> dict[str, Any]:
    """One verdict item for a step's ``data`` payload — attribution optional.

    Same contract as ``engine.persist._verdict_item_dict``: the three
    historical keys always, the JEVRES-004 attribution keys only when the
    item carries them (i.e. a pre-screen ran).
    """
    d: dict[str, Any] = {
        "criterion": item.criterion,
        "status": item.status,
        "detail": item.detail,
    }
    probability = getattr(item, "resolution_probability", None)
    cited = getattr(item, "cited_path", None)
    if probability is not None:
        d["resolution_probability"] = probability
    if cited:
        d["cited_path"] = cited
    # GR-176: derived criteria are marked in every persisted item list.
    if getattr(item, "derived", False):
        d["derived"] = True
    return d


def parse_skip_sentinels(output: str) -> list[tuple[str, str]]:
    """Extract ``(step_id, reason)`` pairs from a step's output.

    Only lines carrying :data:`SKIP_SENTINEL` are read, so an ordinary lint or
    test output can never be mistaken for a skip.
    """
    found: list[tuple[str, str]] = []
    for line in (output or "").split("\n"):
        idx = line.find(SKIP_SENTINEL)
        if idx == -1:
            continue
        payload = line[idx + len(SKIP_SENTINEL) :].strip()
        step, sep, reason = payload.partition("=")
        step = step.strip()
        if not step:
            continue
        found.append((step, reason.strip() if sep else "reason not recorded"))
    return found


_SECRETS_SCANNERS_LINE = re.compile(r"^secrets:\s*scanners=(?P<ids>[^\n]+)$", re.M)


def parse_secrets_scanners(output: str) -> list[str]:
    """Scanner ids the Tier 1 ``secrets`` step reported running.

    DF-GITREINS-POC-15: the step echoes ``secrets: scanners=...`` (TRUST-003)
    so the console and the run log name the active scanner, but the verdict's
    step ``data`` carried no machine-readable scanner id — a consumer had to
    parse prose to learn whether gitleaks or only the built-in cross-check
    graded the tree. Returns ``["gitleaks", "builtin"]`` in the order the step
    named them, ``[]`` when the line is absent or unparseable (never a guess:
    the fallback is "no attribution recorded", not a default scanner).
    """
    match = _SECRETS_SCANNERS_LINE.search(output or "")
    if not match:
        return []
    raw = match.group("ids").strip()
    # "gitleaks+builtin cross-check" | "builtin cross-check only (gitleaks not on PATH)"
    raw = raw.split(" only")[0]
    ids: list[str] = []
    for token in raw.split("+"):
        token = token.strip()
        if not token:
            continue
        scanner = "gitleaks" if token.startswith("gitleaks") else "builtin"
        if scanner not in ids:
            ids.append(scanner)
    return ids


@dataclass
class StepResult:
    id: str
    type: str  # "script" | "ai_eval" | "output"
    passed: bool = True
    output: str = ""
    error: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    # A guard may report a skip-only degraded pass with a non-zero contract
    # exit code. Keep that distinction on the step rather than making the
    # stage look like an ordinary green gate.
    degraded: bool = False

    def to_dict(self) -> dict[str, Any]:
        result = {
            "id": self.id,
            "type": self.type,
            "passed": self.passed,
            # DF-GITREINS-POC-8: head+tail bounding instead of a head-only
            # [:500] slice that discarded pytest's short test summary.
            "output": _bound_step_evidence(strip_ansi(self.output)),
            "error": self.error,
            "data": self.data,
        }
        if self.degraded:
            result["degraded"] = True
        return result


def _is_degraded_guard_pass(command: str, exit_code: int | None, output: str) -> bool:
    """Recognize the guard's skip-only degraded exit without weakening failures.

    ``gitreins guard`` deliberately exits 2 when ``allow_skips`` is absent,
    even though its own result says ``Tier 1: DEGRADED PASS``. That is distinct
    from a failed guard (which prints FAIL) and from a timeout (handled before
    this classifier). Only the guard command's explicit degraded-pass banner
    can opt into this compatibility mapping.
    """
    if exit_code != 2:
        return False
    # The configured command may be a shell wrapper, so identify the guard by
    # its own unambiguous result line rather than by command text.
    return bool(re.search(r"Tier 1:\s+DEGRADED PASS\b", output or "")) and not bool(
        re.search(r"Tier 1 Guards:\s+FAIL\b", output or "")
    )


@dataclass
class StageContext:
    """Structured input handed from one pipeline stage to the next.

    The previous stage is represented by its serialized result rather than a
    derived summary, so findings remain verbatim and provider/model details do
    not leak into the context contract. ``diff_digest`` is stable for the
    pipeline run and lets a later stage identify the tree it is reviewing.
    """

    prior_findings: Any
    prior_verdict: str
    prior_status: str
    diff_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "prior_findings": self.prior_findings,
            "prior_verdict": self.prior_verdict,
            "prior_status": self.prior_status,
            "diff_digest": self.diff_digest,
        }


@dataclass
class StageResult:
    id: str
    passed: bool = True
    steps: list[StepResult] = field(default_factory=list)
    any_failed: bool = False
    summary: str = ""
    # DF-GITREINS-POC-16: coverage marker. `coverage` names the checks this
    # stage actually graded; `degraded` is True when the stage ran a SUBSET of
    # the gate for the detected language (e.g. secrets-only because nothing
    # was detectable). A degraded stage may still be `passed` — the marker
    # exists so a narrow pass cannot be mistaken for a full one.
    coverage: str = ""
    degraded: bool = False
    skipped_steps: list[str] = field(default_factory=list)
    degradation_reason: str = ""
    # REVIEW-GITREINS-030: the judge's credential-free provenance (model +
    # base_url_source + api_key_source), folded onto the stage dict so
    # verdict.json's `stages.<id>` names WHO judged it without key or URL
    # values. Absent (not null) when the client carries no identity fields.
    provenance: dict[str, Any] = field(default_factory=dict)
    # GR-196: the criteria the stage's ai_eval verdict never graded, folded
    # from the step data onto the stage dict — a FAIL that is really a
    # coverage gap must name its reason where verdict.json's ``stages.<id>``
    # is read, not only inside ``steps[].data``. Absent (never an empty list)
    # when coverage is complete, keeping the historical shape byte-identical.
    ungraded_criteria: list[str] = field(default_factory=list)
    # GR-235: the fix-verification stage's per-finding dispositions, folded
    # from the step data onto the stage dict the same way — a reader of
    # ``stages.fix_verification`` in verdict.json must not have to walk
    # ``steps[].data`` to see how each first-round finding was handled.
    # Absent (never an empty list) when the stage verified nothing.
    dispositions: list[dict[str, Any]] = field(default_factory=list)
    # GR-235: ``needs_review`` while the dispositions leave a
    # critical/high/medium (or unnamed-severity) finding open — the cap the
    # merge gate and ``merge_recommendation`` honor. Absent (never empty)
    # when nothing is open, keeping other stages' dicts byte-identical.
    verdict_cap: str = ""
    # TRBL-135 (AC2): a tier-2 wall-clock ceiling trip, folded from the
    # ai_eval step data onto the stage dict so verdict.json's
    # ``stages.<id>.outcome`` names the timeout without walking
    # ``steps[].data``. Empty for every other exit, byte-identical.
    tier2_outcome: str = ""
    # TRBL-135 (AC1): the tier-2 context census folded from the ai_eval step
    # data (diff_source / diff_bytes / prompt_bytes) onto the stage dict the
    # same way — ``stages.<id>.context_stats`` answers "did the judge see the
    # change?" without walking ``steps[].data``. Empty (absent) otherwise.
    context_stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = {
            "id": self.id,
            "passed": self.passed,
            "any_failed": self.any_failed,
            "summary": self.summary,
            "steps": [s.to_dict() for s in self.steps],
        }
        if self.provenance:
            # Flat on the stage dict (AC 3: ``stages.tier2["model"]`` /
            # ``["api_key_source"]``), never nested — a reader must not have
            # to know the pipeline's internal step shape to name the judge.
            for _field, _value in self.provenance.items():
                d.setdefault(_field, _value)
        if self.coverage:
            d["coverage"] = self.coverage
        if self.degraded:
            d["degraded"] = True
            d["skipped_steps"] = list(self.skipped_steps)
            d["degradation_reason"] = self.degradation_reason
        if self.ungraded_criteria:
            # GR-196: flat on the stage dict like the provenance fold above —
            # a reader of ``stages.tier2`` must not have to walk
            # ``steps[].data`` to learn WHY a verdict failed.
            d["ungraded_criteria"] = list(self.ungraded_criteria)
        if self.tier2_outcome:
            # TRBL-135 (AC2): flat like the folds above — the timeout is
            # named where ``stages.tier2`` is read, not only inside the step.
            d["outcome"] = self.tier2_outcome
        if self.context_stats:
            # TRBL-135 (AC1): flat like the folds above — the prompt/diff
            # census is readable at ``stages.tier2`` directly.
            d["context_stats"] = dict(self.context_stats)
        if self.dispositions:
            # GR-235: flat on the stage dict like the folds above —
            # ``stages.fix_verification`` names each first-round finding's
            # disposition and evidence without walking ``steps[].data``.
            d["dispositions"] = list(self.dispositions)
        if self.verdict_cap:
            d["verdict_cap"] = self.verdict_cap
        return d


def _fold_step_provenance(stage: StageResult) -> None:
    """Lift one ai_eval step's provenance dict onto the STAGE result dict.

    REVIEW-GITREINS-030: the tier2 verdict used to carry the judge's identity
    only inside ``steps[].data["provenance"]``, which was easy to lose between
    the pipeline result shape and the persisted verdict. Folding it here puts
    the SAME fields on ``stages.<id>`` in verdict.json (and in
    ``judge_result_to_dict``) as a first-class, credential-free attribution:
    model + base_url_source + api_key_source, never key values or URLs. A
    step with no provenance (an old fake, a non-ai_eval script) leaves the
    stage's dict absent (not null).

    Multi-ai_eval stages take the last non-empty one deterministically by
    step order — in practice the default pipeline has exactly one.
    """
    folded: dict[str, Any] = {}
    for step in stage.steps:
        data = step.data if isinstance(step.data, dict) else {}
        prov = data.get("provenance")
        if isinstance(prov, dict) and prov:
            folded = prov
    if folded:
        stage.provenance = dict(folded)


def _fold_step_tier2_outcome(stage: StageResult) -> None:
    """Lift one ai_eval step's tier-2 outcome onto the STAGE result dict.

    TRBL-135 (AC2): the wall-clock timeout used to live only inside
    ``steps[].data["outcome"]``, invisible at the stage a reader (and the
    persisted verdict's ``stages.tier2``) reads. Folding it here puts the
    same value on ``stages.<id>.outcome``. Only the tier-2 wall-clock
    outcome is folded — no other exit carries one, so an ai_eval step with
    no outcome (a normal grade, an exception, a non-timeout cap) leaves the
    stage dict absent (not null), byte-identical to the pre-TRBL-135 shape.
    """
    folded = ""
    for step in stage.steps:
        data = step.data if isinstance(step.data, dict) else {}
        outcome = data.get("outcome")
        if isinstance(outcome, str) and outcome:
            folded = outcome
    if folded:
        stage.tier2_outcome = folded


def _fold_step_context_stats(stage: StageResult) -> None:
    """Lift one ai_eval step's context census onto the STAGE result dict.

    TRBL-135 (AC1): the prompt/diff sizes (``diff_source`` / ``diff_bytes`` /
    ``prompt_bytes``) recorded by the evaluator are folded onto
    ``stages.<id>.context_stats`` exactly like the outcome fold above, so
    verdict.json answers "how big was the prompt the judge graded?" from the
    stage dict. A step with no census (an old fake, a non-ai_eval step,
    prompt_bytes never recorded) leaves the stage dict absent (not empty).
    """
    folded: dict[str, Any] = {}
    for step in stage.steps:
        data = step.data if isinstance(step.data, dict) else {}
        stats = data.get("context_stats")
        if isinstance(stats, dict) and stats:
            folded = stats
    if folded:
        stage.context_stats = dict(folded)


def _fold_step_ungraded_criteria(stage: StageResult) -> None:
    """Lift one ai_eval step's ``ungraded_criteria`` onto the STAGE result.

    GR-196: a run that failed because a criterion was never graded presented
    ``passed: false`` at the top of the tier2 verdict with no reason — the
    gap was named only inside ``steps[].data``, the same burial the
    REVIEW-GITREINS-030 provenance fold fixed for judge identity. Folding
    here puts the SAME list on ``stages.<id>`` in verdict.json (and in
    ``judge_result_to_dict``), where a reviewer reads the stage. Multi-ai_eval
    stages take the last non-empty one deterministically by step order — in
    practice the default pipeline has exactly one. Nothing is folded when no
    step named a gap, so a fully graded verdict serializes byte-identically.
    """
    folded: list[str] = []
    for step in stage.steps:
        data = step.data if isinstance(step.data, dict) else {}
        ungraded = data.get("ungraded_criteria")
        if isinstance(ungraded, list) and ungraded:
            folded = [str(c) for c in ungraded]
    if folded:
        stage.ungraded_criteria = folded


# ── GR-235: the fix-verification stage ──────────────────────────────────────
#
# GR-234 forwards each review stage's findings verbatim to the next stage
# (StageContext.prior_findings), but nothing checked that the flagged code was
# actually HANDLED by the judged change. This stage answers that question
# deterministically — no LLM call, no new transport — by locating each
# finding's flagged file:line in the judged tree and classifying what the
# judged diff (``git diff HEAD``) did to it.

FIX_VERIFICATION_STAGE_ID = "fix_verification"

#: The verdict level an open critical/high/medium finding caps the run to.
#: Same vocabulary engine.gate uses for the merge recommendation (GR-178).
FIX_VERIFICATION_CAP = "needs_review"

#: The four dispositions a finding can receive (the row's binding vocabulary).
FIX_DISPOSITIONS = ("fixed", "not_fixed", "partially_fixed", "superseded")

#: Dispositions that leave a finding open — these are the ones that cap.
_FIX_OPEN_DISPOSITIONS = ("not_fixed", "partially_fixed")

#: Severities whose open findings cap the verdict. The empty string is a
#: severity the finding text did not name — treated as capping (fail-closed:
#: an unclassified finding is never provably low).
_FIX_CAP_SEVERITIES = ("critical", "high", "medium", "")

_SEVERITY_WORDS = {
    "critical": "critical",
    "blocker": "critical",
    "high": "high",
    "medium": "medium",
    "moderate": "medium",
    "low": "low",
    "minor": "low",
    "info": "info",
    "informational": "info",
}

_SEVERITY_RE = re.compile(
    r"\b(critical|blocker|high|medium|moderate|low|minor|info|informational)\b", re.I
)

# A ``path[:line]`` candidate in a finding's text: repo-relative, no spaces,
# extension-bearing; the line number is optional.
_PATH_TOKEN_RE = re.compile(
    r"(?P<path>(?:\./)?[A-Za-z0-9_.][A-Za-z0-9_./-]*\.[A-Za-z0-9]{1,6})(?::(?P<line>\d+))?"
)

# An explicitly quoted code snippet in a finding's text (backticks — the
# finding format this codebase's reviewers write).
_QUOTED_RE = re.compile(r"`([^`\n]{4,160})`")


@dataclass
class FindingDisposition:
    """One first-round finding's verified disposition (GR-235)."""

    finding_id: str
    severity: str
    disposition: str
    evidence: str
    path: str = ""
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "finding_id": self.finding_id,
            "severity": self.severity,
            "disposition": self.disposition,
            "evidence": self.evidence,
            "path": self.path,
            "line": self.line,
        }

    def caps_verdict(self) -> bool:
        """True when this disposition caps the final verdict at needs_review."""
        return self.disposition in _FIX_OPEN_DISPOSITIONS and self.severity in _FIX_CAP_SEVERITIES


class FixVerification:
    """Deterministic per-finding fix verification for the tier-2 pipeline (GR-235).

    Input is the immediately preceding review stage's serialized steps — the
    GR-234 ``StageContext.prior_findings`` — read structurally: every
    ``FAIL``/``NOT_VERIFIABLE`` verdict item and every structured
    ``data["findings"]`` entry (the GR-171 second-pass shape, should a custom
    stage forward one) is one finding. A severity word and a repo-relative
    ``path:line`` location are parsed from the finding's own text.

    Each finding's flagged code is then checked against the CURRENT working
    tree, with the judged diff (``git diff HEAD``) as the evidence:

    ================================  ===========================================
    flagged code in the tree          disposition
    ================================  ===========================================
    absent, diff removes/modifies     ``fixed`` — the diff removed the flagged
    the file                          code (or the whole file)
    absent, code moved to another     ``superseded`` — invalidated by that
    file of the judged change         upstream change (the second round re-flags
                                      the code at its new location)
    absent, diff does not touch       ``superseded`` — the code is gone without
    the file                          a change touching the file, so the
                                      finding no longer names the judged code
    present at the flagged line       ``not_fixed`` — the region is unchanged
    present, moved within the file    ``partially_fixed`` — the region changed
                                      but the flagged code survives
    no path / no needle resolvable    ``not_fixed`` — fail-closed: an
                                      unverifiable finding is never reported
                                      handled
    ================================  ===========================================

    A ``not_fixed``/``partially_fixed`` finding on a critical/high/medium (or
    unnamed) severity carries the ``needs_review`` verdict cap — see
    :func:`engine.gate.fix_verification_unhandled` and
    docs/fix-verification.md.
    """

    def __init__(self, workdir: str) -> None:
        self.workdir = os.path.abspath(workdir)
        self._diff_files: dict[str, str] | None = None
        self._head_cache: dict[str, list[str] | None] = {}

    # ── extraction ──

    def extract(self, prior_findings: Any) -> list[dict[str, Any]]:
        """The open findings carried by the prior stage's serialized steps."""
        findings: list[dict[str, Any]] = []
        if not isinstance(prior_findings, list):
            return findings
        for step_index, step in enumerate(prior_findings):
            if not isinstance(step, dict):
                continue
            step_id = str(step.get("id") or f"step_{step_index}")
            raw_data = step.get("data")
            data = raw_data if isinstance(raw_data, dict) else {}
            raw_items = data.get("items")
            items: list[Any] = raw_items if isinstance(raw_items, list) else []
            for item_index, item in enumerate(items):
                if not isinstance(item, dict):
                    continue
                status = str(item.get("status") or "").strip().upper()
                if status not in ("FAIL", "NOT_VERIFIABLE"):
                    continue
                text = " ".join(str(item.get(k) or "") for k in ("criterion", "detail")).strip()
                if not text:
                    continue
                findings.append(
                    {
                        "finding_id": f"{step_id}:item_{item_index}",
                        "text": text,
                        "severity": self._parse_severity(text),
                    }
                )
            raw_findings = data.get("findings")
            structured: list[Any] = raw_findings if isinstance(raw_findings, list) else []
            for finding_index, entry in enumerate(structured):
                if not isinstance(entry, dict):
                    continue
                text = " ".join(
                    str(entry.get(k) or "")
                    for k in ("title", "description", "detail", "message", "file", "path")
                ).strip()
                if not text:
                    continue
                severity = self._parse_severity(str(entry.get("severity") or "") or text)
                findings.append(
                    {
                        "finding_id": f"{step_id}:finding_{finding_index}",
                        "text": text,
                        "severity": severity,
                    }
                )
        return findings

    @staticmethod
    def _parse_severity(text: str) -> str:
        match = _SEVERITY_RE.search(text or "")
        if not match:
            return ""
        return _SEVERITY_WORDS.get(match.group(1).lower(), "")

    # ── tree / diff access ──

    def _git(self, *args: str) -> str | None:
        """``git <args>`` in the graded tree, or None on any failure."""
        try:
            completed = subprocess.run(
                ["git", *args],
                cwd=self.workdir,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        return completed.stdout

    def _diff_file_changes(self) -> dict[str, str]:
        """path -> one-letter change status (M/A/D/R/…) for the judged diff."""
        if self._diff_files is None:
            self._diff_files = {}
            output = self._git("diff", "HEAD", "--no-color", "--name-status")
            if output is not None:
                for raw_line in output.splitlines():
                    parts = raw_line.split("\t")
                    if len(parts) >= 2 and parts[0]:
                        # R/C lines carry two paths; the tree now names the last.
                        self._diff_files.setdefault(parts[-1], parts[0][:1])
        return self._diff_files

    def _head_lines(self, path: str) -> list[str] | None:
        """The pre-diff (HEAD) content of *path*, or None when unreadable."""
        if path not in self._head_cache:
            output = self._git("show", f"HEAD:{path}")
            self._head_cache[path] = output.splitlines() if output is not None else None
        return self._head_cache[path]

    @staticmethod
    def _read_lines(abs_path: str) -> list[str] | None:
        try:
            with open(abs_path, encoding="utf-8", errors="replace") as handle:
                return handle.read().splitlines()
        except OSError:
            return None

    def _diff_excerpt(self, path: str, limit: int = 6) -> str:
        output = self._git("diff", "HEAD", "--no-color", "--", path)
        if not output:
            return ""
        lines = [line for line in output.splitlines() if line.strip()][:limit]
        return " | ".join(lines)[:300]

    # ── resolution ──

    def _resolve_location(self, text: str) -> tuple[str, int | None]:
        """The flagged ``(path, line)`` — grounded in the tree or the diff."""
        candidates: list[tuple[str, int | None]] = []
        for match in _PATH_TOKEN_RE.finditer(text or ""):
            raw = match.group("path")
            if os.path.isabs(raw) or ".." in raw.split("/"):
                continue
            rel = raw[2:] if raw.startswith("./") else raw
            line_text = match.group("line")
            candidates.append((rel, int(line_text) if line_text else None))
        for rel, line in candidates:
            if os.path.isfile(os.path.join(self.workdir, rel)):
                return rel, line
        # A path the working tree no longer has can still be the flagged file
        # when the judged diff removes it — resolve against the diff file list.
        changes = self._diff_file_changes()
        for rel, line in candidates:
            if rel in changes:
                return rel, line
        return "", None

    def _resolve_needle(
        self, text: str, path: str, line: int | None, current_lines: list[str] | None
    ) -> str:
        """The flagged code content to search the current tree for."""
        head_lines = self._head_lines(path) if path else None
        # 1. An explicitly quoted snippet the pre-diff tree actually contained
        #    — grounded, not a reviewer's paraphrase.
        head_text = "\n".join(head_lines) if head_lines is not None else ""
        for match in _QUOTED_RE.finditer(text or ""):
            snippet = match.group(1).strip()
            if len("".join(snippet.split())) >= 4 and snippet in head_text:
                return snippet
        # 2. The flagged line as the pre-diff tree (HEAD) had it — findings
        #    are flagged against the judged change, whose baseline is HEAD.
        if path and line and head_lines is not None and 1 <= line <= len(head_lines):
            stripped = head_lines[line - 1].strip()
            if len("".join(stripped.split())) >= 4:
                return stripped
        # 3. The flagged line as the current tree has it — best effort when
        #    HEAD cannot be read (e.g. a file the judged diff adds).
        if path and line and current_lines is not None and 1 <= line <= len(current_lines):
            stripped = current_lines[line - 1].strip()
            if len("".join(stripped.split())) >= 4:
                return stripped
        return ""

    @staticmethod
    def _locate_needle(needle: str, current_lines: list[str] | None) -> int | None:
        """1-based line of the first current-tree line containing *needle*."""
        if not needle:
            return None
        for index, content in enumerate(current_lines or []):
            if needle in content:
                return index + 1
        return None

    def _locate_in_other_diff_files(self, needle: str, exclude_path: str) -> tuple[str, int] | None:
        """``(path, line)`` where *needle* now lives in another judged-diff file.

        Bounded to the files the judged diff touches (the change set under
        review, not the whole tree), sorted for determinism. A deleted file
        cannot be the new home, so only existing files are searched.
        """
        if not needle:
            return None
        for changed_path in sorted(self._diff_file_changes()):
            if changed_path == exclude_path:
                continue
            abs_path = os.path.join(self.workdir, changed_path)
            if not os.path.isfile(abs_path):
                continue
            found_at = self._locate_needle(needle, self._read_lines(abs_path))
            if found_at is not None:
                return changed_path, found_at
        return None

    # ── classification ──

    def _disposition_for(self, finding: dict[str, Any]) -> FindingDisposition:
        text = finding["text"]
        path, line = self._resolve_location(text)
        if not path:
            return FindingDisposition(
                finding_id=finding["finding_id"],
                severity=finding["severity"],
                disposition="not_fixed",
                evidence=(
                    "flagged code location could not be resolved from the finding text "
                    "(no repo-relative path matched the working tree or the judged diff); "
                    "reported unhandled (fail-closed)"
                ),
            )
        abs_path = os.path.join(self.workdir, path)
        current_lines = self._read_lines(abs_path) if os.path.isfile(abs_path) else None
        changes = self._diff_file_changes()
        status = changes.get(path, "")

        if current_lines is None:
            # The flagged file is gone from the working tree.
            if status:
                return FindingDisposition(
                    finding_id=finding["finding_id"],
                    severity=finding["severity"],
                    disposition="fixed",
                    path=path,
                    line=line,
                    evidence=(
                        f"flagged file absent from the working tree; the judged diff "
                        f"records '{status}' for {path} — the flagged code was removed "
                        "with the file"
                    ),
                )
            return FindingDisposition(
                finding_id=finding["finding_id"],
                severity=finding["severity"],
                disposition="superseded",
                path=path,
                line=line,
                evidence=(
                    f"flagged path {path} is absent from the working tree and the judged "
                    "diff does not remove it — the finding no longer names the judged code"
                ),
            )

        needle = self._resolve_needle(text, path, line, current_lines)
        if not needle:
            return FindingDisposition(
                finding_id=finding["finding_id"],
                severity=finding["severity"],
                disposition="not_fixed",
                path=path,
                line=line,
                evidence=(
                    f"flagged line content could not be resolved to verify {path}"
                    + (f":{line}" if line else "")
                    + "; reported unhandled (fail-closed)"
                ),
            )

        found_at = self._locate_needle(needle, current_lines)
        if found_at is not None and (line is None or found_at == line):
            evidence = f"flagged code still present at {path}:{found_at} — region unchanged"
            if status:
                evidence += (
                    f"; the judged diff touches {path} ('{status}') but leaves this line intact"
                )
            return FindingDisposition(
                finding_id=finding["finding_id"],
                severity=finding["severity"],
                disposition="not_fixed",
                path=path,
                line=line,
                evidence=evidence,
            )
        if found_at is not None:
            return FindingDisposition(
                finding_id=finding["finding_id"],
                severity=finding["severity"],
                disposition="partially_fixed",
                path=path,
                line=line,
                evidence=(
                    f"flagged code still present at {path}:{found_at}"
                    + (f" (was :{line})" if line else "")
                    + " — the region changed but the flagged code survives"
                ),
            )
        # The flagged code is gone from the current file.
        if status:
            moved = self._locate_in_other_diff_files(needle, path)
            if moved is not None:
                # GR-235: the flagged code moved to another file of the judged
                # change — the finding is invalidated by that upstream change,
                # not resolved by it (the second round re-flags it there).
                return FindingDisposition(
                    finding_id=finding["finding_id"],
                    severity=finding["severity"],
                    disposition="superseded",
                    path=path,
                    line=line,
                    evidence=(
                        f"flagged code absent from {path}"
                        + (f" (was :{line})" if line else "")
                        + f" and now lives at {moved[0]}:{moved[1]} — the flagged code "
                        "moved, so the first-round finding is invalidated"
                    ),
                )
            evidence = (
                f"flagged code no longer present in {path}"
                + (f" (was :{line})" if line else "")
                + f"; the judged diff records '{status}' for {path}"
            )
            excerpt = self._diff_excerpt(path)
            if excerpt:
                evidence += " — diff: " + excerpt
            return FindingDisposition(
                finding_id=finding["finding_id"],
                severity=finding["severity"],
                disposition="fixed",
                path=path,
                line=line,
                evidence=evidence,
            )
        return FindingDisposition(
            finding_id=finding["finding_id"],
            severity=finding["severity"],
            disposition="superseded",
            path=path,
            line=line,
            evidence=(
                f"flagged code absent from {path} and the judged diff leaves the file "
                "untouched — the finding no longer names the judged code"
            ),
        )

    def verify(self, prior_findings: Any) -> list[FindingDisposition]:
        """Classify every open first-round finding against the judged tree."""
        return [self._disposition_for(finding) for finding in self.extract(prior_findings)]


def _fold_step_dispositions(stage: StageResult) -> None:
    """Lift a fix_verification step's dispositions + verdict cap onto the STAGE.

    GR-235: the dispositions and the ``needs_review`` cap used to live only
    inside ``steps[].data``, the same burial the GR-196 ungraded-criteria fold
    fixed for coverage gaps. Folding here puts the SAME lists on
    ``stages.fix_verification`` in verdict.json (and in
    ``judge_result_to_dict``), where a reviewer and
    :func:`engine.gate.fix_verification_unhandled` read the stage. Only
    fix_verification steps contribute, so other stages' serialized dicts are
    byte-identical; a step that verified nothing folds nothing.
    """
    folded: list[dict[str, Any]] = []
    cap = ""
    for step in stage.steps:
        if step.type != FIX_VERIFICATION_STAGE_ID:
            continue
        data = step.data if isinstance(step.data, dict) else {}
        dispositions = data.get("dispositions")
        if isinstance(dispositions, list) and dispositions:
            folded = [dict(entry) for entry in dispositions if isinstance(entry, dict)]
            named_cap = data.get("verdict_cap")
            cap = str(named_cap) if isinstance(named_cap, str) and named_cap else ""
    if folded:
        stage.dispositions = folded
        stage.verdict_cap = cap


def degradation_warning(stage: dict[str, Any]) -> str | None:
    """One-line CLI warning for a stage narrower than the guard gate.

    Returns None for a stage with no degradation marker, so callers can call
    this unconditionally while printing a stage summary.
    """
    if not stage.get("degraded"):
        return None
    skipped = ", ".join(stage.get("skipped_steps") or []) or "unknown checks"
    reason = stage.get("degradation_reason") or "unknown reason"
    # GR-183: a skip-only guard pass RAN and graded a subset — saying it "did not
    # run" (the right words for a step that never executed) would be false here.
    verb = "graded a subset of their checks" if "DEGRADED PASS" in reason else "did not run"
    return (
        f"WARNING: coverage is {stage.get('coverage') or 'unknown'} — {skipped} {verb} "
        f"({reason}); run `gitreins guard` for the full gate"
    )


def _step_skip_reason(step: StepResult) -> str:
    """The reason a PASSING step graded nothing, from its own data.

    DF-GITREINS-POC-49: :meth:`Pipeline._run_script_step` marks a benign
    pytest exit 5 (pytest collected no tests) as passed-with-skip and records
    ``data["skipped"]``/``data["skip_reason"]`` — the same facts the guard's
    ``GuardResult`` carries for the same tree. This is the reader: the stage
    summary renders such a step in the ``~`` (DEGRADED) register instead of a
    ✓ that would claim the tests ran. Returns "" for every other step, and
    never trusts the marker on a FAILED step — a malformed record must not be
    able to hide a failure behind a skip.
    """
    data = step.data if isinstance(step.data, dict) else {}
    if not step.passed or not data.get("skipped"):
        return ""
    return str(data.get("skip_reason") or "") or "unknown reason"


def _step_budget_timeout(step: StepResult) -> int | None:
    """The budget a script step exhausted, from its machine-readable data.

    GAP-058: :meth:`_run_script_step` stamps ``data["timed_out"]``/``data
    ["timeout_s"]`` when :func:`command_hygiene.run_bounded` reports a
    ``timed_out`` — the step's own budget, not an inference from prose.
    Returns None for a step that ended any other way, so a REAL failing
    test never carries the marker.
    """
    data = step.data if isinstance(step.data, dict) else {}
    if step.type == "script" and data.get("timed_out"):
        try:
            return int(data["timeout_s"])
        except (KeyError, TypeError, ValueError):
            return 0
    return None


def _record_runtime_skips(stage: StageResult) -> None:
    """Fold runtime skip sentinels and budget timeouts into the degradation marker.

    TRUST-001: :func:`tier1_plan` declares the skips it knows statically (an
    undetectable tree). A step can also skip on the machine it actually runs
    on — ``_lint_step_run`` echoes :data:`SKIP_SENTINEL` and exits 0 when the
    linter is not on PATH. Without this pass, that run reads as a graded lint
    and the verdict carries no ``skipped_steps`` for a merge-back to refuse.
    GAP-058: a script step that exhausted its own budget joins the same
    record — it is the sibling case (a gate that ran OUT OF TIME, not a gate
    that found code), and ``worktree_manager._tier1_skipped_steps`` must see
    it in ``skipped_steps`` like any other gate that did not finish its work.
    INT-FLAKE-6: a contention-degraded tier1 tests step joins them the same
    way — its PASS carries ``data["skipped_tests"] = True`` (never an output
    parse, always the step's own data field, so prose in the captured output
    cannot mint a skip). ``worktree_manager._tier1_skipped_steps`` must see
    "tests" in ``skipped_steps``, or the fail-open remains: a tier1 PASS that
    graded nothing could merge as green.
    Idempotent: ids are deduplicated and an existing reason is preserved.
    """
    found: list[tuple[str, str]] = []
    for step in stage.steps:
        step_data = step.data if isinstance(step.data, dict) else {}
        if step.degraded or step_data.get("degraded"):
            if step.id not in stage.skipped_steps and step.id not in [sid for sid, _ in found]:
                found.append((step.id, str(step_data.get("skip_reason") or "degraded guard pass")))
        for step_id, reason in parse_skip_sentinels(f"{step.output}\n{step.error}"):
            if step_id in stage.skipped_steps or step_id in [sid for sid, _ in found]:
                continue
            found.append((step_id, reason))
    # GAP-058: the timed-out lane itself — never an output parse, always the
    # step's own data field, so a step whose output merely mentions a timeout
    # cannot be misclassified.
    for step in stage.steps:
        if step.passed or step.id in stage.skipped_steps or step.id in [sid for sid, _ in found]:
            continue
        budget = _step_budget_timeout(step)
        if budget is None:
            continue
        found.append((step.id, f"timed out after {budget}s (step budget)"))
    # INT-FLAKE-6: the contention-degraded lane — a PASSING tier1 tests step
    # whose data carries the skipped_tests marker. Same trust rule as the
    # timeout lane above: the step's own data field, never a parse of its
    # captured output. Idempotent with the lanes above via the same
    # already-found/already-recorded checks.
    for step in stage.steps:
        if step.id in stage.skipped_steps or step.id in [sid for sid, _ in found]:
            continue
        data = step.data if isinstance(step.data, dict) else {}
        if not data.get("skipped_tests"):
            continue
        reason = str(data.get("skip_reason") or "") or "contention-degraded tier1 tests step"
        found.append((step.id, reason))
    if not found:
        return
    stage.degraded = True
    stage.skipped_steps.extend(step_id for step_id, _ in found)
    detail = "skipped at runtime — " + ", ".join(f"{sid}: {reason}" for sid, reason in found)
    stage.degradation_reason = (
        f"{stage.degradation_reason}; {detail}" if stage.degradation_reason else detail
    )


# The commit-audit ``mode`` values the auditor and the CLI wording support.
_COMMIT_AUDIT_MODES = ("warn", "block", "suggest")


def resolve_commit_audit_mode(
    pipeline_config: dict[str, Any],
    audit_config: dict[str, Any],
    step_def: dict[str, Any] | None = None,
) -> str:
    """Resolve the commit-audit ``mode`` with an EXPLICIT precedence.

    DF-GITREINS-POC-30: ``_load_commit_audit_config`` used to return the
    top-level section only, so a ``mode`` written on the pipeline STAGE (the
    placement ``docs/cli-reference.md`` describes) or under
    ``defaults.commit_audit`` was dead config — the audit ran, printed
    "(Warning only — commit will proceed)" and exited 0 while the user believed
    their commits were gated. All three placements are live now, and this
    function is the single place that decides which one wins, so the rule is
    testable without an LLM:

        1. the stage's own ``mode``            (most specific)
        2. ``defaults.commit_audit.mode``
        3. top-level ``commit_audit.mode``     (legacy placement — still blocks)
        4. ``"warn"``                          (default)

    A value outside the supported set falls through to the next source rather
    than being passed to the auditor as-is: the mode is a security-relevant
    choice, so an unrecognised value must not be able to read as "whatever the
    next rule would have said".
    """
    defaults_cfg = pipeline_config.get("defaults", {}).get("commit_audit", {}) or {}
    sources = (
        (step_def or {}).get("mode"),
        defaults_cfg.get("mode") if isinstance(defaults_cfg, dict) else None,
        audit_config.get("mode"),
    )
    for value in sources:
        if isinstance(value, str) and value.strip().lower() in _COMMIT_AUDIT_MODES:
            return value.strip().lower()
    return "warn"


_COMMIT_AUDIT_ENABLED_TRUE = ("true", "yes", "1", "on")
_COMMIT_AUDIT_ENABLED_FALSE = ("false", "no", "0", "off")


def _normalize_commit_audit_enabled(value: object) -> bool | None:
    """Normalize a YAML ``enabled`` value to True/False, or None when unset.

    Booleans pass through. The string forms YAML users actually write
    (``"true"``/``"yes"``/``"1"``/``"on"`` and the negative twins,
    case-insensitive, whitespace-tolerated) normalize too — a quoted
    ``"false"`` must not read as truthy. Any OTHER value (an int like ``2``,
    a typo like ``"flase"``, ``None``) is treated as UNSET rather than
    crashing or guessing: resolution falls through to the next source, and
    the last fallback is the documented default (``True``).
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        token = value.strip().lower()
        if token in _COMMIT_AUDIT_ENABLED_TRUE:
            return True
        if token in _COMMIT_AUDIT_ENABLED_FALSE:
            return False
    return None


def resolve_commit_audit_enabled(
    pipeline_config: dict[str, Any],
    audit_config: dict[str, Any],
    step_def: dict[str, Any] | None = None,
) -> tuple[bool, str]:
    """Resolve the commit-audit ``enabled`` switch with an EXPLICIT precedence.

    REVIEW-GITREINS-024: ``commit_audit.enabled`` existed as a config key with
    default ``True`` (engine/config.py) and was serialized into the config
    surface — but NOTHING in the execution path read it. A repo that wrote
    ``enabled: false`` believing it had turned the audit off still had its
    messages graded by the LLM. This function is the single place that
    decides whether the audit runs, so the rule is testable without an LLM:

        1. the stage's own ``enabled``          (most specific)
        2. the merged ``commit_audit`` section  (defaults + top-level, the
           same merge ``_load_commit_audit_config`` performs — top-level
           keys win on conflict)
        3. ``True``                             (the documented default)

    YAML string forms (``"false"``, ``"no"``, ``"0"`` / ``"true"``,
    ``"yes"``, ``"1"``) are normalized; an unrecognized value is treated as
    UNSET at that level and resolution falls through to the next source
    (see :func:`_normalize_commit_audit_enabled`) — it must not crash a
    commit, and it must not silently read as ``false`` either: an
    ``enabled: flase`` typo disabling nothing is the honest outcome.

    Returns ``(enabled, source)`` where ``source`` names where the winning
    value came from, so the skip line can say exactly which placement
    disabled the audit: ``"pipeline.stages[].enabled"``,
    ``"defaults.commit_audit.enabled"``, ``"commit_audit.enabled"``, or
    ``"default"``.
    """
    stage_value = _normalize_commit_audit_enabled((step_def or {}).get("enabled"))
    if stage_value is not None:
        return stage_value, "pipeline.stages[].enabled"

    merged_value = _normalize_commit_audit_enabled(audit_config.get("enabled"))
    if merged_value is not None:
        # Name the exact placement the merge carried: top-level wins on
        # conflict (the order _load_commit_audit_config merges in), so a
        # recognized top-level value is what the merged section holds.
        top_value = _normalize_commit_audit_enabled(
            (pipeline_config.get("commit_audit") or {}).get("enabled")
        )
        source = (
            "commit_audit.enabled" if top_value is not None else "defaults.commit_audit.enabled"
        )
        return merged_value, source

    # Direct callers may pass the RAW pipeline config with an empty merged
    # section — read the placements straight from it before defaulting. A
    # recognized value at one placement is honored; an unrecognized one
    # (the ``enabled: flase`` typo) does not shadow a valid value beneath.
    top_value = _normalize_commit_audit_enabled(
        (pipeline_config.get("commit_audit") or {}).get("enabled")
    )
    if top_value is not None:
        return top_value, "commit_audit.enabled"
    defaults_value = _normalize_commit_audit_enabled(
        ((pipeline_config.get("defaults") or {}).get("commit_audit") or {}).get("enabled")
    )
    if defaults_value is not None:
        return defaults_value, "defaults.commit_audit.enabled"

    return True, "default"


def _is_commit_audit_stage(stage_def: dict[str, Any]) -> bool:
    """True for a stage that declares the commit-audit step type.

    The step type is ``type:``; the id is conventionally ``commit_audit`` and
    is accepted as well, so an id-only stage (the shape the docs and the
    onboarding snippet use) is recognised and reported instead of being
    mistaken for "you declared nothing".
    """
    if not isinstance(stage_def, dict):
        return False
    return stage_def.get("type") == "commit_audit" or stage_def.get("id") == "commit_audit"


def commit_audit_skip_message(
    config: dict[str, Any], trigger: str, result: dict[str, Any] | None = None
) -> str:
    """The named skip line for a run where the commit audit did NOT happen.

    DF-GITREINS-POC-30: the command used to print NOTHING (exit 0, stdout and
    stderr both empty) when no commit_audit stage was armed — the hook was
    indistinguishable from a passing audit. This names which of the three
    states the repo is in, reading only the config the run used, so the message
    cannot disagree with what actually ran:

      * no stage of that type at all — the fresh ``install`` + ``init`` state;
      * a stage exists but does not list this trigger in ``on:``;
      * a stage is armed for this trigger yet produced no result (its
        ``condition`` excluded it.

    If ``result`` is supplied, a configured audit stage present in its stage
    results is known to have run; never label that run as condition-skipped.
    Exit codes stay 0: a skip is not a failure.
    """
    stages = config.get("pipeline", {}).get("stages", []) or []
    audit_stages = [s for s in stages if _is_commit_audit_stage(s)]
    stage_results = (result or {}).get("stages", {})
    if any(stage.get("id") in stage_results for stage in audit_stages):
        return f"commit audit: pipeline stage ran for trigger {trigger}"
    if not audit_stages:
        return (
            f"commit audit: no pipeline stage with type commit_audit for trigger {trigger} "
            "— audit NOT run"
        )
    armed = [s for s in audit_stages if trigger in (s.get("on") or [])]
    if not armed:
        ids = ", ".join(str(s.get("id")) for s in audit_stages)
        return (
            f"commit audit: pipeline stage {ids} is not armed for trigger {trigger} "
            "(add `on: [commit-msg]`) — audit NOT run"
        )
    return (
        f"commit audit: pipeline stage armed for trigger {trigger} was skipped "
        "(condition not met) — audit NOT run"
    )


class Pipeline:
    """Execute a pipeline of stages against a task."""

    def __init__(
        self,
        config: dict[str, Any],
        workdir: str = ".",
        llm: Any = None,  # noqa: ANN401 - LLMClient, injected lazily by Judge
        *,
        persist_telemetry: bool = True,
        scan_root: str | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.workdir = os.path.abspath(workdir)
        self.scan_root = os.path.abspath(scan_root or workdir)
        self.config = config
        self.stages: list[dict[str, Any]] = config.get("pipeline", {}).get("stages", [])
        self._stage_results: dict[str, StageResult] = {}
        self._stage_context: StageContext | None = None
        self._diff_digest = self._compute_diff_digest()
        self._llm = llm  # Can be injected by Judge
        # DF-034: optional phase callback forwarded to every ai_eval step's
        # evaluator, so a pipeline-driven tier-2 reports its iterations the
        # same way the legacy path does. None (every existing caller) is
        # silent, as before.
        self._progress = progress
        # EVID-003: the judge's token telemetry is appended to
        # ``<workdir>/.gitreins/usage.jsonl`` — a write INSIDE the graded tree.
        # An ephemeral judge run (``persist_telemetry=False``) leaves nothing
        # there; it also has no persisted verdict the line could ever be
        # attributed to (see engine/usage.py), so nothing is lost. Keyword-only
        # and defaulting True so every existing caller writes it as before.
        self.persist_telemetry = persist_telemetry
        # GR-195: the execution transcript (capped {tool_calls,
        # tool_calls_omitted}) of the last ai_eval step that ran an evaluator,
        # or None. Kept on the pipeline — not in the step or stage dicts — so
        # the transcript is persisted ONCE, at ``stages.tier2.tool_calls``, by
        # build_verdict_data (Judge lifts it onto its JudgeResult).
        self._last_tool_transcript: dict[str, Any] | None = None

    def run(self, task: dict[str, Any], trigger: str = "pre-eval") -> dict[str, Any]:
        """Run all stages that match the trigger.

        Args:
            task: Task dict with id, title, criteria, status.
            trigger: "pre-commit" or "pre-eval" — filters which stages run.

        Returns:
            Dict with overall verdict and per-stage results.
        """
        self._stage_results = {}
        self._stage_context = None
        self._diff_digest = self._compute_diff_digest()

        for stage_def in self.stages:
            # Check if this stage should run for this trigger
            stage_on = stage_def.get("on", ["pre-eval", "pre-commit"])
            if trigger not in stage_on:
                logger.debug(
                    "Skipping stage %s (trigger mismatch: %s)", stage_def.get("id"), trigger
                )
                continue

            # Check condition
            if not self._check_condition(stage_def.get("condition"), task):
                logger.debug("Skipping stage %s (condition not met)", stage_def.get("id"))
                continue

            stage_id = stage_def.get("id", f"stage_{len(self._stage_results)}")
            logger.info("Running stage: %s", stage_id)

            # DF-034: name each phase as it happens. An ai_eval stage reports
            # before its (long, silent) evaluator loop starts; the evaluator's
            # own iterations and judging then report through the callback. The
            # guard is named the moment its stage lands — never after the
            # whole run, which would print "tier-1 guard ok" after the
            # tier-2 lines it precedes.
            if self._stage_has_ai_eval(stage_def):
                self._report("tier-2 evaluating")

            if stage_def.get("parallel"):
                result = self._run_parallel_stage(stage_id, stage_def, task)
            else:
                result = self._run_sequential_stage(stage_id, stage_def, task)

            # DF-GITREINS-POC-16: carry the stage's coverage marker into the
            # verdict (see StageResult). Declared by the stage definition, so
            # a custom pipeline simply has none.
            result.coverage = stage_def.get("coverage", "")
            result.degraded = bool(stage_def.get("degraded", False))
            result.skipped_steps = list(stage_def.get("skipped_steps") or [])
            result.degradation_reason = stage_def.get("degradation_reason", "")
            # TRUST-001: a step that skipped AT RUNTIME (linter missing on this
            # machine) is a degradation the stage definition cannot declare in
            # advance. Fold it in so verdict.json carries skipped_steps and a
            # judge-gated merge-back can refuse a pass whose gates never ran.
            _record_runtime_skips(result)

            self._stage_results[stage_id] = result
            self._stage_context = self._context_from_stage(result)
            if stage_id == "tier1":
                # DF-034: one line per evaluator phase — the guard closes here,
                # in run order, whether or not the evaluator stage follows.
                self._report("tier-1 guard ok" if result.passed else "tier-1 guard failed")

        return self._compile_results()

    @staticmethod
    def _stage_has_ai_eval(stage_def: dict[str, Any]) -> bool:
        """True when the stage (or any of its steps) runs an LLM evaluator."""
        if stage_def.get("type") == "ai_eval":
            return True
        return any(step.get("type") == "ai_eval" for step in stage_def.get("steps", []))

    def _report(self, message: str) -> None:
        """Emit one phase line through the optional progress callback (DF-034)."""
        if self._progress is not None:
            self._progress(message)

    def _check_condition(self, condition: str | None, task: dict[str, Any]) -> bool:
        """Evaluate a condition expression.

        Supported:
            - None/empty → always true
            - "stage.X.any_failed" → true if stage X had failures
            - "stage.X.passed" → true if stage X passed
            - "task.has_criteria" → true if task has criteria
            - "task.skip_tier2" → true if task has skip_tier2 flag set
            - "not task.skip_tier2" → true if task does NOT have skip_tier2 flag
            - "true" / "always" → always true
            - "false" → always false
            - Expressions with AND/OR: "stage.tier1.any_failed or task.has_criteria"
        """
        if not condition:
            return True
        if condition in ("true", "always"):
            return True
        if condition == "false":
            return False

        # Parse simple expressions
        condition = condition.strip()

        # Handle OR
        if " or " in condition:
            parts = condition.split(" or ")
            return any(self._check_condition(p.strip(), task) for p in parts)

        # Handle AND
        if " and " in condition:
            parts = condition.split(" and ")
            return all(self._check_condition(p.strip(), task) for p in parts)

        # Handle individual predicates
        if condition == "task.has_criteria":
            return bool(task.get("criteria"))
        if condition == "task.skip_tier2":
            return bool(task.get("skip_tier2", False))
        if condition == "not task.skip_tier2":
            return not bool(task.get("skip_tier2", False))
        if condition.startswith("stage."):
            # stage.tier1.any_failed
            parts = condition.split(".")
            if len(parts) == 3:
                stage_id = parts[1]
                prop = parts[2]
                stage = self._stage_results.get(stage_id)
                if stage:
                    if prop == "any_failed":
                        return stage.any_failed
                    elif prop == "passed":
                        return stage.passed
            return False

        return True

    def _run_parallel_stage(
        self, stage_id: str, stage_def: dict[str, Any], task: dict[str, Any]
    ) -> StageResult:
        """Run all steps in parallel."""
        steps = stage_def.get("steps", [])
        result = StageResult(id=stage_id)

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(steps)) as executor:
            futures = {
                executor.submit(self._run_step, step, task, stage_id): step for step in steps
            }
            for future in concurrent.futures.as_completed(futures):
                step_result = future.result()
                result.steps.append(step_result)

        # Check results
        result.any_failed = any(not s.passed for s in result.steps)
        result.passed = not result.any_failed
        result.summary = self._summarize_stage(result)
        _fold_step_provenance(result)
        _fold_step_ungraded_criteria(result)
        _fold_step_dispositions(result)
        _fold_step_tier2_outcome(result)
        _fold_step_context_stats(result)
        return result

    def _run_sequential_stage(
        self, stage_id: str, stage_def: dict[str, Any], task: dict[str, Any]
    ) -> StageResult:
        """Run steps sequentially — in order, no concurrency."""
        result = StageResult(id=stage_id)

        steps = stage_def.get("steps", [])
        if steps:
            # Multi-step sequential stage (e.g. tier1 with secrets→lint→tests)
            for step_def in steps:
                step_result = self._run_step(step_def, task, stage_id)
                result.steps.append(step_result)
                if not step_result.passed and step_def.get("on_fail") != "continue":
                    # Stop at first hard failure — later steps won't change the verdict
                    break
            result.any_failed = any(not s.passed for s in result.steps)
            result.passed = not result.any_failed
            result.summary = self._summarize_stage(result)
            return result

        if stage_def.get("type") == "ai_eval":
            step_result = self._run_ai_eval(stage_def, task)
        elif stage_def.get("type") == "commit_audit":
            step_result = self._run_commit_audit(stage_def, task)
        elif stage_def.get("type") == "output":
            step_result = self._run_output(stage_def, task)
        elif stage_def.get("type") == FIX_VERIFICATION_STAGE_ID:
            step_result = self._run_fix_verification(stage_def, task)
        else:
            # Treat as a single script step
            step_result = self._run_script_step(stage_def, task, stage_id)

        result.steps.append(step_result)
        result.passed = step_result.passed
        result.any_failed = not step_result.passed
        result.summary = step_result.output or step_result.error
        _fold_step_provenance(result)
        _fold_step_ungraded_criteria(result)
        _fold_step_dispositions(result)
        _fold_step_tier2_outcome(result)
        _fold_step_context_stats(result)
        return result

    def _run_step(
        self, step_def: dict[str, Any], task: dict[str, Any], stage_id: str | None = None
    ) -> StepResult:
        """Run a single step (used by parallel stages)."""
        step_type = step_def.get("type", "script")
        step_id = step_def.get("id", "unnamed")

        if step_type == "script":
            return self._run_script_step(step_def, task, stage_id)
        elif step_type == "ai_eval":
            return self._run_ai_eval(step_def, task)
        elif step_type == "commit_audit":
            return self._run_commit_audit(step_def, task)
        elif step_type == "output":
            return self._run_output(step_def, task)
        elif step_type == FIX_VERIFICATION_STAGE_ID:
            return self._run_fix_verification(step_def, task)
        else:
            return StepResult(
                id=step_id, type=step_type, passed=False, error=f"Unknown step type: {step_type}"
            )

    def _guard_log_ref(self, stage_id: str | None) -> str | None:
        """Return no guard-log path unless this pipeline owns that run.

        A pipeline stage does not produce a guard run. Looking up the newest
        file on disk therefore binds tier-1 evidence to an unrelated earlier
        run (AUG-160). The guard command itself attaches its own persisted log
        through ``Tier1Result.extra``; a pipeline verdict omits this optional
        field when it has no run-scoped log to attach.
        """
        return None

    def _run_script_step(
        self, step_def: dict[str, Any], task: dict[str, Any], stage_id: str | None = None
    ) -> StepResult:
        """Execute a shell command."""
        step_id = step_def.get("id", "unnamed")
        cmd = step_def.get("run", "")

        if not cmd:
            return StepResult(id=step_id, type="script", passed=False, error="No command specified")

        # Template substitution
        cmd = self._template(cmd, task)
        # DF-GITREINS-POC-55: a step may declare the command WITHOUT its spawn
        # wrapper (`analysis_cmd`). Classifiers that parse the command text —
        # the POC-51 runner-missing check below — read that one, so a nice
        # wrapper can never change a verdict's classification. Absent (every
        # hand-authored step) it is simply the command itself.
        analysis_cmd = self._template(step_def.get("analysis_cmd") or cmd, task)

        logger.debug("Running script: %s", cmd)
        try:
            # Strip GIT_* env vars (GIT_INDEX_FILE etc.) leaked by the
            # pre-commit hook — they poison nested git commands in tests
            # (same class as DF-008; guards.py got this in 3cad082).
            sanitized_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
            # DF-GITREINS-POC-61: a step def may declare extra child env
            # (`env: {NAME: value}`), merged OVER the sanitized ambient env so
            # the step wins. Per-step on purpose: the stamp reaches the child
            # process only — this process's environ stays untouched, so a
            # step's marker can never leak into a sibling step (secrets, lint)
            # or into the caller's own environment. Values are stringified:
            # the child env is str → str.
            step_env = step_def.get("env") or {}
            if step_env:
                sanitized_env.update({str(k): str(v) for k, v in step_env.items()})
            # 2026-09-18: run through command_hygiene so a step that backgrounds work
            # cannot leak orphans (the tier-2 run_command path caused 278 survivors
            # reparented to systemd --user) and a busy-wait step is refused with a
            # pointer at the bounded alternative. Same output/exit-code contract.
            # INT-FLAKE-6: the TIER1 tests step is serialized per-repo (blocking
            # flock on a lock file outside the repo, keyed by the repo root) so
            # two concurrent judges in one workdir cannot run the suite against
            # each other — verdict 8435b664 recorded two async-job tests red
            # under three concurrent judges on a tree that passed 2487/0 alone.
            # The wait is bounded; on expiry the step still runs (degraded, and
            # the step data names ``tier1_tests_lock: wait-expired``). Guard and
            # manual runs never take the lock (tier1_lock.acquire returns None).
            from engine import tier1_lock

            tier1_lock_result = None
            hold_tier1_lock = stage_id == "tier1" and step_id == "tests" and tier1_lock.in_tier1()
            if hold_tier1_lock:
                tier1_lock_result = tier1_lock.acquire()
            try:
                out = command_hygiene.run_bounded(
                    cmd,
                    cwd=self.workdir,
                    timeout=step_def.get("timeout", 120),
                    env=sanitized_env,
                )
            finally:
                if hold_tier1_lock:
                    tier1_lock.release()

            if out.get("refused"):
                return StepResult(id=step_id, type="script", passed=False, error=out["reason"])
            if out.get("timed_out"):
                # GAP-058: a budget exhaustion is not a code finding. The step
                # still fails (the gate did not finish), but the record must
                # name the budget so a reader can tell "ran out of time" from
                # "your code failed" without reading the source — the same
                # honesty split TRUST-001 draws for a gate that did no work.
                # run_bounded already reports the elapsed budget in its own
                # error; the timeout value on the step def is the configured
                # budget the run was held to.
                budget = step_def.get("timeout", 120)
                return StepResult(
                    id=step_id,
                    type="script",
                    passed=False,
                    error=f"Command timed out after {budget}s (step budget)",
                    data={"timed_out": True, "timeout_s": budget},
                )
            output = out["output"]
            exit_code = out["exit_code"]
            # A non-zero exit is a hard failure regardless of on_fail. on_fail
            # only controls whether later steps still run; it must never turn a
            # failed lint/test into a pass (previously `on_fail: continue` and
            # generated `cmd || true` both zeroed the failure). 2026-08-08.
            degraded_guard_pass = _is_degraded_guard_pass(cmd, exit_code, output)
            passed = exit_code == 0 or degraded_guard_pass

            data: dict[str, Any] = {"exit_code": exit_code}
            if degraded_guard_pass:
                data["degraded"] = True
                data["skip_reason"] = "guard reported DEGRADED PASS"

            if tier1_lock_result is not None:
                # INT-FLAKE-6 attribution: the verdict shows the run took the
                # per-repo tier1 tests lock (acquired / waited / wait-expired).
                data["tier1_tests_lock"] = tier1_lock_result
            if out.get("leftover_pids"):
                # Never hide a reap failure: the verdict carries the evidence.
                data["leftover_pids"] = out["leftover_pids"]
            # INT-FLAKE-2: an exit code cannot say WHY pytest ended — under
            # `-x` + xdist a REAL failing test exits 2 (INTERRUPTED), exactly
            # like a signalled run. Classify from the output so the verdict
            # names the cause instead of the reader's guess.
            if _PYTEST_INVOCATION.search(cmd):
                outcome = pytest_outcome(exit_code, output)
                data["pytest_outcome"] = outcome
                # DF-GITREINS-POC-49: the guard and the judge grade the SAME
                # tree, so they must reach the SAME verdict on it. `gitreins
                # guard` has treated pytest exit 5 carrying pytest's own "no
                # tests ran" summary — and no collection errors — as a benign
                # SKIPPED pass since GR-GAP-048 (engine/guard_manager.py:2081),
                # but this step graded only the number, so a consumer repo with
                # no test suite got an automatic `Stage tier1: FAIL` /
                # `Overall: FAIL` from `gitreins judge` on the commit the
                # pre-commit hook had just passed.
                #
                # Classify with the guard's own predicate (imported, never
                # copied — one definition, two callers) and record the same
                # skip facts its GuardResult carries. Nothing is swallowed: the
                # number stays in data["exit_code"] and the classification in
                # data["pytest_outcome"], so the verdict still shows a 5 that
                # graded nothing. An exit 5 WITH collection errors fails the
                # benign check and stays a failure, exactly as before.
                if outcome.get("kind") == "no-tests-collected" and _pytest_no_tests_benign(output):
                    passed = True
                    data["skipped"] = True
                    data["skip_reason"] = _PYTEST_SKIP_NO_TESTS
                # DF-GITREINS-POC-51: the pytest RUNNER itself is missing (no
                # pytest on PATH, a pinned interpreter without it, a venv that
                # was never created), so the step graded nothing. The guard
                # grades the same lane SKIPPED with this fix line as its reason
                # (engine/guard_manager.py), and the judge must not report
                # `Stage tier1: FAIL` for a tree whose only problem is an
                # unprovisioned environment — the hook/guard disagreement the
                # row was filed for, same shape as POC-49 above (one predicate,
                # imported never copied). A real failing run never lands here:
                # the classifier demands both the not-started evidence and an
                # unresolvable runner.
                elif runner_missing := _pytest_runner_missing_hint(
                    analysis_cmd, self.workdir, exit_code, output
                ):
                    passed = True
                    data["skipped"] = True
                    data["skip_reason"] = runner_missing
            # INT-FLAKE-6: a tier1 tests step whose pytest failure carries NO
            # recognized classification (unknown kind — typically truncated or
            # interleaved output under heavy load) is recorded as
            # contention-degraded with the skipped_tests marker, so a
            # green-alone/red-concurrent result can never be read as a code
            # defect. A classified failure (failed/maxfail) still fails hard.
            if (
                stage_id == "tier1"
                and step_id == "tests"
                and not passed
                and data.get("pytest_outcome", {}).get("kind") == "unknown"
                and tier1_lock_result is not None
            ):
                data["skipped"] = True
                # The machine-readable marker: this PASS graded no tests. It
                # is the key `_record_runtime_skips` reads (never an output
                # parse) to fold the step into stage.skipped_steps — the
                # record worktree_manager._tier1_skipped_steps refuses at
                # merge-back. Only the marker is machine-consumed; a reader
                # keying on the human `skip_reason` prose would be fragile.
                data["skipped_tests"] = True
                data["skip_reason"] = (
                    "unknown-class pytest failure under concurrent tier1 runs "
                    f"(tier1_tests_lock: {tier1_lock_result}) — contention, not "
                    "a code defect; re-run the judge alone to grade the tree"
                )
                passed = True
            # DF-018: the tier-1 stage points the written verdict at the raw
            # guard evidence (complete, untruncated run log) when one exists.
            guard_log = self._guard_log_ref(stage_id)
            if guard_log:
                data["guard_log"] = guard_log
            # DF-GITREINS-POC-15: stamp the secrets scanners this run actually
            # used, so a consumer reads the attribution instead of parsing the
            # step output's prose. Absent when the step reported none.
            if step_id == "secrets":
                scanners = parse_secrets_scanners(output)
                if scanners:
                    data["secrets_scanners"] = scanners

            return StepResult(
                id=step_id,
                type="script",
                passed=passed,
                output=output,
                data=data,
                degraded=degraded_guard_pass,
            )
        except subprocess.TimeoutExpired:
            # GAP-058: same budget attribution as the run_bounded path above —
            # this except clause is the legacy safety net for the same event.
            budget = step_def.get("timeout", 120)
            return StepResult(
                id=step_id,
                type="script",
                passed=False,
                error=f"Command timed out after {budget}s (step budget)",
                data={"timed_out": True, "timeout_s": budget},
            )
        except Exception as e:
            return StepResult(id=step_id, type="script", passed=False, error=str(e))

    def _run_ai_eval(self, step_def: dict[str, Any], task: dict[str, Any]) -> StepResult:
        """Run the AI evaluator as a pipeline step."""
        step_id = step_def.get("id", "ai_eval")
        model = step_def.get("model")
        max_iterations = step_def.get("max_iterations", -1)

        # Lazy init LLM client
        if self._llm is None:
            from engine.llm import LLMClient

            if model:
                self._llm = LLMClient(model=model)
            else:
                self._llm = LLMClient()

        from engine.eval_cap import (
            EvalCap,
            _parse_time,
            _parse_tokens,
            eval_cap_from_config,
        )
        from engine.evaluator import AgenticEvaluator, tool_calls_transcript

        # Cap resolution: config.yaml evaluator: section is the base;
        # caps EXPLICITLY set in the step config override it. A step that
        # sets nothing (or only max_iterations: -1 = "defer to evaluator
        # config", the default) passes eval_cap=None so AgenticEvaluator
        # reads the config itself — the documented working path (helix
        # tick 60: -1 → full caps from config, zero compaction cycles).
        #
        # Do NOT build an explicit EvalCap with -1 defaults for token
        # caps: compaction threshold int(-1*0.9)=0 → the evaluator
        # compacts on every turn and never produces a verdict (fleet-wide
        # tier2 INCOMPLETE 'Context near limit (N/-1 tokens)', 2026-08).
        explicit_caps: dict[str, float | int] = {}
        if max_iterations not in (None, -1):
            explicit_caps["max_iterations"] = max_iterations
        max_time = step_def.get("max_time")
        if max_time:
            max_seconds = _parse_time(str(max_time))
            if max_seconds is not None:
                explicit_caps["max_seconds"] = float(max_seconds)
        for key in ("max_input_tokens", "max_output_tokens"):
            raw = step_def.get(key)
            if raw is None or raw == -1:
                continue
            if isinstance(raw, str):
                parsed = _parse_tokens(raw)
                if parsed is None:
                    logger.warning("Unparseable %s=%r in step %s — ignoring", key, raw, step_id)
                    continue
                explicit_caps[key] = parsed
            else:
                explicit_caps[key] = int(raw)
        if step_def.get("tool_call_weight") is not None:
            explicit_caps["tool_call_weight"] = float(step_def["tool_call_weight"])

        if explicit_caps:
            # Merge step overrides over the config base so unset caps
            # never fall back to unlimited.
            base = eval_cap_from_config(self.config)
            eval_cap = EvalCap(
                max_iterations=float(explicit_caps.get("max_iterations", base.max_iterations)),
                max_seconds=float(explicit_caps.get("max_seconds", base.max_seconds)),
                max_input_tokens=int(explicit_caps.get("max_input_tokens", base.max_input_tokens)),
                max_output_tokens=int(
                    explicit_caps.get("max_output_tokens", base.max_output_tokens)
                ),
                tool_call_weight=float(
                    explicit_caps.get("tool_call_weight", base.tool_call_weight)
                ),
            )
            evaluator = AgenticEvaluator(
                self._llm,
                self.scan_root,
                eval_cap=eval_cap,
                config_root=self.workdir,
                progress=self._progress,
            )
        else:
            # Nothing set in the step — defer to .gitreins/config.yaml
            evaluator = AgenticEvaluator(
                self._llm,
                self.scan_root,
                config_root=self.workdir,
                progress=self._progress,
            )

        # GR-234: make the immediate predecessor available to the evaluator's
        # input prompt. Do not add the key for the first stage: preserving the
        # historical task shape is part of the single-stage contract.
        pipeline_context = self._get_pipeline_context()
        if self._stage_context is not None:
            task["_stage_context"] = dict(pipeline_context["stage_context"])
        else:
            task.pop("_stage_context", None)

        # The evaluator receives this context through the task input. It is
        # deliberately also available to prompt templates for custom stages.
        task["_pipeline_context"] = pipeline_context
        if step_def.get("prompt_template"):
            import json as _json

            ctx_str = _json.dumps(pipeline_context.get("stages", {}), default=str)[:4000]
            rendered = step_def["prompt_template"].replace("{{ pipeline_context }}", ctx_str)
            task["_system_prompt_override"] = rendered
        else:
            task.pop("_system_prompt_override", None)

        # GR-195: a fresh step clears the slot so a previous ai_eval stage's
        # transcript can never be mistaken for this one's.
        self._last_tool_transcript = None
        try:
            verdict = evaluator.evaluate(task)
            # GR-195: the evaluator's in-run tool record (GR-179) becomes the
            # execution transcript the verdict persists. None (the slot stays
            # empty) when the evaluator kept no record — absent, never empty.
            self._last_tool_transcript = tool_calls_transcript(
                getattr(evaluator, "_tool_record", [])
            )
            # GR-167: a COMPLETE verdict is not a pass on its own word. The
            # items must grade every task criterion (76 of 297 stored passes
            # carried no items at all), and the criteria that were not graded
            # are named in the step output a reader sees.
            from engine.gate import (
                check_coverage,
                ungraded_note,
                verdict_is_passing,
                verdict_outcome,
            )

            coverage = check_coverage(verdict, task.get("criteria"))
            passed = verdict_is_passing(verdict, task.get("criteria"))
            if passed and verdict_outcome(verdict) == "INCOMPLETE":
                # GR-233: four PASS items with complete task-criterion coverage
                # were rejected solely because the aggregate label said
                # INCOMPLETE. The evidence gate is authoritative; repair the
                # rendered/persisted label and summary to match it.
                verdict.verdict = "COMPLETE"
                verdict.summary = (
                    f"All {len(coverage.graded)} task criteria were graded and passed."
                )

            from engine.evaluator import item_glyph

            items_output = "\n".join(
                f"  {item_glyph(i.status)} {i.criterion}: {i.detail}" for i in verdict.items
            )
            coverage_note = ungraded_note(coverage)
            if coverage_note:
                items_output = f"{items_output}\n  ✗ {coverage_note}".lstrip("\n")

            # Persist the judge's real token usage so external tools (e.g. the
            # coding-hermes scheduler dashboard) can sum GitReins judge cost
            # alongside foreman/worker cost. GitReins uses its own LLM client,
            # so its usage never appears in Hermes' state.db telemetry; without
            # this, judge cost was invisible. Append to .gitreins/usage.jsonl,
            # timestamped, one JSON line per judge run. Best-effort — never
            # blocks or fails the eval on a write error. (2026-08-08)
            # EVID-003: ``persist_telemetry=False`` (the ephemeral judge) skips
            # it — the line lands inside the graded tree, and an ephemeral run
            # has no persisted verdict to attribute it to anyway.
            # REVIEW-GITREINS-030: credential-free judge provenance, taken from
            # the SAME client the evaluator ran (self._llm) — the resolved model
            # plus WHERE the base URL and API key came from, the exact fields
            # LLMClient.describe() renders. Never the URL or key values. Duck-
            # typed fakes without the attributes contribute no fields.
            provenance = usage.provenance_fields(self._llm) if self._llm is not None else {}

            self._append_judge_usage(evaluator, step_id, outcome="graded")

            # TRBL-135 (AC1/AC2): the run's context census and, when the
            # wall clock tripped, its first-class outcome ride the step
            # data — additive (absent, never null) so every verdict without
            # them serializes exactly as before.
            context_stats = getattr(verdict, "context_stats", None)
            tier2_outcome = getattr(verdict, "outcome", None)

            return StepResult(
                id=step_id,
                type="ai_eval",
                passed=passed,
                output=f"{verdict.verdict}\n{items_output}\n{verdict.summary}",
                data={
                    "verdict": verdict.verdict,
                    "items": [_verdict_item_data(i) for i in verdict.items],
                    "summary": verdict.summary,
                    # GR-167: the criteria the verdict never graded, named so a
                    # consumer can act on the gap instead of only seeing a
                    # failed step. Absent when coverage is complete, keeping the
                    # historical shape for a fully graded verdict.
                    **({"ungraded_criteria": list(coverage.ungraded)} if coverage.ungraded else {}),
                    # REVIEW-GITREINS-030: persisted with the verdict so judge
                    # provenance is attributable in verdict.json; absent (not
                    # null) when the client carries no identity fields.
                    **({"provenance": dict(provenance)} if provenance else {}),
                    # TRBL-135 (AC2): "timeout" when the wall-clock ceiling —
                    # now a default — ended the run; absent for every other
                    # exit, so non-timeout verdicts keep the historical shape.
                    **({"outcome": tier2_outcome} if tier2_outcome else {}),
                    # TRBL-135 (AC1): diff_source/diff_bytes/prompt_bytes from
                    # the evaluator's own run record; absent when none were
                    # recorded (no prompt built).
                    **(
                        {"context_stats": dict(context_stats)}
                        if isinstance(context_stats, dict) and context_stats
                        else {}
                    ),
                },
            )
        except Exception as e:
            logger.exception("AI eval failed")
            # JUDGE-TIER2-001: a run that DIED still burned its budget. The row
            # used to be written only after a verdict came back, so the measured
            # 48-minute / 3.1M-token run that hit a UnicodeDecodeError left no
            # cost row at all — the ledger showed the failure as free. Written
            # here, on the failure path, with outcome="error" so the two shapes
            # are distinguishable.
            self._append_judge_usage(evaluator, step_id, outcome="error")
            return StepResult(id=step_id, type="ai_eval", passed=False, error=str(e))

    def _append_judge_usage(self, evaluator: Any, step_id: str, *, outcome: str) -> None:
        """Append this run's cost row to ``.gitreins/usage.jsonl`` — on any exit.

        Best-effort by the same contract as the ledger itself (see
        ``engine.usage.append_usage_row``): telemetry never fails or blocks a
        grade. ``outcome`` is ``"graded"`` (a verdict was produced, COMPLETE or
        INCOMPLETE alike) or ``"error"`` (the run raised), so a spent-but-dead
        run is visible in the ledger instead of absent from it.
        """
        try:
            cap = getattr(evaluator, "eval_cap", None)
            if cap is None or not self.persist_telemetry:
                return
            provenance = usage.provenance_fields(self._llm) if self._llm is not None else {}
            usage.append_usage_row(
                self.workdir,
                step=step_id,
                tokens_in=getattr(cap, "cumulative_input_tokens", 0),
                tokens_out=getattr(cap, "cumulative_output_tokens", 0),
                cache_read=getattr(cap, "cumulative_cache_read", 0),
                cache_write=getattr(cap, "cumulative_cache_write", 0),
                outcome=outcome,
                model=provenance.get("model"),
                base_url_source=provenance.get("base_url_source"),
                api_key_source=provenance.get("api_key_source"),
            )
        except Exception:
            pass  # non-fatal — the same contract the inline append held

    def _run_commit_audit(self, step_def: dict[str, Any], task: dict[str, Any]) -> StepResult:
        """Run the commit message auditor as a pipeline step.

        Reads the commit message from ``task["commit_message"]`` and the
        staged diff from git.  Uses the CommitAuditor to validate the
        message against the diff, with optional LLM exploration
        (configured via ``max_iterations`` in the step or config).

        ``mode`` is resolved with an EXPLICIT precedence (DF-GITREINS-POC-30):
        the stage's own ``mode`` wins, then ``defaults.commit_audit.mode``, then
        top-level ``commit_audit.mode`` (the legacy placement), then ``"warn"``.
        Before this, only the top-level key was read, so the stage-scoped and
        ``defaults`` placements documented in ``docs/cli-reference.md`` were
        dead config — a ``mode: block`` there stayed a warning with exit 0.

        ``enabled`` is resolved with the same precedence discipline
        (REVIEW-GITREINS-024): the stage's own boolean ``enabled`` wins,
        then the merged ``commit_audit`` section's ``enabled``, then the
        documented default ``True``. When the resolved value is false the
        step returns a NAMED skip (``passed=True``, exit 0) BEFORE any LLM
        client or auditor is constructed, so disabling the audit means the
        LLM is never called and the commit is not failed. YAML string
        forms ("false"/"no"/"0"/"off" and "true"/"yes"/"1"/"on",
        case-insensitive) are normalized; an UNRECOGNIZED value (``enabled:
        flase``, an int) is treated as unset at that level and resolution
        falls through — deliberately neither a crash nor a silent false:
        a typo must not disable a security-relevant audit, and it must not
        read as "off" either.

        Other config keys (from .gitreins/config.yaml):
          ``commit_audit.strictness`` — "lenient" | "standard" (default) | "strict"
          ``commit_audit.max_iterations`` — int, default 3
          ``commit_audit.suggest_message`` — bool, default True
          ``commit_audit.review_score_threshold`` — float, default 8.0 (GR-066)
          ``commit_audit.review_score_offset`` — float, default 1.0 (GR-066)
        """
        step_id = step_def.get("id", "commit_audit")

        # Read config for commit_audit settings — BEFORE any LLM/auditor
        # construction, so ``enabled: false`` can mean what it says: the
        # audit does not grade, and no LLM client is ever built (a client
        # that is never constructed cannot make a network call).
        config = self._load_commit_audit_config()
        # GR-148: per-invocation overrides (CLI ``--review-profile`` /
        # ``--review-effort``) ride on the task dict and take precedence over
        # repo config; the profile default is the base beneath both.
        task_overrides = task.get("commit_audit_overrides") or {}
        task_review_effort_override = None
        if isinstance(task_overrides, dict):
            task_review_effort_override = task_overrides.get("review_effort")
            config = {**config, **{k: v for k, v in task_overrides.items() if v is not None}}

        enabled, enabled_source = resolve_commit_audit_enabled(self.config, config, step_def)
        if not enabled:
            # REVIEW-GITREINS-024: named skip, passed=True, exit 0 — a repo
            # that disabled the audit must not fail its commit, and the line
            # must say WHY nothing ran (same vocabulary as the other named
            # skips in this file) instead of looking like a passing audit.
            return StepResult(
                id=step_id,
                type="commit_audit",
                passed=True,
                output=(f"commit audit: disabled ({enabled_source} is false) \u2014 audit NOT run"),
                data={"mode": "disabled", "enabled_source": enabled_source},
            )

        mode = resolve_commit_audit_mode(self.config, config, step_def)

        # Lazy init LLM client
        if self._llm is None:
            from engine.llm import LLMClient

            self._llm = LLMClient()

        from engine.commit_audit import CommitAuditor

        score_threshold = float(config.get("review_score_threshold", 8.0))
        score_offset = float(config.get("review_score_offset", 1.0))

        auditor = CommitAuditor(
            self._llm,
            self.workdir,
            strictness=config.get("strictness", "standard"),
            max_iterations=config.get("max_iterations", 3),
            suggest_message=config.get("suggest_message", True),
            review_mode=config.get("review_mode", "message"),
            review_checks=config.get("review_checks"),
            review_severity=config.get("review_severity", "standard"),
            review_suggest_fix=config.get("review_suggest_fix", True),
            review_score_threshold=score_threshold,
            review_score_offset=score_offset,
            review_profile=config.get("review_profile", "standard"),
            review_effort_override=task_review_effort_override or config.get("review_effort"),
            mode=mode,
        )

        message = task.get("commit_message", "")
        if not message:
            # Try reading from git commit message file
            msg_path = os.path.join(self.workdir, ".git", "COMMIT_EDITMSG")
            if os.path.exists(msg_path):
                try:
                    with open(msg_path) as f:
                        raw = f.read().strip()
                    # Strip comment lines
                    message = "\n".join(
                        line for line in raw.split("\n") if not line.startswith("#")
                    ).strip()
                except Exception:
                    pass

        if not message:
            return StepResult(
                id=step_id,
                type="commit_audit",
                passed=True,
                output="No commit message to audit.",
            )

        try:
            result = auditor.audit(message)
        except Exception as e:
            logger.warning("Commit audit failed: %s", e)
            return StepResult(
                id=step_id,
                type="commit_audit",
                passed=mode != "block",
                output=(
                    f"BLOCK-UNVERIFIED: commit audit could not obtain an LLM verdict: {e}"
                    if mode == "block"
                    else f"Audit error (passing): {e}"
                ),
            )

        passed = result.valid or mode != "block"

        output_lines: list[str] = []
        if (
            mode == "block"
            and not result.valid
            and any(
                issue.startswith(("LLM unavailable:", "LLM error on iteration"))
                for issue in result.issues
            )
        ):
            output_lines.append("BLOCK-UNVERIFIED: LLM audit unavailable; commit blocked.")

        # ── CVE-style scoring (GR-066) ──
        all_review_issues = getattr(result, "review_issues", [])
        if all_review_issues:
            # Determine highest effective score
            max_effective = 0.0
            for ri in all_review_issues:
                raw_score = ri.get("score", 0.0)
                effective = raw_score * score_offset
                ri["effective_score"] = effective
                if effective > max_effective:
                    max_effective = effective

            sev_marker = {
                "critical": "🔴 CRITICAL",
                "high": "🟠 HIGH",
                "medium": "🟡 MEDIUM",
                "low": "🟢 LOW",
                "info": "ℹ️ INFO",
            }
            output_lines.append(
                f"⚠ Commit review — {len(all_review_issues)} issue(s) found (overall: {max_effective:.1f}/{score_threshold:.1f})"
            )
            review_summary = getattr(result, "review_summary", "")
            if review_summary:
                output_lines.append(f"   {review_summary}")
            output_lines.append("")

            blocked = False
            warn_issues = False
            for ri in all_review_issues:
                sev = sev_marker.get(ri.get("severity", "info"), "ℹ️ INFO")
                cat = ri.get("category", "unknown")
                file_ref = f"{ri.get('file', '')}:{ri.get('line', 0)}"
                title = ri.get("title", "")
                desc = ri.get("description", "")
                sugg = ri.get("suggestion", "")
                effective = ri.get("effective_score", 0.0)

                # Score-based action marker (GR-066)
                if effective >= score_threshold:
                    action_mark = "🚫 BLOCK"
                    blocked = True
                elif effective >= score_threshold * 0.75:
                    action_mark = "⚠️ WARN"
                    warn_issues = True
                else:
                    action_mark = "ℹ️ INFO"

                output_lines.append(
                    f"  {file_ref} [{cat}] [{sev}] {action_mark} (score: {effective:.1f}) — {title}"
                )
                if desc:
                    output_lines.append(f"    {desc}")
                if sugg:
                    output_lines.append(f"    Fix: {sugg}")
                output_lines.append("")

            # Apply scoring to pass/fail
            if blocked and mode == "block":
                passed = False
            elif blocked and mode == "warn":
                output_lines.append("(Warning: issues above threshold — review recommended)")
            elif warn_issues:
                output_lines.append("(Warning: issues in warning range — review recommended)")

        # ── Message audit ──
        if result.valid:
            output_lines.append("✓ Commit message looks good.")
        else:
            output_lines.append("⚠ Commit message issues:")
            for issue in result.issues:
                output_lines.append(f"  - {issue}")
            if result.suggested_message:
                output_lines.append(f"\nSuggested message: {result.suggested_message}")
            if mode == "block":
                output_lines.append(
                    "\n(Commit BLOCKED — fix message or set commit_audit.mode=warn)"
                )
            elif mode == "warn":
                output_lines.append("\n(Warning only — commit will proceed)")

        return StepResult(
            id=step_id,
            type="commit_audit",
            passed=passed,
            output="\n".join(output_lines),
            data={
                "valid": result.valid,
                "issues": result.issues,
                "suggested_message": result.suggested_message,
                "mode": mode,
                "iterations_used": result.iterations_used,
                "review_issues": getattr(result, "review_issues", []),
                "review_summary": getattr(result, "review_summary", ""),
            },
        )

    def _run_fix_verification(self, step_def: dict[str, Any], task: dict[str, Any]) -> StepResult:
        """Run the deterministic fix-verification stage (GR-235).

        Verifies that the immediately preceding review stage's findings
        (``StageContext.prior_findings`` — GR-234) were actually handled by
        the judged change, via :class:`FixVerification` — no LLM call. The
        step PASSES when the verification ran: the stage's job is to report
        per-finding dispositions with evidence, and an open
        critical/high/medium finding is carried as a ``needs_review`` verdict
        cap (``data["verdict_cap"]``, folded onto the stage dict) rather than
        as a stage failure — the cap is the downgrade policy, never a hard
        pass (see engine.gate.fix_verification_unhandled,
        WorktreeManager._refuse_unhandled_findings and
        docs/fix-verification.md). A stage with no prior context — the first
        stage in a pipeline — returns a named skip so it cannot read as a
        graded verification.
        """
        step_id = step_def.get("id", FIX_VERIFICATION_STAGE_ID)
        context = self._stage_context
        if context is None:
            return StepResult(
                id=step_id,
                type="fix_verification",
                passed=True,
                output=f"{step_id}: no prior stage context — nothing to verify (named skip)",
                data={"skipped": True, "skip_reason": "no prior stage findings to verify"},
            )
        try:
            dispositions = FixVerification(self.workdir).verify(context.prior_findings)
        except Exception as e:  # noqa: BLE001 — a broken verifier must not crash the judge
            logger.exception("Fix verification failed")
            return StepResult(
                id=step_id,
                type="fix_verification",
                passed=False,
                error=f"fix verification failed: {e}",
            )

        capped = [d for d in dispositions if d.caps_verdict()]
        counts: dict[str, int] = {}
        for disposition in dispositions:
            counts[disposition.disposition] = counts.get(disposition.disposition, 0) + 1
        data: dict[str, Any] = {
            "dispositions": [d.to_dict() for d in dispositions],
            "counts": counts,
        }
        lines: list[str] = []
        for d in dispositions:
            glyph = "✗" if d.caps_verdict() else ("~" if d.disposition == "superseded" else "✓")
            lines.append(
                f"  {glyph} {d.finding_id} [{d.severity or 'unclassified'}] "
                f"{d.disposition}: {d.evidence}"
            )
        if capped:
            # The binding cap rule (GR-235): an open critical/high/medium (or
            # unnamed-severity) finding caps the final verdict at
            # needs_review — never a hard pass. The cap rides the data (and
            # the folded stage dict); the gates read it from there.
            data["verdict_cap"] = FIX_VERIFICATION_CAP
            data["capped_findings"] = [d.finding_id for d in capped]
            lines.append(
                f"  ⚠ verdict capped at {FIX_VERIFICATION_CAP} by {len(capped)} unhandled finding(s)"
            )
        output = f"{step_id}: {len(dispositions)} finding(s) verified"
        if lines:
            output += "\n" + "\n".join(lines)
        return StepResult(
            id=step_id, type="fix_verification", passed=True, output=output, data=data
        )

    def _load_commit_audit_config(self) -> dict[str, Any]:
        """Read the commit_audit section from .gitreins/config.yaml.

        The section is the MERGE of top-level ``commit_audit`` (legacy, where
        ``mode`` used to be read from) and ``defaults.commit_audit`` — the
        natural place, since every other setting lives under ``defaults:``.
        Neither is a superset of the other: ``defaults`` carries everything the
        installer writes, the top-level block is what users of the older docs
        already have. Top-level keys win on conflict, so a repo that set both
        keeps its recorded behavior. ``mode`` itself is resolved separately by
        :func:`resolve_commit_audit_mode`, which also consults the stage def.
        """
        import yaml

        config_path = os.path.join(self.workdir, ".gitreins", "config.yaml")
        if os.path.exists(config_path):
            try:
                with open(config_path) as f:
                    cfg = yaml.safe_load(f) or {}
                merged: dict[str, Any] = {}
                defaults_cfg = cfg.get("defaults", {}).get("commit_audit", {}) or {}
                if isinstance(defaults_cfg, dict):
                    merged.update(defaults_cfg)
                top_cfg = cfg.get("commit_audit", {}) or {}
                if isinstance(top_cfg, dict):
                    merged.update(top_cfg)
                return merged
            except Exception:
                pass
        return {}

    def _run_output(self, step_def: dict[str, Any], task: dict[str, Any]) -> StepResult:
        """Compile output from all stages."""
        step_id = step_def.get("id", "output")
        fmt = step_def.get("format", "{{ stages }}")

        output = self._template(fmt, task)
        return StepResult(id=step_id, type="output", passed=True, output=output)

    def _template(self, text: str, task: dict[str, Any]) -> str:
        """Simple template substitution with {{ var }} syntax.

        Available vars:
            {{ task.id }}, {{ task.title }}, {{ task.criteria }}
            {{ stage.<id>.passed }}, {{ stage.<id>.any_failed }}
            {{ stage.<id>.summary }}
            {{ stages }} — full stage results as JSON
        """
        # Task vars
        text = text.replace("{{ task.id }}", str(task.get("id", "")))
        text = text.replace("{{ task.title }}", str(task.get("title", "")))
        text = text.replace("{{ task.criteria }}", json.dumps(task.get("criteria", []), indent=2))

        # Stage vars
        for stage_id, stage in self._stage_results.items():
            prefix = f"{{{{ stage.{stage_id}"
            text = text.replace(f"{prefix}.passed }}}}", str(stage.passed))
            text = text.replace(f"{prefix}.any_failed }}}}", str(stage.any_failed))
            text = text.replace(f"{prefix}.summary }}}}", str(stage.summary))
            text = text.replace(f"{prefix} }}}}", json.dumps(stage.to_dict(), indent=2))

        # All stages
        stages_json = json.dumps(
            {sid: s.to_dict() for sid, s in self._stage_results.items()},
            indent=2,
        )
        text = text.replace("{{ stages }}", stages_json)

        return text

    def _compute_diff_digest(self) -> str:
        """Return a stable digest of the tree input shared by all stages."""
        try:
            completed = subprocess.run(
                ["git", "diff", "HEAD", "--no-color"],
                cwd=self.workdir,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            diff = completed.stdout
        except (OSError, subprocess.SubprocessError):
            diff = ""
        return hashlib.sha256(diff.encode("utf-8")).hexdigest()

    def _context_from_stage(self, stage: StageResult) -> StageContext:
        serialized = stage.to_dict()
        findings = serialized.get("steps", [])
        verdict = ""
        for step in stage.steps:
            if isinstance(step.data, dict) and step.data.get("verdict"):
                verdict = str(step.data["verdict"])
        return StageContext(
            prior_findings=findings,
            prior_verdict=verdict,
            prior_status="passed" if stage.passed else "failed",
            diff_digest=self._diff_digest,
        )

    def _get_pipeline_context(self) -> dict[str, Any]:
        """Get context from previous stages to inject into AI evaluation."""
        context: dict[str, Any] = {
            "stages": {sid: s.to_dict() for sid, s in self._stage_results.items()},
        }
        if self._stage_context is not None:
            context["stage_context"] = self._stage_context.to_dict()
        return context

    def _summarize_stage(self, stage: StageResult) -> str:
        """Create a summary string for a stage — ONE line per step.

        DF-GITREINS-POC-14: this used to render ``output[:100]`` verbatim, so
        a step whose capture carried a newline printed as several lines and a
        step with raw escape codes (gitleaks' ``\\x1b[32mINF`` log lines)
        printed them; a step that produced NO output printed a dangling
        ``✓ lint: ``. Now every detail is ANSI-stripped, single-line, and
        named when empty.
        """
        lines = []
        for step in stage.steps:
            skip_reason = _step_skip_reason(step)
            if skip_reason:
                # DF-GITREINS-POC-49: a benign pytest exit 5 PASSES but graded
                # nothing — render it in the same ~ (DEGRADED) register the guard
                # console uses for a skipped gate ("~ tests — skipped (no tests
                # collected)"), never as a ✓ that claims the tests ran.
                lines.append(f"  ~ {step.id}: skipped ({skip_reason})")
                continue
            budget = _step_budget_timeout(step) if not step.passed else None
            if budget is not None:
                # GAP-058: the ~ marker is the DEGRADED vocabulary (a gate
                # that did no work prints it, never a ✓) — a gate that ran
                # OUT OF TIME renders in the same register, and the line
                # names the budget so "ran out of time" is readable without
                # the source. The stage is still a failure: it did not
                # finish grading.
                lines.append(f"  ~ {step.id}: Command timed out after {budget}s (step budget)")
                continue
            status = "✓" if step.passed else "✗"
            source = step.output or step.error
            detail = _first_nonblank_line(source)
            if not step.passed and step.output:
                # A failing step's first 100 chars are the pytest banner;
                # surface the first FAILED/ERROR short-summary line instead
                # so the failing test id is visible (DF-021 kin /
                # DF-GITREINS-POC-8). TRUST-003 (AC1): the parsed id is named
                # with the same '[first failing id]' marker the guard console
                # uses. Fall back to the sanitized head when the output
                # carries no recognizable failure line.
                clean = strip_ansi(step.output)
                first_id = parse_first_failing_test(clean)
                if first_id:
                    detail = f"FAIL ({first_id} [first failing id])"[:100]
                else:
                    for line in clean.split("\n"):
                        stripped = line.strip()
                        if (
                            _FAILED_TEST_LINE.match(stripped)
                            or _ERROR_TEST_LINE.match(stripped)
                            or _SECRETS_FINDING_LINE.search(stripped)
                        ):
                            detail = stripped[:100]
                            break
            if not detail:
                # Never print a dangling colon: name the empty case instead.
                detail = "ok (no output)" if step.passed else "no output"
            lines.append(f"  {status} {step.id}: {detail}")
        return "\n".join(lines)

    def _compile_results(self) -> dict[str, Any]:
        """Compile final results from all stages."""
        all_passed = all(s.passed for s in self._stage_results.values())
        result: dict[str, Any] = {
            "passed": all_passed,
            "stages": {sid: s.to_dict() for sid, s in self._stage_results.items()},
        }
        # GR-235: a fix-verification stage that left a critical/high/medium
        # finding open caps the run's final verdict at needs_review — carried
        # additively at the top level so a consumer answers "what does this
        # run mean" without walking the stages. Runs with nothing open keep
        # the historical {"passed", "stages"} shape byte-identically.
        cap = next((s.verdict_cap for s in self._stage_results.values() if s.verdict_cap), "")
        if cap:
            result["verdict_cap"] = cap
        return result


def _normalize_yaml_bool_keys(obj: object) -> object:
    """Recursively convert boolean keys to their string equivalents.

    PyYAML 1.1 parses unquoted ``on``, ``off``, ``yes``, ``no``, ``true``,
    ``false`` as Python bools.  When those appear as mapping keys they break
    lookups: ``stage_def.get("on")`` returns None because the real key is
    ``True``.  This walker converts them back to the lowercase string form.
    """
    if isinstance(obj, dict):
        fixed = {}
        for k, v in obj.items():
            if isinstance(k, bool):
                k = str(k).lower()  # True→"true", False→"false"
            fixed[k] = _normalize_yaml_bool_keys(v)
        return fixed
    if isinstance(obj, list):
        return [_normalize_yaml_bool_keys(i) for i in obj]
    return obj


# Mapping of Python bool → YAML 1.1 boolean keyword that would have
# produced it when used as a plain scalar key.
_YAML_BOOL_KEY_MAP: dict[bool, str] = {
    True: "on",
    False: "off",
}


def _fix_on_key(obj: object) -> object:
    """Post-processor specifically for the ``on`` / ``off`` key pitfall.

    YAML 1.1 interprets ``on: [...]`` as ``True: [...]``.  This second pass
    converts bool-to-string using the most-common-intent mapping
    (True→"on", False→"off") rather than the generic True→"true".
    """
    if isinstance(obj, dict):
        fixed: dict[Any, Any] = {}
        for k, v in obj.items():
            if isinstance(k, bool):
                k = _YAML_BOOL_KEY_MAP.get(k, str(k).lower())
            fixed[k] = _fix_on_key(v)
        return fixed
    if isinstance(obj, list):
        return [_fix_on_key(i) for i in obj]
    return obj


def _lint_step_run(lint_cmd: str, config: dict[str, Any] | None = None) -> str:
    """Wrap *lint_cmd* with the guard's missing-linter semantics.

    ``GuardManager._check_lint`` returns PASS ("No linter found — skipped")
    when the linter binary is absent. The judge's lint step must behave the
    same way, or Tier 1 would FAIL on a machine that merely lacks ruff while
    ``gitreins guard`` passes on the identical tree (DF-GITREINS-POC-16).
    The binary is only checked for EXISTENCE; a lint finding still fails.

    DF-GITREINS-POC-55: the linter is a spawned external scanner, so it runs
    behind the shared nice policy (``guards.scanner_nice`` /
    ``GITREINS_SCANNER_NICE``, default 10, 0 = off). The probe lives INSIDE
    this script (fail-open: no ``nice`` → run unprefixed, one note line), and
    at level 0 no prologue is emitted at all — the command is byte-identical
    to the pre-change one.
    """
    binary = lint_cmd.split()[0]
    level, _source = scanner_nice.resolve_level(config)
    prologue, nice_prefix = scanner_nice.shell_prologue(level)
    return (
        f"if command -v {binary} >/dev/null 2>&1; then {prologue}{nice_prefix}{lint_cmd}; "
        f'else echo "{SKIP_SENTINEL} lint=no linter on PATH ({binary} not found)"; '
        f"exit 0; fi"
    )


def _resolve_gitleaks_scan_scope(scan_root: str) -> tuple[str | None, str | None]:
    """GR-244: resolve the full-tree gitleaks source to a repo-CONTAINED scope.

    Returns ``(refusal_reason, quoted_source)``. ``refusal_reason`` is None
    when the scan may run, and ``quoted_source`` is then an absolute,
    realpath-resolved path INSIDE the repo root that
    ``git rev-parse --show-toplevel`` establishes for *scan_root* — never a
    bare ``.``, so the ``--no-git`` filesystem walk can never start above
    the repo and escape into the home directory or the whole server. When
    no repo root can be established (the path is not inside a git repo, or
    git itself is unusable) the scope is uncontainable: the reason is
    returned and the caller must refuse the scan — an un-bounded
    ``--no-git`` walk reads every file under the source path and peaks
    ~1010% CPU (GR-244).

    The GIT_* environment is stripped for the rev-parse probe (DF-008): a
    pre-commit-hook-exported ``GIT_DIR`` would otherwise resolve a foreign
    repo's root and bless an uncontained scope.
    """
    src = os.path.realpath(scan_root)
    try:
        proc = subprocess.run(
            ["git", "-C", src, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
        )
    except (OSError, subprocess.TimeoutExpired):
        return ("not a git repo — scope cannot be contained", None)
    if proc.returncode != 0:
        return ("not a git repo — scope cannot be contained", None)
    repo_root = os.path.realpath(proc.stdout.strip())
    if not (src == repo_root or src.startswith(repo_root + os.sep)):
        return ("scan root outside the repo — scope cannot be contained", None)
    return (None, shlex.quote(src))


def _secrets_step_run(
    workdir: str, config: dict[str, Any] | None = None, scan_root: str | None = None
) -> str:
    """Shell command for the Tier 1 ``secrets`` step.

    DF-012: gitleaks' default rules (and the generated config) miss sk-/ghp_
    patterns, so "gitleaks clean" is not proof of clean. The built-in
    scanner (workdir mode — the judged changes are committed, not staged)
    ALWAYS runs, and the step fails if EITHER scanner finds anything.

    TRUST-002 / POC-17: gitleaks is handed the generated harness-state
    exclusion config, and the exclusion is echoed into the step output so a
    FAIL diagnosis reads it instead of re-chasing the canary. DF-2026-20:
    the echo used to run UNCONDITIONALLY at the top of the output, so a
    failing step's one-line summary picked it as the failure reason and
    every secrets FAIL was narrated as a harness-state scope disagreement
    (fleet row DF-2026-20 was filed on exactly that misreading — the real
    finding was the builtin credential-store path class on a repo's tracked
    .npmrc/.env.example). The echo now prints only in the gitleaks-failure
    branch, after the findings evidence, where TRUST-002 wants it. Fail-closed:
    if mktemp fails the missing config makes gitleaks exit non-zero rather
    than silently scanning with no scope.

    gitleaks absent → that half is skipped and the built-in cross-check
    still grades the tree. The built-in scanner runs under the interpreter
    that is executing gitreins (sys.executable), with PYTHONPATH pointing at
    the engine package root — a bare `python3` from PATH cannot import
    `engine`, which made this step fail with ModuleNotFoundError in any env
    where the package is only importable by the venv (2026-08-15 fix).

    DF-GITREINS-POC-55: gitleaks is an external scanner, so it runs behind the
    shared nice policy (``guards.scanner_nice`` / ``GITREINS_SCANNER_NICE``,
    default 10, 0 = off). The probe is in-band and fail-open — a host with no
    ``nice`` runs gitleaks unprefixed and says so once — and at level 0 the
    built command is byte-identical to the pre-change one. The prefix travels
    in ``$_gr_nice`` and applies to the gitleaks invocation only: the built-in
    cross-check is not an external spawn.
    """
    explicit_scan_root = scan_root is not None and os.path.realpath(scan_root) != os.path.realpath(
        workdir
    )
    scan_root = os.path.abspath(scan_root or workdir)
    gitleaks_config_root = scan_root if explicit_scan_root else workdir
    if explicit_scan_root:
        builtin_code = (
            "from engine.guard_manager import GuardManager; import os, sys; "
            "gm = GuardManager('.', scan_root=os.environ['GITREINS_SCAN_ROOT']); "
            "r = gm._builtin_secrets_scan(staged_only=False); "
            "print('secrets: builtin cross-check: ' + r.output); "
            "print('secrets: builtin cross-check status: ' "
            "+ (r.scanners[0][1] if r.scanners else 'clean')); "
            "sys.exit(1 if not r.passed else 0)"
        )
        builtin_command = (
            f"PYTHONPATH={shlex.quote(_engine_root())} "
            f"GITREINS_SCAN_ROOT={shlex.quote(scan_root)} "
            f"{shlex.quote(sys.executable)} -c {shlex.quote(builtin_code)}"
        )
    else:
        builtin_command = (
            f'PYTHONPATH="{_engine_root()}" {sys.executable} -c "from engine.guard_manager import GuardManager; '
            "import sys; gm = GuardManager('.'); "
            "r = gm._builtin_secrets_scan(staged_only=False); "
            "print('secrets: builtin cross-check: ' + r.output); "
            "print('secrets: builtin cross-check status: ' "
            "+ (r.scanners[0][1] if r.scanners else 'clean')); "
            'sys.exit(1 if not r.passed else 0)"'
        )
    exclusions = ", ".join(f"{d}/**" for d in HARNESS_STATE_DIRS)
    level, _source = scanner_nice.resolve_level(config)
    prologue, nice_prefix = scanner_nice.shell_prologue(level)
    # PROF-002-adjacent: two bounds on the scan itself.
    #
    # Staged mode. The pre-commit hook sets GITREINS_GUARD_STAGED=1: the thing
    # being judged at commit time is the STAGED DIFF, so the scan is
    # `gitleaks protect --staged` — O(changed files), not O(worktree). Without
    # this every commit walked the whole tree, and a fleet of lanes committing
    # concurrently stacked several 16-core scans into host-wide saturation
    # (measured 2026-11-11: gitleaks at ~1010% CPU through routine lane
    # commits; the largest repo alone scans 409 MB in 48 s). The judge's pass
    # runs WITHOUT the env and keeps the full-tree detect scan: it grades the
    # committed tree, where staged-only would be the wrong scope.
    staged_mode = os.environ.get("GITREINS_GUARD_STAGED", "") == "1"
    if staged_mode:
        scan_cmd = 'gitleaks protect --staged --no-banner --no-color --config "$_glcfg"'
        scan_note = 'echo "secrets: gitleaks scope=staged diff (GITREINS_GUARD_STAGED=1)"; '
    else:
        # GR-244: the full-tree scan is repo-CONTAINED, no matter what. The
        # source is resolved through `git rev-parse --show-toplevel` — never
        # the caller's cwd — and a scan whose scope cannot be contained
        # inside a repo root is REFUSED outright: `--no-git` walks the
        # FILESYSTEM with no ls-files boundary, so an uncontained source
        # (a lane run from a non-repo cwd, an explicit scan root above the
        # repo) made gitleaks read the entire server for no reason. The
        # refusal grades gitleaks as scanner-not-run (the built-in
        # cross-check still grades the tree) and names itself in the
        # evidence. A contained scan additionally runs under a hard
        # GOMAXPROCS cap: nice deprioritizes, it does not bound CPU
        # (~1010% peak measured 2026-11-11).
        refusal_reason, scan_source = _resolve_gitleaks_scan_scope(scan_root)
        if refusal_reason is not None:
            scan_cmd = (
                f'echo "gitleaks: skipped ({refusal_reason})"; '
                f'echo "secrets: gitleaks scope-refused source" {shlex.quote(scan_root)}; '
                'echo "secrets: gitleaks: not run (scope cannot be contained)"; '
                "_glrc=0; _gl_skipped=1"
            )
            scan_note = ""
        else:
            scan_cmd = (
                f"gitleaks detect --source {scan_source} "
                '--no-git --no-banner --no-color --config "$_glcfg"'
            )
            # The cap is an EXPORT (shell builtin), not an `env` spawn: the
            # step must not grow a binary dependency, and the assignment has
            # to sit in the shell — a `nice`-prefixed argv cannot carry a
            # VAR=VALUE prefix. Inherited by the gitleaks child through
            # timeout/nice alike.
            scan_note = (
                f"export GOMAXPROCS={GITLEAKS_MAX_PROCS}; "
                f'echo "secrets: gitleaks scope=repo-root" {scan_source}; '
                f'echo "secrets: gitleaks parallelism=GOMAXPROCS={GITLEAKS_MAX_PROCS}"; '
            )
    # Timeout bound. A scan that never finishes proves nothing, so the bound
    # fails the step (timeout exits 124) and names itself in the evidence
    # instead of grading an unfinished scan green or letting it run unbounded.
    timeout_bound, _timeout_source = scanner_nice.resolve_timeout_s(config)
    bound = scanner_nice.timeout_prefix(config)
    return (
        "if command -v gitleaks >/dev/null 2>&1; then "
        '_glcfg="$(mktemp -t gitreins-gitleaks-XXXXXX.toml)"; '
        "cat > \"$_glcfg\" <<'GITREINS_GITLEAKS_CFG'\n"
        f"{harness_scan_gitleaks_config(gitleaks_config_root)}\n"
        "GITREINS_GITLEAKS_CFG\n"
        # TRUST-003: name the scanners and each one's outcome in the step
        # evidence, so a judge FAIL says WHICH scanner raised it instead of
        # leaving "secrets" ambiguous (the ambiguity that cost POC-15 a cycle).
        'echo "secrets: scanners=gitleaks+builtin cross-check"; '
        f'echo "secrets: gitleaks bound=timeout {timeout_bound}s (0=off)"; '
        # DF-GITREINS-POC-55: the nice policy, probed in-band so it sees the
        # PATH the scan runs with, and fail-open so a missing `nice` can never
        # turn a scan into a failure. Level 0 emits neither line nor prefix.
        f"{prologue}{scan_note}"
        # DF-GITREINS-POC-14: --no-color keeps gitleaks' logrus colour codes
        # out of the captured evidence — verdict.json recorded raw
        # `\x1b[32mINF\x1b[0m scanned ~5 MB` lines and the console summary
        # printed them when one landed first. The INFO lines themselves stay:
        # they are the scope evidence ("scanned ~5 MB") a post-mortem reads.
        f"{bound}{nice_prefix}{scan_cmd}; "
        '_glrc=$?; rm -f "$_glcfg"; '
        f'if [ "$_glrc" -eq 124 ]; then echo "secrets: gitleaks: TIMED OUT after {timeout_bound}s — the scan did not finish, so it cannot grade clean"; '
        # GR-244: a scope-refused scan already named itself above — it must
        # not fall through to the "clean" verdict, which would claim a scan
        # that never ran.
        'elif [ "${_gl_skipped:-0}" -eq 1 ]; then :; '
        'elif [ "$_glrc" -eq 0 ]; then echo "secrets: gitleaks: clean"; '
        f'else echo "secrets: gitleaks: findings found (exit $_glrc)"; echo "secrets: harness state excluded from gitleaks scope ({exclusions})"; fi; '
        "else _glrc=0; "
        'echo "secrets: scanners=builtin cross-check only (gitleaks not on PATH)"; '
        'echo "secrets: gitleaks: not on PATH"; fi; g1=$_glrc; '
        f"{builtin_command}; "
        'g2=$?; [ "$g1" -eq 0 ] && [ "$g2" -eq 0 ]'
    )


def _data_protection_enabled(config: dict[str, Any] | None) -> bool:
    """True when the operator opted into the data-protection lane (GR-146).

    Only a literal ``enabled: true`` in the top-level ``data_protection:``
    block arms the step — absent/false/wrong-typed all read as OFF, matching
    ``GuardManager`` and the jev-resolution block's posture.
    """
    block = (config or {}).get("data_protection")
    return isinstance(block, dict) and block.get("enabled") is True


def _data_protection_step_run(
    workdir: str, config: dict[str, Any] | None = None, scan_root: str | None = None
) -> str:
    """Build the Tier-1 data-protection step (GR-146).

    Keep policy/config lookup at the control root while scanning only the
    selected source root when the caller supplied a distinct scope.
    """
    explicit_scan_root = scan_root is not None and os.path.realpath(scan_root) != os.path.realpath(
        workdir
    )
    if not explicit_scan_root:
        return (
            f'PYTHONPATH="{_engine_root()}" {sys.executable} -c "'
            "from engine.guard_manager import GuardManager; import sys; "
            "gm = GuardManager('.', scope='working-tree'); "
            "r = gm._check_data_protection(); "
            "print('data_protection: ' + (r.output or '')); "
            'sys.exit(0 if r.passed else 1)"'
        )

    source_root = os.path.abspath(scan_root or "")
    python_code = (
        "from engine.guard_manager import GuardManager; import os, sys; "
        "gm = GuardManager('.', scope='working-tree', "
        "scan_root=os.environ['GITREINS_SCAN_ROOT']); "
        "r = gm._check_data_protection(); "
        "print('data_protection: ' + (r.output or '')); "
        "sys.exit(0 if r.passed else 1)"
    )
    return (
        f"PYTHONPATH={shlex.quote(_engine_root())} "
        f"GITREINS_SCAN_ROOT={shlex.quote(source_root)} "
        f"{shlex.quote(sys.executable)} -c {shlex.quote(python_code)}"
    )


def tier1_plan(
    workdir: str, config: dict[str, Any] | None = None, scan_root: str | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build the default Tier 1 steps plus their coverage marker.

    DF-GITREINS-POC-16: Tier 1 must grade the SAME set the guard would grade
    on this tree — secrets, lint (unless ``guards.lint: false``), tests — with
    the test command coming from ``guards.test_command`` (or the detected
    language's default) resolved through the guard's own
    ``_resolve_test_command``. Language detection is delegated to
    ``engine.lang_detect``, the single source of truth shared with the guard
    and ``init``; the signature-file table alone used to leave marker-less
    repos (plain ``.py`` trees) with a secrets-only Tier 1 that a green judge
    verdict then misreported as a full pass.

    Returns ``(steps, marker)`` where *marker* is always a dict:
    ``coverage`` (which checks ran, ``+``-joined step ids, or
    ``"secrets-only"``), ``degraded`` (bool), ``skipped_steps`` (list) and
    ``reason``. A degraded marker never flips the stage's verdict — it makes
    a narrower run honest.
    """
    guards_cfg = (config or {}).get("guards", {})
    configured_test_cmd = guards_cfg.get("test_command")
    test_timeout = int(guards_cfg.get("test_timeout", 120))
    # DF-GITREINS-POC-55: one nice policy for every external scanner this Tier 1
    # spawns — resolved once here (env > guards.scanner_nice > default 10) and
    # handed to each step builder below.
    nice_level, _nice_source = scanner_nice.resolve_level(config)
    steps: list[dict[str, Any]] = [
        {
            "id": "secrets",
            "type": "script",
            "run": _secrets_step_run(workdir, config, scan_root),
            "on_fail": "continue",
        },
    ]
    # GR-146: the data-protection step rides beside secrets when the operator
    # enabled it. It is NOT substantive (a skip never degrades the run) and it
    # never gates secrets — it only adds the opt-in PII/IP filter to the same
    # Tier-1 surface the guard uses.
    if _data_protection_enabled(config):
        steps.append(
            {
                "id": "data_protection",
                "type": "script",
                "run": _data_protection_step_run(workdir, config, scan_root),
                "on_fail": "continue",
            }
        )

    language = lang_detect.detect_language(workdir)
    commands = lang_detect.lint_test_commands(language)
    if commands is None:
        # LOUD degradation (workstream c): say exactly what did not run and
        # why. Callers surface this in the CLI and in verdict.json.
        reason = (
            f"no language detected in {workdir}"
            if language is None
            else f"no lint/test commands declared for language '{language}'"
        )
        return steps, {
            "coverage": "+".join(s["id"] for s in steps) if len(steps) > 1 else "secrets-only",
            "degraded": True,
            "skipped_steps": ["lint", "tests"],
            "reason": reason,
        }

    lint_cmd, test_cmd = commands
    # Honor guards.lint: false — the guard mode already skips lint when
    # disabled; the judge tier1 pipeline must match, otherwise repos with
    # no lint setup (e.g. no eslint dep / no eslint.config) get an
    # env-dependent lint FP from `npx eslint .` (npx fetches eslint from
    # cache/registry, so PATH hygiene cannot suppress it). Ring-runner
    # RR-GAP-040 / off-by-one answer 1286.
    if guards_cfg.get("lint", True):
        steps.append({"id": "lint", "type": "script", "run": _lint_step_run(lint_cmd, config)})

    # Same command the guard would run, resolved by the guard's own helper so
    # a missing runner prefix (`uv run` on a pip-only machine) degrades the
    # same way in both engines.
    resolved_test_cmd, resolution_warning = _resolve_test_command(configured_test_cmd or test_cmd)
    # DF-GITREINS-POC-61: Tier 1 grades the same TREE the guard grades, but it
    # runs the FULL test command — `guards.test_mode: diff` is a guard-only
    # narrowing, so every judge run executed the whole suite including a
    # skipif-guarded LIVE egress test (OpenRouter + hilo). That is a category
    # error: a non-deterministic host-global network smoke cannot decide a
    # deterministic gate. Under concurrent judges the losers came back
    # rate-limited and the tier-1 FAIL short-circuited tier-2 — one flake cost a
    # whole judge cycle. The fix is a per-step env stamp; tests/conftest.py
    # skips `live`-marked tests when it sees TIER1_ENV_VAR=1. Manual `pytest`
    # runs and `gitreins guard` do not set it, so the live test still runs there
    # (flock-serialized against a concurrent run).
    test_step: dict[str, Any] = {
        "id": "tests",
        "type": "script",
        # DF-GITREINS-POC-55: the resolved command is wrapped WHOLE (one
        # `sh -c`) so a chain like `check_docs_drift.py && pytest` is covered
        # end to end — prefixing only its first word would leave the actual
        # test runner at default priority. Fail-open, and byte-identical to
        # the pre-change command at level 0.
        "run": scanner_nice.shell_wrap(resolved_test_cmd, nice_level),
        # The same command WITHOUT the nice wrapper. Classifiers that PARSE the
        # command text must read this one: the POC-51 runner-missing check looks
        # for pytest invocations, and a `sh -c '<cmd>'` wrapper hides the whole
        # body inside one quoted token (_run_script_step).
        "analysis_cmd": resolved_test_cmd,
        "env": {TIER1_ENV_VAR: "1"},
    }
    if resolution_warning:
        test_step["resolution_warning"] = resolution_warning
    if test_timeout > 0:
        test_step["timeout"] = test_timeout
    steps.append(test_step)

    return steps, {
        "coverage": "+".join(s["id"] for s in steps),
        "degraded": False,
        "skipped_steps": [],
        "reason": "",
    }


def _default_tier1_steps(
    workdir: str, config: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Return language-appropriate default Tier 1 pipeline steps.

    Thin wrapper over :func:`tier1_plan` (kept for the existing callers and
    tests); see that function for the parity contract.
    """
    steps, _marker = tier1_plan(workdir, config)
    return steps


def _engine_root() -> str:
    """Absolute path of the directory containing the `engine` package.

    Works in both source checkouts (…/gitreins/engine/pipeline.py) and
    installed layouts (…/site-packages/engine/pipeline.py) — the parent of
    the engine dir is the import root that must go on PYTHONPATH for the
    default-pipeline built-in scanner subprocess.
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_pipeline_config(workdir: str = ".", scan_root: str | None = None) -> dict[str, Any]:
    """Load pipeline configuration from the control root, scanning source at scan_root."""
    scan_root = os.path.abspath(scan_root or workdir)
    config_path = os.path.join(workdir, ".gitreins", "config.yaml")
    if not os.path.exists(config_path):
        # No config file at all — the default pipeline (marker included, so a
        # degraded tier1 stays honest on a repo that was never `init`-ed).
        _missing_cfg_steps, missing_cfg_marker = tier1_plan(workdir, None, scan_root)
        return {
            "pipeline": {
                "stages": [
                    {
                        "id": "tier1",
                        "parallel": True,
                        "on": ["pre-commit", "pre-eval"],
                        "steps": _missing_cfg_steps,
                        "coverage": missing_cfg_marker["coverage"],
                        "degraded": missing_cfg_marker["degraded"],
                        "skipped_steps": missing_cfg_marker["skipped_steps"],
                        "degradation_reason": missing_cfg_marker["reason"],
                    },
                    {
                        "id": "tier2",
                        "type": "ai_eval",
                        "on": ["pre-eval"],
                        "condition": "true",
                        "max_iterations": -1,
                        "tools": [
                            "read_file",
                            "run_command",
                            "search_pattern",
                            "read_diff",
                            "sandbox",
                        ],
                    },
                    # GR-235: the fix-verification stage rides the default
                    # pipeline after tier2 — it verifies, deterministically
                    # (no LLM call), that the review stage's first-round
                    # findings were actually handled by the judged diff, and
                    # caps the verdict at needs_review when a critical/high/
                    # medium finding survives. A repo whose .gitreins/
                    # config.yaml declares pipeline.stages keeps working
                    # unchanged; this stage is opt-in there by declaring it
                    # (docs/fix-verification.md).
                    {
                        "id": FIX_VERIFICATION_STAGE_ID,
                        "type": FIX_VERIFICATION_STAGE_ID,
                        "on": ["pre-eval"],
                        "condition": "true",
                    },
                ]
            }
        }

    try:
        with open(config_path) as f:
            config: dict[str, Any] = yaml.safe_load(f) or {}
        # Fix YAML 1.1 boolean-key pitfall: unquoted ``on:`` / ``off:``
        # are parsed as ``True:`` / ``False:`` and break key lookups.
        fixed_config = _fix_on_key(config)
        assert isinstance(fixed_config, dict)
        config = fixed_config
        if "pipeline" not in config:
            tier1_steps, tier1_marker = tier1_plan(workdir, config, scan_root)
            config["pipeline"] = {
                "stages": [
                    {
                        "id": "tier1",
                        "parallel": True,
                        "on": ["pre-commit", "pre-eval"],
                        "steps": tier1_steps,
                        # DF-GITREINS-POC-16: what this tier1 actually graded
                        # ("secrets+lint+tests", "secrets-only", …) so a run
                        # narrower than the guard gate is machine-readable.
                        "coverage": tier1_marker["coverage"],
                        "degraded": tier1_marker["degraded"],
                        "skipped_steps": tier1_marker["skipped_steps"],
                        "degradation_reason": tier1_marker["reason"],
                    },
                    {
                        "id": "tier2",
                        "type": "ai_eval",
                        "on": ["pre-eval"],
                        "condition": "true",
                        "max_iterations": -1,
                        "tools": [
                            "read_file",
                            "run_command",
                            "search_pattern",
                            "read_diff",
                            "sandbox",
                        ],
                    },
                    # GR-235: the fix-verification stage rides the default
                    # pipeline after tier2 — it verifies, deterministically
                    # (no LLM call), that the review stage's first-round
                    # findings were actually handled by the judged diff, and
                    # caps the verdict at needs_review when a critical/high/
                    # medium finding survives. A repo whose .gitreins/
                    # config.yaml declares pipeline.stages keeps working
                    # unchanged; this stage is opt-in there by declaring it
                    # (docs/fix-verification.md).
                    {
                        "id": FIX_VERIFICATION_STAGE_ID,
                        "type": FIX_VERIFICATION_STAGE_ID,
                        "on": ["pre-eval"],
                        "condition": "true",
                    },
                ]
            }
        return config
    except Exception as e:
        logger.warning("Failed to load pipeline config: %s", e)
        return {"pipeline": {"stages": []}}
