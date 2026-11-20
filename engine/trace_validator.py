"""Mechanical validation for repository ``ch:trace`` markers.

The validator checks diff-added marker lines by default. Full-tree runs retain
structural checks on historical markers and apply the evidence grammar only to
markers blamed after the supplied cutoff. Commit-message footers and semantic
resolution of fields other than evidence are intentionally out of scope.

ch:trace row=GR-237 test=tests/test_guard_trace.py evidence=tests/test_guard_trace.py
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path

DOCTRINE_DATE = date(2026, 10, 11)
_MARKER = re.compile(r"ch:trace(?:[ \t]+[A-Za-z_][A-Za-z0-9_-]*=[^\s\"']+)+")
_FIELDS = re.compile(r"([A-Za-z_][A-Za-z0-9_-]*)=([^\s\"']+)")
_HISTORY_LOCATOR = re.compile(
    r"\.gitreins/history/\d{4}-\d{2}-\d{2}/[0-9a-fA-F]{8,16}/verdict\.json\Z"
)
_COMMIT_LOCATOR = re.compile(r"commit-[0-9a-fA-F]{7,}\Z")


@dataclass(frozen=True)
class TraceFinding:
    path: str
    line: int
    marker: str
    reason: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.reason}: {self.marker!r}"


def _surface_files(root: Path) -> list[Path]:
    """Return the §7 text surfaces plus this validator's source docstring."""
    paths: set[Path] = set()
    for rel in (".coding-hermes/board/tasks.jsonl", ".coding-hermes/board/events.jsonl"):
        path = root / rel
        if path.is_file():
            paths.add(path)
    for rel in ("docs", "specs"):
        directory = root / rel
        if directory.is_dir():
            paths.update(path for path in directory.rglob("*.md") if path.is_file())
    own_source = root / "engine/trace_validator.py"
    if own_source.is_file():
        paths.add(own_source)
    return sorted(paths)


def _is_doctrine_grammar_example(path: Path, line: str) -> bool:
    # The doctrine discusses the token and its grammar; those explanatory
    # lines are not claims and should not become self-referential violations.
    grammar_surfaces = ("docs/traceability-doctrine.md", "engine/trace_validator.py")
    return path.as_posix().endswith(grammar_surfaces) and (
        "grep -rIn" in line or "ch:trace row=<ID>" in line or "a marker" in line.lower()
    )


def scan_trace_markers(root: str | Path) -> list[tuple[str, int, str]]:
    """Discover prefixed markers on the documented text surfaces."""
    repo = Path(root).resolve()
    found: list[tuple[str, int, str]] = []
    for path in _surface_files(repo):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError):
            continue
        rel = path.relative_to(repo).as_posix()
        for number, line in enumerate(lines, 1):
            if _is_doctrine_grammar_example(path, line):
                continue
            found.extend((rel, number, match.group(0)) for match in _MARKER.finditer(line))
    return found


def _changed_lines(repo: Path, relative: str, scope: str) -> set[int]:
    """Return added/replaced line numbers for one file in the selected Git scope."""
    path = repo / relative
    if not path.exists():
        return set()
    tracked = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--error-unmatch", relative], capture_output=True
    )
    if not tracked.returncode:
        if scope == "working-tree":
            diff_args = ["diff", "HEAD", "--unified=0", "--", relative]
        else:
            diff_args = ["diff", "--cached", "--unified=0", "--", relative]
        result = subprocess.run(
            ["git", "-C", str(repo), *diff_args], capture_output=True, text=True
        )
        if result.returncode:
            return set()
        changed: set[int] = set()
        for line in result.stdout.splitlines():
            match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if match:
                start = int(match.group(1))
                count = int(match.group(2) or "1")
                changed.update(range(start, start + count))
        return changed
    return set(range(1, len(path.read_text(encoding="utf-8").splitlines()) + 1))


def _blame_date(repo: Path, relative: str, line: int) -> date | None:
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "blame",
            "--line-porcelain",
            "-L",
            f"{line},{line}",
            "--",
            relative,
        ],
        capture_output=True,
        text=True,
    )
    match = re.search(r"^author-time (\d+)$", result.stdout, re.MULTILINE)
    if result.returncode or not match:
        return None
    from datetime import datetime, timezone

    return datetime.fromtimestamp(int(match.group(1)), timezone.utc).date()


def _is_legacy_marker(repo: Path, relative: str, line: int, cutoff: date | None) -> bool:
    path = repo / relative
    if path.name == "tasks.jsonl":
        try:
            raw = path.read_text(encoding="utf-8").splitlines()[line - 1]
            record = json.loads(raw)
            created = date.fromisoformat(record.get("created_at", "")[:10])
            if created < DOCTRINE_DATE:
                return True
        except (OSError, IndexError, AttributeError, TypeError, ValueError, json.JSONDecodeError):
            pass
    if cutoff is None:
        return False
    introduced = _blame_date(repo, relative, line)
    return introduced is not None and introduced <= cutoff


def _valid_evidence(repo: Path, evidence: str) -> bool:
    if evidence.startswith("none:"):
        return len(evidence) > len("none:")
    if _COMMIT_LOCATOR.fullmatch(evidence) or evidence == ".gitreins/verdicts/verdict.json":
        return True
    history_match = _HISTORY_LOCATOR.fullmatch(evidence)
    if history_match:
        try:
            date.fromisoformat(evidence.split("/")[2])
        except ValueError:
            return False
        return True
    candidate = Path(evidence)
    if candidate.is_absolute():
        return False
    try:
        resolved = (repo / candidate).resolve()
        resolved.relative_to(repo)
    except (OSError, ValueError):
        return False
    return resolved.exists()


def validate_trace_markers(
    root: str | Path,
    *,
    full: bool = True,
    changed_files: list[str] | None = None,
    scope: str = "staged",
    cutoff: date | str | None = None,
) -> list[TraceFinding]:
    """Return marker violations, using diff scope unless ``full`` is requested.

    ``cutoff`` is the oldest introduction date eligible for full evidence
    grammar validation; older or undated markers receive structural checks.
    """
    repo = Path(root).resolve()
    cutoff_date = date.fromisoformat(cutoff[:10]) if isinstance(cutoff, str) else cutoff
    files = changed_files if changed_files is not None else []
    scope_lines: dict[str, set[int]] = {}
    if not full:
        for relative in files:
            scope_lines[relative] = _changed_lines(repo, relative, scope)
    findings: list[TraceFinding] = []
    for relative, line_number, marker in scan_trace_markers(repo):
        if not full and line_number not in scope_lines.get(relative, set()):
            continue
        fields = dict(_FIELDS.findall(marker.removeprefix("ch:trace")))
        if not fields.get("row"):
            findings.append(TraceFinding(relative, line_number, marker, "missing required row="))
        if not full or not _is_legacy_marker(repo, relative, line_number, cutoff_date):
            evidence = fields.get("evidence")
            if evidence is None:
                findings.append(
                    TraceFinding(relative, line_number, marker, "missing required evidence=")
                )
            elif not _valid_evidence(repo, evidence):
                findings.append(
                    TraceFinding(
                        relative,
                        line_number,
                        marker,
                        f"invalid evidence value: {evidence}",
                    )
                )
    return findings
