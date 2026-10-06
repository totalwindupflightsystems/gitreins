"""Type definitions for GitReins guard results."""

import re

from dataclasses import dataclass, field

# pytest short test summary info lines look like
# "FAILED tests/test_x.py::test_y - AssertionError: boom". Failure counting is
# anchored to this shape — a bare "FAIL" substring elsewhere in the output
# (build logs, error prose) is not a pytest failure and must not inflate the
# count (DF-021).
_FAILED_TEST_LINE = re.compile(r"^FAILED \S+::")

# TRUST-003 (dogfood POC-14): the same anchored shapes, capturing the test id
# so a bounded console line and the persisted run log can NAME the failure
# instead of leaving the reader to re-run pytest. FAILED is preferred over
# ERROR because ERROR lines describe setup/teardown, not the failing assertion;
# an ERROR is still a usable id when there is no FAILED line (collection error
# with `-x`). Ordering is file order — "the first failing test".
_FAILED_TEST_ID = re.compile(r"^FAILED\s+(\S+?::\S+)")
_ERROR_TEST_ID = re.compile(r"^ERROR\s+(\S+?::\S+)")

# Fallback id source when the short-summary block never reached the captured
# output: pytest's in-traceback header
# "________ TestY.test_z ________" followed by the failing location
# "tests/test_x.py:12: AssertionError". Best-effort — the composed id uses the
# header text verbatim, so a class method reads "tests/test_x.py::TestY.test_z".
_TRACEBACK_HEADER = re.compile(r"^_{3,}\s*(\S.*?)\s*_{3,}$")
_LOCATION_LINE = re.compile(r"^([\w./\-]+\.py):(\d+):")

# The built-in scanner reports one finding per line as
# "<path>:<line>: [<label>] <sanitized value>" — the locator half is what a
# console line or log reader needs (DF-004), and it is a different report
# shape from gitleaks' "File:"/"Line:" block fields.
_BUILTIN_FINDING_LINE = re.compile(r"^(\S+:\d+): \[[^\]]+\]")

# TRUST-001: which guards are SUBSTANTIVE gates. A run where one of these did
# no work (nothing staged, no linter on PATH, no tests collected) is a
# DEGRADED pass, not a pass — CI and merge-back both consume an exit code as
# truth, so a gate that never ran must not be indistinguishable from one that
# passed. Config-disabled guards are NOT degradations (they never run at all),
# and a language-appropriate replacement (Go's vet/test/build for a Go repo)
# is not one either.
#
# static_analysis joined this set with REVIEW-GITREINS-018: its lane
# reported a green ✓ for a repo whose language selected no tool (a tree of
# Python files with no packaging marker), which is exactly the "gate that never
# ran reading as a pass" this set exists to catch. Its Go branch is unaffected —
# that path returns no skip flag, so the replacement stays non-substantive.
_SUBSTANTIVE_STEPS = frozenset({"lint", "tests", "lsp", "static_analysis"})

# DF-GITREINS-POC-44 / POC-42: a Go repo runs the Go-native lanes INSTEAD of
# the Python steps above (GuardManager.run_all) — the same substantive gates
# under different names. Keyed on the Python names alone, the degradation net
# never armed on a Go repo: a run where all three Go lanes graded no file
# printed a green PASS and exited 0 even with allow_skips: false. The alias set
# is the minimal honest fix — it extends the id mapping without restructuring
# Tier1Result, and it keeps `~ go_build — skipped (...)` naming the real lane.
_SUBSTANTIVE_STEP_ALIASES = frozenset({"go_lint", "go_tests", "go_build"})


def _is_substantive_step(step_id: str) -> bool:
    """True when *step_id* is a substantive gate under either lane naming."""
    return step_id in _SUBSTANTIVE_STEPS or step_id in _SUBSTANTIVE_STEP_ALIASES


def _step_id(name: str) -> str:
    """Base guard id for a result name ('tests (diff: 3 files)' → 'tests')."""
    return name.split(" ", 1)[0].strip()


