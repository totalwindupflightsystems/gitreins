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
_SUBSTANTIVE_STEPS = frozenset({"lint", "tests", "lsp"})


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
    # ``(scanner_id, status)`` pairs — status is ``clean``, ``not on PATH`` or
    # ``N finding(s)``. The console line and the run log name the scanners
    # instead of printing an unattributed "clean"/"fail" (POC-15 cost a
    # diagnosis cycle to that ambiguity). Empty for guards that have no
    # scanner concept, and the summary then falls back to the old wording.
    scanners: tuple[tuple[str, str], ...] = ()

    def _pass_detail(self) -> str:
        """Short detail string for passing guards (e.g. 'clean', '3 files')."""
        if self.name == "secrets":
            if self.scanners:
                return f" — {render_secrets_scanners(self.scanners)}"
            return " — clean"
        elif self.name in ("lint", "go_lint", "go_build", "go_vet"):
            return " — ok"
        elif self.name in ("tests", "go_tests"):
            if "passed" in self.output.lower() or "ok" in self.output.lower():
                return " — passed"
            return ""
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
_SCANNER_LABELS = {"gitleaks": "gitleaks", "builtin": "builtin cross-check"}


def scanner_label(scanner_id: str) -> str:
    """Display name for a scanner id ('builtin' → 'builtin cross-check')."""
    return _SCANNER_LABELS.get(scanner_id, scanner_id)


def scanner_finding_status(findings: int) -> str:
    """Per-scanner outcome vocabulary: '1 finding' / '2 findings'."""
    return "1 finding" if findings == 1 else f"{findings} findings"


def render_secrets_scanners(scanners: tuple[tuple[str, str], ...]) -> str:
    """Render scanner attribution for a console line or the run log.

    All clean: ``clean (gitleaks + builtin cross-check)``.
    Anything found: ``FAIL (builtin cross-check: 2 findings; gitleaks: clean)``
    — the offending scanner(s) first, with counts, then the rest. An absent
    gitleaks is named rather than silently implied.
    """
    labelled = [(scanner_label(sid), status) for sid, status in scanners]
    failing = [pair for pair in labelled if pair[1] not in (SCANNER_CLEAN, SCANNER_NOT_RUN)]
    if failing:
        ordered = failing + [pair for pair in labelled if pair not in failing]
        return "FAIL (" + "; ".join(f"{name}: {status}" for name, status in ordered) + ")"
    ran_clean = [name for name, status in labelled if status == SCANNER_CLEAN]
    absent = [name for name, status in labelled if status == SCANNER_NOT_RUN]
    detail = " + ".join(ran_clean) if ran_clean else "no scanner ran"
    if absent:
        return f"clean ({detail}; {', '.join(absent)} {SCANNER_NOT_RUN})"
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
        """Skipped steps among the SUBSTANTIVE gates (lint/tests/lsp)."""
        return [s for s in self.skipped_steps if s["step"] in _SUBSTANTIVE_STEPS]

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
            elif r.passed:
                detail = r._pass_detail()
            lines.append(f"  {status} {r.name}{detail}")
            lines.extend(follow_up)
            if r.warning:
                lines.append(f"  ⚠ {r.warning}")
        return "\n".join(lines)
