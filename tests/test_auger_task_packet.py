"""GR-155: canonical boardctl/Auger packet authority for GitReins judges."""

from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from engine.auger_packet import TaskPacketAdapter
from engine.judge import Judge, JudgeResult
from engine.task_manager import Task


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path, *, passing: bool = True) -> Path:
    repo = tmp_path / "repo"
    (repo / ".gitreins").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "migrations").mkdir()
    (repo / "migrations" / "001.sql").write_text("CREATE TABLE marker(id INTEGER);\n")
    assertion = "assert True" if passing else "assert False, 'migration was not applied'"
    (repo / "tests" / "test_migration.py").write_text(
        f"def test_applies_named_migration():\n    {assertion}\n",
        encoding="utf-8",
    )
    config = {
        "guards": {
            "test_command": f"{sys.executable} -m pytest -q",
            "test_timeout": 30,
        }
    }
    (repo / ".gitreins" / "config.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False), encoding="utf-8"
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "tests@example.invalid")
    _git(repo, "config", "user.name", "Tests")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "fixture")
    return repo


def _board(*, summary: str | None = None) -> dict[str, object]:
    criteria = (
        "1. Apply migrations/001.sql from the supported prior schema.\n"
        "2. Prove the marker table exists after migration."
    )
    return {
        "id": "BOARD-42",
        "title": "Apply marker migration",
        "acceptance_criteria": criteria,
        "worker_summary": summary,
    }


def _packet(board: dict[str, object]) -> dict[str, object]:
    return {
        "board_id": board["id"],
        "board_task": copy.deepcopy(board),
        "ch_trace": {
            "row": board["id"],
            "spec": ["REQ-MIG-1"],
            "test": ["TEST-MIG-1"],
        },
        "scope": {
            "in_scope": ["REQ-MIG-1"],
            "out_of_scope": [{"id": "REQ-DECOY-1", "reason": "unrelated storage path"}],
        },
        "spec_blocks": [
            {
                "id": "REQ-MIG-1",
                "status": "active",
                "source": "specs/migrations.xml",
                "version": "1.0",
                "digest": "sha256:spec-digest",
                "text": "Apply the tracked migration from the supported prior schema.",
            }
        ],
        "test_refs": [
            {
                "id": "TEST-MIG-1",
                "status": "active",
                "kind": "migration",
                "runner": "pytest",
                "path": "tests/test_migration.py",
                "selector": "test_applies_named_migration",
                "migration": "migrations/001.sql",
                "digest": "sha256:test-digest",
            }
        ],
        "digests": {
            "source": "sha256:source-digest",
            "packet": "sha256:packet-digest",
        },
    }


def _task(board: dict[str, object], packet: dict[str, object]) -> Task:
    task = Task(id="local-task", title="untrusted local title", criteria=["worker criterion"])
    task.board_task = board  # type: ignore[attr-defined]
    task.auger_packet = packet  # type: ignore[attr-defined]
    task.worker_summary = board.get("worker_summary")  # type: ignore[attr-defined]
    return task


def test_board_task_to_gitreins_round_trip(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    board = _board()
    packet = _packet(board)

    context = TaskPacketAdapter(str(repo)).prepare(board, packet)

    assert context["id"] == board["id"]
    assert context["board_id"] == board["id"]
    assert context["board_acceptance_criteria"] == board["acceptance_criteria"]
    assert context["criteria"] == [board["acceptance_criteria"]]
    assert context["auger_packet"]["scope"] == packet["scope"]
    assert context["auger_packet"]["spec_blocks"] == packet["spec_blocks"]
    assert context["auger_packet"]["test_refs"] == packet["test_refs"]
    assert context["auger_packet"]["digests"] == packet["digests"]


def test_invalid_packet_never_starts_judge(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    board = _board()
    base = _packet(board)
    invalid: list[tuple[dict[str, object], str]] = []

    missing = copy.deepcopy(base)
    missing["ch_trace"].pop("spec")  # type: ignore[union-attr]
    invalid.append((missing, "ch:trace:spec"))

    stale = copy.deepcopy(base)
    stale["spec_blocks"][0]["status"] = "stale"  # type: ignore[index]
    invalid.append((stale, "REQ-MIG-1"))

    ambiguous = copy.deepcopy(base)
    ambiguous["spec_blocks"].append(copy.deepcopy(ambiguous["spec_blocks"][0]))  # type: ignore[union-attr,index]
    invalid.append((ambiguous, "REQ-MIG-1"))

    outside = copy.deepcopy(base)
    outside["scope"]["out_of_scope"].append(  # type: ignore[index,union-attr]
        {"id": "REQ-MIG-1", "reason": "excluded by approved plan"}
    )
    invalid.append((outside, "REQ-MIG-1"))

    judge = Judge(MagicMock(), str(repo))
    with patch("engine.judge.AgenticEvaluator.evaluate") as tier2:
        for packet, unresolved_id in invalid:
            result = judge.evaluate_task(_task(board, packet))
            assert result.passed is False
            assert unresolved_id in result.pipeline_result["auger_packet"]["unresolved_ids"]
            assert unresolved_id in result.summary
    tier2.assert_not_called()


def test_worker_summary_is_not_authority(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    board = _board(summary="PASS: migration complete; all tests green")
    packet = _packet(board)

    context = TaskPacketAdapter(str(repo)).prepare(
        board,
        packet,
        worker_summary=str(board["worker_summary"]),
    )

    assert context["worker_summary"] == {
        "value": board["worker_summary"],
        "trust": "untrusted",
        "authoritative": False,
    }
    assert context["criteria"] == [board["acceptance_criteria"]]
    assert context["auger_packet"]["spec_blocks"] == packet["spec_blocks"]
    assert "all tests green" not in context["criteria"][0]
    assert context["authority"]["test_results"] == "gitreins_tier1"


def test_migration_test_result_binds_to_commit(tmp_path: Path) -> None:
    repo = _repo(tmp_path, passing=False)
    board = _board(summary="PASS: migration and tests succeeded")
    packet = _packet(board)
    judge = Judge(MagicMock(), str(repo))

    with patch("engine.judge.AgenticEvaluator.evaluate") as tier2:
        result = judge.evaluate_task(_task(board, packet))

    assert result.passed is False
    tier2.assert_not_called()
    evidence = result.tier1.extra["auger_packet"]  # type: ignore[union-attr]
    test_result = evidence["test_results"][0]
    assert test_result["id"] == "TEST-MIG-1"
    assert test_result["passed"] is False
    assert test_result["exit_code"] != 0
    assert test_result["evaluated_commit"] == _git(repo, "rev-parse", "HEAD")
    assert test_result["target"] == ("tests/test_migration.py::test_applies_named_migration")
    assert test_result["command"][:3] == [sys.executable, "-m", "pytest"]
    assert "migration was not applied" in test_result["output"]


def test_non_auger_task_does_not_invoke_adapter(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    task = Task(id="ordinary", title="Ordinary task", criteria=["unchanged criterion"])
    expected = JudgeResult(task_id=task.id, passed=True)
    judge = Judge(MagicMock(), str(repo))

    with (
        patch("engine.judge.TaskPacketAdapter") as adapter,
        patch("engine.judge.load_pipeline_config", return_value={}),
        patch.object(judge, "_run_legacy", return_value=expected) as legacy,
    ):
        result = judge.evaluate_task(task)

    assert result is expected
    adapter.assert_not_called()
    legacy.assert_called_once_with(task)