@dataclass(frozen=True)
class GuardResult:
    name: str
    passed: bool
    output: str = ""
    error: str = ""
    # Non-fatal note for guard output (e.g. GR-GAP-037 runner fallback:
    # configured test_command's runner binary missing → python -m pytest).
    warning: str = ""
    # Exit code of the underlying tool, when the guard ran one (lint, tests,
    # gitleaks). None for guards that are not a single subprocess (LSP,
    # static analysis, skylos) — the persisted run log records "n/a" for
    # those. Recorded per guard so a post-mortem can tell a test failure
    # from a runner error (DF-018).
    exit_code: int | None = None
    # TRUST-001: this guard did no work. `skipped` keeps the result passing
    # (the tool is not at fault) while making the zero-work run visible:
    # `skip_reason` is the short user-facing reason ("no staged files") the
    # summary and the DEGRADED PASS line print.
    skipped: bool = False
    skip_reason: str = ""

    # TRUST-003: which secrets scanners ran and what each found, as
    # ``(scanner_id, status)`` pairs — status is ``clean``, ``not on PATH``
    # (not installed), ``not run (working-tree scope)`` (skipped for the scope,
    # REVIEW-GITREINS-026) or ``N finding(s)``. The console line and the run log
    # name the scanners instead of printing an unattributed "clean"/"fail"
    # (POC-15 cost a diagnosis cycle to that ambiguity). Empty for guards that
    # have no scanner concept, and the summary then falls back to the old
    # wording.
    scanners: tuple[tuple[str, str], ...] = ()

    # DF-GITREINS-POC-55: the nice(1) prefix this lane's external scanner was
    # spawned with — ``scanners: nice=nice -n 10`` — or why it could not be
    # applied (``scanners: nice unavailable (not on PATH); running at default
    # priority``). Empty when the knob is off (level 0) and for lanes that
    # spawned nothing, so the console never claims a priority that was not set.
    nice_note: str = ""

    def _pass_detail(self) -> str:
        """Short detail string for passing guards (e.g. 'clean', '3 files')."""
        if self.name == "secrets":
            if self.scanners:
                return f" — {render_secrets_scanners(self.scanners)}"
            return " — clean"
        elif self.name in ("lint", "go_lint", "go_build", "go_vet"):
            return " — ok"
        elif self.name == "quality":
            return f" — {self.output}" if self.output else " — unavailable"
        elif self.name in ("tests", "go_tests"):
            if "passed" in self.output.lower() or "ok" in self.output.lower():
                return " — passed"
            return ""
        return ""


@dataclass(frozen=True)
class DataProtectionResult:
    """Outcome of the Tier-1 data-protection lane (GR-146).

    A sibling of :class:`GuardResult` rather than a mutation of it, because it
    carries structured per-finding evidence the run log and the judge verdict
    consume (``findings``, per-state counts) and because it must be constructible
    with no policy in play (the skipped/disabled case).
    """

    name: str = "data_protection"
    passed: bool = True
    output: str = ""
    error: str = ""
    warning: str = ""
    skipped: bool = False
    skip_reason: str = ""
    #: JSON-safe finding dicts (values redacted unless the policy preserves).
    findings: tuple[dict, ...] = ()
    blocked_count: int = 0
    warned_count: int = 0
    suppressed_count: int = 0
    scanned_files: int = 0


def strip_ansi(text: str) -> str:
    """Remove terminal escape sequences from captured tool output.

    DF-GITREINS-POC-14: the tier-1 secrets step captures gitleaks' logrus
    output, which colourises the ``6:51PM INF scanned ~5 MB`` lines. Those
    escapes were recorded verbatim into ``verdict.json`` and, when a step's
    first output line happened to be one, printed raw on the console. Strip
    them at the presentation/record boundary — the text is unchanged, only
    the colour codes go.

    Handles CSI sequences (``\\x1b[32m``) and OSC sequences
    (``\\x1b]8;;url\\x07``), the two families terminal tooling actually emits.
    """
    return _ANSI_CSI.sub("", _ANSI_OSC.sub("", text or ""))


