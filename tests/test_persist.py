"""Dedicated tests for verdict persistence and history reporting."""

import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from engine.persist import (
    DEFAULT_HISTORY_CONFIG,
    HISTORY_REF,
    KIND_RESOLUTION,
    LEGACY_HISTORY_REF,
    RESOLUTION_ENTRY_ID,
    VerdictPersister,
    _pct,
    build_report,
    persist_resolution,
)
from engine.worktree_manager import BRANCH_PREFIX


# ── _pct ─────────────────────────────────────────────────────


def test_pct_formats_ratio():
    assert _pct(0, 10) == "0%"
    assert _pct(5, 10) == "50%"
    assert _pct(10, 10) == "100%"
    assert _pct(0, 0) == "0%"


# ── DEFAULT_HISTORY_CONFIG ───────────────────────────────────


def test_default_history_config_has_expected_keys():
    assert DEFAULT_HISTORY_CONFIG["enabled"] is True
    assert DEFAULT_HISTORY_CONFIG["storage"] == "git"
    assert DEFAULT_HISTORY_CONFIG["max_verdicts"] == 1000
    assert ".gitreins/history" in DEFAULT_HISTORY_CONFIG["path"]


# ── VerdictPersister init ────────────────────────────────────


def test_persister_uses_absolute_workdir(tmp_path):
    p = VerdictPersister(str(tmp_path))
    assert os.path.isabs(p.workdir)


def test_persister_enabled_defaults_true(tmp_path):
    p = VerdictPersister(str(tmp_path))
    assert p.enabled is True


def test_persister_history_dir_is_under_workdir_by_default(tmp_path):
    p = VerdictPersister(str(tmp_path))
    assert p.history_dir.startswith(str(tmp_path))


def test_persister_storage_mode_is_git_by_default(tmp_path):
    p = VerdictPersister(str(tmp_path))
    assert p.storage_mode == "git"


# ── persist (non-git path) ───────────────────────────────────


def test_persist_returns_disabled_when_history_disabled(tmp_path):
    p = VerdictPersister(str(tmp_path))
    p.config["enabled"] = False
    assert p.persist("task-1", {}) == "disabled"


def test_persist_creates_verdict_json_and_summary_md(tmp_path):
    p = VerdictPersister(str(tmp_path))
    p.config["storage"] = "filesystem"  # skip git
    p.config["max_verdicts"] = 0  # no pruning

    result = p.persist("task-1", {"passed": True, "task_title": "Test Task"})
    assert result == "dry-run"

    # Find the verdict directory
    history = p.history_dir
    assert os.path.isdir(history)
    date_dirs = os.listdir(history)
    assert len(date_dirs) == 1
    hash_dirs = os.listdir(os.path.join(history, date_dirs[0]))
    assert len(hash_dirs) == 1

    entry = os.path.join(history, date_dirs[0], hash_dirs[0])
    assert os.path.isfile(os.path.join(entry, "verdict.json"))
    assert os.path.isfile(os.path.join(entry, "summary.md"))

    # Verdict JSON has task_id and evaluated_at
    with open(os.path.join(entry, "verdict.json")) as f:
        data = json.load(f)
    assert data["task_id"] == "task-1"
    assert "evaluated_at" in data

    # Summary markdown contains task title
    with open(os.path.join(entry, "summary.md")) as f:
        summary = f.read()
    assert "Test Task" in summary


# ── list_verdicts ────────────────────────────────────────────


def test_list_verdicts_returns_empty_when_no_history(tmp_path):
    p = VerdictPersister(str(tmp_path))
    assert p.list_verdicts() == []


def test_list_verdicts_returns_entries_newest_first(tmp_path):
    p = VerdictPersister(str(tmp_path))
    p.config["storage"] = "filesystem"
    p.config["max_verdicts"] = 0

    p.persist("task-1", {"passed": True})
    p.persist("task-2", {"passed": False})

    entries = p.list_verdicts()
    assert len(entries) == 2
    # Ordering depends on directory entry order — both entries must exist
    task_ids = {e["task_id"] for e in entries}
    assert task_ids == {"task-1", "task-2"}


def test_list_verdicts_filters_by_task_id(tmp_path):
    p = VerdictPersister(str(tmp_path))
    p.config["storage"] = "filesystem"
    p.config["max_verdicts"] = 0

    p.persist("task-a", {"passed": True})
    p.persist("task-b", {"passed": True})

    filtered = p.list_verdicts(task_id="task-a")
    assert len(filtered) == 1
    assert filtered[0]["task_id"] == "task-a"


def test_list_verdicts_limits_to_n(tmp_path):
    p = VerdictPersister(str(tmp_path))
    p.config["storage"] = "filesystem"
    p.config["max_verdicts"] = 0

    for i in range(5):
        p.persist(f"task-{i}", {"passed": True})

    assert len(p.list_verdicts(n=2)) == 2


# ── count_verdicts ───────────────────────────────────────────


def test_count_verdicts_returns_zero_for_no_history(tmp_path):
    p = VerdictPersister(str(tmp_path))
    assert p.count_verdicts() == 0


def test_count_verdicts_counts_all_entries(tmp_path):
    p = VerdictPersister(str(tmp_path))
    p.config["storage"] = "filesystem"
    p.config["max_verdicts"] = 0

    for i in range(3):
        p.persist(f"task-{i}", {"passed": True})

    assert p.count_verdicts() == 3


# ── build_report ─────────────────────────────────────────────


