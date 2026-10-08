"""Hermetic tests for MCP verdict persistence (DF-GITREINS-POC-23).

An MCP-dispatched evaluation used to write only its job record, so
``gitreins serve`` / ``gitreins report`` — which read
``<workdir>/.gitreins/history`` — never showed the run. These tests assert the
REAL artefact on disk (a parsed ``verdict.json`` carrying the task id, the
graded items and the producing job id), not a call count, on both MCP paths
(async job and ``judge.evaluate wait=true``), plus the non-fatal/disabled
contracts and the CLI↔MCP parity that keeps the two surfaces from drifting.

Hermetic by construction: ``Judge.evaluate_task`` is stubbed (no LLM, no test
suite run) and every ambient provider credential is popped while the base URL
points at a dead loopback port (``http://127.0.0.1:9/v1``), so a test that
somehow reached a real provider would fail fast instead of billing.
"""

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path

import pytest

import engine.persist
from engine.job_store import load_job
from engine.judge import Judge
from gitreins_mcp.server import GitReinsMCPServer

# Every env var engine.llm.LLMClient will accept as a credential.
_CREDENTIAL_ENV_VARS = (
    "GITREINS_LLM_API_KEY",
    "NEURALWATT_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "DEEPSEEK_API_KEY",
    "KIMI_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
)

_DEAD_LOOPBACK_BASE_URL = "http://127.0.0.1:9/v1"
# Deliberately NOT credential-shaped: the MCP paths need a truthy api_key to
# dispatch an evaluation at all, but a placeholder like "sk-…" is exactly what
# the secrets guard exists to catch, and allowlisting this file for it would
# widen the scanner's blind spot for no gain. The base URL above is a dead
# loopback port, so a test that somehow reached a real provider would fail
# instantly instead of billing.
_FAKE_KEY = "placeholder-not-a-credential"


@pytest.fixture(autouse=True)
def hermetic_llm_env(monkeypatch):
    """No ambient credential; base URL pinned at a dead loopback port."""
    for var in _CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GITREINS_LLM_BASE_URL", _DEAD_LOOPBACK_BASE_URL)
    monkeypatch.setenv("GITREINS_LLM_API_KEY", _FAKE_KEY)


# ── Stubs (no LLM, no suite) ────────────────────────────────────────────────


class _FakeTier1:
    passed = True


class _FakeItem:
    def __init__(self, criterion, status, detail):
        self.criterion = criterion
        self.status = status
        self.detail = detail


class _FakeTier2:
    def __init__(self):
        self.verdict = "COMPLETE"
        self.items = [_FakeItem("c1", "PASS", "ok")]
        self.summary = "all criteria met"


class _FakeJudgeResult:
    """Stand-in for engine.judge.JudgeResult.

    ``verdict`` is set the way ``Judge.evaluate_task`` sets it
    (engine/judge.py: ``result.verdict = tier2``) — the persisted verdict data
    is built from ``result.verdict``, so a fake that leaves it ``None`` would
    make the items assertion vacuous.
    """

    def __init__(self, passed=True):
        self.passed = passed
        self.tier1 = _FakeTier1()
        self.tier2 = _FakeTier2()
        self.verdict = self.tier2
        self.pipeline_result = {}

    @property
    def summary(self):
        return "Judge Result: stub\n\nTier 2 (Agentic Evaluator): COMPLETE\nOverall: PASS ✓"


def _stub_judge_evaluate(monkeypatch, passed=True):
    def _fake(self, task, **kwargs):
        return _FakeJudgeResult(passed=passed)

    monkeypatch.setattr(Judge, "evaluate_task", _fake)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _init_real_git_repo(tmp_path):
    """Init a real repo with one commit (the `storage: git` path needs it)."""
    repo = tmp_path / "real-repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "GitReins Tests"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "gitreins-tests@example.invalid"],
        check=True,
    )
    (repo / "base.txt").write_text("base\n")
    subprocess.run(["git", "-C", str(repo), "add", "base.txt"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "initial"], check=True)
    return str(repo)


