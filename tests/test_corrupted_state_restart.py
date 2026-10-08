"""Corrupted-state restart verification (QA-GITREINS-POC-6).

The QA chaos-corruption cell truncates a state file and then watches the next
start. It recorded ``state file truncated: ./dev.db — watch crash behavior on
next start``, never observed that restart, and left the row ``unverified``.
These tests ARE that observation, run against the harness's OWN state files,
through the real CLI boundary where the CLI is what restarts:

* ``.gitreins/tasks.yaml`` — an unreadable task store must not crash, must
  report itself, and must never be destroyed by the next write;
* ``.gitreins/qa-ledger.jsonl`` — a garbage line costs that line only;
* ``.gitreins/history/**/verdict.json`` — an undecodable verdict is skipped,
  not fatal to the listing;
* ``.gitreins/config.yaml`` — an unreadable config falls back to defaults
  with a named diagnostic (validation errors still raise);
* ``.gitreins/disposable.json`` — garbage reads as an empty registry and the
  bytes are left alone (read paths never destroy state).

Probed live before the fix (2026-09-17): ``qa list`` / ``report`` died with
``UnicodeDecodeError`` tracebacks (rc 1), ``load_defaults()`` raised on a
binary-corrupted config, and the first write after an unreadable tasks.yaml
replaced it with an empty-but-valid file — the corrupted bytes were gone.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from engine.config import load_defaults, load_raw_config
from engine.persist import VerdictPersister, build_report
from engine.qa_ledger import list_rows
from engine.task_manager import TaskManager, TaskStateCorruptError
from engine.worktree_disposable import _load_disposable_file

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Bytes no UTF-8 decoder accepts, i.e. what a truncated-then-appended file
# looks like. Deterministic so a failure names the same payload every run.
GARBAGE = b"\xff\xfe\x00\x01 not yaml or json \x80\x81\xc3\x28 \x00\xff"

# Provider credentials must never leak into a CLI child (INT-FLAKE-1: a child
# with an ambient key silently performed a live Tier 2 call inside a 30 s
# timeout). Pinned at a loopback dead port for the same reason.
LLM_CREDENTIAL_ENV_KEYS = (
    "GITREINS_LLM_API_KEY",
    "NEURALWATT_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "KIMI_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
)
HERMETIC_LLM_BASE_URL = "http://127.0.0.1:9/v1"

REQUIRES_UNREADABLE_FILE = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="chmod 000 does not make a file unreadable for root",
)

HEALTHY_TASKS_YAML = (
    "tasks:\n"
    "- id: keep-me\n"
    "  title: keep me\n"
    "  criteria:\n"
    "  - c1\n"
    "  status: pending\n"
    "  created_at: '2026-09-01T00:00:00+00:00'\n"
)


def _write_bytes(path: str, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


def _read_bytes(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


def _sidecars(path: str) -> list[str]:
    directory = os.path.dirname(path)
    base = os.path.basename(path) + ".corrupt-"
    return sorted(
        os.path.join(directory, name) for name in os.listdir(directory) if name.startswith(base)
    )


def _state_path(workdir: str, name: str) -> str:
    return os.path.join(workdir, ".gitreins", name)


def _corrupt_tasks_yaml(workdir: str) -> str:
    path = _state_path(workdir, "tasks.yaml")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _write_bytes(path, GARBAGE)
    return path


def _run_cli(workdir: str, *args: str) -> subprocess.CompletedProcess:
    """Run the CLI as a child process with a hermetic environment."""
    env = {key: value for key, value in os.environ.items() if key not in LLM_CREDENTIAL_ENV_KEYS}
    env["GITREINS_LLM_BASE_URL"] = HERMETIC_LLM_BASE_URL
    env["PYTHONPATH"] = PROJECT_ROOT + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    return subprocess.run(
        [sys.executable, "-m", "gitreins", *args],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=workdir,
    )


def _assert_no_traceback(result: subprocess.CompletedProcess) -> None:
    combined = f"{result.stdout}\n{result.stderr}"
    assert "Traceback (most recent call last)" not in combined, combined
    assert "UnicodeDecodeError" not in combined, combined


# ── .gitreins/tasks.yaml ───────────────────────────────────────


class TestTaskStateRestart:
    def test_unreadable_task_state_warns_instead_of_crashing(self, tmp_workdir, capsys):
        """The restart after a corrupted task store exits cleanly and says so."""
        path = _corrupt_tasks_yaml(tmp_workdir)

        tm = TaskManager(tmp_workdir)

        # Warnings go to stderr (protocol purity: MCP stdio stdout stays JSON-RPC-clean).
        captured = capsys.readouterr()
        combined = captured.out + captured.err
        assert "Warning: failed to load tasks:" in combined
        assert "tasks.yaml" in combined
        assert tm.list_tasks() == []
        assert _read_bytes(path) == GARBAGE, "a read must not mutate the corrupt store"

    def test_unreadable_task_state_is_preserved_with_a_content_addressed_copy(self, tmp_workdir):
        path = _corrupt_tasks_yaml(tmp_workdir)

        TaskManager(tmp_workdir)

        sidecars = _sidecars(path)
        assert len(sidecars) == 1, sidecars
        assert _read_bytes(sidecars[0]) == GARBAGE, "the preserved copy holds the original bytes"

    def test_repeated_loads_do_not_churn_sidecars(self, tmp_workdir):
        path = _corrupt_tasks_yaml(tmp_workdir)

        for _ in range(3):
            TaskManager(tmp_workdir)

        assert len(_sidecars(path)) == 1

    def test_next_write_keeps_the_preserved_bytes_and_writes_fresh_state(self, tmp_workdir):
        path = _corrupt_tasks_yaml(tmp_workdir)
        tm = TaskManager(tmp_workdir)

        tm.create("after-corruption", "post corruption", ["c1"])

        preserved = _sidecars(path)
        assert len(preserved) == 1
        assert _read_bytes(preserved[0]) == GARBAGE
        reloaded = TaskManager(tmp_workdir)
        assert [task.id for task in reloaded.list_tasks()] == ["after-corruption"]

    def test_second_distinct_corruption_gets_its_own_copy(self, tmp_workdir):
        path = _corrupt_tasks_yaml(tmp_workdir)
        TaskManager(tmp_workdir).create("first", "first", [])
        second = b"\x00\x01 second corruption \xfe\xff"
        _write_bytes(path, second)

        TaskManager(tmp_workdir).create("second", "second", [])

        preserved = _sidecars(path)
        assert len(preserved) == 2, preserved
        assert sorted(_read_bytes(p) for p in preserved) == sorted([GARBAGE, second])

    def test_tasks_can_be_recovered_from_a_copy_taken_before_the_corruption(self, tmp_workdir):
        """The QA cell's own contract: back up, corrupt, preserve, restore, work again."""
        path = _state_path(tmp_workdir, "tasks.yaml")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(HEALTHY_TASKS_YAML)
        backup = _read_bytes(path)

        _write_bytes(path, GARBAGE)
        TaskManager(tmp_workdir).create("fresh", "fresh", [])
        preserved = _sidecars(path)
        assert len(preserved) == 1
        assert _read_bytes(preserved[0]) == GARBAGE, "the corrupt bytes are still on disk"

        _write_bytes(path, backup)
        restored = TaskManager(tmp_workdir)
        assert [task.id for task in restored.list_tasks()] == ["keep-me"]

    @REQUIRES_UNREADABLE_FILE
    def test_save_refuses_to_overwrite_state_it_could_not_read(self, tmp_workdir):
        path = _corrupt_tasks_yaml(tmp_workdir)
        os.chmod(path, 0o000)
        tm = TaskManager(tmp_workdir)

        with pytest.raises(TaskStateCorruptError) as caught:
            tm.create("never-written", "never written", [])

        os.chmod(path, 0o600)
        assert "refusing to overwrite" in str(caught.value)
        assert _read_bytes(path) == GARBAGE
        assert _sidecars(path) == [], "nothing could be preserved, so nothing was written"

    def test_cli_restart_after_task_state_corruption_is_clean(self, tmp_workdir):
        path = _corrupt_tasks_yaml(tmp_workdir)

        result = _run_cli(tmp_workdir, "task", "create", "cli-after-corrupt", "t", "c1")

        _assert_no_traceback(result)
        assert result.returncode == 0, result.stdout + result.stderr
        assert (
            "failed to load tasks" in result.stderr
        )  # warnings go to stderr (MCP stdio protocol purity)
        assert len(_sidecars(path)) == 1
        reloaded = TaskManager(tmp_workdir)
        assert [task.id for task in reloaded.list_tasks()] == ["cli-after-corrupt"]

    @REQUIRES_UNREADABLE_FILE
    def test_cli_refuses_unpreservable_state_with_one_clean_line(self, tmp_workdir):
        path = _corrupt_tasks_yaml(tmp_workdir)
        os.chmod(path, 0o000)

        result = _run_cli(tmp_workdir, "task", "create", "nope", "t", "c1")

        _assert_no_traceback(result)
        assert result.returncode == 1
        # Warnings precede the final error line on stderr (stderr is the
        # diagnostic stream; stdout stays protocol-pure for MCP stdio).
        assert result.stderr.rstrip().endswith("refusing to overwrite it")
        assert "refusing to overwrite" in result.stderr
        os.chmod(path, 0o600)
        assert _read_bytes(path) == GARBAGE