_ANSI_CSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_ANSI_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def _first_nonblank_line(text: str, limit: int = 100) -> str:
    """ANSI-stripped first non-blank line of *text*, truncated to *limit*.

    DF-GITREINS-POC-14: a stage summary is ONE line per step, but it was
    rendered from ``output[:100]`` — a raw slice that carried embedded
    newlines (so one step printed as several lines) and terminal escapes.
    Returns "" when the text has no non-blank line, letting the caller name
    that case instead of printing a dangling ``✓ lint: ``.
    """
    for line in strip_ansi(text).split("\n"):
        line = line.strip()
        if line:
            return line[:limit]
    return ""


def _truncate_line(line: str, limit: int = 100) -> str:
    """Truncate a single-line detail to *limit* chars with a trailing ellipsis."""
    if len(line) > limit:
        return line[: limit - 3] + "..."
    return line


def _first_traceback_id(output: str) -> str | None:
    """Compose an id from a pytest traceback header + its location line."""
    lines = output.split("\n")
    for i, line in enumerate(lines):
        header = _TRACEBACK_HEADER.match(line.strip())
        if not header:
            continue
        for follow in lines[i + 1 : i + 4]:
            location = _LOCATION_LINE.match(follow.strip())
            if location:
                return f"{location.group(1)}::{header.group(1)}"
    return None


def parse_first_failing_test(output: str) -> str | None:
    """First failing test id in *output* ('tests/test_x.py::test_y'), or None.

    TRUST-003 (dogfood POC-14 / AC1): the guard console summary is bounded to
    the output tail, so a multi-failure run showed a banner or the last
    traceback frame and the reader had to re-run pytest to learn WHICH test
    broke. Sources, in order of trust:

    1. the first pytest short-summary ``FAILED <id> - ...`` line,
    2. the first ``ERROR <id> - ...`` line (set-up/collection error),
    3. the in-traceback header plus the location line that follows it.

    Returns None when nothing recognizable is present — callers keep their
    previous tail-only behavior rather than inventing an id.
    """
    lines = output.split("\n")
    for pattern in (_FAILED_TEST_ID, _ERROR_TEST_ID):
        for line in lines:
            match = pattern.match(line.strip())
            if match:
                return match.group(1)
    return _first_traceback_id(output)


def first_failing_test_detail(output: str, fail_count: int = 0) -> str:
    """``FAIL (<id> [first failing id]; N failure(s))``, or "" when unparsed.

    AC1's console-string contract in one place so the guard summary, the judge
    stage summary and the run log all name the same test.
    """
    test_id = parse_first_failing_test(output)
    if not test_id:
        return ""
    clauses = [f"{test_id} [first failing id]"]
    if fail_count:
        clauses.append(f"{fail_count} failure(s)")
    return "FAIL (" + "; ".join(clauses) + ")"