def _write_history_config(workdir, body):
    cfg_dir = os.path.join(workdir, ".gitreins")
    os.makedirs(cfg_dir, exist_ok=True)
    with open(os.path.join(cfg_dir, "config.yaml"), "w") as f:
        f.write(body)


def _verdict_files(workdir):
    """Every verdict.json under <workdir>/.gitreins/history."""
    return sorted(_history_root(workdir).glob("*/*/verdict.json"), key=str)


def _history_root(workdir):
    return Path(workdir, ".gitreins", "history")


def _verdict_records(workdir):
    """Every verdict entry as ``(path, parsed verdict.json)``."""
    return [(p, json.loads(p.read_text())) for p in _verdict_files(workdir)]


def _record_path(workdir, entry_path):
    """The workdir-relative, "/"-joined path a verdict record points at.

    ``supersedes``/``superseded_by`` name the sibling record's ENTRY DIRECTORY
    (``.../history/<date>/<hash>``) in the same shape it has on the ``gitreins``
    branch; *entry_path* is the verdict.json path inside it.
    """
    return Path(entry_path).parent.relative_to(workdir).as_posix()


def _live_records(workdir, job_id):
    """Records for *job_id* that no later attempt has superseded."""
    return [
        (p, d)
        for p, d in _verdict_records(workdir)
        if d.get("job_id") == job_id and not d.get("superseded_by")
    ]


def _mcp_call(server, name, arguments):
    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    assert "result" in response, f"tools/call {name} failed: {response}"
    return json.loads(response["result"]["content"][0]["text"])


def _create_task(server, task_id):
    _mcp_call(server, "task.create", {"id": task_id, "title": task_id, "criteria": ["c1"]})


def _poll_status(server, job_id, deadline=5.0):
    end = time.monotonic() + deadline
    last = None
    while time.monotonic() < end:
        last = _mcp_call(server, "judge.status", {"job_id": job_id})
        if last["status"] in ("complete", "error"):
            return last
        time.sleep(0.02)
    pytest.fail(f"job {job_id} did not finish within {deadline}s — last status: {last}")


@pytest.fixture
def mcp_server(tmp_workdir, monkeypatch):
    server = GitReinsMCPServer(tmp_workdir)
    monkeypatch.setattr(server.llm, "api_key", _FAKE_KEY)
    return server


# ── Async MCP path ──────────────────────────────────────────────────────────


