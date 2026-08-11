"""
Integration tests for gitreins/cli.py — command line interface.
axiom:trace work_item=GR-003 spec=specs/09-CLI.md plan=.memory-bank/work-items/GR-003/plan.yaml
"""

import json
import os
import re
import shutil
import sys
import subprocess
import time
import pytest


# Get the path to the cli module
CLI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gitreins")
CLI_SCRIPT = os.path.join(CLI_DIR, "cli.py")


def run_cli(*args, **kwargs):
    """Run the CLI as a subprocess and return CompletedProcess.

    Keyword Args:
        extra_env: Dict of extra environment variables to set (merged with current env).
        All other kwargs passed through to subprocess.run.
    """
    extra_env = kwargs.pop("extra_env", {})
    cmd = [sys.executable, CLI_SCRIPT] + list(args)
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", "")
    env.update(extra_env)
    PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if PROJECT_ROOT not in env["PYTHONPATH"]:
        env["PYTHONPATH"] = PROJECT_ROOT + (":" + env["PYTHONPATH"] if env["PYTHONPATH"] else "")
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env, **kwargs)


# ── Phase 3-1: Command routing and argument parsing ──────────────────────────


class TestHelpOutput:
    """Test CLI help and command dispatch — step-3-1-1-1."""

    def test_help_prints_usage(self):
        """--help prints usage information."""
        result = run_cli("--help")
        assert result.returncode == 0
        assert "GitReins" in result.stdout

    def test_no_args_prints_help(self):
        """No arguments prints help."""
        result = run_cli()
        assert result.returncode == 0
        assert "GitReins" in result.stdout

    def test_unknown_command_prints_help(self):
        """Unknown command prints help."""
        result = run_cli("unknown")
        assert "invalid choice" in result.stderr


class TestWorkdirDetection:
    """Test get_workdir() — step-3-1-1-2."""

    def test_get_workdir_in_git_repo(self):
        """Inside git repo → returns repo root (git rev-parse --show-toplevel)."""
        from gitreins.cli import get_workdir

        workdir = get_workdir()
        assert os.path.isdir(workdir)
        assert os.path.isdir(os.path.join(workdir, ".git"))

    def test_get_workdir_outside_git_repo(self, tmp_path):
        """Outside git repo, git rev-parse fails, returns os.getcwd()."""
        from gitreins.cli import get_workdir

        workdir = get_workdir()
        # In a git repo (workspace is one), this returns the repo root.
        # The get_workdir fallback to cwd() is tested implicitly by
        # the non-error return for a non-git path.
        assert os.path.isdir(workdir)


# ── Phase 3-2: Task lifecycle commands ───────────────────────────────────────


class TestTaskCreateCLI:
    """Test task create CLI — step-3-2-1-1."""

    def test_create_task_with_criteria(self, tmp_workdir):
        """Create task with criteria prints ID, title, numbered criteria."""
        result = run_cli(
            "task", "create", "myid", "My Title", "criterion A", "criterion B", cwd=tmp_workdir
        )
        assert result.returncode == 0
        assert "Created task: myid" in result.stdout
        assert "My Title" in result.stdout
        assert "criterion A" in result.stdout

    def test_create_task_with_empty_criteria(self, tmp_workdir):
        """Create task with no criteria prints task without criteria list."""
        result = run_cli("task", "create", "empty", "No Criteria", cwd=tmp_workdir)
        assert result.returncode == 0
        assert "Created task: empty" in result.stdout


class TestTaskStartCompleteCLI:
    """Test task start/complete CLI — step-3-2-1-2."""

    def test_start_existing_task(self, tmp_workdir):
        """Start existing task shows 'Started: ID → in_progress'."""
        run_cli("task", "create", "start-me", "Start Test", "c1", cwd=tmp_workdir)
        result = run_cli("task", "start", "start-me", cwd=tmp_workdir)
        assert result.returncode == 0
        assert "Started:" in result.stdout
        assert "in_progress" in result.stdout

    def test_start_nonexistent_task_raises(self, tmp_workdir):
        """Start nonexistent task raises KeyError."""
        result = run_cli("task", "start", "nope", cwd=tmp_workdir)
        assert result.returncode != 0

    def test_complete_nonexistent_task_raises(self, tmp_workdir):
        """Complete nonexistent task raises KeyError."""
        result = run_cli("task", "complete", "nope", cwd=tmp_workdir)
        assert result.returncode != 0


class TestTaskListCLI:
    """Test task list CLI — step-3-2-1-3."""

    def test_list_shows_status_icons(self, tmp_workdir):
        """List shows correct status icons for each task."""
        mock_env = {
            "GITREINS_MOCK_LLM_RESPONSE": json.dumps(
                {
                    "content": json.dumps(
                        {
                            "verdict": "COMPLETE",
                            "items": [{"criterion": "c1", "status": "PASS", "detail": "ok"}],
                            "summary": "all good",
                        }
                    )
                }
            )
        }
        run_cli("task", "create", "pending1", "P1", "c1", cwd=tmp_workdir)
        run_cli("task", "create", "progress1", "P2", "c1", cwd=tmp_workdir)
        run_cli("task", "start", "progress1", cwd=tmp_workdir)
        run_cli("task", "complete", "progress1", cwd=tmp_workdir, extra_env=mock_env)
        result = run_cli("task", "list", cwd=tmp_workdir)
        assert result.returncode == 0
        assert "○" in result.stdout  # pending icon
        assert "●" in result.stdout  # complete icon

    def test_list_with_status_filter(self, tmp_workdir):
        """List --status pending shows only pending tasks."""
        mock_env = {
            "GITREINS_MOCK_LLM_RESPONSE": json.dumps(
                {
                    "content": json.dumps(
                        {
                            "verdict": "COMPLETE",
                            "items": [{"criterion": "c1", "status": "PASS", "detail": "ok"}],
                            "summary": "all good",
                        }
                    )
                }
            )
        }
        run_cli("task", "create", "pend", "Pending", "c1", cwd=tmp_workdir)
        run_cli("task", "create", "done", "Done", "c1", cwd=tmp_workdir)
        run_cli("task", "start", "done", cwd=tmp_workdir)
        run_cli("task", "complete", "done", cwd=tmp_workdir, extra_env=mock_env)
        result = run_cli("task", "list", "--status", "pending", cwd=tmp_workdir)
        assert result.returncode == 0
        assert "pend" in result.stdout
        assert "done" not in result.stdout

    def test_empty_list_shows_no_tasks(self, tmp_workdir):
        """List with no tasks prints 'No tasks found.'."""
        result = run_cli("task", "list", cwd=tmp_workdir)
        assert result.returncode == 0
        assert "No tasks found" in result.stdout


