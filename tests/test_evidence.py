"""Tests for worker execution evidence embedded in a verdict directory.

Two layers are covered:

* the collector itself (source selection, bounds, best-effort failures), and
* the ``task complete`` flow end-to-end in a scratch repository — the contract
  the viewer depends on: a verdict directory that carries the brief, the
  driver-log tail and the graded patch next to ``verdict.json``.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from engine import evidence

REPO_ROOT = Path(__file__).resolve().parents[1]

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Evidence Fixture",
    "GIT_AUTHOR_EMAIL": "evidence@example.invalid",
    "GIT_COMMITTER_NAME": "Evidence Fixture",
    "GIT_COMMITTER_EMAIL": "evidence@example.invalid",
}


def _git(repo: Path, *args: str) -> str:
    env = dict(os.environ)
    env.update(GIT_ENV)
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
        check=True,
        timeout=60,
    )
    return result.stdout


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "scratch"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    return repo


def _items(manifest: dict) -> dict:
    return {item["name"]: item for item in manifest["items"]}


def _sources(manifest: dict) -> str:
    return " ".join(item.get("source", "") for item in manifest["items"])


# ── collector ────────────────────────────────────────────────────────────────


def test_brief_is_taken_from_the_env_var_and_recorded_with_its_source(tmp_path):
    brief = tmp_path / "worker-brief.md"
    brief.write_text("# Brief\nLand the fix.\n", encoding="utf-8")
    entry = tmp_path / "entry"
    repo = _repo(tmp_path)

    manifest = evidence.collect_evidence(
        str(repo), str(entry), env={evidence.BRIEF_ENV: str(brief)}
    )

    items = _items(manifest)
    assert set(items) == {evidence.BRIEF_NAME}
    assert items[evidence.BRIEF_NAME]["file"] == evidence.BRIEF_FILENAME
    assert str(brief) in items[evidence.BRIEF_NAME]["source"]
    assert (entry / evidence.BRIEF_FILENAME).read_text(encoding="utf-8") == (
        "# Brief\nLand the fix.\n"
    )
    assert items[evidence.BRIEF_NAME]["bytes"] == len("# Brief\nLand the fix.\n".encode())


def test_brief_falls_back_to_the_worktree_brief_and_missing_sources_are_omitted(tmp_path):
    repo = _repo(tmp_path)
    (repo / ".gitreins").mkdir()
    (repo / ".gitreins" / "worker-brief.md").write_text("worktree brief\n", encoding="utf-8")
    entry = tmp_path / "entry"

    # No env vars at all: the log and the patch are absent (nothing staged, no
    # commit), the worktree brief is the only artifact — and it is not
    # fabricated for the two sources that do not exist.
    manifest = evidence.collect_evidence(str(repo), str(entry), env={})

    assert [item["name"] for item in manifest["items"]] == [evidence.BRIEF_NAME]
    assert "worktree brief" in _sources(manifest)


def test_driver_log_keeps_the_tail_and_records_truncation(tmp_path, monkeypatch):
    monkeypatch.setattr(evidence, "MAX_LOG_BYTES", 32)
    log = tmp_path / "driver.log"
    log.write_text("HEAD-" + "x" * 200 + "-TAIL\n", encoding="utf-8")
    entry = tmp_path / "entry"
    repo = _repo(tmp_path)

    manifest = evidence.collect_evidence(str(repo), str(entry), env={evidence.LOG_ENV: str(log)})

    item = _items(manifest)[evidence.LOG_NAME]
    text = (entry / evidence.LOG_FILENAME).read_text(encoding="utf-8")
    assert item["truncated"] is True
    assert text.endswith("-TAIL\n")
    assert "HEAD-" not in text
    assert "bytes dropped" in text
    assert item["bytes"] == len(text.encode())


def test_patch_is_the_graded_working_tree_diff_when_the_tree_is_dirty(tmp_path):
    repo = _repo(tmp_path)
    (repo / "mod.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "mod.py")
    _git(repo, "commit", "-q", "-m", "base")
    (repo / "mod.py").write_text("value = 2\n", encoding="utf-8")
    entry = tmp_path / "entry"

    manifest = evidence.collect_evidence(str(repo), str(entry), env={})

    item = _items(manifest)[evidence.PATCH_NAME]
    patch = (entry / evidence.PATCH_FILENAME).read_text(encoding="utf-8")
    assert "+value = 2" in patch
    assert item["source"].startswith("git diff HEAD")


def test_patch_falls_back_to_the_stamped_commit_when_the_tree_is_clean(tmp_path):
    repo = _repo(tmp_path)
    (repo / "mod.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "mod.py")
    _git(repo, "commit", "-q", "-m", "landed fix")
    commit = _git(repo, "rev-parse", "HEAD").strip()
    entry = tmp_path / "entry"

    manifest = evidence.collect_evidence(str(repo), str(entry), commit=commit, env={})

    item = _items(manifest)[evidence.PATCH_NAME]
    assert f"git show {commit[:12]}" == item["source"]
    assert "landed fix" in (entry / evidence.PATCH_FILENAME).read_text(encoding="utf-8")


def test_collector_is_best_effort_when_entry_dir_cannot_be_created(tmp_path):
    repo = _repo(tmp_path)
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory\n", encoding="utf-8")

    manifest = evidence.collect_evidence(str(repo), str(blocker / "entry"), env={})

    assert manifest["items"] == []
    assert manifest["detail"]


def test_manifest_items_skips_unusable_entries():
    verdict = {
        "evidence": {
            "items": [
                {"name": "brief", "file": "worker-brief.md"},
                {"name": "bad", "file": "../escape.md"},
                {"name": "bad2", "file": "sub/dir.md"},
                {"name": "bad3"},
                "not-a-dict",
            ]
        }
    }

    assert [item["name"] for item in evidence.manifest_items(verdict)] == ["brief"]
    assert evidence.manifest_items({}) == []
    assert evidence.manifest_items({"evidence": "nope"}) == []


def test_read_evidence_serves_only_manifest_declared_plain_files(tmp_path):
    entry = tmp_path / "entry"
    entry.mkdir()
    (entry / "worker-brief.md").write_text("brief body\n", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside the verdict dir\n", encoding="utf-8")
    verdict = {"evidence": {"items": [{"name": "brief", "file": "worker-brief.md"}]}}

    assert evidence.read_evidence(str(entry), verdict, "brief") == (
        "worker-brief.md",
        "brief body\n",
    )
    # Undeclared name, traversal name, and an artifact deleted after collection.
    assert evidence.read_evidence(str(entry), verdict, "patch") is None
    assert (
        evidence.read_evidence(
            str(entry), {"evidence": {"items": [{"name": "x", "file": "../secret.txt"}]}}, "x"
        )
        is None
    )
    (entry / "worker-brief.md").unlink()
    assert evidence.read_evidence(str(entry), verdict, "brief") is None


# ── the task complete flow (the contract the viewer reads) ───────────────────


@pytest.fixture()
def scratch_repo(tmp_path: Path) -> Path:
    """A minimal repository that can complete a task without an LLM."""
    repo = _repo(tmp_path)
    (repo / ".gitreins").mkdir()
    (repo / ".gitreins" / "config.yaml").write_text(
        "guards:\n"
        "  secrets: true\n"
        "  lint: false\n"
        "  tests: false\n"
        "  allow_skips: true\n"
        "history:\n"
        "  enabled: true\n"
        "  storage: filesystem\n",
        encoding="utf-8",
    )
    (repo / ".gitreins" / "tasks.yaml").write_text(
        "tasks:\n"
        "- id: EVIDENCE-1\n"
        "  title: Land the fix with evidence\n"
        "  criteria:\n"
        "  - the verdict directory carries the run\n"
        "  status: pending\n",
        encoding="utf-8",
    )
    (repo / "mod.py").write_text("value = 1\n", encoding="utf-8")
    _git(repo, "add", "mod.py", ".gitreins/config.yaml")
    _git(repo, "commit", "-q", "-m", "base commit")
    # The graded fix lives in the working tree, exactly like a worker's edit.
    (repo / "mod.py").write_text("value = 2\n", encoding="utf-8")
    return repo


def test_task_complete_writes_brief_log_and_patch_next_to_verdict_json(
    scratch_repo: Path, tmp_path: Path
):
    brief = tmp_path / "brief.md"
    brief.write_text("# Worker brief\nEmbed the evidence.\n", encoding="utf-8")
    log = tmp_path / "driver.log"
    log.write_text("driver start\nstep two\nfinished\n", encoding="utf-8")

    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT)] + ([existing] if existing else []))
    env["GITREINS_WORKER_BRIEF"] = str(brief)
    env["GITREINS_DRIVER_LOG"] = str(log)
    for key in ("GITREINS_LLM_API_KEY", "GITREINS_LLM_BASE_URL", "GITREINS_LLM_MODEL"):
        env.pop(key, None)

    result = subprocess.run(
        [sys.executable, "-m", "gitreins", "task", "complete", "EVIDENCE-1", "--skip-tier2"],
        cwd=str(scratch_repo),
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )
    assert "Evaluating" in result.stdout, result.stdout + result.stderr

    entries = sorted((scratch_repo / ".gitreins" / "history").glob("*/*"))
    assert len(entries) == 1, entries
    entry = entries[0]

    verdict = json.loads((entry / "verdict.json").read_text(encoding="utf-8"))
    manifest = {item["name"]: item for item in verdict["evidence"]["items"]}
    assert set(manifest) == {evidence.BRIEF_NAME, evidence.LOG_NAME, evidence.PATCH_NAME}
    assert verdict["commit"], "the verdict still stamps the commit it graded"

    assert (entry / evidence.BRIEF_FILENAME).read_text(encoding="utf-8") == (
        "# Worker brief\nEmbed the evidence.\n"
    )
    assert (entry / evidence.LOG_FILENAME).read_text(encoding="utf-8").endswith("finished\n")
    patch = (entry / evidence.PATCH_FILENAME).read_text(encoding="utf-8")
    assert "+value = 2" in patch
    assert manifest[evidence.PATCH_NAME]["source"].startswith("git diff HEAD")

    # And the same artifacts are reachable through the module the viewer uses.
    assert evidence.read_evidence(str(entry), verdict, evidence.PATCH_NAME)[1] == patch
