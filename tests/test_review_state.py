"""GR-211 persistent finding-state reconciliation through verdict history.

ch:trace row=GR-211 spec=.coding-hermes/board/tasks.jsonl#GR-211 wave=none:serial test=tests/test_review_state.py::TestPersistentFindingState doc=none evidence=tests/test_review_state.py witness=none:worker-local commit=none:foreman-integrates memory=none:foreman-managed
STATUS: partial
TIER: T1
EVIDENCE: tests/test_review_state.py
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from engine.fingerprint import compute
from engine.persist import VerdictPersister
from engine.review_state import ACTIVE, DISMISSED, finding_fingerprint, reconcile_review


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)
    (repo / ".gitignore").write_text(".gitreins/history/\n", encoding="utf-8")
    (repo / "code.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", ".gitignore", "code.py"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def _finding(detail: str = "the evidence") -> dict[str, object]:
    return {
        "severity": "high",
        "file": "code.py",
        "line": 1,
        "detection": "unsafe value is accepted",
        "detail": detail,
    }


def _persist(repo: Path, review: dict[str, object]) -> dict[str, object]:
    tree = compute(str(repo))
    data = {
        "task_id": "GR-211",
        "task_title": "finding state",
        "passed": False,
        "commit": "",
        "working_tree_content_fingerprint": tree.digest,
        "working_tree_dirty_path_count": tree.dirty_count,
        "review": review,
    }
    persister = VerdictPersister(str(repo))
    persister.config["storage"] = "filesystem"
    persister.config["max_verdicts"] = 0
    assert persister.persist("GR-211", data) == "dry-run"
    entries = persister.list_verdicts(task_id="GR-211")
    assert entries
    return entries[0]


class TestPersistentFindingState:
    def test_unchanged_review_carries_verbatim_and_is_not_new(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        first = reconcile_review(str(repo), {"findings": [_finding()]}, None)
        first_finding = first["findings"][0]
        assert first_finding["state"] == ACTIVE
        fingerprint = first_finding["fingerprint"]
        prior = _persist(repo, first)
        assert prior["working_tree_content_fingerprint"] == compute(str(repo)).digest
        assert prior["review"]["finding_state"]["findings"]

        second = reconcile_review(str(repo), {"findings": [_finding()]}, prior)
        assert second["findings"] == [first_finding]
        assert second["findings"][0]["fingerprint"] == fingerprint
        assert second["finding_state"]["summary"] == {
            "carried": 1,
            "new": 0,
            "resolved": 0,
            "dismissed": 0,
        }

    def test_dismissed_finding_is_suppressed_unchanged(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        first = reconcile_review(str(repo), {"findings": [_finding()]}, None)
        dismissed = dict(first["finding_state"])
        dismissed["findings"] = [
            {**dismissed["findings"][0], "state": DISMISSED}
        ]
        prior = _persist(repo, {**first, "finding_state": dismissed})

        second = reconcile_review(str(repo), {"findings": [_finding()]}, prior)
        assert second["findings"] == []
        assert second["finding_state"]["summary"]["dismissed"] == 1

    def test_changed_code_does_not_blindly_carry(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        first = reconcile_review(str(repo), {"findings": [_finding()]}, None)
        prior = _persist(repo, first)
        (repo / "code.py").write_text("value = 2\n", encoding="utf-8")

        second = reconcile_review(str(repo), {"findings": [_finding()]}, prior)
        assert second["finding_state"]["summary"]["new"] == 1
        assert second["finding_state"]["summary"]["carried"] == 0

    def test_malformed_prior_state_is_not_carried(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        malformed = {
            "working_tree_content_fingerprint": compute(str(repo)).digest,
            "review": {"finding_state": {"version": 1, "findings": "not-a-list"}},
        }
        result = reconcile_review(str(repo), {"findings": [_finding()]}, malformed)
        assert result["finding_state"]["summary"]["new"] == 1
        assert result["finding_state"]["summary"]["carried"] == 0

    def test_fingerprint_normalizes_identity(self) -> None:
        left = _finding("Evidence\nwith   spaces")
        right = _finding(" evidence with spaces ")
        assert finding_fingerprint(left) == finding_fingerprint(right)

    def test_persisted_state_is_json_serializable(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        review = reconcile_review(str(repo), {"findings": [_finding()]}, None)
        assert json.dumps(review)