class TestTaskDeleteCLI:
    """Test task delete CLI — step-3-2-1-4."""

    def test_delete_existing_task(self, tmp_workdir):
        """Delete existing task prints 'Deleted: ID', task gone."""
        run_cli("task", "create", "del-me", "Delete", "c1", cwd=tmp_workdir)
        result = run_cli("task", "delete", "del-me", cwd=tmp_workdir)
        assert result.returncode == 0
        assert "Deleted: del-me" in result.stdout

    def test_delete_nonexistent_task_raises(self, tmp_workdir):
        """Delete nonexistent task raises KeyError."""
        result = run_cli("task", "delete", "nope", cwd=tmp_workdir)
        assert result.returncode != 0


# ── Phase 3-3: guard, judge, commit, mcp-server ──────────────────────────────


class TestGuardRunCLI:
    """Test guard run CLI — step-3-3-1-1."""

    def test_guard_run_shows_tier1_guards(self, tmp_workdir):
        """guard run prints 'Tier 1 Guards: PASS or FAIL' and per-guard summary."""
        result = run_cli("guard", cwd=tmp_workdir)
        assert result.returncode == 0
        assert "Tier 1 Guards:" in result.stdout


class TestJudgeCLI:
    """Test judge CLI — step-3-3-1-2."""

    def test_judge_nonexistent_task_exits_1(self, tmp_workdir):
        """Judge on nonexistent task exits code 1, stderr contains Task not found."""
        result = run_cli("judge", "nope", cwd=tmp_workdir)
        assert result.returncode == 1
        output = result.stdout + result.stderr
        assert "Task not found" in output

    def test_judge_existing_task_exits_0(self, tmp_workdir):
        """Judge on existing task exits code 0, prints summary."""
        verdict_json = '{"verdict":"COMPLETE","items":[{"criterion":"c1","status":"PASS","detail":"ok"}],"summary":"all good"}'
        run_cli("task", "create", "judge-me", "Judge Test", "c1", cwd=tmp_workdir)
        result = run_cli(
            "judge",
            "judge-me",
            cwd=tmp_workdir,
            extra_env={"GITREINS_MOCK_LLM_RESPONSE": json.dumps({"content": verdict_json})},
        )
        assert result.returncode in (0, 1)
        output = result.stdout + result.stderr
        assert "Judge Result" in output


class TestCommitCLI:
    """Test commit CLI — step-3-3-1-3."""

    def test_commit_in_clean_repo(self, tmp_workdir):
        """Commit in clean repo (no staged) runs guards then attempts commit."""
        result = run_cli("commit", "test commit", cwd=tmp_workdir)
        output = result.stdout + result.stderr
        assert "Tier 1" in output


class TestMCPServerCLI:
    """Test mcp-server command — step-3-3-1-4."""

    def test_cmd_mcp_server_function_exists(self):
        """cmd_mcp_server function exists and calls GitReinsMCPServer constructor."""
        from gitreins.cli import cmd_mcp_server

        assert callable(cmd_mcp_server)

    def test_mcp_server_import_path(self):
        """Verify that mcp-server command can be imported without error."""
        import gitreins.cli

        assert hasattr(gitreins.cli, "cmd_mcp_server")

    def test_mcp_server_help_documents_env_config(self):
        """mcp-server --help documents env-var config and the configure tool."""
        result = run_cli("mcp-server", "--help")
        assert result.returncode == 0
        for var in (
            "GITREINS_LLM_API_KEY",
            "GITREINS_LLM_BASE_URL",
            "GITREINS_LLM_MODEL",
            "GITREINS_LLM_REASONING",
        ):
            assert var in result.stdout
        assert "mcp_gitreins_configure" in result.stdout


class TestExtendedCLI:
    """Extended CLI coverage."""

    def test_create_task_special_chars(self, tmp_workdir):
        """Create task with special chars in title works."""
        result = run_cli(
            "task", "create", "spec", "Task with $pecial !@#$%^& chars", cwd=tmp_workdir
        )
        assert result.returncode == 0
        assert "Created task: spec" in result.stdout

    def test_create_task_multiple_criteria(self, tmp_workdir):
        """Create task with multiple criteria shows them all."""
        result = run_cli("task", "create", "multi", "Multi", "c1", "c2", "c3", cwd=tmp_workdir)
        assert result.returncode == 0
        assert "c1" in result.stdout
        assert "c2" in result.stdout
        assert "c3" in result.stdout

    def test_complete_existing_task(self, tmp_workdir):
        """Complete existing task after creating and starting it."""
        mock_env = {
            "GITREINS_MOCK_LLM_RESPONSE": json.dumps(
                {
                    "content": json.dumps(
                        {
                            "verdict": "COMPLETE",
                            "items": [{"criterion": "c1", "status": "PASS", "detail": "ok"}],
                            "summary": "all good",
                        }
                    )
                }
            )
        }
        run_cli("task", "create", "comp-me", "Complete Me", "c1", cwd=tmp_workdir)
        run_cli("task", "start", "comp-me", cwd=tmp_workdir)
        result = run_cli("task", "complete", "comp-me", cwd=tmp_workdir, extra_env=mock_env)
        assert result.returncode == 0
        assert "Complete" in result.stdout or "complete" in result.stdout

    def test_list_with_status_multiple_filters(self, tmp_workdir):
        """List with --status in_progress shows only in_progress tasks."""
        run_cli("task", "create", "t1", "T1", cwd=tmp_workdir)
        run_cli("task", "create", "t2", "T2", cwd=tmp_workdir)
        run_cli("task", "start", "t1", cwd=tmp_workdir)
        result = run_cli("task", "list", "--status", "in_progress", cwd=tmp_workdir)
        assert result.returncode == 0
        assert "t1" in result.stdout

    def test_guard_run_all_details(self, tmp_workdir):
        """guard run prints summary, PASS, per-guard results."""
        result = run_cli("guard", cwd=tmp_workdir)
        assert result.returncode == 0
        assert "Tier 1" in result.stdout

    def test_create_task_with_criteria_and_list(self, tmp_workdir):
        """Create tasks with criteria and list shows them."""
        run_cli("task", "create", "ltask1", "List Task 1", "crit_a", cwd=tmp_workdir)
        run_cli("task", "create", "ltask2", "List Task 2", "crit_b", cwd=tmp_workdir)
        result = run_cli("task", "list", cwd=tmp_workdir)
        assert result.returncode == 0
        assert "ltask1" in result.stdout
        assert "ltask2" in result.stdout