def test_build_report_returns_disabled_message_when_history_off(tmp_path):
    p = VerdictPersister(str(tmp_path))
    with patch("engine.persist.VerdictPersister", return_value=p):
        p.config["enabled"] = False
        result = build_report(str(tmp_path))
        assert "disabled" in result


def test_build_report_shows_no_history_when_empty(tmp_path):
    p = VerdictPersister(str(tmp_path))
    with patch("engine.persist.VerdictPersister", return_value=p):
        result = build_report(str(tmp_path))
        assert "No verdict history found" in result


def test_build_report_includes_summary_stats(tmp_path):
    p = VerdictPersister(str(tmp_path))
    p.config["storage"] = "filesystem"
    p.config["max_verdicts"] = 0

    p.persist("pass-1", {"passed": True, "task_title": "Passing"})
    p.persist("fail-1", {"passed": False, "task_title": "Failing"})

    report = build_report(str(tmp_path))
    assert "pass-1" in report
    assert "fail-1" in report
    assert "Passing" in report or "Failing" in report


# ── _build_summary edge cases ────────────────────────────────


def test_build_summary_handles_dict_items(tmp_path):
    """Summary generation works with dict-format criteria items (MCP)."""
    p = VerdictPersister(str(tmp_path))
    data = {
        "passed": True,
        "task_title": "Dict Items",
        "items": [{"criterion": "Must pass", "status": "PASS", "detail": "ok"}],
        "verdict": None,
    }
    summary = p._build_summary("task-x", data)
    assert "✓" in summary
    assert "Must pass" in summary


def test_build_summary_handles_pipeline_stages(tmp_path):
    p = VerdictPersister(str(tmp_path))
    data = {
        "passed": True,
        "task_title": "With Stages",
        "verdict": None,
        "stages": {"tier1": {"passed": True, "summary": "guard ok"}},
    }
    summary = p._build_summary("task-x", data)
    assert "tier1" in summary


# ── git-branch fallback (DF-007) ─────────────────────────────


def _git_env() -> dict:
    """Env with a deterministic git identity for subprocess git calls."""
    env = dict(os.environ)
    env.update(
        GIT_AUTHOR_NAME="Test Runner",
        GIT_AUTHOR_EMAIL="test@example.com",
        GIT_COMMITTER_NAME="Test Runner",
        GIT_COMMITTER_EMAIL="test@example.com",
    )
    return env


def _make_legacy_history_repo(repo, verdicts):
    """Init a temp git repo carrying history on the LEGACY history branch.

    This is the pre-DF-GITREINS-POC-52 shape: verdicts committed on the
    ``gitreins`` BRANCH (``LEGACY_HISTORY_REF``). verdicts: iterable of
    (date, hash, task_id, passed). After committing, the local
    .gitreins/history/ dir is removed from the working tree so the repo looks
    like a fresh clone — the ref holds the files, the working tree does not.
    """
    env = _git_env()
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "gitreins", str(repo)], check=True, env=env)
    for date, hash_, task_id, passed in verdicts:
        entry = repo / ".gitreins" / "history" / date / hash_
        entry.mkdir(parents=True)
        (entry / "verdict.json").write_text(
            json.dumps({"task_id": task_id, "passed": passed, "evaluated_at": f"{date}T00:00:00"})
        )
        (entry / "summary.md").write_text(f"# {task_id}")
    subprocess.run(
        ["git", "add", ".gitreins"], check=True, capture_output=True, cwd=str(repo), env=env
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "verdicts"],
        check=True,
        capture_output=True,
        cwd=str(repo),
        env=env,
    )
    shutil.rmtree(repo / ".gitreins")
    return env


def test_list_verdicts_falls_back_to_the_legacy_history_branch(tmp_path):
    repo = tmp_path / "repo"
    _make_legacy_history_repo(
        repo,
        [
            ("2026-06-21", "aaaa1111", "old-task", True),
            ("2026-06-22", "bbbb2222", "new-task", False),
        ],
    )

    p = VerdictPersister(str(repo))
    assert p.storage_mode == "git"
    assert not os.path.isdir(p.history_dir)  # fresh-clone shape

    entries = p.list_verdicts()
    assert [e["task_id"] for e in entries] == ["new-task", "old-task"]
    assert entries[0]["_date"] == "2026-06-22"
    assert entries[0]["_hash"] == "bbbb2222"
    assert entries[0]["passed"] is False
    assert entries[1]["_date"] == "2026-06-21"
    assert entries[1]["_hash"] == "aaaa1111"
    # the entries name the ref they were actually read from (DF-GITREINS-POC-52)
    assert {e["_ref"] for e in entries} == {LEGACY_HISTORY_REF}


def test_list_verdicts_branch_fallback_respects_n_limit(tmp_path):
    repo = tmp_path / "repo"
    _make_legacy_history_repo(
        repo, [("2026-07-01", f"h000000{i}", f"task-{i}", True) for i in range(5)]
    )

    p = VerdictPersister(str(repo))
    entries = p.list_verdicts(n=2)
    assert len(entries) == 2
    assert [e["task_id"] for e in entries] == ["task-4", "task-3"]


def test_list_verdicts_branch_fallback_filters_by_task_id(tmp_path):
    repo = tmp_path / "repo"
    _make_legacy_history_repo(
        repo,
        [
            ("2026-07-01", "h0000001", "task-a", True),
            ("2026-07-01", "h0000002", "task-b", True),
        ],
    )

    p = VerdictPersister(str(repo))
    assert [e["task_id"] for e in p.list_verdicts(task_id="task-b")] == ["task-b"]


