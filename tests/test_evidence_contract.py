"""Automation contract and non-mutating working-tree scope tests."""

import hashlib
import json
import os
import subprocess

import yaml

from engine.evidence import (
    EVIDENCE_SCHEMA,
    EVIDENCE_SCHEMA_VERSION,
    MAX_EVIDENCE_BYTES,
    dumps_evidence,
    guard_evidence,
)
from engine.guard_manager import GuardManager
from engine.types import GuardResult, Tier1Result
from tests.test_cli import run_cli


def _parse_v1(result):
    payload = json.loads(result.stdout)
    assert payload["$schema"] == EVIDENCE_SCHEMA
    assert payload["schemaVersion"] == EVIDENCE_SCHEMA_VERSION
    assert payload["metadata"]["redacted"] is True
    assert len(result.stdout.encode("utf-8")) <= MAX_EVIDENCE_BYTES + 1
    return payload


def _index_digest(workdir):
    index = os.path.join(workdir, ".git", "index")
    if not os.path.exists(index):
        return None
    return hashlib.sha256(open(index, "rb").read()).hexdigest()


def test_working_tree_scope_finds_unstaged_and_untracked_without_index_mutation(tmp_workdir):
    tracked = os.path.join(tmp_workdir, "tracked.py")
    open(tracked, "w").write("value = 1\n")
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_workdir, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_workdir, check=True)
    subprocess.run(["git", "add", "tracked.py"], cwd=tmp_workdir, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=tmp_workdir, check=True, capture_output=True)

    open(tracked, "w").write('api_key = "sk-abcdefghijklmnopqrstuvwxyz123456"  # gitleaks:allow\n')
    open(os.path.join(tmp_workdir, "untracked.py"), "w").write("safe = True\n")
    before_index = _index_digest(tmp_workdir)
    before_cached = subprocess.run(
        ["git", "diff", "--cached", "--binary"], cwd=tmp_workdir,
        check=True, capture_output=True, text=True,
    ).stdout

    manager = GuardManager(
        tmp_workdir,
        {"guards": {"secrets": True, "lint": False, "tests": False}},
        scope="working-tree",
    )
    result = manager.run_all()

    assert manager.changed_files == ["tracked.py", "untracked.py"]
    assert result.passed is False
    assert "abcdefghijklmnopqrstuvwxyz123456" not in result.summary
    assert _index_digest(tmp_workdir) == before_index
    after_cached = subprocess.run(
        ["git", "diff", "--cached", "--binary"], cwd=tmp_workdir,
        check=True, capture_output=True, text=True,
    ).stdout
    assert after_cached == before_cached


def test_working_tree_lsp_scope_excludes_unsupported_files(tmp_workdir):
    from engine.lsp import select_lsp_files

    py_file = os.path.join(tmp_workdir, "module.py")
    md_file = os.path.join(tmp_workdir, "README.md")
    open(py_file, "w").write("value = 1\n")
    open(md_file, "w").write("# Documentation — not Python\n")
    assert select_lsp_files("pylsp", tmp_workdir, ["module.py", "README.md"]) == [py_file]


def test_evaluator_working_context_includes_untracked_content(tmp_workdir, llm_client):
    from engine.evaluator import AgenticEvaluator

    open(os.path.join(tmp_workdir, "new_module.py"), "w").write("UNTRACKED_SENTINEL = True\n")
    evaluator = AgenticEvaluator(llm_client, tmp_workdir, max_iterations=1)
    context = evaluator._build_code_context({"guards": {"test_mode": "diff"}})
    assert "new_module.py" in context
    assert "UNTRACKED_SENTINEL" in context
    assert "new_module.py" in evaluator._compute_allowed_files()


def test_staged_scope_does_not_include_only_unstaged_files(tmp_workdir):
    open(os.path.join(tmp_workdir, "only-working.py"), "w").write("value = 1\n")
    manager = GuardManager(
        tmp_workdir,
        {"guards": {"secrets": True, "lint": False, "tests": False}},
        scope="staged",
    )
    assert manager.changed_files == []
    assert manager.run_all().passed is True


def test_guard_json_contract_is_bounded_and_redacted():
    secret = "Bearer abcdefghijklmnopqrstuvwxyz.1234567890"
    result = Tier1Result(
        passed=False,
        results=[GuardResult("secrets", False, (secret + "\n") * 1000)],
        extra={"changed_count": 1},
    )
    payload = dumps_evidence(guard_evidence(result, "working-tree"))
    decoded = json.loads(payload)
    assert len(payload.encode("utf-8")) <= MAX_EVIDENCE_BYTES
    assert secret not in payload
    assert "[REDACTED]" in payload
    assert decoded["metadata"]["truncated"] is True
    assert decoded["outcome"] == "fail"