# ── pytest outcome classification (INT-FLAKE-2) ──────────────────
# A pytest exit code does not say WHY a run ended, and the missing half is the
# dangerous half: `-x` (maxfail) combined with pytest-xdist makes the master
# raise xdist's own ``Interrupted(KeyboardInterrupt)`` as soon as maxfail is
# reached, and pytest maps KeyboardInterrupt onto ``ExitCode.INTERRUPTED`` (2).
# A suite with a REAL failing test therefore exits 2 — the same code an
# externally interrupted run exits — while a failure with maxfail reached but
# no xdist exits 1. Every reader that trusted the number filed the failure as a
# harness flake: the tier1 tests step recorded ``exit_code: 2`` and the
# maxfail/FAILED evidence sat past the head-only capture slice, so INT-FLAKE-2
# was filed as an "environment interruption" for six verdicts before the
# mapping was reproduced live (2026-09-16: ``pytest -x --tb=short -n 2`` over a
# 3-test suite with one bad assertion → exit 2, output ending in
# ``xdist.dsession.Interrupted: stopping after 1 failures``).
#
# These markers are what tells the two apart from captured output.
_MAXFAIL_MARKER = re.compile(r"xdist\.dsession\.Interrupted:\s*stopping after (\d+) failure")
# `-x` without xdist prints the same banner (and exits 1). Anchored per line —
# the banner is one line in the middle of the captured output.
_MAXFAIL_BANNER = re.compile(r"^!+\s*stopping after (\d+) failures?\s*!+$", re.MULTILINE)
_KEYBOARD_INTERRUPT_MARKER = re.compile(r"^!*\s*KeyboardInterrupt\s*!*$", re.MULTILINE)
# pytest's terminal summary: "===== 1 failed, 2 passed in 1.17s =====".
_PYTEST_FAILED_COUNT = re.compile(r"(\d+) failed")
_PYTEST_NO_TESTS = re.compile(r"no tests ran", re.IGNORECASE)
_PYTEST_USAGE_ERROR = re.compile(r"^ERROR: ", re.MULTILINE)

# ``kind`` values returned by pytest_outcome(). Kept as a tuple so tests and
# downstream readers can enumerate them instead of hardcoding strings.
PYTEST_OUTCOME_KINDS = (
    "passed",
    "failed",
    "maxfail",
    "interrupted",
    "interrupted-unclassified",
    "internal-error",
    "usage-error",
    "no-tests-collected",
    "unknown",
)


def _pytest_reported_failures(output: str) -> int | None:
    """N from pytest's terminal ``N failed`` clause, or None when absent.

    Scanned from the END because the terminal summary is the last thing pytest
    prints; a test that merely echoes "3 failed" earlier in the log must not
    win over the real summary line.
    """
    for line in reversed(output.split("\n")):
        match = _PYTEST_FAILED_COUNT.search(line)
        if match:
            return int(match.group(1))
    return None