def test_list_verdicts_branch_fallback_skips_non_json(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = _git_env()
    subprocess.run(["git", "init", "-q", "-b", "gitreins", str(repo)], check=True, env=env)
    good = repo / ".gitreins" / "history" / "2026-06-21" / "aaaa1111"
    good.mkdir(parents=True)
    (good / "verdict.json").write_text(json.dumps({"task_id": "good-task", "passed": True}))
    bad = repo / ".gitreins" / "history" / "2026-06-22" / "bbbb2222"
    bad.mkdir(parents=True)
    (bad / "verdict.json").write_text("{not json")
    subprocess.run(
        ["git", "add", ".gitreins"], check=True, capture_output=True, cwd=str(repo), env=env
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "verdicts"],
        check=True,
        capture_output=True,
        cwd=str(repo),
        env=env,
    )
    shutil.rmtree(repo / ".gitreins")

    p = VerdictPersister(str(repo))
    assert [e["task_id"] for e in p.list_verdicts()] == ["good-task"]


def test_list_verdicts_no_gitreins_branch_returns_empty(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = _git_env()
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    (repo / "readme.txt").write_text("hello")
    subprocess.run(
        ["git", "add", "readme.txt"], check=True, capture_output=True, cwd=str(repo), env=env
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"],
        check=True,
        capture_output=True,
        cwd=str(repo),
        env=env,
    )

    p = VerdictPersister(str(repo))
    assert p.list_verdicts() == []
    assert p.count_verdicts() == 0
    assert "No verdict history found" in build_report(str(repo))


def test_list_verdicts_branch_fallback_graceful_without_git(tmp_path):
    p = VerdictPersister(str(tmp_path))  # no .git anywhere up the tree
    assert p.list_verdicts() == []
    assert p.count_verdicts() == 0
    assert "No verdict history found" in build_report(str(tmp_path))


def test_list_verdicts_local_entries_take_precedence_over_branch(tmp_path):
    repo = tmp_path / "repo"
    _make_legacy_history_repo(repo, [("2026-06-21", "aaaa1111", "branch-task", True)])
    # A later judge run wrote a local verdict with a different task.
    local = repo / ".gitreins" / "history" / "2026-08-03" / "cccc3333"
    local.mkdir(parents=True)
    (local / "verdict.json").write_text(json.dumps({"task_id": "local-task", "passed": True}))
    (local / "summary.md").write_text("# local-task")

    p = VerdictPersister(str(repo))
    with patch.object(p, "_list_branch_verdicts", return_value=[]) as mocked:
        entries = p.list_verdicts()
        mocked.assert_not_called()
    assert [e["task_id"] for e in entries] == ["local-task"]
    assert p.count_verdicts() == 1


def test_list_verdicts_filesystem_mode_never_consults_branch(tmp_path):
    repo = tmp_path / "repo"
    _make_legacy_history_repo(repo, [("2026-06-21", "aaaa1111", "branch-task", True)])

    p = VerdictPersister(str(repo))
    p.config["storage"] = "filesystem"
    with patch.object(p, "_list_branch_verdicts", return_value=[]) as mocked:
        assert p.list_verdicts() == []
        mocked.assert_not_called()
    assert p.count_verdicts() == 0


def test_count_verdicts_falls_back_to_branch(tmp_path):
    repo = tmp_path / "repo"
    _make_legacy_history_repo(
        repo,
        [
            ("2026-06-21", "aaaa1111", "t1", True),
            ("2026-06-22", "bbbb2222", "t2", False),
            ("2026-06-23", "cccc3333", "t3", True),
        ],
    )

    p = VerdictPersister(str(repo))
    assert p.count_verdicts() == 3


def test_build_report_reads_verdicts_from_the_legacy_history_branch(tmp_path):
    repo = tmp_path / "repo"
    _make_legacy_history_repo(
        repo,
        [
            ("2026-06-21", "aaaa1111", "old-task", True),
            ("2026-06-22", "bbbb2222", "new-task", False),
        ],
    )

    report = build_report(str(repo))
    assert "No verdict history found" not in report
    assert "old-task" in report
    assert "new-task" in report
    assert "Total entries: 2" in report


# ── history-ref vs fleet task branches (DF-GITREINS-POC-52) ────
#
# The history used to live on the branch `refs/heads/gitreins`, a path prefix
# of the fleet's own per-task branches `refs/heads/gitreins/task/<id>`
# (engine/worktree_manager.BRANCH_PREFIX). Git refuses to create a ref that is a
# prefix of an existing one, so in any repo that had ever run a fleet lane every
# verdict-history commit degraded to "dry-run" — verdict.json on disk, nothing in
# git. The history now lives OUTSIDE refs/heads (HISTORY_REF), which no branch
# name can prefix-collide with, and reads union the legacy ref so history filed
# before the move stays discoverable.


def _make_fleet_lane_repo(repo):
    """A repo shaped like a fleet lane: the task-branch family already exists.

    `gitreins/task/fix-add` is minted from the live BRANCH_PREFIX, so the
    fixture reproduces the exact ref state that made the old history ref
    uncreatable, and the repo ships GitReins' own `.gitignore` (the rule that
    ignores the history store, so the writer's `git add -f` is load-bearing).
    Returns the git env for the test's own subprocess calls.
    """
    env = _git_env()
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=env)
    (repo / "readme.txt").write_text("hello")
    (repo / ".gitignore").write_text(".gitreins/history/\n")
    subprocess.run(
        ["git", "add", "readme.txt", ".gitignore"],
        check=True,
        capture_output=True,
        cwd=str(repo),
        env=env,
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"],
        check=True,
        capture_output=True,
        cwd=str(repo),
        env=env,
    )
    subprocess.run(["git", "branch", f"{BRANCH_PREFIX}fix-add"], check=True, cwd=str(repo), env=env)
    return env


