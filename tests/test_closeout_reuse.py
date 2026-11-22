"""CH-MAINT-016: closeout verdict reuse tests.

The closeout (`gitreins task complete`) re-judged byte-identical trees the
plain `gitreins judge` had graded COMPLETE minutes earlier and burned the
whole tier2 `max_time` wall twice in two runs (verdicts 84f17bf2 /
3ad6e7b1 in chimera-v2, 2026-11-22). The reuse module
(``engine/closeout_reuse.py``) adopts a stored fully-graded verdict for the
same tree/criteria/lane instead. These tests pin every adoption identity:
tree (HEAD + workspace fingerprint), criteria, lane model, graded
completeness, supersession — plus each rejection shape.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from engine.closeout_reuse import (
    build_adopted_result,
    find_reusable_verdict,
)
from engine.fingerprint import compute as compute_fingerprint
from engine.gate import PASS_CLASS_JUDGED, pass_class


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:8]


def _git(tmp_path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=T", *args],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A tiny real repo: tracked files, one commit, clean tree.

    ``.gitreins/history`` is gitignored exactly like real checkouts: the
    working-tree fingerprint must not see the stored verdict bytes this
    fixture (and a real closeout) writes there.
    """
    msg = tmp_path / "init-msg.txt"
    msg.write_text("init\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("closeout reuse fixture\n")
    (repo / ".gitignore").write_text(".gitreins/history\n")
    (repo / ".gitreins").mkdir()
    (repo / ".gitreins" / "config.yaml").write_text(
        "evaluator:\n  max_time: 20m\n  max_iterations: 300\n"
    )
    _git(repo, "add", "README.md", ".gitignore", ".gitreins/config.yaml")
    _git(repo, "commit", "-q", "-F", str(msg))
    return repo


def write_verdict_entry(
    workdir: Path,
    task_id: str,
    payload: dict,
    *,
    date: str = "2026-11-22",
    suffix: str = "",
) -> tuple[str, str]:
    """Persist one verdict record under .gitreins/history/<date>/<hash>/."""
    entry_hash = _hash(f"{task_id}:{suffix}{json.dumps(payload, sort_keys=True)}")
    entry_dir = workdir / ".gitreins" / "history" / date / entry_hash
    entry_dir.mkdir(parents=True, exist_ok=True)
    record = {"task_id": task_id, "evaluated_at": f"{date}T12:00:00", **payload}
    record.pop("_date", None)
    record.pop("_hash", None)
    (entry_dir / "verdict.json").write_text(json.dumps(record))
    return date, entry_hash


def _tier2_stage(model: str = "glm-5.3-flash") -> dict:
    return {
        "passed": True,
        "model": model,
        "id": "tier2",
        "summary": "COMPLETE",
    }


def _adoptable_payload(
    commit: str,
    fingerprint: str,
    criteria: list[str],
    *,
    model: str = "glm-5.3-flash",
) -> dict:
    return {
        "task_id": "T-016",
        "task_criteria": list(criteria),
        "passed": True,
        "pass_class": "judged",
        "commit": commit,
        "working_tree_content_fingerprint": fingerprint,
        "items": [
            {"criterion": criterion, "status": "PASS", "detail": f"graded: {criterion}"}
            for criterion in criteria
        ],
        "stages": {
            "tier1": {"passed": True, "summary": "Tier 1: DEGRADED PASS"},
            "tier2": _tier2_stage(model),
        },
        "summary": "stage summary",
    }


CRITERIA = ["the fix works", "tests cover it"]


class TestFindReusableVerdict:
    def test_adopts_fully_graded_verdict_for_identical_tree(self, git_repo: Path) -> None:
        fingerprint = compute_fingerprint(str(git_repo)).digest
        assert compute_fingerprint(str(git_repo)).state == "authoritative"
        commit = _git(git_repo, "rev-parse", "HEAD")
        date, entry_hash = write_verdict_entry(
            git_repo, "T-016", _adoptable_payload(commit, fingerprint, CRITERIA)
        )
        task = SimpleNamespace(id="T-016", criteria=list(CRITERIA))
        llm = SimpleNamespace(model="glm-5.3-flash")

        reused = find_reusable_verdict(str(git_repo), task, llm)

        assert reused is not None
        assert reused.entry == f"{date}/{entry_hash}"
        assert reused.verdict["items"][0]["status"] == "PASS"
        assert reused.verdict["items"][0]["criterion"] == CRITERIA[0]

    def test_changed_tree_regrades(self, git_repo: Path) -> None:
        """A stored verdict for DIFFERENT workspace bytes is never adopted."""
        fingerprint = compute_fingerprint(str(git_repo)).digest
        commit = _git(git_repo, "rev-parse", "HEAD")
        write_verdict_entry(
            git_repo,
            "T-016",
            _adoptable_payload(commit, fingerprint, CRITERIA),
        )
        task = SimpleNamespace(id="T-016", criteria=list(CRITERIA))
        llm = SimpleNamespace(model="glm-5.3-flash")

        # The tree moved after the verdict was graded.
        (git_repo / "README.md").write_text("now different bytes\n")
        assert find_reusable_verdict(str(git_repo), task, llm) is None

    def test_changed_head_regrades(self, git_repo: Path) -> None:
        fingerprint = compute_fingerprint(str(git_repo)).digest
        stale_commit = "0" * 40
        write_verdict_entry(
            git_repo,
            "T-016",
            _adoptable_payload(stale_commit, fingerprint, CRITERIA),
        )
        task = SimpleNamespace(id="T-016", criteria=list(CRITERIA))
        llm = SimpleNamespace(model="glm-5.3-flash")
        assert find_reusable_verdict(str(git_repo), task, llm) is None

    def test_changed_criteria_regrades(self, git_repo: Path) -> None:
        fingerprint = compute_fingerprint(str(git_repo)).digest
        commit = _git(git_repo, "rev-parse", "HEAD")
        write_verdict_entry(git_repo, "T-016", _adoptable_payload(commit, fingerprint, CRITERIA))
        task = SimpleNamespace(id="T-016", criteria=["different criterion"])
        llm = SimpleNamespace(model="glm-5.3-flash")
        assert find_reusable_verdict(str(git_repo), task, llm) is None

    def test_lane_mismatch_regrades(self, git_repo: Path) -> None:
        """CH-MAINT-015: the graded model must match the closeout lane."""
        fingerprint = compute_fingerprint(str(git_repo)).digest
        commit = _git(git_repo, "rev-parse", "HEAD")
        write_verdict_entry(
            git_repo,
            "T-016",
            _adoptable_payload(commit, fingerprint, CRITERIA, model="kimi-k3"),
        )
        task = SimpleNamespace(id="T-016", criteria=list(CRITERIA))
        llm = SimpleNamespace(model="glm-5.3-flash")
        assert find_reusable_verdict(str(git_repo), task, llm) is None

    def test_capped_partial_verdict_is_never_adopted(self, git_repo: Path) -> None:
        """A time-cap record (UNVERIFIED item, 0 graded) re-judges instead.

        The CH-MAINT-013 loud cap persists an INCOMPLETE partial verdict with
        "Not examined — evaluation ended before this criterion was reached"
        items; mirroring one would launder a partial grade into a closeout.
        """
        fingerprint = compute_fingerprint(str(git_repo)).digest
        commit = _git(git_repo, "rev-parse", "HEAD")
        payload = _adoptable_payload(commit, fingerprint, CRITERIA)
        payload["items"] = [
            {
                "criterion": CRITERIA[0],
                "status": "UNVERIFIED",
                "detail": "Not examined — evaluation ended before this criterion was reached",
            }
        ]
        payload["ungraded_criteria"] = list(CRITERIA)
        payload["passed"] = False
        write_verdict_entry(git_repo, "T-016", payload)
        task = SimpleNamespace(id="T-016", criteria=list(CRITERIA))
        llm = SimpleNamespace(model="glm-5.3-flash")
        assert find_reusable_verdict(str(git_repo), task, llm) is None

    def test_superseded_verdict_is_never_adopted(self, git_repo: Path) -> None:
        fingerprint = compute_fingerprint(str(git_repo)).digest
        commit = _git(git_repo, "rev-parse", "HEAD")
        payload = _adoptable_payload(commit, fingerprint, CRITERIA)
        payload["superseded_by"] = "2026-11-22/abcd1234"
        write_verdict_entry(git_repo, "T-016", payload)
        task = SimpleNamespace(id="T-016", criteria=list(CRITERIA))
        llm = SimpleNamespace(model="glm-5.3-flash")
        assert find_reusable_verdict(str(git_repo), task, llm) is None

    def test_no_criteria_never_adopts(self, git_repo: Path) -> None:
        task = SimpleNamespace(id="T-016", criteria=[])
        llm = SimpleNamespace(model="glm-5.3-flash")
        assert find_reusable_verdict(str(git_repo), task, llm) is None

    def test_registry_flip_does_not_change_the_workspace_digest(self, git_repo: Path) -> None:
        """CH-MAINT-016: .gitreins/tasks.yaml is self state, not content.

        `task complete` flips status/completed_at into the (tracked) task
        registry BEFORE re-observing the tree — the first closeout graded
        one registry state and the adoption re-observe finds another. The
        exclusion keeps the delivered-workspace digest stable across that
        flip, so a consecutive closeout of the SAME task on the SAME tree
        can adopt instead of re-spending the tier2 wall (measured live:
        without the exclusion the re-grade ran 29.67s instead of adopting).
        """
        before = compute_fingerprint(str(git_repo)).digest
        # Mimic what the lifecycle does: register + flip a task to complete.
        (git_repo / ".gitreins" / "tasks.yaml").write_text("tasks:\n  T-016:\n")
        _git(git_repo, "add", ".gitreins/tasks.yaml")
        (git_repo / ".gitreins" / "tasks.yaml").write_text(
            "tasks:\n  T-016:\n    status: complete\n    completed_at: '2026-11-22T15:00:00'\n"
        )
        after_flip = compute_fingerprint(str(git_repo)).digest
        (git_repo / ".gitreins" / "tasks.yaml").write_text(
            "tasks:\n  T-016:\n    status: complete\n    completed_at: '2026-11-22T16:00:00'\n"
        )
        after_second_flip = compute_fingerprint(str(git_repo)).digest

        # The registry file does not exist at `before`, is new in the index
        # after the flip, and its own byte-level edits do not move the
        # workspace digest in ANY of the three states — self state is not
        # content.
        assert before == after_flip == after_second_flip
        # and a verdict graded before the flip still matches the tree.
        fingerprint = compute_fingerprint(str(git_repo)).digest
        commit = _git(git_repo, "rev-parse", "HEAD")
        write_verdict_entry(git_repo, "T-016", _adoptable_payload(commit, fingerprint, CRITERIA))
        task = SimpleNamespace(id="T-016", criteria=list(CRITERIA))
        llm = SimpleNamespace(model="glm-5.3-flash")
        assert find_reusable_verdict(str(git_repo), task, llm) is not None

    def test_unresolvable_lane_never_adopts(self, git_repo: Path) -> None:
        fingerprint = compute_fingerprint(str(git_repo)).digest
        commit = _git(git_repo, "rev-parse", "HEAD")
        write_verdict_entry(git_repo, "T-016", _adoptable_payload(commit, fingerprint, CRITERIA))
        task = SimpleNamespace(id="T-016", criteria=list(CRITERIA))
        llm = SimpleNamespace(model="")  # model not resolvable: fail closed
        assert find_reusable_verdict(str(git_repo), task, llm) is None


class TestBuildAdoptedResult:
    def test_adopted_result_mirrors_stored_grade(self, git_repo: Path) -> None:
        fingerprint = compute_fingerprint(str(git_repo)).digest
        commit = _git(git_repo, "rev-parse", "HEAD")
        payload = _adoptable_payload(commit, fingerprint, CRITERIA)
        date, entry_hash = write_verdict_entry(git_repo, "T-016", payload)
        task = SimpleNamespace(id="T-016", criteria=list(CRITERIA))
        reused = find_reusable_verdict(str(git_repo), task, SimpleNamespace(model="glm-5.3-flash"))
        assert reused is not None

        result = build_adopted_result(reused, task.id)

        assert result.passed is True
        assert result.task_id == "T-016"
        assert result.verdict is not None
        assert result.verdict.verdict == "COMPLETE"
        assert [item.status for item in result.verdict.items] == ["PASS", "PASS"]
        assert [item.criterion for item in result.verdict.items] == list(CRITERIA)
        # The adoption record classifies as a JUDGED pass: it carries tier2
        # artifact evidence, not a tier1-only claim.
        assert pass_class(result) == PASS_CLASS_JUDGED
        assert result.pipeline_result["stages"]["tier2"]["model"] == "glm-5.3-flash"
        assert reused.entry == f"{date}/{entry_hash}"

    def test_adopted_failure_mirrors_the_fail(self, git_repo: Path) -> None:
        """A graded FAIL verdict is adopted honestly (and exits non-zero
        through the shared closeout contract)."""
        fingerprint = compute_fingerprint(str(git_repo)).digest
        commit = _git(git_repo, "rev-parse", "HEAD")
        payload = _adoptable_payload(commit, fingerprint, CRITERIA)
        payload["passed"] = False
        payload["items"] = [
            {"criterion": CRITERIA[0], "status": "FAIL", "detail": "broken"},
            {"criterion": CRITERIA[1], "status": "PASS", "detail": "ok"},
        ]
        write_verdict_entry(git_repo, "T-016", payload)
        task = SimpleNamespace(id="T-016", criteria=list(CRITERIA))
        reused = find_reusable_verdict(str(git_repo), task, SimpleNamespace(model="glm-5.3-flash"))
        assert reused is not None

        result = build_adopted_result(reused, task.id)
        assert result.passed is False
        assert pass_class(result) == PASS_CLASS_JUDGED


class TestTaskCompleteAdoptionCLI:
    """cmd_task_complete drives the adoption seam end to end (HTTP mocked)."""

    def _invoke(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture,
        *,
        skip_tier2: bool,
        reuse_state: dict,
    ) -> tuple[int, list, list]:
        import time as _time  # noqa: F401

        from gitreins import cli as cli_mod

        control = tmp_path / "control"
        scan = tmp_path / "project"
        (control / ".gitreins").mkdir(parents=True, exist_ok=True)
        scan.mkdir(exist_ok=True)
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setattr(cli_mod, "get_workdir", lambda: str(control))
        monkeypatch.setattr(cli_mod, "_require_task", lambda manager, task_id: object())
        persist_calls: list = []
        monkeypatch.setattr(cli_mod, "_persist_result", lambda *args: persist_calls.append(args))

        class FakeTaskManager:
            def __init__(self, _workdir: object) -> None:
                pass

            def check_dependencies(self, _task_id: str) -> list:
                return []

            def complete(self, task_id: str, force: bool = False) -> object:
                return SimpleNamespace(id=task_id, status="complete")

        judge_constructed: list = []

        class FakeJudge:
            def __init__(
                self,
                _llm: object,
                _workdir: object,
                guard_config: object = None,
                scan_root: object = None,
            ) -> None:
                judge_constructed.append(True)

            def evaluate_task(self, task: object, skip_tier2: bool = False) -> object:
                reuse_state["regraded"].append(task.id)
                return SimpleNamespace(passed=True, task_id=task.id, summary="PASS", tier2=None)

        monkeypatch.setattr("engine.task_manager.TaskManager", FakeTaskManager)
        monkeypatch.setattr("engine.judge.Judge", FakeJudge)
        monkeypatch.setattr("engine.llm.LLMClient", lambda: reuse_state["llm"])
        monkeypatch.setattr(cli_mod, "_validate_judge_lane_or_exit", lambda *a, **k: None)
        monkeypatch.setattr(
            "engine.closeout_reuse.find_reusable_verdict",
            lambda *a, **k: reuse_state.get("reusable"),
        )
        monkeypatch.setattr(
            "engine.closeout_reuse.build_adopted_result",
            lambda reusable, task_id: reuse_state["adopted_result"],
        )

        args = SimpleNamespace(id="T-016", scan_root=str(scan), force=True, skip_tier2=skip_tier2)
        exit_code = 0
        try:
            cli_mod.cmd_task_complete(args)
        except SystemExit as exc:
            exit_code = int(exc.code or 0)
        return exit_code, judge_constructed, persist_calls

    def test_matching_verdict_adopts_without_regrade(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        import engine.closeout_reuse as reuse_mod

        reuse_state = {
            "regraded": [],
            "llm": SimpleNamespace(api_key="k", model="glm-5.3-flash"),
            "reusable": reuse_mod.ReusableVerdict(
                entry="2026-11-22/abc12345",
                verdict={"items": [{"criterion": "c", "status": "PASS", "detail": "d"}]},
                evaluated_at="2026-11-22T12:00:00",
            ),
            "adopted_result": SimpleNamespace(
                passed=True,
                task_id="T-016",
                summary="Judge Result: T-016",
                tier2=object(),
                pipeline_result={"passed": True, "stages": {}},
            ),
        }
        exit_code, judge_constructed, persist_calls = self._invoke(
            tmp_path, monkeypatch, capsys, skip_tier2=False, reuse_state=reuse_state
        )

        assert exit_code == 0
        # No re-grade: the closeout judge was never constructed.
        assert judge_constructed == []
        assert reuse_state["regraded"] == []
        out = capsys.readouterr().out
        assert "Adopting stored verdict 2026-11-22/abc12345" in out
        # The adoption markers ride the persisted record.
        assert len(persist_calls) == 1
        assert persist_calls[0][3]["reused_from"] == "2026-11-22/abc12345"

    def test_no_reuse_falls_back_to_regrade(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        reuse_state = {
            "regraded": [],
            "llm": SimpleNamespace(api_key="k", model="glm-5.3-flash"),
            "reusable": None,
        }
        exit_code, judge_constructed, persist_calls = self._invoke(
            tmp_path, monkeypatch, capsys, skip_tier2=False, reuse_state=reuse_state
        )

        assert exit_code == 0
        assert judge_constructed == [True]
        assert reuse_state["regraded"] == ["T-016"]
        out = capsys.readouterr().out
        assert "Evaluating..." in out
        assert "Adopting stored verdict" not in out
        assert len(persist_calls) == 1
        assert persist_calls[0][3] is None

    def test_skip_tier2_never_adopts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
    ) -> None:
        """--skip-tier2 keeps its no-LLM contract: no adoption, Tier 1 only."""
        import engine.closeout_reuse as reuse_mod

        reuse_state = {
            "regraded": [],
            "llm": SimpleNamespace(api_key="k", model="glm-5.3-flash"),
            "reusable": reuse_mod.ReusableVerdict(
                entry="2026-11-22/abc12345",
                verdict={"items": [{"criterion": "c", "status": "PASS", "detail": "d"}]},
                evaluated_at="2026-11-22T12:00:00",
            ),
            "adopted_result": SimpleNamespace(
                passed=True, task_id="T-016", summary="x", tier2=object()
            ),
        }
        exit_code, judge_constructed, persist_calls = self._invoke(
            tmp_path, monkeypatch, capsys, skip_tier2=True, reuse_state=reuse_state
        )

        assert exit_code == 0
        assert judge_constructed == [True]
        assert reuse_state["regraded"] == ["T-016"]
        assert "Adopting stored verdict" not in capsys.readouterr().out