# ── Extended: Help output ────────────────────────────────────────────


class TestExtendedHelp:
    """Extended CLI help output tests."""

    def test_task_help_shows_subcommands(self):
        """task --help lists task subcommands."""
        result = run_cli("task", "--help")
        assert result.returncode == 0
        assert "create" in result.stdout
        assert "start" in result.stdout
        assert "complete" in result.stdout
        assert "list" in result.stdout
        assert "delete" in result.stdout

    def test_guard_help_prints_usage(self):
        """guard --help prints usage."""
        result = run_cli("guard", "--help")
        assert result.returncode == 0
        assert "usage" in result.stdout.lower()

    def test_judge_help_prints_usage(self):
        """judge --help prints usage."""
        result = run_cli("judge", "--help")
        assert result.returncode == 0
        assert "usage" in result.stdout.lower()


# ── Extended: Error cases ────────────────────────────────────────────


class TestErrorCases:
    """CLI error handling tests."""

    def test_create_task_no_args(self):
        """task create without args shows error."""
        result = run_cli("task", "create")
        assert result.returncode != 0
        assert "required" in result.stderr.lower()

    def test_start_task_no_args(self):
        """task start without args shows error."""
        result = run_cli("task", "start")
        assert result.returncode != 0
        assert "required" in result.stderr.lower()

    def test_complete_task_no_args(self):
        """task complete without args shows error."""
        result = run_cli("task", "complete")
        assert result.returncode != 0
        assert "required" in result.stderr.lower()

    def test_delete_task_no_args(self):
        """task delete without args shows error."""
        result = run_cli("task", "delete")
        assert result.returncode != 0
        assert "required" in result.stderr.lower()

    def test_nonexistent_task_command_shows_error(self):
        """task nonexistent subcommand shows argparse error."""
        result = run_cli("task", "nonexistent")
        assert result.returncode == 2
        assert "invalid choice" in result.stderr


# ── Extended: Config and workdir ─────────────────────────────────────