def pytest_outcome(exit_code: int | None, output: str) -> dict:
    """Classify why a pytest run ended, for a verdict record.

    INT-FLAKE-2: ``StepResult.data['exit_code']`` alone made a real failing
    suite indistinguishable from a harness interruption, because ``-x`` plus
    xdist exits 2 on the first failure. This is the honest mapping, derived
    from the captured output rather than from the number:

    * ``passed`` — exit 0.
    * ``failed`` — exit 1: tests failed (no maxfail reach).
    * ``maxfail`` — exit 2 whose output carries the xdist maxfail marker, the
      non-xdist maxfail banner, or the FAILED/ERROR id that triggered it: a
      REAL failure, not an interruption.
    * ``interrupted`` — exit 2 with a KeyboardInterrupt banner and no failing
      test: the run was signalled from outside.
    * ``interrupted-unclassified`` — exit 2 with neither piece of evidence
      (typically a truncated capture): reported as unknown, never as a code
      defect.
    * ``internal-error`` / ``usage-error`` / ``no-tests-collected`` — pytest
      exit 3 / 4 / 5.

    Returns a JSON-safe dict with ``kind``, ``detail`` (one human line),
    ``first_failing_test``, ``failures`` and ``interrupted``. ``failures`` is
    pytest's own reported count when the terminal summary is present, else the
    number of FAILED lines seen (a lower bound on truncated output).
    """
    first = parse_first_failing_test(output)
    failed_lines = sum(1 for line in output.split("\n") if _FAILED_TEST_ID.match(line.strip()))
    reported = _pytest_reported_failures(output)
    failures = reported if reported is not None else failed_lines

    kind = "unknown"
    detail = ""
    interrupted = False

    if exit_code == 0:
        kind = "passed"
        detail = "pytest passed"
    elif exit_code == 1:
        kind = "failed"
        detail = f"{failures} failure(s)" if failures else "pytest reported failures"
        if first:
            detail += f"; first: {first}"
    elif exit_code == 2:
        marker = _MAXFAIL_MARKER.search(output)
        banner = _MAXFAIL_BANNER.search(output.strip())
        if marker or banner or first:
            kind = "maxfail"
            stop_at = int(marker.group(1)) if marker else None
            if stop_at is None:
                stop_at = int(banner.group(1)) if banner else (failures or 1)
            detail = (
                f"real test failure(s): maxfail stopped the run after {stop_at} "
                "failure(s) — pytest exit 2 here is xdist's Interrupted, not an "
                "interruption of the run"
            )
            if first:
                detail += f"; first: {first}"
        elif _KEYBOARD_INTERRUPT_MARKER.search(output):
            kind = "interrupted"
            interrupted = True
            detail = (
                "run interrupted by a signal (KeyboardInterrupt) — no failing "
                "test in the captured output"
            )
        else:
            kind = "interrupted-unclassified"
            interrupted = True
            detail = (
                "pytest exited 2 (INTERRUPTED) with neither a FAILED line nor a "
                "KeyboardInterrupt banner in the captured output — cause not "
                "determinable from this evidence"
            )
    elif exit_code == 3 and "INTERNALERROR" in output.upper():
        kind = "internal-error"
        detail = "pytest crashed with an internal error"
    elif exit_code == 4 or (exit_code is not None and _PYTEST_USAGE_ERROR.search(output)):
        kind = "usage-error"
        detail = "pytest rejected the invocation (usage error)"
    elif exit_code == 5 or _PYTEST_NO_TESTS.search(output):
        kind = "no-tests-collected"
        detail = "pytest collected no tests"
    elif exit_code is not None and exit_code < 0:
        kind = "unknown"
        detail = f"pytest was killed by signal {-exit_code} before it could summarise"
    else:
        kind = "unknown"
        detail = f"pytest exited with unexpected code {exit_code}"

    return {
        "kind": kind,
        "detail": detail,
        "first_failing_test": first,
        "failures": failures,
        "interrupted": interrupted,
    }


# ── Secrets scanner attribution (TRUST-003 / dogfood POC-15) ──────
# The secrets guard runs TWO scanners — gitleaks (when installed) and the
# built-in regex cross-check — and fails when EITHER finds something, but the
# console printed a bare "clean"/"fail". A FAIL that came only from the
# low-entropy built-in cross-check (the case gitleaks' entropy filter skips)
# was therefore indistinguishable from one gitleaks raised, and diagnosing
# POC-15 cost a cycle to that ambiguity. Both facts are now named on the
# console line AND recorded in the run log.
SCANNER_CLEAN = "clean"
SCANNER_NOT_RUN = "not on PATH"
# REVIEW-GITREINS-026: the working-tree scope deliberately SKIPS gitleaks —
# `protect --staged` grades the index only, so it cannot see the
# unstaged/untracked files that scope exists to grade. That skip used to be
# tagged SCANNER_NOT_RUN, the SAME string as the genuine missing-binary case,
# so an operator on a box where gitleaks IS installed read "gitleaks not on
# PATH" and went hunting a missing install. Scope-skips get their own status.
SCANNER_NOT_RUN_SCOPE = "not run (working-tree scope)"
# Every per-scanner status that means "produced no verdict": not installed, or
# skipped for the selected scope. The renderer treats them alike — never a
# failure, always named on the console line.
SCANNER_NOT_RUN_STATUSES = (SCANNER_NOT_RUN, SCANNER_NOT_RUN_SCOPE)
# DF-GITREINS-POC-54: a scanner that died BEFORE scanning. gitleaks exits 2 with
# a Go panic when an ``[allowlist] paths`` entry is not a compilable regexp —
# the pre-DF-001 globs ('*.log', '*.egg-info/') that older `gitreins init`
# wrote. Reporting that as "reported findings" sent operators hunting a
# nonexistent secret (or bypassing the gate) while NO scan had happened at all.
# The status carries the FIRST bad pattern so the console line points straight
# at the entry to fix; the leg still FAILS (fail-closed).
SCANNER_CONFIG_ERROR = "CONFIG ERROR"
_SCANNER_LABELS = {"gitleaks": "gitleaks", "builtin": "builtin cross-check"}