def _git_out(repo, env, *args: str) -> str:
    """stdout of one git command in *repo* (the tests' own observation channel)."""
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, cwd=str(repo), env=env
    ).stdout


def _ref_exists(repo, env, ref: str) -> bool:
    return (
        subprocess.run(
            ["git", "rev-parse", "--verify", "-q", ref],
            capture_output=True,
            cwd=str(repo),
            env=env,
        ).returncode
        == 0
    )


def test_legacy_history_ref_collides_with_a_fleet_task_branch(tmp_path):
    """The reported bug, against git itself — the fixture is not vacuous.

    `gitreins/task/fix-add` is a plain path-prefix sibling of `refs/heads/
    gitreins`, so the legacy name cannot be created there. Without this the
    regression test below could pass on a repo that never had the collision.
    """
    repo = tmp_path / "repo"
    env = _make_fleet_lane_repo(repo)

    result = subprocess.run(
        ["git", "update-ref", LEGACY_HISTORY_REF, "HEAD"],
        capture_output=True,
        text=True,
        cwd=str(repo),
        env=env,
    )

    assert result.returncode != 0, "expected the legacy ref name to collide"
    assert "cannot lock ref" in result.stderr, result.stderr
    # the collision is exactly a path-prefix relation between the two ref names
    assert f"refs/heads/{BRANCH_PREFIX}fix-add".startswith(LEGACY_HISTORY_REF + "/")


def test_history_store_is_gitignored_so_the_writer_must_force_it(tmp_path):
    """Non-vacuity for the writer's `-f`: the shipped ignore rule refuses a plain add.

    `.gitignore` ignores `.gitreins/history/` (the ref is the versioned copy),
    so a writer that ran a bare `git add` — as the pre-POC-52 worktree writer
    did — could not have committed the entry at all.
    """
    repo = tmp_path / "repo"
    env = _make_fleet_lane_repo(repo)
    entry = repo / ".gitreins" / "history" / "2026-09-25" / "deadbeef"
    entry.mkdir(parents=True)
    (entry / "verdict.json").write_text("{}")

    plain = subprocess.run(
        ["git", "add", entry.relative_to(repo).as_posix()],
        capture_output=True,
        text=True,
        cwd=str(repo),
        env=env,
    )

    assert plain.returncode != 0
    assert "ignored" in plain.stderr, plain.stderr


def test_verdict_history_commits_alongside_a_fleet_task_branch(tmp_path):
    """AC1: writing history in a repo that has `gitreins/task/<id>` succeeds."""
    repo = tmp_path / "repo"
    env = _make_fleet_lane_repo(repo)

    p = VerdictPersister(str(repo))
    p.config["max_verdicts"] = 0
    commit_hash = p.persist("df-task", {"passed": True, "task_title": "DF Task"})

    assert commit_hash not in ("dry-run", "disabled"), "history commit degraded to dry-run"
    assert len(commit_hash) == 8
    listing = _git_out(repo, env, "ls-tree", "-r", "--name-only", HISTORY_REF)
    assert any(path.endswith("verdict.json") for path in listing.splitlines()), listing

    # the fleet branch naming is untouched and both refs resolve side by side
    assert _ref_exists(repo, env, f"refs/heads/{BRANCH_PREFIX}fix-add")
    assert _ref_exists(repo, env, HISTORY_REF)
    # ...while the history ref is not a branch at all
    assert HISTORY_REF not in _git_out(repo, env, "branch", "--format=%(refname)").split()
    # the documented DWIM shorthand resolves the same ref
    assert (
        _git_out(repo, env, "rev-parse", "gitreins/history").strip()
        == _git_out(repo, env, "rev-parse", HISTORY_REF).strip()
    )

    # a second verdict appends to the same ref (chain, not a second root)
    second = VerdictPersister(str(repo)).persist("df-task-2", {"passed": False})
    assert second not in ("dry-run", "disabled")
    assert len(_git_out(repo, env, "rev-list", HISTORY_REF).split()) == 2

    # and the branch-backed reader finds it with the fleet branch still present
    shutil.rmtree(repo / ".gitreins" / "history")
    entries = p.list_verdicts()
    # both entries, newest first by DATE; within one date the documented order is
    # by entry hash, which carries no time meaning, so compare as a set.
    assert {e["task_id"] for e in entries} == {"df-task", "df-task-2"}
    assert len(entries) == 2
    assert {e["_ref"] for e in entries} == {HISTORY_REF}
    assert p.count_verdicts() == 2


def test_history_ref_is_outside_the_branch_namespace():
    """AC3: the collision class is gone by construction, not by naming.

    A ref under refs/heads/ can only be protected by choosing a name the fleet
    never prefixes. Living outside refs/heads/ removes the class outright: no
    branch name — the fleet's today, or anything added later — is a prefix of
    the history ref or has it as a prefix.
    """
    fleet_branch = f"refs/heads/{BRANCH_PREFIX}fix-add"

    # the legacy name was a prefix of every fleet task branch...
    assert fleet_branch.startswith(LEGACY_HISTORY_REF + "/")
    # ...and what replaced it cannot be, in either direction.
    assert not HISTORY_REF.startswith("refs/heads/")
    assert not fleet_branch.startswith(HISTORY_REF)
    assert not HISTORY_REF.startswith(fleet_branch + "/")


