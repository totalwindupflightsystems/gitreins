"""Regression tests for the mechanical ch:trace tier-1 guard."""

import json
from pathlib import Path

import pytest

from engine.guard_manager import GuardManager
from engine.trace_validator import scan_trace_markers, validate_trace_markers


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _fields(marker: str) -> dict[str, str]:
    """Parse the marker's fields the way the validator does."""
    from engine.trace_validator import _FIELDS

    return dict(_FIELDS.findall(marker.removeprefix("ch:trace")))


def test_dangling_evidence_fails_and_quotes_marker(tmp_path: Path) -> None:
    marker = "ch:trace row=GR-X evidence=artifacts/missing.log"
    _write(tmp_path / "docs/example.md", marker + "\n")

    findings = validate_trace_markers(tmp_path)

    assert len(findings) == 1
    assert marker in findings[0].render()
    result = GuardManager(str(tmp_path), {"guards": {"trace": True}}, scope="tree")._check_trace()
    assert result.passed is False
    assert marker in result.output
    assert "docs/example.md:1" in result.output


def test_missing_row_fails(tmp_path: Path) -> None:
    _write(tmp_path / "docs/example.md", "ch:trace evidence=none:fixture-only\n")

    findings = validate_trace_markers(tmp_path)

    assert len(findings) == 1
    assert "missing required row=" in findings[0].reason
    assert "ch:trace evidence=none:fixture-only" in findings[0].render()


def test_valid_row_and_existing_evidence_pass(tmp_path: Path) -> None:
    _write(tmp_path / "docs/artifact.txt", "generated output\n")
    _write(
        tmp_path / "specs/example.md",
        "ch:trace row=GR-X evidence=docs/artifact.txt test=not-validated\n",
    )

    assert validate_trace_markers(tmp_path) == []