def test_guard_cli_json_is_single_v1_document(tmp_workdir):
    os.makedirs(os.path.join(tmp_workdir, ".gitreins"), exist_ok=True)
    with open(os.path.join(tmp_workdir, ".gitreins", "config.yaml"), "w") as handle:
        yaml.safe_dump({"guards": {"secrets": False, "lint": False, "tests": False}}, handle)
    result = run_cli("guard", "--json", "--scope", "working-tree", cwd=tmp_workdir)
    assert result.returncode == 0
    payload = _parse_v1(result)
    assert payload["command"] == "guard"
    assert payload["scope"] == "working-tree"


def test_ephemeral_judge_has_no_task_history_stash_or_branch_side_effects(tmp_workdir):
    os.makedirs(os.path.join(tmp_workdir, ".gitreins"), exist_ok=True)
    config_path = os.path.join(tmp_workdir, ".gitreins", "config.yaml")
    with open(config_path, "w") as handle:
        yaml.safe_dump({
            "guards": {"secrets": False, "lint": False, "tests": False},
            "history": {"enabled": True, "storage": "git"},
        }, handle)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_workdir, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_workdir, check=True)
    subprocess.run(["git", "add", ".gitreins/config.yaml"], cwd=tmp_workdir, check=True)
    subprocess.run(["git", "commit", "-m", "config"], cwd=tmp_workdir, check=True, capture_output=True)
    open(os.path.join(tmp_workdir, "change.py"), "w").write("value = 2\n")

    before_branch = subprocess.run(
        ["git", "branch", "--show-current"], cwd=tmp_workdir,
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    before_status = subprocess.run(
        ["git", "status", "--porcelain=v1"], cwd=tmp_workdir,
        check=True, capture_output=True, text=True,
    ).stdout
    before_stashes = subprocess.run(
        ["git", "stash", "list"], cwd=tmp_workdir,
        check=True, capture_output=True, text=True,
    ).stdout
    verdict = json.dumps({
        "verdict": "COMPLETE",
        "items": [{"criterion": "works", "status": "PASS", "detail": "verified"}],
        "summary": "all good",
    })
    result = run_cli(
        "judge", "rorca-run-7-story-1", "--ephemeral",
        "--title", "Rorca story gate", "--criterion", "works",
        "--scope", "working-tree", "--json", cwd=tmp_workdir,
        extra_env={"GITREINS_MOCK_LLM_RESPONSE": json.dumps({"content": verdict})},
    )

    assert result.returncode == 0, result.stderr
    payload = _parse_v1(result)
    assert payload["command"] == "judge"
    assert payload["subject"]["ephemeral"] is True
    assert payload["metadata"]["historyPersisted"] is False
    assert not os.path.exists(os.path.join(tmp_workdir, ".gitreins", "tasks.yaml"))
    assert not os.path.exists(os.path.join(tmp_workdir, ".gitreins", "history"))
    assert subprocess.run(
        ["git", "branch", "--show-current"], cwd=tmp_workdir,
        check=True, capture_output=True, text=True,
    ).stdout.strip() == before_branch
    assert subprocess.run(
        ["git", "branch", "--list", "gitreins"], cwd=tmp_workdir,
        check=True, capture_output=True, text=True,
    ).stdout.strip() == ""
    assert subprocess.run(
        ["git", "stash", "list"], cwd=tmp_workdir,
        check=True, capture_output=True, text=True,
    ).stdout == before_stashes
    assert subprocess.run(
        ["git", "status", "--porcelain=v1"], cwd=tmp_workdir,
        check=True, capture_output=True, text=True,
    ).stdout == before_status


def test_report_cli_json_contract_redacts_history(tmp_workdir):
    history = os.path.join(tmp_workdir, ".gitreins", "history", "2026-07-13", "abcdef12")
    os.makedirs(history, exist_ok=True)
    with open(os.path.join(history, "verdict.json"), "w") as handle:
        json.dump({
            "task_id": "task-1",
            "task_title": "Bearer abcdefghijklmnopqrstuvwxyz123456",
            "passed": True,
        }, handle)
    result = run_cli("report", "--json", cwd=tmp_workdir)
    assert result.returncode == 0
    payload = _parse_v1(result)
    assert payload["command"] == "report"
    assert payload["scope"] == "history"
    assert "abcdefghijklmnopqrstuvwxyz123456" not in result.stdout