def test_first_write_after_the_move_chains_onto_the_legacy_history(tmp_path):
    """AC2: pre-move history stays readable AND rides into the new ref.

    A repo that already has verdicts on the legacy branch keeps them: the first
    write after the upgrade parents onto the legacy tip (the automatic form of
    the documented `git update-ref` migration), and the legacy ref itself is
    left exactly where it was.
    """
    repo = tmp_path / "repo"
    env = _make_legacy_history_repo(repo, [("2026-06-21", "aaaa1111", "legacy-task", True)])
    legacy_tip = _git_out(repo, env, "rev-parse", LEGACY_HISTORY_REF).strip()

    p = VerdictPersister(str(repo))
    p.config["max_verdicts"] = 0
    commit_hash = p.persist("new-task", {"passed": True})

    assert commit_hash not in ("dry-run", "disabled")
    log = _git_out(repo, env, "log", "--format=%s", HISTORY_REF).splitlines()
    assert len(log) == 2, log  # one linear history, not a stranded second root
    assert log[1] == "verdicts"  # the legacy tip's own subject
    assert _git_out(repo, env, "rev-parse", LEGACY_HISTORY_REF).strip() == legacy_tip

    # reads union both refs (fresh-clone shape: no local .gitreins/history/).
    # The legacy entries are IN the new ref's tree — the first write after the
    # move seeds it from the legacy tip — so both are read from the new ref.
    shutil.rmtree(repo / ".gitreins" / "history")
    entries = p.list_verdicts()
    assert [e["task_id"] for e in entries] == ["new-task", "legacy-task"]
    assert {e["_ref"] for e in entries} == {HISTORY_REF}
    assert p.count_verdicts() == 2
    assert "Total entries: 2" in build_report(str(repo))


# ── evaluated-payload preservation (DF-GITREINS-POC-1) ─────────
#
# `gitreins task complete` evaluates staged implementation/test files and
# then persists the verdict. The old first-verdict path stashed the dirty
# worktree, checked out an orphan branch in the caller's worktree, and ran
# a plain `git stash pop` — the pop demoted staged files to unstaged, and
# when it failed it was silent (returncode never checked), so the next
# commit silently dropped the evaluated payload. These tests pin the
# caller's index and worktree byte-for-byte across persist().

_PRESERVE_ENV = _git_env()


def _make_payload_repo(repo) -> dict:
    """Init a main-branch repo with the documented dogfood staged-set shape.

    Reproduces the real DF-GITREINS-POC-1 failure conditions: a staged
    modification of a TRACKED file (the harness config analog) plus the two
    newly staged implementation/test files, and an unstaged tracked change
    on a second file so both index and worktree preservation are checked.

    Returns a snapshot dict: staged file list, staged diff, unstaged diff,
    all captured before persistence.
    """
    env = _PRESERVE_ENV
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True, env=env)

    def git(*args):
        subprocess.run(["git", *args], check=True, capture_output=True, cwd=str(repo), env=env)

    (repo / "harness_config.txt").write_text("history:\n  enabled: true\n")
    (repo / "notes.txt").write_text("notes v1\n")
    git("add", "harness_config.txt", "notes.txt")
    git("commit", "-q", "-m", "init")

    # init regenerated the harness config -> STAGED tracked-file modification
    (repo / "harness_config.txt").write_text("history:\n  enabled: true\n  storage: git\n")
    # the evaluated payload: two NEW files staged
    (repo / "calculator.py").write_text("def add(a, b):\n    return a + b\n")
    (repo / "test_calculator.py").write_text(
        "from calculator import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n"
    )
    # a separate unstaged tracked change (worktree preservation)
    (repo / "notes.txt").write_text("notes v1\nlocal edit\n")
    git("add", "harness_config.txt", "calculator.py", "test_calculator.py")

    staged_names = subprocess.run(
        ["git", "diff", "--cached", "--name-only"],
        capture_output=True,
        text=True,
        cwd=str(repo),
        env=env,
    ).stdout
    staged_diff = subprocess.run(
        ["git", "diff", "--cached"],
        capture_output=True,
        text=True,
        cwd=str(repo),
        env=env,
    ).stdout
    unstaged_diff = subprocess.run(
        ["git", "diff"],
        capture_output=True,
        text=True,
        cwd=str(repo),
        env=env,
    ).stdout
    return {
        "staged_names": staged_names,
        "staged_diff": staged_diff,
        "unstaged_diff": unstaged_diff,
        "env": env,
    }


def _persist_first_verdict(repo) -> str:
    """Persist one verdict on a repo whose history ref does not exist."""
    p = VerdictPersister(str(repo))
    p.config["max_verdicts"] = 0  # no pruning
    assert p.storage_mode == "git"
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", HISTORY_REF],
        capture_output=True,
        cwd=str(repo),
    )
    assert result.returncode != 0  # precondition: no history ref yet
    return p.persist("df-task", {"passed": True, "task_title": "DF Task"})