class TestMcpAsyncVerdictPersistence:
    """`task.complete` / `judge.evaluate wait=false` land a browsable verdict."""

    def test_task_complete_writes_verdict_json_with_job_id(self, mcp_server, monkeypatch):
        _stub_judge_evaluate(monkeypatch)
        _create_task(mcp_server, "mcp-async-persist")

        dispatched = _mcp_call(mcp_server, "task.complete", {"id": "mcp-async-persist"})
        job_id = dispatched["job_id"]
        final = _poll_status(mcp_server, job_id)
        assert final["status"] == "complete"

        files = _verdict_files(mcp_server.workdir)
        assert len(files) == 1, f"expected exactly one verdict, found {files}"
        path = files[0]
        # <workdir>/.gitreins/history/<YYYY-MM-DD>/<short-hash>/verdict.json
        assert path.relative_to(_history_root(mcp_server.workdir)).parts[0:1]
        entry_hash = path.parent.name
        date_dir = path.parent.parent.name
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_dir), date_dir
        assert re.fullmatch(r"[0-9a-f]{8}", entry_hash), entry_hash
        # The browsable unit is self-contained: summary.md sits beside it.
        assert path.with_name("summary.md").is_file()

        verdict = json.loads(path.read_text())
        assert verdict["task_id"] == "mcp-async-persist"
        assert verdict["passed"] is True
        assert verdict["items"] == [{"criterion": "c1", "status": "PASS", "detail": "ok"}]
        assert "Tier 2 (Agentic Evaluator): COMPLETE" in verdict["summary"]
        # The provenance that made an MCP run invisible before: the job id.
        assert verdict["job_id"] == job_id
        assert verdict["source"] == "mcp"

    def test_verdict_lands_before_the_job_reads_complete(self, mcp_server, monkeypatch):
        """Invariant: a job that reads terminal `complete` has its verdict on disk.

        The spy samples the job store at the exact moment persistence runs, so
        an implementation that finished the job first and persisted afterwards
        fails here even though the verdict eventually exists.
        """
        _stub_judge_evaluate(monkeypatch)
        _create_task(mcp_server, "mcp-ordering")
        real_persist = engine.persist.persist_evaluation
        observed = {}

        def _spy(workdir, task, result, **kwargs):
            observed["job_status_at_persist"] = load_job(job_id)["status"]
            observed["verdict_files_at_persist"] = len(_verdict_files(workdir))
            return real_persist(workdir, task, result, **kwargs)

        monkeypatch.setattr(engine.persist, "persist_evaluation", _spy)
        job_id = _mcp_call(mcp_server, "task.complete", {"id": "mcp-ordering"})["job_id"]
        assert _poll_status(mcp_server, job_id)["status"] == "complete"

        assert observed["job_status_at_persist"] == "running"
        # The persister writes the verdict during that very call.
        assert observed["verdict_files_at_persist"] == 0
        assert len(_verdict_files(mcp_server.workdir)) == 1

    def test_async_persist_writes_nothing_to_stdout(self, mcp_server, monkeypatch, capsys):
        """The MCP stdout is the JSON-RPC channel — persistence must stay silent."""
        _stub_judge_evaluate(monkeypatch)
        _create_task(mcp_server, "mcp-quiet")
        capsys.readouterr()  # discard anything written before the job

        job_id = _mcp_call(mcp_server, "task.complete", {"id": "mcp-quiet"})["job_id"]
        assert _poll_status(mcp_server, job_id)["status"] == "complete"
        captured = capsys.readouterr()
        assert captured.out == ""
        assert len(_verdict_files(mcp_server.workdir)) == 1

    def test_async_persist_commits_verdict_to_the_history_ref(self, tmp_path, monkeypatch):
        """`storage: git` (the shipped default) commits the verdict, MCP side.

        The committed path is read from the real tree (the first verdict's root
        commit holds bare ``verdict.json``/``summary.md`` at the root; a later
        verdict nests them under ``<date>/<hash>/``), so this proves the
        ARGUMENT the persister hands git, not the test's copy of the path
        prefix. The ref is read by its full name (DF-GITREINS-POC-52: it lives
        outside refs/heads so the fleet's ``gitreins/task/<id>`` branches can
        never prefix-collide with it).
        """
        repo = _init_real_git_repo(tmp_path)
        server = GitReinsMCPServer(repo)
        monkeypatch.setattr(server.llm, "api_key", _FAKE_KEY)
        _stub_judge_evaluate(monkeypatch)
        _create_task(server, "mcp-git-storage")

        job_id = _mcp_call(server, "task.complete", {"id": "mcp-git-storage"})["job_id"]
        assert _poll_status(server, job_id)["status"] == "complete"

        branch = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--verify", engine.persist.HISTORY_REF],
            capture_output=True,
            text=True,
        )
        assert branch.returncode == 0, "verdict was not committed to the history ref"
        listing = subprocess.run(
            ["git", "-C", repo, "ls-tree", "-r", "--name-only", engine.persist.HISTORY_REF],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        committed = [p for p in listing.splitlines() if p.endswith("verdict.json")]
        assert len(committed) == 1, listing
        blob = subprocess.run(
            ["git", "-C", repo, "show", f"{engine.persist.HISTORY_REF}:{committed[0]}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        stored = json.loads(blob)
        assert stored["job_id"] == job_id
        assert stored["task_id"] == "mcp-git-storage"
        assert stored["source"] == "mcp"
        # summary.md rides in the same commit — the browsable unit is complete.
        assert any(p.endswith("summary.md") for p in listing.splitlines()), listing


# ── Sync MCP path ───────────────────────────────────────────────────────────


class TestMcpSyncVerdictPersistence:
    def test_wait_true_persists_with_sync_marker(self, mcp_server, monkeypatch):
        _stub_judge_evaluate(monkeypatch)
        _create_task(mcp_server, "mcp-sync-persist")

        result = _mcp_call(mcp_server, "judge.evaluate", {"id": "mcp-sync-persist", "wait": True})
        assert result["task_id"] == "mcp-sync-persist"
        assert result["passed"] is True

        files = _verdict_files(mcp_server.workdir)
        assert len(files) == 1, f"expected exactly one verdict, found {files}"
        verdict = json.loads(files[0].read_text())
        assert verdict["task_id"] == "mcp-sync-persist"
        assert verdict["job_id"] is None
        assert verdict["source"] == "mcp-sync"
        assert verdict["items"] == [{"criterion": "c1", "status": "PASS", "detail": "ok"}]

    def test_response_shape_unchanged_by_persistence(self, mcp_server, monkeypatch):
        """Additive change: the sync result dict keeps its documented keys."""
        _stub_judge_evaluate(monkeypatch)
        _create_task(mcp_server, "mcp-shape")

        result = _mcp_call(mcp_server, "judge.evaluate", {"id": "mcp-shape", "wait": True})
        assert set(result.keys()) == {
            "task_id",
            "passed",
            "workdir",
            "tier1_passed",
            "verdict",
            "items",
            "summary",
        }
        # Non-vacuous: the documented shape and the persisted verdict coexist.
        assert len(_verdict_files(mcp_server.workdir)) == 1


# ── Non-fatal / disabled contracts ──────────────────────────────────────────


class TestPersistenceFailureIsNonFatal:
    def test_async_job_still_completes_when_persist_raises(self, mcp_server, monkeypatch, caplog):
        _stub_judge_evaluate(monkeypatch)
        _create_task(mcp_server, "mcp-boom")

        def _boom(self, task_id, verdict_data, collect_evidence=None):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(engine.persist.VerdictPersister, "persist", _boom)
        with caplog.at_level(logging.WARNING, logger="gitreins.persist"):
            dispatched = _mcp_call(mcp_server, "task.complete", {"id": "mcp-boom"})
            assert set(dispatched.keys()) == {"task", "job_id", "status", "note"}
            final = _poll_status(mcp_server, dispatched["job_id"])

        assert final["status"] == "complete"
        assert final["result"]["passed"] is True
        assert final["result"]["items"] == [{"criterion": "c1", "status": "PASS", "detail": "ok"}]
        assert _verdict_files(mcp_server.workdir) == []
        assert any(
            "mcp-boom" in r.getMessage() or "non-fatal" in r.getMessage() for r in caplog.records
        ), [r.getMessage() for r in caplog.records]

    def test_sync_call_returns_result_when_persist_raises(self, mcp_server, monkeypatch, caplog):
        _stub_judge_evaluate(monkeypatch)
        _create_task(mcp_server, "mcp-boom-sync")

        def _boom(self, task_id, verdict_data, collect_evidence=None):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(engine.persist.VerdictPersister, "persist", _boom)
        with caplog.at_level(logging.WARNING, logger="gitreins.persist"):
            result = _mcp_call(mcp_server, "judge.evaluate", {"id": "mcp-boom-sync", "wait": True})

        assert result["passed"] is True
        assert result["task_id"] == "mcp-boom-sync"
        assert _verdict_files(mcp_server.workdir) == []
        assert any("non-fatal" in r.getMessage() for r in caplog.records)


class TestHistoryDisabled:
    def test_disabled_history_writes_no_history_dir_but_completes(self, mcp_server, monkeypatch):
        """Both arms in one test: the MCP path DOES persist, and honours the opt-out.

        Order matters — the enabled arm runs first, so the absence of a verdict
        in the disabled arm is the opt-out and not an MCP path that never
        persists at all.
        """
        _stub_judge_evaluate(monkeypatch)

        # Arm 1: history enabled (the shipped default) → a verdict lands.
        assert engine.persist.VerdictPersister(mcp_server.workdir).enabled is True
        _create_task(mcp_server, "mcp-enabled-arm")
        enabled_job = _mcp_call(mcp_server, "task.complete", {"id": "mcp-enabled-arm"})["job_id"]
        assert _poll_status(mcp_server, enabled_job)["status"] == "complete"
        assert len(_verdict_files(mcp_server.workdir)) == 1

        # Arm 2: history disabled → the job still completes, nothing is written.
        _write_history_config(mcp_server.workdir, "history:\n  enabled: false\n  max_verdicts: 0\n")
        assert engine.persist.VerdictPersister(mcp_server.workdir).enabled is False
        _create_task(mcp_server, "mcp-disabled")
        job_id = _mcp_call(mcp_server, "task.complete", {"id": "mcp-disabled"})["job_id"]
        final = _poll_status(mcp_server, job_id)

        assert final["status"] == "complete"
        assert final["result"]["items"] == [{"criterion": "c1", "status": "PASS", "detail": "ok"}]
        # Nothing was added by the disabled run: the only verdict is arm 1's.
        verdicts = [json.loads(p.read_text()) for p in _verdict_files(mcp_server.workdir)]
        assert [v["task_id"] for v in verdicts] == ["mcp-enabled-arm"]


# ── CLI ↔ MCP parity ────────────────────────────────────────────────────────


class TestCliUsesSharedHelper:
    def test_cli_persist_result_delegates_to_shared_helper(self, tmp_workdir, monkeypatch, capsys):
        """The CLI persists through the SAME helper — the surfaces cannot drift."""
        from gitreins.cli import _persist_result

        calls = []
        task = type("T", (), {"id": "cli-parity", "title": "t", "criteria": ["c"]})()
        result = type(
            "R", (), {"passed": True, "verdict": None, "pipeline_result": {}, "summary": "s"}
        )()

        def _spy(workdir, seen_task, seen_result, **kwargs):
            calls.append((os.path.abspath(workdir), seen_task, seen_result, kwargs))
            return "deadbeef"

        monkeypatch.setattr(engine.persist, "persist_evaluation", _spy)
        _persist_result(tmp_workdir, task, result)

        assert len(calls) == 1, "cli._persist_result did not use the shared helper"
        workdir, seen_task, seen_result, kwargs = calls[0]
        assert workdir == os.path.abspath(tmp_workdir)
        assert seen_task is task and seen_result is result
        assert callable(kwargs.get("collect_evidence"))

        # Console behaviour is preserved: the CLI still reports the hash.
        captured = capsys.readouterr()
        assert "📋 Verdict saved: deadbeef" in captured.out

    def test_cli_reports_the_shared_helpers_error_result(self, tmp_workdir, monkeypatch, capsys):
        """A helper-level failure keeps today's non-fatal CLI warning."""
        from gitreins.cli import _persist_result

        monkeypatch.setattr(engine.persist, "persist_evaluation", lambda *a, **k: "error")
        task = type("T", (), {"id": "cli-err", "title": "t", "criteria": []})()
        result = type(
            "R", (), {"passed": False, "verdict": None, "pipeline_result": {}, "summary": "s"}
        )()
        _persist_result(tmp_workdir, task, result)

        captured = capsys.readouterr()
        assert "Failed to persist verdict (non-fatal)" in captured.err

    def test_cli_skips_persistence_when_history_disabled(self, tmp_workdir):
        """Both directions: enabled → a verdict lands; disabled → it does not.

        Real (unstubbed) helper on both arms, asserting only the on-disk
        artefact. No spy and no call count: entry directories are named
        ``<date>/<sha8(task_id:evaluated_at)>`` with microsecond timestamps, so
        every real persist lands a fresh ``verdict.json`` and any extra one —
        from this test's own arms or a stale background thread from an earlier
        test in this xdist worker — would move the count off the asserted value.
        """
        from gitreins.cli import _persist_result

        task = type("T", (), {"id": "cli-toggle", "title": "t", "criteria": ["c"]})()
        result = type(
            "R", (), {"passed": True, "verdict": None, "pipeline_result": {}, "summary": "s"}
        )()

        # Arm 1: enabled (default) — a verdict lands on disk.
        _persist_result(tmp_workdir, task, result)
        assert len(_verdict_files(tmp_workdir)) == 1

        # Arm 2: disabled — the CLI short-circuits, no second verdict.
        _write_history_config(tmp_workdir, "history:\n  enabled: false\n")
        _persist_result(tmp_workdir, task, result)
        assert len(_verdict_files(tmp_workdir)) == 1


class TestVerdictDataBuilder:
    """Unit coverage for the shared builder moved out of the CLI."""

    def test_empty_branch_and_worktree_for_non_git_workdir(self, tmp_path, monkeypatch):
        from engine.persist import build_verdict_data

        non_git = tmp_path / "plain"
        non_git.mkdir()
        data = build_verdict_data(
            str(non_git),
            type("T", (), {"id": "t", "title": "ti", "criteria": ["c"]})(),
            _FakeJudgeResult(),
        )
        assert data["task_id"] == "t"
        assert data["worktree"] == str(non_git)
        assert data["branch"] == ""
        assert data["commit"] == ""
        assert data["items"] == [{"criterion": "c1", "status": "PASS", "detail": "ok"}]

    def test_extra_keys_are_stamped_by_persist_evaluation(self, tmp_workdir, monkeypatch):
        from engine.persist import persist_evaluation

        captured = {}

        def _spy(self, task_id, verdict_data, collect_evidence=None):
            captured.update(verdict_data)
            return "deadbeef"

        monkeypatch.setattr(engine.persist.VerdictPersister, "persist", _spy)
        task = type("T", (), {"id": "stamped", "title": "t", "criteria": []})()
        out = persist_evaluation(
            tmp_workdir, task, _FakeJudgeResult(), extra={"job_id": "job-1", "source": "mcp"}
        )
        assert out == "deadbeef"
        assert captured["job_id"] == "job-1"
        assert captured["source"] == "mcp"
        assert captured["task_id"] == "stamped"


# ── Resume supersedes the interrupted attempt (DF-GITREINS-POC-26) ───────────


class TestResumeSupersedesInterruptedVerdict:
    """A resumed job re-dispatches under the SAME job id.

    The history entry is keyed on timestamp + task id, so the resume used to
    append a SECOND verdict for one logical run: two records carrying the same
    job_id, neither labelled, and a consumer joining history to the job store
    could not tell which record was graded. The persister now supersedes the
    earlier attempt for that job id instead of leaving both looking live.
    """

    def test_resume_leaves_exactly_one_live_record_per_job_id(self, mcp_server, monkeypatch):
        from engine.job_store import make_job, save_job

        _stub_judge_evaluate(monkeypatch)
        _create_task(mcp_server, "mcp-resume")
        task = mcp_server.tasks.get("mcp-resume")

        # The interrupted attempt, fabricated exactly as a crashed server leaves
        # it: the verdict already persisted, the job record still `running` with
        # a dead owner — the state the next `judge.status` poll resumes.
        job = make_job("mcp-resume", mcp_server.workdir)
        job["pid"] = 99999999  # dead pid
        save_job(job)
        job_id = job["id"]
        assert engine.persist.persist_evaluation(
            mcp_server.workdir,
            task,
            _FakeJudgeResult(),
            extra={"job_id": job_id, "source": "mcp"},
        ) not in ("error", "disabled")

        interrupted_path, interrupted = _verdict_records(mcp_server.workdir)[0]
        assert interrupted["job_id"] == job_id
        assert interrupted.get("superseded_by") is None

        # The orphaned job is resumed in this server instance and persists its
        # own verdict under the SAME job id.
        assert _mcp_call(mcp_server, "judge.status", {"job_id": job_id})["status"] == "running"
        assert _poll_status(mcp_server, job_id)["status"] == "complete"

        records = _verdict_records(mcp_server.workdir)
        assert len(records) == 2, [str(p) for p, _ in records]
        live = _live_records(mcp_server.workdir, job_id)
        assert len(live) == 1, f"expected one live record for {job_id}, got {live}"
        resumed_path, resumed = live[0]
        assert resumed_path != interrupted_path

        # The superseded attempt is identifiable in BOTH directions...
        assert resumed["supersedes"] == _record_path(mcp_server.workdir, interrupted_path)
        reread = json.loads(interrupted_path.read_text())
        assert reread["superseded_by"] == _record_path(mcp_server.workdir, resumed_path)
        assert isinstance(reread["superseded_at"], str) and reread["superseded_at"]
        # ...and it is labelled, not deleted: its own evidence survives.
        assert interrupted_path.is_file()
        assert reread["job_id"] == job_id
        assert reread["source"] == "mcp"
        assert reread["items"] == [{"criterion": "c1", "status": "PASS", "detail": "ok"}]
        # The live record is the resumed run's: same provenance, no marker.
        assert resumed["job_id"] == job_id
        assert resumed["source"] == "mcp"
        assert resumed.get("superseded_by") is None

    def test_supersede_is_keyed_on_job_id(self, tmp_workdir):
        """Only the SAME job id supersedes — the scan is not a blanket rewrite.

        A second job (and a sync record with no job id at all) must stay live,
        otherwise the marker would erase unrelated runs from every consumer's
        view of history.
        """
        persister = engine.persist.VerdictPersister(tmp_workdir)
        persister.config["storage"] = "filesystem"
        persister.config["max_verdicts"] = 0

        for job_id, passed in (("job-a", True), ("job-b", True), (None, True), ("job-a", False)):
            persister.persist(
                "resume-keyed",
                {
                    "passed": passed,
                    "job_id": job_id,
                    "source": "mcp" if job_id else "mcp-sync",
                },
            )

        records = _verdict_records(tmp_workdir)
        assert len(records) == 4

        def _live(job_id):
            return [
                (p, d)
                for p, d in records
                if d.get("job_id") == job_id and not d.get("superseded_by")
            ]

        live_a = _live("job-a")
        assert len(live_a) == 1, live_a
        assert live_a[0][1]["passed"] is False  # the newest attempt for job-a
        assert len(_live("job-b")) == 1
        assert len(_live(None)) == 1
        assert len([p for p, d in records if not d.get("superseded_by")]) == 3

        superseded = [(p, d) for p, d in records if d.get("superseded_by")]
        assert len(superseded) == 1, superseded
        assert superseded[0][1]["passed"] is True  # job-a's first attempt
        assert superseded[0][1]["superseded_by"] == _record_path(tmp_workdir, live_a[0][0])

    def test_chain_of_resumes_leaves_one_live_record(self, tmp_workdir):
        """Three attempts at one job id: one live record, each older one chained
        to the attempt that replaced it."""
        persister = engine.persist.VerdictPersister(tmp_workdir)
        persister.config["storage"] = "filesystem"
        persister.config["max_verdicts"] = 0

        for passed in (True, False, True):
            persister.persist(
                "resume-chain", {"passed": passed, "job_id": "job-x", "source": "mcp"}
            )

        records = _verdict_records(tmp_workdir)
        assert len(records) == 3
        live = [p for p, d in records if not d.get("superseded_by")]
        assert len(live) == 1
        # Both older records name their successor, and the two successors name
        # their predecessor — the chain is walkable in either direction.
        assert len({d["superseded_by"] for _p, d in records if d.get("superseded_by")}) == 2
        assert len([1 for _p, d in records if d.get("supersedes")]) == 2
        assert _live_records(tmp_workdir, "job-x")[0][0] == live[0]