# ── .gitreins/qa-ledger.jsonl ──────────────────────────────────


class TestQaLedgerRestart:
    def _ledger(self, workdir: str) -> str:
        path = _state_path(workdir, "qa-ledger.jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path

    def test_rows_around_a_garbage_line_survive(self, tmp_workdir):
        path = self._ledger(tmp_workdir)
        first = b'{"ts":"2026-09-01T00:00:00Z","project":"p","status":"clean"}\n'
        last = b'{"ts":"2026-09-02T00:00:00Z","project":"p","status":"clean"}\n'
        _write_bytes(path, first + GARBAGE + b"\n" + last)

        rows = list_rows(tmp_workdir)

        assert [row["ts"] for row in rows] == ["2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z"]

    def test_a_garbage_first_line_keeps_the_rest_of_the_file(self, tmp_workdir):
        path = self._ledger(tmp_workdir)
        good = b'{"ts":"2026-09-02T00:00:00Z","project":"p","status":"clean"}\n'
        _write_bytes(path, GARBAGE + b"\n" + good)

        rows = list_rows(tmp_workdir)

        assert [row["ts"] for row in rows] == ["2026-09-02T00:00:00Z"]

    def test_a_read_does_not_rewrite_the_ledger(self, tmp_workdir):
        path = self._ledger(tmp_workdir)
        payload = GARBAGE + b"\n"
        _write_bytes(path, payload)

        list_rows(tmp_workdir)

        assert _read_bytes(path) == payload

    def test_cli_qa_list_survives_a_binary_corrupted_ledger(self, tmp_workdir):
        path = self._ledger(tmp_workdir)
        _write_bytes(
            path,
            b'{"ts":"2026-09-02T00:00:00Z","project":"p","status":"clean"}\n' + GARBAGE + b"\n",
        )

        result = _run_cli(tmp_workdir, "qa", "list")

        _assert_no_traceback(result)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "2026-09-02T00:00:00Z" in result.stdout


# ── .gitreins/history/**/verdict.json ──────────────────────────


class TestVerdictHistoryRestart:
    def _history(self, workdir: str) -> str:
        good_dir = os.path.join(workdir, ".gitreins", "history", "2026-09-01", "aaaa1111")
        bad_dir = os.path.join(workdir, ".gitreins", "history", "2026-09-02", "bbbb2222")
        os.makedirs(good_dir, exist_ok=True)
        os.makedirs(bad_dir, exist_ok=True)
        good = {
            "task_id": "good-task",
            "passed": True,
            "evaluated_at": "2026-09-01T00:00:00+00:00",
        }
        with open(os.path.join(good_dir, "verdict.json"), "w", encoding="utf-8") as fh:
            json.dump(good, fh)
        _write_bytes(os.path.join(bad_dir, "verdict.json"), GARBAGE)
        return bad_dir

    def test_an_undecodable_verdict_is_skipped_not_fatal(self, tmp_workdir):
        self._history(tmp_workdir)

        entries = VerdictPersister(tmp_workdir).list_verdicts(n=10)

        assert [entry["task_id"] for entry in entries] == ["good-task"]

    def test_report_renders_the_readable_verdicts_only(self, tmp_workdir):
        self._history(tmp_workdir)

        report = build_report(tmp_workdir, n=10)

        assert "good-task" in report
        assert "Traceback" not in report

    def test_cli_report_survives_a_corrupted_verdict_record(self, tmp_workdir):
        self._history(tmp_workdir)

        result = _run_cli(tmp_workdir, "report")

        _assert_no_traceback(result)
        assert result.returncode == 0, result.stdout + result.stderr


# ── .gitreins/config.yaml ──────────────────────────────────────


class TestConfigRestart:
    def test_unreadable_config_falls_back_to_the_built_in_defaults(self, tmp_workdir):
        path = _state_path(tmp_workdir, "config.yaml")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _write_bytes(path, GARBAGE)

        defaults = load_defaults(tmp_workdir)

        assert defaults.max_concurrent_worktrees > 0
        assert load_raw_config(tmp_workdir) == {}

    def test_config_validation_errors_still_raise(self, tmp_workdir):
        """Corruption is tolerated; a wrong VALUE is still the operator's to fix."""
        path = _state_path(tmp_workdir, "config.yaml")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("worktree_fleet:\n  max_concurrent_worktrees: 0\n")

        with pytest.raises(ValueError):
            load_defaults(tmp_workdir)


# ── .gitreins/disposable.json ──────────────────────────────────


class TestDisposableRegistryRestart:
    def test_garbage_reads_as_an_empty_registry_and_is_left_alone(self, tmp_workdir):
        path = _state_path(tmp_workdir, "disposable.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        _write_bytes(path, GARBAGE)

        assert _load_disposable_file(Path(path)) == []

        assert _read_bytes(path) == GARBAGE


# ── whole-harness restart ──────────────────────────────────────


def test_guard_restarts_with_every_state_file_corrupted(tmp_workdir):
    """A guarded commit still gets a defined exit code, never a traceback."""
    _corrupt_tasks_yaml(tmp_workdir)
    _write_bytes(_state_path(tmp_workdir, "config.yaml"), GARBAGE)
    _write_bytes(_state_path(tmp_workdir, "qa-ledger.jsonl"), GARBAGE + b"\n")

    result = _run_cli(tmp_workdir, "guard", "--full")

    _assert_no_traceback(result)
    assert result.returncode in (0, 1, 2), result.stdout + result.stderr