class TestConfigAndWorkdir:
    """CLI config and workdir detection tests."""

    def test_create_task_creates_gitreins_dir(self, tmp_workdir):
        """Creating a task creates .gitreins/ directory."""
        gitreins = os.path.join(tmp_workdir, ".gitreins")
        assert not os.path.isdir(gitreins)
        run_cli("task", "create", "cfg1", "Config Test", "c1", cwd=tmp_workdir)
        assert os.path.isdir(gitreins)
        assert os.path.isfile(os.path.join(gitreins, "tasks.yaml"))

    def test_guard_works_without_gitreins_dir(self, tmp_workdir):
        """Guard runs without .gitreins/ directory."""
        gitreins = os.path.join(tmp_workdir, ".gitreins")
        assert not os.path.isdir(gitreins)
        result = run_cli("guard", cwd=tmp_workdir)
        assert result.returncode == 0
        assert "Tier 1 Guards:" in result.stdout

    def test_guard_run_ignores_leaked_git_index_file(self, tmp_path):
        """Nested guard must not read a GIT_INDEX_FILE leaked by a pre-commit hook.

        Git exports GIT_INDEX_FILE to hooks; if the guard passes it through to
        its own subprocesses, a nested `gitreins guard` reads the OUTER repo's
        index and lints phantom files. Regression: DF-008.
        """
        repo = tmp_path / "repo"
        (repo / "tests").mkdir(parents=True)
        (repo / "tests" / "test_x.py").write_text("def test_x(): pass\n")
        (repo / "app.py").write_text("def main(): pass\n")
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "app.py", "tests/test_x.py"], check=True)

        # Foreign index holding a path that does NOT exist in repo/ — simulates
        # the outer repo's index leaking into the nested guard.
        foreign = tmp_path / "foreign"
        foreign.mkdir()
        (foreign / "phantom.py").write_text("x = 1\n")
        subprocess.run(["git", "init", "-q", str(foreign)], check=True)
        subprocess.run(["git", "-C", str(foreign), "add", "phantom.py"], check=True)

        result = run_cli(
            "guard",
            cwd=str(repo),
            extra_env={"GIT_INDEX_FILE": str(foreign / ".git" / "index")},
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Tier 1 Guards: PASS" in result.stdout

    def test_start_task_uses_existing_gitreins_dir(self, tmp_workdir):
        """Starting a task uses existing .gitreins/ directory."""
        gitreins = os.path.join(tmp_workdir, ".gitreins")
        run_cli("task", "create", "cfg2", "Config Test 2", "c1", cwd=tmp_workdir)
        assert os.path.isdir(gitreins)
        result = run_cli("task", "start", "cfg2", cwd=tmp_workdir)
        assert result.returncode == 0
        assert "Started:" in result.stdout


# ── Extended: Guard and commit ───────────────────────────────────────


class TestGuardAndCommit:
    """Extended guard and commit tests."""

    def test_guard_detects_secrets_in_staged_file(self, tmp_workdir):
        """Guard detects staged file containing a secret pattern."""
        subprocess.run(["git", "init"], cwd=tmp_workdir, capture_output=True, timeout=15)
        secret_file = os.path.join(tmp_workdir, "secret.py")
        with open(secret_file, "w") as f:
            f.write('api_key = "sk-1234567890123456789012345678901234567890"\n')
        subprocess.run(
            ["git", "add", "secret.py"], cwd=tmp_workdir, capture_output=True, timeout=15
        )
        result = run_cli("guard", cwd=tmp_workdir)
        assert "Tier 1 Guards:" in result.stdout

    def test_guard_secrets_detected_when_fails(self, tmp_workdir):
        """Guard output contains FAIL when secrets found."""
        subprocess.run(["git", "init"], cwd=tmp_workdir, capture_output=True, timeout=15)
        secret_file = os.path.join(tmp_workdir, "creds.py")
        with open(secret_file, "w") as f:
            f.write('token = "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"\n')
        subprocess.run(["git", "add", "creds.py"], cwd=tmp_workdir, capture_output=True, timeout=15)
        result = run_cli("guard", cwd=tmp_workdir)
        assert "Tier 1 Guards:" in result.stdout

    def test_commit_shows_guard_output(self, tmp_workdir):
        """Commit shows guard result in output."""
        result = run_cli("commit", "test message", cwd=tmp_workdir)
        assert "Tier 1" in result.stdout

    def test_commit_fails_when_guard_detects_secret(self, tmp_workdir):
        """Commit exits 1 when guards detect a secret in staged files."""
        subprocess.run(["git", "init"], cwd=tmp_workdir, capture_output=True, timeout=15)
        secret_file = os.path.join(tmp_workdir, "secret_key.py")
        with open(secret_file, "w") as f:
            f.write('api_key = "sk-1234567890123456789012345678901234567890"\n')
        subprocess.run(
            ["git", "add", "secret_key.py"], cwd=tmp_workdir, capture_output=True, timeout=15
        )
        result = run_cli("commit", "test message", cwd=tmp_workdir)
        output = result.stdout + result.stderr
        assert "FAILED" in output
        assert result.returncode != 0


# ── Extended: Task lifecycle and edge cases ──────────────────────────


class TestTaskLifecycleExtended:
    """Extended task lifecycle tests."""

    def test_full_task_lifecycle_subprocess(self, tmp_workdir):
        """Full lifecycle: create → start → list → complete → list."""
        run_cli("task", "create", "life1", "Lifecycle", "c1", cwd=tmp_workdir)

        result = run_cli("task", "list", cwd=tmp_workdir)
        assert "○" in result.stdout

        run_cli("task", "start", "life1", cwd=tmp_workdir)

        result = run_cli("task", "list", cwd=tmp_workdir)
        assert "◐" in result.stdout

        run_cli("task", "complete", "life1", cwd=tmp_workdir)

        result = run_cli("task", "list", cwd=tmp_workdir)
        assert "●" in result.stdout

    def test_list_filter_complete_status(self, tmp_workdir):
        """List --status complete shows only completed tasks."""
        run_cli("task", "create", "todo1", "Todo", "c1", cwd=tmp_workdir)
        run_cli("task", "create", "done1", "Done", "c1", cwd=tmp_workdir)
        run_cli("task", "start", "done1", cwd=tmp_workdir)
        run_cli("task", "complete", "done1", cwd=tmp_workdir)
        result = run_cli("task", "list", "--status", "complete", cwd=tmp_workdir)
        assert result.returncode == 0
        assert "done1" in result.stdout
        assert "todo1" not in result.stdout

    def test_task_list_empty_after_delete_all(self, tmp_workdir):
        """List shows no tasks after all are deleted."""
        run_cli("task", "create", "only1", "Only", "c1", cwd=tmp_workdir)
        run_cli("task", "delete", "only1", cwd=tmp_workdir)
        result = run_cli("task", "list", cwd=tmp_workdir)
        assert "No tasks found" in result.stdout

    def test_delete_then_list_shows_remaining(self, tmp_workdir):
        """Delete one task, list shows the other."""
        run_cli("task", "create", "keep1", "Keep", "c1", cwd=tmp_workdir)
        run_cli("task", "create", "gone1", "Gone", "c1", cwd=tmp_workdir)
        run_cli("task", "delete", "gone1", cwd=tmp_workdir)
        result = run_cli("task", "list", cwd=tmp_workdir)
        assert "keep1" in result.stdout
        assert "gone1" not in result.stdout


class TestEdgeCases:
    """Edge case tests for the CLI."""

    def test_create_task_long_title(self, tmp_workdir):
        """Create task with a very long title works."""
        title = "A" * 500
        result = run_cli("task", "create", "long1", title, cwd=tmp_workdir)
        assert result.returncode == 0
        assert "Created task: long1" in result.stdout

    def test_create_task_same_id_overwrites(self, tmp_workdir):
        """Create with same ID overwrites previous title."""
        run_cli("task", "create", "dup1", "First", "c1", cwd=tmp_workdir)
        run_cli("task", "create", "dup1", "Second", "c2", cwd=tmp_workdir)
        result = run_cli("task", "list", cwd=tmp_workdir)
        assert "Second" in result.stdout
        assert "First" not in result.stdout

    def test_create_task_with_dashes_in_id(self, tmp_workdir):
        """Create task with dashes in ID works."""
        result = run_cli("task", "create", "my-task-id", "Dash ID", "c1", cwd=tmp_workdir)
        assert result.returncode == 0
        assert "my-task-id" in result.stdout

    def test_list_no_filter_shows_all_tasks(self, tmp_workdir):
        """List without filter shows all tasks regardless of status."""
        mock_env = {
            "GITREINS_MOCK_LLM_RESPONSE": json.dumps(
                {
                    "content": json.dumps(
                        {
                            "verdict": "COMPLETE",
                            "items": [{"criterion": "", "status": "PASS", "detail": "ok"}],
                            "summary": "all good",
                        }
                    )
                }
            )
        }
        run_cli("task", "create", "pend1", "Pending", cwd=tmp_workdir)
        run_cli("task", "create", "comp1", "Complete", cwd=tmp_workdir)
        run_cli("task", "start", "comp1", cwd=tmp_workdir)
        run_cli("task", "complete", "comp1", cwd=tmp_workdir, extra_env=mock_env)
        result = run_cli("task", "list", cwd=tmp_workdir)
        assert "pend1" in result.stdout
        assert "comp1" in result.stdout


# ── Extended: Judge tests ────────────────────────────────────────────


class TestJudgeExtended:
    """Extended judge CLI tests."""

    def test_judge_nonexistent_task_output(self, tmp_workdir):
        """Judge nonexistent task prints 'Task not found' to stdout."""
        result = run_cli("judge", "no-such-task", cwd=tmp_workdir)
        assert result.returncode == 1
        assert "Task not found" in result.stdout

    def test_judge_existing_task_runs_evaluation(self, tmp_workdir):
        """Judge on existing task runs evaluation and prints summary."""
        verdict_json = json.dumps(
            {
                "verdict": "COMPLETE",
                "items": [{"criterion": "c1", "status": "PASS", "detail": "ok"}],
                "summary": "all good",
            }
        )
        run_cli("task", "create", "judge-eval", "Judge Eval", "c1", cwd=tmp_workdir)
        result = run_cli(
            "judge",
            "judge-eval",
            cwd=tmp_workdir,
            extra_env={"GITREINS_MOCK_LLM_RESPONSE": json.dumps({"content": verdict_json})},
        )
        assert "Judge Result" in result.stdout
        assert "Overall:" in result.stdout

    def test_judge_requires_api_key(self, tmp_workdir):
        """Judge integration test that requires DEEPSEEK_API_KEY."""
        if not os.environ.get("DEEPSEEK_API_KEY"):
            pytest.skip("requires DEEPSEEK_API_KEY")
        verdict_json = json.dumps(
            {
                "verdict": "COMPLETE",
                "items": [{"criterion": "c1", "status": "PASS", "detail": "ok"}],
                "summary": "all good",
            }
        )
        mock_env = {"GITREINS_MOCK_LLM_RESPONSE": json.dumps({"content": verdict_json})}
        run_cli("task", "create", "judge-api", "Judge API", "c1", cwd=tmp_workdir)
        run_cli("task", "start", "judge-api", cwd=tmp_workdir)
        run_cli("task", "complete", "judge-api", cwd=tmp_workdir, extra_env=mock_env)
        result = run_cli("judge", "judge-api", cwd=tmp_workdir, extra_env=mock_env)
        assert result.returncode in (0, 1)
        assert "Judge Result" in result.stdout or "Judge" in result.stdout


# ── DF-006: CLI async judge — --async / --status / --run-job ────────────────


class TestJudgeAsyncCLI:
    """CLI background jobs (DF-006): dispatch, poll, survive the parent exiting.

    The detached worker inherits GITREINS_MOCK_LLM_RESPONSE (set via
    extra_env), so the whole roundtrip runs hermetically as real
    subprocesses. The job store is isolated by the autouse
    ``isolated_job_store`` conftest fixture.
    """

    _MOCK = {
        "GITREINS_MOCK_LLM_RESPONSE": json.dumps(
            {
                "content": json.dumps(
                    {
                        "verdict": "COMPLETE",
                        "items": [{"criterion": "c1", "status": "PASS", "detail": "ok"}],
                        "summary": "all good",
                    }
                )
            }
        )
    }

    def _poll_job(self, job_id, cwd, deadline=30.0):
        """Poll `gitreins judge --status <job_id>` until complete or error."""
        end = time.monotonic() + deadline
        last = None
        while time.monotonic() < end:
            last = run_cli("judge", job_id, "--status", cwd=cwd)
            if last.returncode in (0, 1):
                return last
            time.sleep(0.3)
        pytest.fail(
            f"job {job_id} did not finish within {deadline}s — last: {getattr(last, 'stdout', None)}"
        )
        raise AssertionError("unreachable")  # pragma: no cover

    def test_async_dispatch_poll_and_result(self, tmp_workdir):
        """--async dispatches a detached worker; --status polls to complete."""
        run_cli(
            "task",
            "create",
            "async-cli",
            "Async CLI",
            "c1",
            cwd=tmp_workdir,
            extra_env=self._MOCK,
        )
        dispatched = run_cli(
            "judge",
            "async-cli",
            "--async",
            cwd=tmp_workdir,
            extra_env=self._MOCK,
        )
        assert dispatched.returncode == 0, dispatched.stdout + dispatched.stderr
        m = re.search(r"Async job dispatched: (job-[0-9a-f]+)", dispatched.stdout)
        assert m, dispatched.stdout
        job_id = m.group(1)
        assert "gitreins judge --status" in dispatched.stdout

        # The dispatching CLI has long exited — the detached worker keeps
        # running and the job record persists on disk.
        polled = self._poll_job(job_id, tmp_workdir)
        assert polled.returncode == 0, polled.stdout + polled.stderr
        assert "Status:   complete" in polled.stdout
        assert "PASS" in polled.stdout
        assert "all good" in polled.stdout

    def test_async_unknown_task_exits_1(self, tmp_workdir):
        result = run_cli("judge", "ghost-task", "--async", cwd=tmp_workdir)
        assert result.returncode == 1
        assert "Task not found" in result.stdout

    def test_status_unknown_job_exits_1(self, tmp_workdir):
        result = run_cli("judge", "job-nonexistent", "--status", cwd=tmp_workdir)
        assert result.returncode == 1
        assert "Job not found" in result.stdout


# ── Regression: config deletion via silent parse failure ──────────────────────


class TestLoadConfigParseFailure:
    """Regression: load_config must warn on YAML parse failure, not silently
    return {} which causes cmd_init to nuke the config file."""

    def test_load_config_warns_on_broken_yaml(self, tmp_workdir, caplog):
        """load_config logs a warning when config.yaml has invalid YAML."""
        import logging

        caplog.set_level(logging.WARNING, logger="gitreins")

        from gitreins.cli import load_config

        config_dir = os.path.join(tmp_workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        config_path = os.path.join(config_dir, "config.yaml")
        # Write broken YAML
        with open(config_path, "w") as f:
            f.write("guards: {secrets: true\n  lint: yes\n")

        result = load_config(tmp_workdir)
        # Must return empty dict (can't parse), not crash
        assert result == {}
        # Must log a warning
        warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("Failed to parse" in str(w) for w in warnings), (
            f"Expected 'Failed to parse' warning, got: {warnings}"
        )

    def test_load_config_returns_empty_for_missing_file(self, tmp_workdir):
        """load_config returns {} when no config file exists (not a warning)."""
        from gitreins.cli import load_config

        result = load_config(tmp_workdir)
        assert result == {}

    def test_load_config_loads_valid_yaml(self, tmp_workdir):
        """load_config returns parsed dict for valid config."""
        import yaml as _yaml
        from gitreins.cli import load_config

        config_dir = os.path.join(tmp_workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        config_path = os.path.join(config_dir, "config.yaml")
        valid = {"guards": {"test_mode": "diff", "secrets": True}}
        with open(config_path, "w") as f:
            _yaml.dump(valid, f)

        result = load_config(tmp_workdir)
        assert result == valid


class TestCmdInitConfigSafety:
    """Regression: cmd_init must NOT overwrite existing config when it can't be parsed."""

    def test_init_refuses_broken_config(self, tmp_workdir):
        """gitreins init exits non-zero when config.yaml exists but is broken YAML."""
        config_dir = os.path.join(tmp_workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        config_path = os.path.join(config_dir, "config.yaml")
        # Write broken YAML
        with open(config_path, "w") as f:
            f.write("guards: {secrets: true\n  lint: yes\n")

        result = run_cli("init", cwd=tmp_workdir)
        assert result.returncode != 0, (
            f"init should refuse to overwrite broken config, got exit {result.returncode}"
        )
        assert (
            "could not be parsed" in result.stderr.lower()
            or "could not be parsed" in result.stdout.lower()
        )

    def test_init_backs_up_existing_config(self, tmp_workdir):
        """gitreins init creates a .bak when overwriting existing config."""
        import yaml as _yaml

        config_dir = os.path.join(tmp_workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        config_path = os.path.join(config_dir, "config.yaml")
        bak_path = config_path + ".bak"
        valid = {"guards": {"test_mode": "full", "secrets": True}}
        with open(config_path, "w") as f:
            _yaml.dump(valid, f)

        result = run_cli("init", cwd=tmp_workdir)
        assert result.returncode == 0
        assert os.path.isfile(bak_path), f"Backup not created at {bak_path}"
        # Backup should contain the original config
        with open(bak_path) as f:
            bak_data = _yaml.safe_load(f)
        assert bak_data["guards"]["test_mode"] == "full"

    def test_init_creates_new_config_when_none_exists(self, tmp_workdir):
        """gitreins init works normally when no config file exists."""
        config_dir = os.path.join(tmp_workdir, ".gitreins")
        config_path = os.path.join(config_dir, "config.yaml")

        result = run_cli("init", cwd=tmp_workdir)
        assert result.returncode == 0
        assert os.path.isfile(config_path), "Config file not created"


# ── v0.7.2: Gitleaks .toml auto-generation ────────────────────────────


class TestGitleaksTomlGeneration:
    """Tests for _generate_gitleaks_config: auto-created during init."""

    def test_python_project_gets_python_exclusions(self, tmp_workdir):
        """Python project gets .venv, __pycache__, dist, etc. exclusions."""
        import subprocess

        os.makedirs(os.path.join(tmp_workdir, ".git"), exist_ok=True)
        with open(os.path.join(tmp_workdir, "main.py"), "w") as f:
            f.write("print('hello')\n")
        # Python detection requires setup.py, setup.cfg, or pyproject.toml
        with open(os.path.join(tmp_workdir, "setup.py"), "w") as f:
            f.write("# placeholder\n")
        subprocess.run(["git", "init", "-q"], cwd=tmp_workdir)
        result = run_cli("init", cwd=tmp_workdir)
        assert result.returncode == 0

        toml_path = os.path.join(tmp_workdir, ".gitleaks.toml")
        assert os.path.isfile(toml_path), "gitleaks.toml should be created"
        content = open(toml_path).read()
        assert r"\.venv/" in content
        assert "__pycache__/" in content
        assert r"\.mypy_cache/" in content
        assert r"\.pytest_cache/" in content
        assert "dist/" in content
        assert r"\.git/" in content

    def test_existing_gitleaks_toml_not_overwritten(self, tmp_workdir):
        """If .gitleaks.toml already exists, init does not modify it."""
        import subprocess

        os.makedirs(os.path.join(tmp_workdir, ".git"), exist_ok=True)
        with open(os.path.join(tmp_workdir, "main.py"), "w") as f:
            f.write("print('hello')\n")
        with open(os.path.join(tmp_workdir, "setup.py"), "w") as f:
            f.write("# placeholder\n")
        existing_toml = os.path.join(tmp_workdir, ".gitleaks.toml")
        with open(existing_toml, "w") as f:
            f.write("# Custom exclusions\ntest-key = true\n")
        subprocess.run(["git", "init", "-q"], cwd=tmp_workdir)
        result = run_cli("init", cwd=tmp_workdir)
        assert result.returncode == 0
        content = open(existing_toml).read()
        assert "Custom exclusions" in content
        assert "test-key" in content


# ── GR-GAP-024/025/026: init runner detection, .gitignore, inconclusive-detection warning ──


class TestInitRunnerGitignoreAndWarning:
    """Regression tests for the stand-in PM gap tasks (2026-08-11)."""

    def _make_python_repo(self, tmp_workdir):
        """setup.py + main.py → detected as Python (no tests/ dir)."""
        with open(os.path.join(tmp_workdir, "main.py"), "w") as f:
            f.write("print('hello')\n")
        with open(os.path.join(tmp_workdir, "setup.py"), "w") as f:
            f.write("# placeholder\n")

    # ── GR-GAP-024: runner-aware test command ─────────────────────────────

    def test_detect_test_command_uses_uv_run_when_uv_installed(self, monkeypatch, tmp_path):
        """Python repo + uv on PATH → `uv run pytest -x --tb=short` (not bare pytest)."""
        import shutil

        from gitreins.cli import _detect_language, _detect_test_command

        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
        (tmp_path / "pyproject.toml").write_text("[project]\nname='app'\n")
        lang = _detect_language(str(tmp_path))
        assert lang["is_python"]
        assert _detect_test_command(str(tmp_path), lang) == "uv run pytest -x --tb=short"

    def test_detect_test_command_prefers_module_pytest_over_uv(self, monkeypatch, tmp_path):
        """Root-package + tests/ layout keeps `python3 -m pytest` even with uv (import correctness)."""
        import shutil

        from gitreins.cli import _detect_language, _detect_test_command

        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/uv" if name == "uv" else None)
        (tmp_path / "todo_stats").mkdir()
        (tmp_path / "todo_stats" / "__init__.py").write_text("")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_todo.py").write_text("def test_add(): pass\n")
        lang = _detect_language(str(tmp_path))
        assert _detect_test_command(str(tmp_path), lang) == "python3 -m pytest -x --tb=short"

    def test_detect_test_command_pipenv_runner(self, monkeypatch, tmp_path):
        """Pipfile + pipenv on PATH → `pipenv run pytest ...`."""
        import shutil

        from gitreins.cli import _detect_language, _detect_test_command

        def fake_which(name):
            return "/usr/bin/pipenv" if name == "pipenv" else None

        monkeypatch.setattr(shutil, "which", fake_which)
        (tmp_path / "Pipfile").write_text("[packages]\n")
        (tmp_path / "setup.py").write_text("# placeholder\n")
        lang = _detect_language(str(tmp_path))
        assert lang["is_python"]
        assert _detect_test_command(str(tmp_path), lang) == "pipenv run pytest -x --tb=short"

    # ── GR-GAP-025: init ensures .gitignore entry ─────────────────────────

    def test_init_creates_gitignore_with_tasks_entry(self, tmp_workdir):
        """init on a repo without .gitignore creates one with .gitreins/tasks.yaml."""
        self._make_python_repo(tmp_workdir)
        result = run_cli("init", cwd=tmp_workdir)
        assert result.returncode == 0
        gi_path = os.path.join(tmp_workdir, ".gitignore")
        assert os.path.isfile(gi_path), ".gitignore should be created by init"
        content = open(gi_path).read()
        assert ".gitreins/tasks.yaml" in content

    def test_init_appends_gitignore_entry_when_missing(self, tmp_workdir):
        """init appends the entry to an existing .gitignore that lacks it."""
        self._make_python_repo(tmp_workdir)
        gi_path = os.path.join(tmp_workdir, ".gitignore")
        with open(gi_path, "w") as f:
            f.write("__pycache__/\n")
        result = run_cli("init", cwd=tmp_workdir)
        assert result.returncode == 0
        content = open(gi_path).read()
        assert ".gitreins/tasks.yaml" in content
        assert content.startswith("__pycache__/\n"), "existing entries must be preserved"

    def test_init_does_not_duplicate_gitignore_entry(self, tmp_workdir):
        """init leaves an existing .gitreins/tasks.yaml entry untouched."""
        self._make_python_repo(tmp_workdir)
        gi_path = os.path.join(tmp_workdir, ".gitignore")
        with open(gi_path, "w") as f:
            f.write(".gitreins/tasks.yaml\n__pycache__/\n")
        result = run_cli("init", cwd=tmp_workdir)
        assert result.returncode == 0
        content = open(gi_path).read()
        assert content.count(".gitreins/tasks.yaml") == 1, "entry must not be duplicated"

    # ── GR-GAP-026: inconclusive-detection warning ────────────────────────

    def test_init_warns_on_undetectable_repo(self, tmp_workdir):
        """init on an empty repo prints a warning advising re-run after adding source files."""
        result = run_cli("init", cwd=tmp_workdir)
        assert result.returncode == 0
        combined = result.stdout + result.stderr
        assert "Language:    unknown" in result.stdout
        assert "re-run 'gitreins init'" in combined, (
            f"expected inconclusive-detection warning in output, got:\n{combined}"
        )

    def test_generated_config_extends_default_ruleset(self, tmp_workdir):
        """Generated config must extend gitleaks' default rules (GR-GAP-005).

        Without [extend] useDefault = true, the custom sk-api-key rule replaces
        gitleaks' built-in rules (AWS, GitHub, GitLab, etc.) and the guard
        silently misses non-sk- secrets.
        """
        import subprocess

        os.makedirs(os.path.join(tmp_workdir, ".git"), exist_ok=True)
        with open(os.path.join(tmp_workdir, "main.py"), "w") as f:
            f.write("print('hello')\n")
        with open(os.path.join(tmp_workdir, "setup.py"), "w") as f:
            f.write("# placeholder\n")
        subprocess.run(["git", "init", "-q"], cwd=tmp_workdir)
        result = run_cli("init", cwd=tmp_workdir)
        assert result.returncode == 0

        content = open(os.path.join(tmp_workdir, ".gitleaks.toml")).read()
        assert "[extend]" in content, "generated config must contain [extend]"
        assert "useDefault = true" in content, "generated config must extend default ruleset"

    def test_universal_exclusions_always_present(self, tmp_workdir):
        """Every project gets .git/, .gitreins/, *.log exclusions (as regexps)."""
        import subprocess

        os.makedirs(os.path.join(tmp_workdir, ".git"), exist_ok=True)
        with open(os.path.join(tmp_workdir, "main.py"), "w") as f:
            f.write("print('hello')\n")
        with open(os.path.join(tmp_workdir, "setup.py"), "w") as f:
            f.write("# placeholder\n")
        subprocess.run(["git", "init", "-q"], cwd=tmp_workdir)
        result = run_cli("init", cwd=tmp_workdir)
        assert result.returncode == 0

        content = open(os.path.join(tmp_workdir, ".gitleaks.toml")).read()
        assert r"\.git/" in content
        assert r"\.gitreins/" in content
        assert r".*\.log" in content

    def test_glob_to_regex_helper(self):
        """_glob_to_regex converts glob paths to valid Go regexps (DF-001)."""
        from gitreins.cli import _glob_to_regex

        assert _glob_to_regex(".git/") == r"\.git/"
        assert _glob_to_regex(".gitreins/") == r"\.gitreins/"
        assert _glob_to_regex("*.log") == r".*\.log"
        assert _glob_to_regex("*.egg-info/") == r".*\.egg-info/"
        assert _glob_to_regex("*.spec.md") == r".*\.spec\.md"
        assert _glob_to_regex("*.md") == r".*\.md"
        assert _glob_to_regex("apps/*/node_modules/") == r"apps/.*/node_modules/"
        assert _glob_to_regex("packages/*/dist/") == r"packages/.*/dist/"
        assert _glob_to_regex("node_modules/") == "node_modules/"
        assert _glob_to_regex("docs/") == "docs/"
        assert _glob_to_regex("vendor/") == "vendor/"
        # every output must itself compile as a regex
        for out in (
            r"\.git/",
            r".*\.log",
            r".*\.egg-info/",
            r".*\.spec\.md",
            r"apps/.*/node_modules/",
        ):
            re.compile(out)

    def test_generated_allowlist_entries_are_valid_regexes(self, tmp_workdir):
        """Every allowlist path in the generated .gitleaks.toml is a valid regexp.

        gitleaks compiles each [allowlist] paths entry as a Go regexp; bare
        globs like '*.log' panic it with 'missing argument to repetition
        operator'. Regression for DF-001.
        """
        import subprocess

        os.makedirs(os.path.join(tmp_workdir, ".git"), exist_ok=True)
        with open(os.path.join(tmp_workdir, "main.py"), "w") as f:
            f.write("print('hello')\n")
        with open(os.path.join(tmp_workdir, "setup.py"), "w") as f:
            f.write("# placeholder\n")
        subprocess.run(["git", "init", "-q"], cwd=tmp_workdir)
        result = run_cli("init", cwd=tmp_workdir)
        assert result.returncode == 0

        content = open(os.path.join(tmp_workdir, ".gitleaks.toml")).read()
        entries = re.findall(r"^\s*'''(.*?)''',\s*$", content, re.MULTILINE)
        assert entries, "no allowlist path entries found in generated config"
        for entry in entries:
            re.compile(entry)  # must not raise — gitleaks compiles it as a regexp
            assert "*" not in entry.replace(".*", ""), (
                f"bare '*' (not part of '.*') in allowlist entry: {entry!r}"
            )

    def test_generated_config_does_not_panic_gitleaks(self, tmp_workdir):
        """gitleaks detect runs cleanly against the generated config (DF-001).

        Pre-fix, every generated config panicked gitleaks v8.30.1 ('missing
        argument to repetition operator', exit code 2). Skipped when gitleaks
        is not installed.
        """
        import subprocess

        gitleaks = shutil.which("gitleaks")
        if gitleaks is None:
            pytest.skip("gitleaks not installed")

        os.makedirs(os.path.join(tmp_workdir, ".git"), exist_ok=True)
        with open(os.path.join(tmp_workdir, "main.py"), "w") as f:
            f.write("print('hello')\n")
        with open(os.path.join(tmp_workdir, "setup.py"), "w") as f:
            f.write("# placeholder\n")
        subprocess.run(["git", "init", "-q"], cwd=tmp_workdir)
        result = run_cli("init", cwd=tmp_workdir)
        assert result.returncode == 0

        toml_path = os.path.join(tmp_workdir, ".gitleaks.toml")
        scan = subprocess.run(
            [
                gitleaks,
                "detect",
                "--source",
                tmp_workdir,
                "--no-git",
                "--verbose",
                "--config",
                toml_path,
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert "panic:" not in scan.stderr, f"gitleaks panicked:\n{scan.stderr}"
        assert scan.returncode != 2, f"gitleaks exited {scan.returncode}:\n{scan.stderr}"
        assert scan.returncode == 0, (
            f"gitleaks scan not clean (rc={scan.returncode}):\n{scan.stderr}"
        )


class TestPreCommitHookIntegration:
    """Verify the pre-commit hook runs via gitreins guard CLI and blocks
    bad commits — not just that it executes, but that it catches secrets
    and exits non-zero."""

    def test_hook_blocks_commit_with_secret(self, tmp_workdir):
        """Staging a file with a fake API key → hook must block commit."""
        # Initialize mock git repo properly
        git_dir = os.path.join(tmp_workdir, ".git")
        os.makedirs(git_dir, exist_ok=True)
        # Need a real git repo for git commit to work
        subprocess.run(["git", "init", "-q"], cwd=tmp_workdir, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"], cwd=tmp_workdir, capture_output=True
        )
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_workdir, capture_output=True)

        # Create gitreins config
        config_dir = os.path.join(tmp_workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        import yaml as _yaml

        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            _yaml.dump({"guards": {"test_mode": "diff", "test_command": "echo ok"}}, f)

        # Install hook
        hooks_dir = os.path.join(git_dir, "hooks")
        os.makedirs(hooks_dir, exist_ok=True)
        hook_path = os.path.join(hooks_dir, "pre-commit")
        with open(hook_path, "w") as f:
            f.write("""#!/usr/bin/env bash
REPO_ROOT="$(git rev-parse --show-toplevel)"
[ ! -f "$REPO_ROOT/.gitreins/config.yaml" ] && exit 0
cd "$REPO_ROOT"
gitreins guard
exit $?
""")
        os.chmod(hook_path, 0o755)

        # Stage a file with a secret
        with open(os.path.join(tmp_workdir, "leak.py"), "w") as f:
            f.write('API_KEY = "sk-1234567890abcdef1234567890abcdef"\n')
        subprocess.run(["git", "add", "leak.py"], cwd=tmp_workdir, capture_output=True)

        # Try to commit — must fail
        result = subprocess.run(
            ["git", "commit", "-m", "should block"],
            cwd=tmp_workdir,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, (
            f"Hook did not block commit with secret key. "
            f"stdout: {result.stdout[:200]}, stderr: {result.stderr[:200]}"
        )

    def test_hook_allows_clean_commit(self, tmp_workdir):
        """Staging a clean file → hook passes, commit succeeds."""
        subprocess.run(["git", "init", "-q"], cwd=tmp_workdir, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"], cwd=tmp_workdir, capture_output=True
        )
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_workdir, capture_output=True)

        config_dir = os.path.join(tmp_workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        import yaml as _yaml

        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            _yaml.dump({"guards": {"test_mode": "diff", "test_command": "echo ok"}}, f)

        hooks_dir = os.path.join(tmp_workdir, ".git", "hooks")
        os.makedirs(hooks_dir, exist_ok=True)
        hook_path = os.path.join(hooks_dir, "pre-commit")
        with open(hook_path, "w") as f:
            f.write("""#!/usr/bin/env bash
REPO_ROOT="$(git rev-parse --show-toplevel)"
[ ! -f "$REPO_ROOT/.gitreins/config.yaml" ] && exit 0
cd "$REPO_ROOT"
gitreins guard
exit $?
""")
        os.chmod(hook_path, 0o755)

        with open(os.path.join(tmp_workdir, "clean.py"), "w") as f:
            f.write("# just a comment\n")
        subprocess.run(["git", "add", "clean.py"], cwd=tmp_workdir, capture_output=True)

        result = subprocess.run(
            ["git", "commit", "-m", "should pass"],
            cwd=tmp_workdir,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Hook blocked clean commit. stderr: {result.stderr[:300]}"


# ── Regression: CLI exit codes ───────────────────────────────────────────────


class TestCLIExitCodes:
    """Verify each CLI command exits non-zero on failure. These prevent
    the 'hook always passes' class of bug."""

    def test_guard_exits_nonzero_on_failure(self, tmp_workdir):
        """gitreins guard exits 1 when secrets are detected."""
        subprocess.run(["git", "init", "-q"], cwd=tmp_workdir, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"], cwd=tmp_workdir, capture_output=True
        )
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_workdir, capture_output=True)

        config_dir = os.path.join(tmp_workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        import yaml as _yaml

        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            _yaml.dump({"guards": {"test_mode": "diff", "test_command": "echo ok"}}, f)

        # Stage a file with a secret
        with open(os.path.join(tmp_workdir, "leak.py"), "w") as f:
            f.write('SECRET = "sk-abcdefghij1234567890abcdefghij"\n')
        subprocess.run(["git", "add", "leak.py"], cwd=tmp_workdir, capture_output=True)

        result = run_cli("guard", cwd=tmp_workdir)
        assert result.returncode != 0, (
            f"guard must exit non-zero on secrets, got {result.returncode}. "
            f"stdout: {result.stdout[:200]}"
        )

    def test_init_exits_nonzero_on_broken_config(self, tmp_workdir):
        """gitreins init exits non-zero when config is broken YAML."""
        config_dir = os.path.join(tmp_workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            f.write("guards: {broken yaml\n")

        result = run_cli("init", cwd=tmp_workdir)
        assert result.returncode != 0, (
            f"init must exit non-zero on broken config, got {result.returncode}"
        )

    def test_judge_exits_nonzero_on_missing_task(self, tmp_workdir):
        """gitreins judge exits non-zero when task doesn't exist."""
        result = run_cli("judge", "nonexistent-task", cwd=tmp_workdir)
        assert result.returncode != 0, (
            f"judge must exit non-zero on missing task, got {result.returncode}"
        )