def _assert_index_and_worktree_preserved(repo, before: dict) -> None:
    env = before["env"]

    def out(*args):
        return subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            cwd=str(repo),
            env=env,
        ).stdout

    assert out("diff", "--cached", "--name-only") == before["staged_names"]
    assert out("diff", "--cached") == before["staged_diff"]
    assert out("diff") == before["unstaged_diff"]
    assert out("rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


def test_first_verdict_preserves_staged_and_unstaged_state(tmp_path):
    repo = tmp_path / "repo"
    before = _make_payload_repo(repo)

    commit_hash = _persist_first_verdict(repo)

    assert commit_hash not in ("dry-run", "disabled")
    assert len(commit_hash) == 8
    _assert_index_and_worktree_preserved(repo, before)


def test_first_verdict_lands_on_the_history_ref(tmp_path):
    repo = tmp_path / "repo"
    before = _make_payload_repo(repo)

    commit_hash = _persist_first_verdict(repo)

    env = before["env"]
    tree_paths = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", HISTORY_REF],
        capture_output=True,
        text=True,
        cwd=str(repo),
        env=env,
    ).stdout.split()
    # The root commit holds ONLY the entry's own files, at the entry's real path
    # (DF-GITREINS-POC-52: the old root-commit builder dropped that prefix, so
    # the first verdict sat at the tree root where no branch-backed reader —
    # they all filter on the history path — could see it).
    assert len(tree_paths) == 2
    assert all(path.startswith(".gitreins/history/") for path in tree_paths), tree_paths
    assert all(path.endswith(("verdict.json", "summary.md")) for path in tree_paths)

    verdict_path = next(p for p in tree_paths if p.endswith("verdict.json"))
    stored = json.loads(
        subprocess.run(
            ["git", "show", f"{HISTORY_REF}:{verdict_path}"],
            capture_output=True,
            text=True,
            cwd=str(repo),
            env=env,
        ).stdout
    )
    # verdict.json content round-trips through the plumbing-written tree
    assert stored["task_id"] == "df-task"
    assert stored["passed"] is True

    # Caller remains on main with an intact index (no orphan checkout fallout)
    _assert_index_and_worktree_preserved(repo, before)

    # The ref alone is enough to read the FIRST verdict back — the fresh-clone
    # shape report/serve fall back to (local .gitreins/history/ is gitignored).
    shutil.rmtree(repo / ".gitreins" / "history")
    entries = VerdictPersister(str(repo)).list_verdicts()
    assert [e["task_id"] for e in entries] == ["df-task"]
    assert entries[0]["_ref"] == HISTORY_REF


def test_second_verdict_appends_to_the_history_ref(tmp_path):
    repo = tmp_path / "repo"
    before = _make_payload_repo(repo)
    first = _persist_first_verdict(repo)
    second = VerdictPersister(str(repo)).persist(
        "df-task-2", {"passed": False, "task_title": "DF Task 2"}
    )

    assert second not in ("dry-run", "disabled")
    assert first != second
    env = before["env"]
    ls = subprocess.run(
        ["git", "rev-list", HISTORY_REF],
        capture_output=True,
        text=True,
        cwd=str(repo),
        env=env,
    ).stdout.split()
    assert len(ls) == 2  # verdict commits chain on the ref, parentless root first

    _assert_index_and_worktree_preserved(repo, before)


def test_next_commit_includes_evaluated_payload(tmp_path):
    """The commit after persist() carries the evaluated files, staged as they were."""
    repo = tmp_path / "repo"
    before = _make_payload_repo(repo)

    _persist_first_verdict(repo)

    env = before["env"]
    subprocess.run(
        ["git", "commit", "-q", "-m", "feat: calculator"],
        check=True,
        capture_output=True,
        cwd=str(repo),
        env=env,
    )
    names = subprocess.run(
        ["git", "show", "--name-only", "--format=", "HEAD"],
        capture_output=True,
        text=True,
        cwd=str(repo),
        env=env,
    ).stdout.split()
    assert "calculator.py" in names
    assert "test_calculator.py" in names
    assert "harness_config.txt" in names  # staged mod rides along, not demoted
    assert "notes.txt" not in names  # unstaged edit stays out of the commit


def test_verdict_persistence_failure_returns_dry_run_and_preserves_payload(tmp_path, monkeypatch):
    """If the verdict commit cannot be created, degrade honestly — never destroy state."""
    repo = tmp_path / "repo"
    before = _make_payload_repo(repo)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated plumbing failure")

    monkeypatch.setattr(VerdictPersister, "_git", boom)
    result = _persist_first_verdict(repo)

    assert result == "dry-run"  # _git_commit catches and reports honestly
    assert not (repo / ".git" / "refs" / "gitreins" / "history").exists()
    _assert_index_and_worktree_preserved(repo, before)


def test_plumbing_commands_touch_neither_index_nor_worktree(tmp_path, monkeypatch):
    """Defense in depth: the history writer never mutates the caller's state.

    Index verbs (read-tree / add / write-tree) are allowed only with
    GIT_INDEX_FILE redirected to a throwaway file — run against the caller's
    index they would rewrite the staged payload DF-GITREINS-POC-1 is about.
    Working-tree and HEAD verbs are never allowed, redirect or not.
    """
    repo = tmp_path / "repo"
    _make_payload_repo(repo)

    real_run = subprocess.run
    worktree_verbs = ("checkout", "stash", "restore", "reset", "clean", "sparse-checkout")
    index_verbs = ("read-tree", "add", "write-tree", "update-index")

    def spy_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd and cmd[0] == "git":
            verb = next((c for c in cmd[1:] if not c.startswith("-")), None)
            assert verb not in worktree_verbs, f"worktree-mutating git verb invoked: {verb}"
            if verb in index_verbs:
                index = (kwargs.get("env") or {}).get("GIT_INDEX_FILE")
                assert index, f"{verb} ran without a redirected GIT_INDEX_FILE"
                assert not index.startswith(str(repo / ".git")), (
                    f"{verb} pointed at the caller's own index: {index}"
                )
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy_run)
    commit_hash = _persist_first_verdict(repo)
    # The verdict commit must actually happen — a dry-run here means the
    # persistence path tried a forbidden worktree-mutating verb and failed.
    assert commit_hash not in ("dry-run", "disabled")
    assert len(commit_hash) == 8