def test_grandfathered_row_and_legacy_bare_field_list_pass(tmp_path: Path) -> None:
    tasks = tmp_path / ".coding-hermes/board/tasks.jsonl"
    tasks.parent.mkdir(parents=True)
    tasks.write_text(
        json.dumps(
            {
                "id": "OLD-1",
                "created_at": "2026-10-10T23:59:59Z",
                "reasoning": "ch:trace row=OLD-1 evidence=old/deleted.log",
            }
        )
        + "\n"
        + json.dumps(
            {
                "id": "PYTYP-GITREINS-01",
                "created_at": "2026-10-12T00:00:00Z",
                "ch_trace": "row=PYTYP-GITREINS-01 evidence=old/deleted.log",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert scan_trace_markers(tmp_path) == [
        (".coding-hermes/board/tasks.jsonl", 1, "ch:trace row=OLD-1 evidence=old/deleted.log")
    ]
    assert validate_trace_markers(tmp_path) == []


def test_none_evidence_escape_passes(tmp_path: Path) -> None:
    _write(tmp_path / "docs/example.md", "ch:trace row=GR-X evidence=none:no-artifact\n")

    assert validate_trace_markers(tmp_path) == []


def test_scanner_finds_board_and_docs_markers(tmp_path: Path) -> None:
    board_marker = "ch:trace row=BOARD-1 evidence=none:fixture"
    docs_marker = "ch:trace row=DOC-1 evidence=none:fixture"
    _write(
        tmp_path / ".coding-hermes/board/events.jsonl",
        json.dumps({"timestamp": "2026-10-12T00:00:00Z", "detail": board_marker}) + "\n",
    )
    _write(tmp_path / "docs/example.md", docs_marker + "\n")

    assert scan_trace_markers(tmp_path) == [
        (".coding-hermes/board/events.jsonl", 1, board_marker),
        ("docs/example.md", 1, docs_marker),
    ]


def test_guard_manager_runs_trace_lane(tmp_path: Path) -> None:
    _write(tmp_path / "docs/example.md", "ch:trace evidence=none:missing-row\n")
    manager = GuardManager(
        str(tmp_path),
        {"guards": {"secrets": False, "lint": False, "tests": False, "trace": True}},
        scope="tree",
        persist_log=False,
    )

    result = manager.run_all()

    assert result.passed is False
    trace = next(item for item in result.results if item.name == "trace")
    assert "missing required row=" in trace.output


def _git_repo(path: Path) -> Path:
    import subprocess

    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "tests@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Trace Tests"], check=True)
    return path


def test_evidence_commit_and_verdict_locators_pass_without_artifacts(tmp_path: Path) -> None:
    _write(
        tmp_path / "docs/example.md",
        "\n".join(
            [
                "ch:trace row=GR-C evidence=commit-108c60d",
                "ch:trace row=GR-H evidence=.gitreins/history/2026-10-12/abcdef0123456789/verdict.json",
                "ch:trace row=GR-V evidence=.gitreins/verdicts/verdict.json",
            ]
        )
        + "\n",
    )

    assert validate_trace_markers(tmp_path) == []


def test_absolute_evidence_path_fails(tmp_path: Path) -> None:
    _write(tmp_path / "docs/example.md", "ch:trace row=GR-X evidence=/tmp/vanished-verdict.json\n")

    findings = validate_trace_markers(tmp_path)

    assert len(findings) == 1
    assert "invalid evidence value: /tmp/vanished-verdict.json" in findings[0].reason


@pytest.mark.parametrize(
    "evidence",
    [
        ".gitreins/history/not-a-date/abcdef01/verdict.json",
        ".gitreins/history/2026-02-30/abcdef01/verdict.json",
        ".gitreins/history/2026-10-12/abc/verdict.json",
        ".gitreins/history/2026-10-12/abcdef01/other.json",
    ],
)
def test_malformed_history_locator_fails(tmp_path: Path, evidence: str) -> None:
    _write(tmp_path / "docs/example.md", f"ch:trace row=GR-X evidence={evidence}\n")

    assert len(validate_trace_markers(tmp_path)) == 1


def test_diff_scope_grades_added_marker_but_not_untouched_marker(tmp_path: Path) -> None:
    import subprocess

    repo = _git_repo(tmp_path)
    original = "ch:trace row=OLD evidence=missing/old\n"
    _write(repo / "docs/example.md", original)
    subprocess.run(["git", "-C", str(repo), "add", "docs/example.md"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "base"], check=True, capture_output=True
    )
    _write(repo / "docs/example.md", original + "ch:trace row=NEW evidence=missing/new\n")

    from engine.trace_validator import _changed_lines

    assert _changed_lines(repo, "docs/example.md", "working-tree") == {2}
    findings = validate_trace_markers(
        repo,
        full=False,
        changed_files=["docs/example.md"],
        scope="working-tree",
    )

    assert len(findings) == 1
    assert findings[0].line == 2
    assert "missing/new" in findings[0].marker


def test_full_scope_exempts_pre_cutoff_marker_evidence(tmp_path: Path) -> None:
    import os
    import subprocess
    from datetime import date

    repo = _git_repo(tmp_path)
    _write(repo / "docs/example.md", "ch:trace row=OLD evidence=/tmp/removed-evidence\n")
    subprocess.run(["git", "-C", str(repo), "add", "docs/example.md"], check=True)
    # Pin the marker's introduction date instead of relying on wall-clock
    # agreement between blame dates (UTC) and the local-date cutoff: a test
    # that commits "now" and cuts off "today" silently flips at a timezone
    # or midnight boundary (blame 11-19 UTC vs local 11-18 → graded, FAIL).
    pinned_env = dict(
        os.environ,
        GIT_AUTHOR_DATE="2026-01-01T00:00:00Z",
        GIT_COMMITTER_DATE="2026-01-01T00:00:00Z",
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-m", "old marker"],
        check=True,
        capture_output=True,
        env=pinned_env,
    )

    assert validate_trace_markers(repo, full=True, cutoff=date.today()) == []

    # The boundary proves both directions: the same marker graded once the
    # cutoff predates its introduction.
    assert validate_trace_markers(repo, full=True, cutoff=date(2025, 1, 1)) != []


def test_history_locator_shape_does_not_require_file(tmp_path: Path) -> None:
    marker = "ch:trace row=GR-H evidence=.gitreins/history/2026-10-12/abcdef0123456789/verdict.json"
    _write(tmp_path / "docs/example.md", marker + "\n")

    assert not (tmp_path / ".gitreins/history/2026-10-12/abcdef0123456789/verdict.json").exists()
    assert validate_trace_markers(tmp_path) == []


class TestAnnotationTolerance:
    """Free-text annotation tokens between field tokens must not truncate a marker."""

    def test_fields_after_annotation_are_parsed(self, tmp_path: Path) -> None:
        _write(tmp_path / "docs/artifact.txt", "generated output\n")
        marker = (
            "ch:trace row=GR-239 spec=docs/example.md annotation-text-here "
            "test=tests/test_guard_trace.py evidence=docs/artifact.txt"
        )
        _write(tmp_path / "docs/example.md", marker + "\n")

        markers = scan_trace_markers(tmp_path)

        assert markers == [("docs/example.md", 1, marker)]
        assert _fields(markers[0][2]) == {
            "row": "GR-239",
            "spec": "docs/example.md",
            "test": "tests/test_guard_trace.py",
            "evidence": "docs/artifact.txt",
        }
        assert validate_trace_markers(tmp_path) == []

    def test_markers_within_one_line_stay_separate(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "docs/example.md",
            "ch:trace row=A-1 note: first claim evidence=none:first, "
            "ch:trace row=B-2 note: second claim evidence=none:second\n",
        )

        assert scan_trace_markers(tmp_path) == [
            ("docs/example.md", 1, "ch:trace row=A-1 note: first claim evidence=none:first,"),
            ("docs/example.md", 1, "ch:trace row=B-2 note: second claim evidence=none:second"),
        ]

    def test_board_json_marker_does_not_swallow_json_fields(self, tmp_path: Path) -> None:
        marker = "ch:trace row=B-1 note: free text here evidence=none:fixture"
        _write(
            tmp_path / ".coding-hermes/board/events.jsonl",
            json.dumps({"detail": marker, "reasoning": "row=OTHER spec=docs/other.md"}) + "\n",
        )

        assert scan_trace_markers(tmp_path) == [(".coding-hermes/board/events.jsonl", 1, marker)]
        assert validate_trace_markers(tmp_path) == []

    def test_annotation_without_evidence_still_fails(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "docs/example.md",
            "ch:trace row=GR-239 note: the artifact went missing test=tests/test_guard_trace.py\n",
        )

        findings = validate_trace_markers(tmp_path)

        assert len(findings) == 1
        assert findings[0].reason == "missing required evidence="

    def test_annotation_without_row_still_fails(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "docs/example.md",
            "ch:trace note: no work unit named test=none:not-a-code-change "
            "evidence=none:fixture-only\n",
        )

        findings = validate_trace_markers(tmp_path)

        assert len(findings) == 1
        assert findings[0].reason == "missing required row="

    def test_annotation_with_dangling_evidence_still_fails(self, tmp_path: Path) -> None:
        _write(
            tmp_path / "docs/example.md",
            "ch:trace row=GR-239 note: run once more test=none:not-a-code-change "
            "evidence=artifacts/missing.log\n",
        )

        findings = validate_trace_markers(tmp_path)

        assert len(findings) == 1
        assert findings[0].reason == "invalid evidence value: artifacts/missing.log"