def scanner_label(scanner_id: str) -> str:
    """Display name for a scanner id ('builtin' → 'builtin cross-check')."""
    return _SCANNER_LABELS.get(scanner_id, scanner_id)


def scanner_config_error_status(pattern: str, scanner: str = "gitleaks") -> str:
    """Per-scanner status for a scanner whose CONFIG failed to load (POC-54).

    ``CONFIG ERROR (gitleaks config failed to compile: *.log)`` — the pattern is
    the first entry gitleaks' Go regexp compiler rejected.
    """
    return f"{SCANNER_CONFIG_ERROR} ({scanner} config failed to compile: {pattern})"


def scanner_finding_status(findings: int) -> str:
    """Per-scanner outcome vocabulary: '1 finding' / '2 findings'."""
    return "1 finding" if findings == 1 else f"{findings} findings"


def render_secrets_scanners(scanners: tuple[tuple[str, str], ...]) -> str:
    """Render scanner attribution for a console line or the run log.

    All clean: ``clean (gitleaks + builtin cross-check)``.
    Anything found: ``FAIL (builtin cross-check: 2 findings; gitleaks: clean)``
    — the offending scanner(s) first, with counts, then the rest. A scanner
    that produced no verdict is named rather than silently implied, with its
    own reason: ``gitleaks not on PATH`` (not installed) or
    ``gitleaks not run (working-tree scope)`` (skipped for the scope,
    REVIEW-GITREINS-026).

    DF-GITREINS-POC-54: a scanner whose CONFIG failed to compile did no scanning
    at all, so it must never render as ``FAIL (... findings)``. Its status is
    the whole clause — ``CONFIG ERROR (gitleaks config failed to compile:
    *.log)`` — with only a non-clean sibling scanner appended (a clean
    cross-check adds nothing to the diagnosis).
    """
    labelled = [(scanner_label(sid), status) for sid, status in scanners]
    config_errors = [
        status for _name, status in labelled if status.startswith(SCANNER_CONFIG_ERROR)
    ]
    if config_errors:
        others = [
            f"{name}: {status}"
            for name, status in labelled
            if not status.startswith(SCANNER_CONFIG_ERROR)
            and status not in (SCANNER_CLEAN, *SCANNER_NOT_RUN_STATUSES)
        ]
        head = "; ".join(config_errors)
        return head if not others else f"{head}; " + "; ".join(others)
    failing = [
        pair for pair in labelled if pair[1] not in (SCANNER_CLEAN, *SCANNER_NOT_RUN_STATUSES)
    ]
    if failing:
        ordered = failing + [pair for pair in labelled if pair not in failing]
        return "FAIL (" + "; ".join(f"{name}: {status}" for name, status in ordered) + ")"
    ran_clean = [name for name, status in labelled if status == SCANNER_CLEAN]
    absent = [(name, status) for name, status in labelled if status in SCANNER_NOT_RUN_STATUSES]
    detail = " + ".join(ran_clean) if ran_clean else "no scanner ran"
    if absent:
        return f"clean ({detail}; {', '.join(f'{name} {status}' for name, status in absent)})"
    return f"clean ({detail})"


