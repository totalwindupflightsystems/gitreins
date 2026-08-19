"""Type definitions for GitReins guard results."""

from dataclasses import dataclass, field


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
                tail = _truncate_line(out_lines[-1].strip()) if out_lines else ""
                if r.name == "secrets":
                    findings = _secrets_findings_detail(r.output)
                    if findings:
                        detail = f" — {findings}"
                if not detail and tail:
                    detail = f" — {tail}"
                fail_count = len([ln for ln in out_lines if "FAIL" in ln or "FAILED" in ln])
                if fail_count:
                    detail = f" — {fail_count} failure(s); {tail}"
            elif r.passed:
                detail = r._pass_detail()
            lines.append(f"  {status} {r.name}{detail}")
            if r.warning:
                lines.append(f"  ⚠ {r.warning}")
        return "\n".join(lines)