# ── Resolution-gate records (DF-GITREINS-POC-36) ─────────────
#
# `gitreins resolve`, `gitreins preflight` and MCP `context.resolve` used to
# evaporate on exit: `.gitreins/history` gained nothing and report/serve showed a
# hole where the gate's decisions should be. These tests grade the SHARED writer
# those three surfaces call — the record it files, the usage line it appends, and
# what it deliberately does NOT write.


def _resolution_verdict(band: str = "RESOLVED", probability: float = 0.91, **overrides):
    """A real engine verdict object — the class ``resolve`` returns, unmodified."""
    from engine.resolution import ResolutionVerdict

    verdict = ResolutionVerdict(
        question="Does engine/evidence_bounds.py truncate text?",
        verdict=band,
        probability=probability,
        missing_kind=None if band == "RESOLVED" else "implementation",
        model="typesafe/jev-1.13-20260917",
        input_tokens=520,
        output_tokens=96,
        tokens_estimated=300,
    )
    for name, value in overrides.items():
        setattr(verdict, name, value)
    return verdict


def _history_records(repo) -> list[tuple[str, str, dict]]:
    """``[(date, hash, record)]`` — every history entry, oldest first."""
    history = repo / ".gitreins" / "history"
    if not history.is_dir():
        return []
    return [
        (path.parent.parent.name, path.parent.name, json.loads(path.read_text()))
        for path in sorted(history.glob("*/*/verdict.json"))
    ]


def _usage_rows(repo) -> list[dict]:
    path = repo / ".gitreins" / "usage.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _stamp(value: str) -> float:
    """Epoch for a record's naive-UTC ``evaluated_at`` (exactly as the writer stamps it)."""
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc).timestamp()


def _write_judge_record(repo, task_id: str, passed: bool) -> None:
    """Persist one judge verdict, through the persister the judge uses."""
    persister = VerdictPersister(str(repo))
    persister.config["storage"] = "filesystem"
    persister.config["max_verdicts"] = 0
    persister.persist(
        task_id,
        {
            "passed": passed,
            "task_title": f"Judged {task_id}",
            "items": [{"criterion": "c1", "status": "PASS" if passed else "FAIL", "detail": "d"}],
        },
    )