def parse_gitleaks_finding_count(output: str) -> int | None:
    """Number of findings in gitleaks output, or None when not determinable.

    TRUST-003: the console line reports per-scanner outcomes, so the count has
    to survive gitleaks' several report shapes without overcounting the
    built-in findings merged into the same buffer:

    1. the ``leaks found: N`` trailer (gitleaks' own tally — authoritative),
    2. ``Finding:`` blocks (verbose/protect output, one per leak),
    3. ``File:`` fields, which appear once per finding in the same block.

    Each source is used only when it is actually present; a scan whose only
    output is banner/prose returns None, and the caller then says the count is
    unavailable instead of claiming zero findings.
    """
    trailer = re.search(r"leaks found:\s*(\d+)", output)
    if trailer:
        return int(trailer.group(1))
    blocks = len(re.findall(r"^\s*Finding:", output, re.MULTILINE))
    if blocks:
        return blocks
    files = len(re.findall(r"^\s*File:", output, re.MULTILINE))
    if files:
        return files
    return None


def _secrets_findings_detail(output: str, limit: int = 100) -> str:
    """Extract gitleaks File:/Line: fields into a compact findings detail.

    Returns "" when the output has no File: fields (the built-in scanner's
    output already embeds ``path:line`` per finding line). Pairs are kept
    whole so a path is never cut mid-value; overflow pairs are dropped with
    a trailing ellipsis.
    """
    files = []
    lines = []
    for ln in output.split("\n"):
        stripped = ln.strip()
        if stripped.startswith("File:"):
            value = stripped.removeprefix("File:").strip()
            if value:
                files.append(value)
        elif stripped.startswith("Line:"):
            value = stripped.removeprefix("Line:").strip()
            if value:
                lines.append(value)
    if not files:
        return ""
    pairs = [f"{f}:{ln}" if ln else f for f, ln in zip(files, lines)]
    return _locations_detail(pairs, limit)


def _locations_detail(locations: list[str], limit: int = 100) -> str:
    """``N finding(s): a:1, b:2`` — items kept whole, overflow dropped.

    Shared by both scanner report shapes so a locator is never cut mid-path.
    """
    if not locations:
        return ""
    label = f"{len(locations)} finding(s): "
    detail = label + ", ".join(locations)
    if len(detail) <= limit:
        return detail
    detail = label
    for location in locations:
        sep = ", " if detail != label else ""
        if len(detail) + len(sep) + len(location) > limit:
            return detail + "…"
        detail += sep + location
    return detail


def _builtin_findings_detail(output: str, limit: int = 100) -> str:
    """Locators from the built-in scanner's own ``path:line: [label]`` lines.

    TRUST-003: the scanner-attribution clause names the scanner and its count
    but not WHERE — this keeps DF-004's file:line visibility for the built-in
    scanner's report shape (gitleaks' shape goes through
    ``_secrets_findings_detail``).
    """
    locators = []
    for ln in output.split("\n"):
        stripped = ln.strip()
        if stripped.startswith("Potential secrets found:"):
            continue
        match = _BUILTIN_FINDING_LINE.match(stripped)
        if match:
            locators.append(match.group(1))
    return _locations_detail(locators, limit)


def _nice_suffix(result: GuardResult) -> str:
    """``"; scanners: nice=nice -n 10"`` — or ``""`` when nothing was applied.

    DF-GITREINS-POC-55: scanner priority is part of the step's evidence, the
    same way TRUST-003 made per-scanner outcomes part of it.
    """
    return f"; {result.nice_note}" if result.nice_note else ""


