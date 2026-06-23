"""Type definitions for GitReins guard results."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class GuardResult:
    name: str
    passed: bool
    output: str = ""
    error: str = ""

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


@dataclass(frozen=True)
class Tier1Result:
    passed: bool
    results: list[GuardResult] = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    @property
    def summary(self) -> str:
        lines = []
        for r in self.results:
            status = "✓" if r.passed else "✗"
            detail = ""
            if not r.passed and r.output:
                out_lines = [ln for ln in r.output.split("\n") if ln.strip()]
                if out_lines:
                    first = out_lines[0].strip()
                    if len(first) > 100:
                        first = first[:97] + "..."
                    detail = f" — {first}"
                fail_count = len([ln for ln in out_lines if "FAIL" in ln or "FAILED" in ln])
                if fail_count:
                    detail = f" — {fail_count} failure(s)"
            elif r.passed:
                detail = r._pass_detail()
            lines.append(f"  {status} {r.name}{detail}")
        return "\n".join(lines)