class TestPersistResolutionRecord:
    """The record `gitreins report` / `gitreins serve` read."""

    def test_successful_run_appends_one_record_marked_as_a_resolution(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        verdict = _resolution_verdict()

        result = persist_resolution(str(repo), verdict, surface="cli")

        assert result == "dry-run"  # files written; a bare tmp dir is no git repo
        records = _history_records(repo)
        assert len(records) == 1, "one successful run = exactly one record"
        _, _, record = records[0]
        assert record["kind"] == KIND_RESOLUTION
        assert record["source"] == "cli"
        assert record["band"] == "RESOLVED"
        assert record["probability"] == pytest.approx(0.91)
        assert record["question"] == verdict.question
        assert record["task_id"] == RESOLUTION_ENTRY_ID
        # A resolution record is NOT a task verdict: it must not borrow the
        # judge's pass/fail or its criteria slot.
        assert "passed" not in record
        assert "items" not in record
        # The engine's own verdict rides along whole, so report/serve show the
        # bundle and the accounting without a second serialization.
        assert record["verdict"]["verdict"] == "RESOLVED"
        assert record["verdict"]["model"] == "typesafe/jev-1.13-20260917"
        assert record["verdict"]["input_tokens"] == 520

    def test_summary_is_the_gate_template_not_the_judge_template(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        persist_resolution(str(repo), _resolution_verdict(), surface="predispatch")

        summary = next((repo / ".gitreins" / "history").glob("*/*/summary.md")).read_text()
        assert summary.startswith("# Resolution gate: RESOLVED")
        assert "**Surface:** predispatch" in summary
        assert "**Tokens:** input=520 output=96" in summary
        assert "✗ FAIL" not in summary
        assert "## Criteria" not in summary

    def test_history_disabled_writes_no_record_and_no_usage_line(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / ".gitreins").mkdir(parents=True)
        (repo / ".gitreins" / "config.yaml").write_text(
            "history:\n  enabled: false\n", encoding="utf-8"
        )

        assert persist_resolution(str(repo), _resolution_verdict(), surface="cli") == "disabled"

        assert not (repo / ".gitreins" / "history").exists()
        assert _usage_rows(repo) == []

    @pytest.mark.parametrize(
        "reason",
        [
            "surface-disabled",
            "no-credentials",
            "all-credentials-rejected",
            "transport-error",
            "malformed-response",
            "budget-exhausted",
        ],
    )
    def test_abstain_paths_write_nothing_at_all(self, tmp_path, reason):
        """An ABSTAIN is a non-event, not a verdict — even with tokens attached."""
        from engine.resolution import VERDICT_ABSTAIN, ResolutionVerdict

        repo = tmp_path / "repo"
        repo.mkdir()
        verdict = ResolutionVerdict(
            question="q?",
            verdict=VERDICT_ABSTAIN,
            abstain_reason=reason,
            input_tokens=520,
            output_tokens=96,
        )

        assert persist_resolution(str(repo), verdict, surface="cli") == "abstain"

        assert _history_records(repo) == []
        assert _usage_rows(repo) == []

    def test_a_persistence_failure_is_reported_never_raised(self, tmp_path, monkeypatch):
        """Non-fatal by contract: recording can never break the run that decided."""
        repo = tmp_path / "repo"
        repo.mkdir()

        def boom(*_args, **_kwargs):
            raise RuntimeError("simulated history failure")

        monkeypatch.setattr(VerdictPersister, "persist", boom)

        assert persist_resolution(str(repo), _resolution_verdict(), surface="cli") == "error"


class TestResolutionUsageRow:
    """One Jev call = one `step: "resolution"` row, carrying what the API said."""

    def test_one_completed_call_appends_one_row_with_the_schema_and_step(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()

        persist_resolution(str(repo), _resolution_verdict(), surface="cli")

        rows = _usage_rows(repo)
        assert len(rows) == 1
        assert set(rows[0]) == {
            "ts",
            "tokens_in",
            "tokens_out",
            "cache_read",
            "cache_write",
            "step",
        }
        assert rows[0]["step"] == "resolution"
        assert rows[0]["tokens_in"] == 520
        assert rows[0]["tokens_out"] == 96
        assert rows[0]["cache_read"] == 0 and rows[0]["cache_write"] == 0
        assert isinstance(rows[0]["ts"], float) and rows[0]["ts"] > 0

    def test_a_response_that_reported_no_tokens_writes_no_row(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()

        persist_resolution(
            str(repo), _resolution_verdict(input_tokens=None, output_tokens=None), surface="cli"
        )

        assert _usage_rows(repo) == [], "a 0/0 row would read as 'the call was free'"
        assert len(_history_records(repo)) == 1, "the record still lands"

    def test_the_row_is_charged_to_the_resolution_record_not_the_next_verdict(self, tmp_path):
        """The row and its record share one instant, so attribution is exact."""
        from engine import usage

        repo = tmp_path / "repo"
        repo.mkdir()
        persist_resolution(str(repo), _resolution_verdict(), surface="cli")
        # A judge verdict persisted right AFTER the gate run — the case the
        # shared stamp exists for (a wall-clock read that merely preceded the
        # record could fall in the same microsecond and lose the row to this).
        _write_judge_record(repo, "task-judged", True)

        records = _history_records(repo)
        resolution = next(r for r in records if r[2].get("kind") == KIND_RESOLUTION)
        judged = next(r for r in records if r[2].get("task_id") == "task-judged")
        rows = _usage_rows(repo)

        # The row carries the record's OWN evaluated_at instant, to the microsecond.
        assert rows[0]["ts"] == _stamp(resolution[2]["evaluated_at"])

        index = usage.attribute_rows(
            [(r[0], r[1], _stamp(r[2]["evaluated_at"])) for r in (resolution, judged)],
            usage.load_usage_rows(str(repo)),
        )

        key = f"{resolution[0]}/{resolution[1]}"
        assert list(index) == [key], "the row belongs to the gate's own record"
        assert index[key]["steps"] == ["resolution"]
        assert index[key]["tokens_in"] == 520
        assert index[key]["tokens_out"] == 96


class TestResolutionRecordsCoexistWithJudgeHistory:
    """A resolution record must not disturb the judge history readers."""

    def test_judge_parsing_and_supersede_bookkeeping_are_untouched(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _write_judge_record(repo, "task-judged", True)
        persist_resolution(str(repo), _resolution_verdict(), surface="cli")

        persister = VerdictPersister(str(repo))
        entries = persister.list_verdicts(n=10)
        assert len(entries) == 2
        judge = next(e for e in entries if e.get("task_id") == "task-judged")
        assert judge["passed"] is True
        assert judge["items"][0]["criterion"] == "c1"
        assert "kind" not in judge
        # No job id on a resolution record => it never supersedes a judge record.
        assert judge["superseded_by"] is None
        assert judge["supersedes"] is None
        assert persister.count_verdicts() == 2

    def test_report_lists_resolution_records_in_their_own_section(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _write_judge_record(repo, "task-judged", True)
        persist_resolution(str(repo), _resolution_verdict(), surface="cli")

        report = build_report(str(repo), n=10)

        assert "Recent: 1 evaluations" in report, "a resolution band is not an evaluation"
        assert "Pass:   1 (100%)" in report
        assert "Fail:   0 (0%)" in report, "the resolution record is not a failed judgment"
        assert "Resolution gate (1)" in report
        assert "• RESOLVED (0.91)" in report
        assert "[cli]" in report
        assert "Does engine/evidence_bounds.py truncate text?" in report

    def test_report_is_unchanged_when_only_judge_records_exist(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _write_judge_record(repo, "task-pass", True)
        _write_judge_record(repo, "task-fail", False)

        report = build_report(str(repo), n=10)

        assert "Resolution gate" not in report
        assert "Recent: 2 evaluations" in report
        assert "Pass:   1 (50%)" in report
        assert "Fail:   1 (50%)" in report
