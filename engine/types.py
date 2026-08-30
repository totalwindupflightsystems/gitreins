"""Type definitions for GitReins guard results."""

import re

from dataclasses import dataclass, field

# pytest short test summary info lines look like
# "FAILED tests/test_x.py::test_y - AssertionError: boom". Failure counting is
# anchored to this shape — a bare "FAIL" substring elsewhere in the output
# (build logs, error prose) is not a pytest failure and must not inflate the
# count (DF-021).
_FAILED_TEST_LINE = re.compile(r"^FAILED \S+::")


@dataclass(frozen=True)
class GuardResult:
    name: str
    passed: bool
    output: str = ""
    error: str = ""
    # Non-fatal note for guard output (e.g. GR-GAP-037 runner fallback:
    # configured test_command's runner binary missing → python -m pytest).
    warning: str = ""

    def _pass_detail(self) -> str:
        """Short detail string for passing guards (e.g. 'clean', '3 files')."""
        if self.name == "secrets":
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
    label = f"{len(pairs)} finding(s): "
    detail = label + ", ".join(pairs)
    if len(detail) <= limit:
        return detail
    detail = label
    for pair in pairs:
        sep = ", " if detail != label else ""
        if len(detail) + len(sep) + len(pair) > limit:
            return detail + "…"
        detail += sep + pair
    return detail


@dataclass(frozen=True)
class Tier1Result:
    passed: bool
    results: list[GuardResult] = field(default_factory=list)
    extra: dict[str, object] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        lines = []
        for r in self.results:
            status = "✓" if r.passed else "✗"
            detail = ""
            if not r.passed and r.output:
                out_lines = [ln for ln in r.output.split("\n") if ln.strip()]
                failed_lines = [
                    ln.strip() for ln in out_lines if _FAILED_TEST_LINE.match(ln.strip())
                ]
                # Prefer the last pytest FAILED line over the final output line:
                # pytest's "=== N failed, M passed ===" banner would otherwise
                # hide the failing test ID (DF-021).
                tail_src = failed_lines[-1] if failed_lines else (
                    out_lines[-1].strip() if out_lines else ""
                )
                tail = _truncate_line(tail_src) if tail_src else ""
                if r.name == "secrets":
                    findings = _secrets_findings_detail(r.output)
                    if findings:
                        detail = f" — {findings}"
                if not detail and tail:
                    detail = f" — {tail}"
                fail_count = len(failed_lines)
                if fail_count:
                    detail = f" — {fail_count} failure(s); {tail}"
            elif r.passed:
                detail = r._pass_detail()
            lines.append(f"  {status} {r.name}{detail}")
            if r.warning:
                lines.append(f"  ⚠ {r.warning}")
        return "\n".join(lines)