@dataclass(frozen=True)
class Tier1Result:
    passed: bool
    results: list[GuardResult] = field(default_factory=list)
    extra: dict[str, object] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def skipped_steps(self) -> list[dict[str, str]]:
        """Every step that did no work, as ``{"step": id, "reason": reason}``.

        TRUST-001: the machine-readable half of the degradation marker. The
        CLI raises it to a DEGRADED PASS line and a non-zero exit code (unless
        ``guards.allow_skips`` is true); the judge persists it in
        ``verdict.json`` so a merge-back can refuse a pass whose gates never
        ran.
        """
        return [
            {"step": _step_id(r.name), "reason": r.skip_reason or "reason not recorded"}
            for r in self.results
            if r.skipped
        ]

    @property
    def degraded_steps(self) -> list[dict[str, str]]:
        """Skipped steps among the SUBSTANTIVE gates (lint/tests/lsp, Go twins)."""
        return [s for s in self.skipped_steps if _is_substantive_step(s["step"])]

    @property
    def degraded(self) -> bool:
        """True when a substantive gate did no work this run."""
        return bool(self.degraded_steps)

    @property
    def skip_summary(self) -> str:
        """'lint=no staged files, tests=no staged files' for the console line."""
        return ", ".join(f"{s['step']}={s['reason']}" for s in self.degraded_steps)

    @property
    def summary(self) -> str:
        lines = []
        for r in self.results:
            if r.skipped:
                # ~ marks a step that did no work — never a ✓.
                lines.append(f"  ~ {r.name} — skipped ({r.skip_reason or 'unknown reason'})")
                if r.warning:
                    lines.append(f"  ⚠ {r.warning}")
                continue
            status = "✓" if r.passed else "✗"
            detail = ""
            follow_up: list[str] = []
            if not r.passed and r.output:
                out_lines = [ln for ln in r.output.split("\n") if ln.strip()]
                failed_lines = [
                    ln.strip() for ln in out_lines if _FAILED_TEST_LINE.match(ln.strip())
                ]
                # Prefer the last pytest FAILED line over the final output line:
                # pytest's "=== N failed, M passed ===" banner would otherwise
                # hide the failing test ID (DF-021).
                tail_src = (
                    failed_lines[-1]
                    if failed_lines
                    else (out_lines[-1].strip() if out_lines else "")
                )
                tail = _truncate_line(tail_src) if tail_src else ""
                fail_count = len(failed_lines)
                if r.name == "secrets" and r.scanners:
                    # TRUST-003 (AC2): name the scanner(s) that ran and each
                    # one's outcome — 'clean'/'fail' alone hid whether the
                    # built-in cross-check or gitleaks raised the finding.
                    detail = f" — {render_secrets_scanners(r.scanners)}"
                    findings = _secrets_findings_detail(r.output) or _builtin_findings_detail(
                        r.output
                    )
                    if findings:
                        # DF-004 stays honored: the file:line locators the
                        # scanner attribution clause no longer carries. Works
                        # for BOTH report shapes (gitleaks fields, builtin
                        # `path:line: [label]` lines).
                        follow_up.append(f"      findings: {findings}")
                elif first_failing_detail := first_failing_test_detail(r.output, fail_count):
                    # TRUST-003 (AC1): name the FIRST failing test id parsed
                    # from the pytest output, not just the tail banner.
                    detail = f" — {first_failing_detail}"
                else:
                    if r.name == "secrets":
                        findings = _secrets_findings_detail(r.output)
                        if findings:
                            detail = f" — {findings}"
                    if not detail and tail:
                        detail = f" — {tail}"
                    if fail_count:
                        detail = f" — {fail_count} failure(s); {tail}"
            elif not r.passed and r.error:
                # DF-GITREINS-POC-46: a spawn failure (a binary that is not on
                # PATH) carries no tool output — the CAUSE *is* the error text,
                # and it used to reach the run log only, leaving a bare ✗ here.
                detail = f" — {_truncate_line(r.error)}"
            elif r.passed:
                detail = r._pass_detail()
            if not r.passed and r.error and _truncate_line(r.error) not in detail:
                # The error text is evidence even when the step also produced
                # output (a refused busy-wait, a runner that started and died):
                # never drop the cause from the step's own console line.
                detail += f" — {_truncate_line(r.error)}"
            # DF-GITREINS-POC-55: the applied scanner nice prefix (or its
            # fail-open reason) rides on the step's own line — one line per
            # step, the DF-GITREINS-POC-14 contract, so an operator sees the
            # priority without opening the run log.
            lines.append(f"  {status} {r.name}{detail}{_nice_suffix(r)}")
            lines.extend(follow_up)
            if r.warning:
                lines.append(f"  ⚠ {r.warning}")
        return "\n".join(lines)
