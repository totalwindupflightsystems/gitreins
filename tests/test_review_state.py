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
from engine.judge import Judge, JudgeResult
from engine.llm import LLMResponse, LLMUsage
from engine.persist import VerdictPersister
from engine.review_state import (
    ACTIVE,
    DISMISSED,
    ReviewStateStore,
    finding_fingerprint,
    reconcile_review,
    review_state_path,
)


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
    def test_real_judge_path_repeats_and_persists_receipt(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)

        class StubLLM:
            def chat(
                self, messages: object, tools: object = None, max_tokens: object = None
            ) -> LLMResponse:
                return LLMResponse(
                    content=json.dumps(
                        {
                            "findings": [
                                {
                                    "severity": "high",
                                    "file": "code.py",
                                    "line": 1,
                                    "detection": "unsafe value is accepted",
                                    "detail": "exact evidence",
                                }
                            ],
                            "summary": "one finding",
                        }
                    ),
                    usage=LLMUsage(),
                )

        judge = Judge(StubLLM(), str(repo), persist_log=False, persist_telemetry=False)
        first = JudgeResult(task_id="GR-211")
        judge._run_second_pass(first)
        second = JudgeResult(task_id="GR-211")
        judge._run_second_pass(second)

        assert first.second_pass_review is not None
        assert second.second_pass_review is not None
        assert first.second_pass_review["review_state"]["carried"] == []
        assert second.second_pass_review["review_state"]["carried"] == []
        assert (
            second.second_pass_review["findings"][0]["echo"]
            == first.second_pass_review["findings"][0]["echo"]
        )
        assert (
            second.second_pass_review["findings"][0]["fingerprint"]
            == first.second_pass_review["findings"][0]["fingerprint"]
        )
        state = json.loads(Path(review_state_path(str(repo))).read_text(encoding="utf-8"))
        assert state["run"] == 2

    def test_real_judge_path_applies_configured_dismissal(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)

        class StubLLM:
            def chat(
                self, messages: object, tools: object = None, max_tokens: object = None
            ) -> LLMResponse:
                return LLMResponse(
                    content=json.dumps(
                        {
                            "findings": [
                                {
                                    "severity": "high",
                                    "file": "code.py",
                                    "line": 1,
                                    "detection": "unsafe value is accepted",
                                    "detail": "exact evidence",
                                }
                            ],
                            "summary": "one finding",
                        }
                    ),
                    usage=LLMUsage(),
                )

        first = Judge(StubLLM(), str(repo), persist_log=False, persist_telemetry=False)
        first_result = JudgeResult(task_id="GR-211")
        first._run_second_pass(first_result)
        assert first_result.second_pass_review is not None
        fingerprint = first_result.second_pass_review["findings"][0]["fingerprint"]
        config = repo / ".gitreins" / "config.yaml"
        config.write_text(
            "review:\n"
            "  second_pass: true\n"
            "  human_decisions:\n"
            f"    - fingerprint: {fingerprint}\n"
            "      decision: dismiss\n"
            "      reviewer: alice\n"
            "      reason: accepted risk\n",
            encoding="utf-8",
        )
        second = Judge(StubLLM(), str(repo), persist_log=False, persist_telemetry=False)
        second_result = JudgeResult(task_id="GR-211")
        second._run_second_pass(second_result)
        assert second_result.second_pass_review is not None
        assert second_result.second_pass_review["findings"] == []
        assert second_result.second_pass_review["review_state"]["emitted"] == []
        state = json.loads(Path(review_state_path(str(repo))).read_text(encoding="utf-8"))
        record = state["findings"][0]
        assert record["status"] == DISMISSED
        assert record["dismissed_by"] == "alice"

    def test_real_store_writes_exact_schema_and_echo(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        result = ReviewStateStore(str(repo)).reconcile_and_save([_finding("  exact\nbody  ")])
        assert result.state.run == 1
        assert result.state.findings[0].status == ACTIVE
        payload = json.loads(Path(review_state_path(str(repo))).read_text(encoding="utf-8"))
        assert set(payload) == {"version", "run", "findings"}
        assert payload["findings"][0]["echo"].endswith("\n  exact\nbody  ")

    def test_real_store_repeated_run_carries_verbatim(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        store = ReviewStateStore(str(repo))
        first = store.reconcile_and_save([_finding("original")])
        second_finding = _finding("original") | {"metadata": "unrelated"}
        second = store.reconcile_and_save([second_finding])
        assert second.carried == ()
        assert second.emitted[0]["fingerprint"] == first.state.findings[0].fingerprint
        assert second.emitted[0]["echo"] == first.emitted[0]["echo"]

    def test_real_store_cap_keeps_all_state(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path)
        store = ReviewStateStore(str(repo))
        findings = [_finding(f"finding {i}") | {"file": f"f{i}.py"} for i in range(3)]
        result = store.reconcile_and_save(findings, max_findings=2)
        assert len(result.emitted) == 2
        assert len(result.state.findings) == 3

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
        dismissed["findings"] = [{**dismissed["findings"][0], "state": DISMISSED}]
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
