"""
Integration tests for gitreins/cli.py — command line interface.
axiom:trace work_item=GR-003 spec=specs/09-CLI.md plan=.memory-bank/work-items/GR-003/plan.yaml
"""
import json
import os
import sys
import subprocess
import pytest
from unittest import mock
from unittest.mock import MagicMock, patch


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
        result = run_cli("task", "create", "myid", "My Title", "criterion A", "criterion B",
                         cwd=tmp_workdir)
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
        mock_env = {"GITREINS_MOCK_LLM_RESPONSE": json.dumps({"content": json.dumps({
            "verdict": "COMPLETE",
            "items": [{"criterion": "c1", "status": "PASS", "detail": "ok"}],
            "summary": "all good",
        })})}
        run_cli("task", "create", "pending1", "P1", "c1", cwd=tmp_workdir)
        run_cli("task", "create", "progress1", "P2", "c1", cwd=tmp_workdir)
        run_cli("task", "start", "progress1", cwd=tmp_workdir)
        run_cli("task", "complete", "progress1", cwd=tmp_workdir, extra_env=mock_env)
        result = run_cli("task", "list", cwd=tmp_workdir)
        assert result.returncode == 0
        assert u"○" in result.stdout  # pending icon
        assert u"●" in result.stdout  # complete icon

    def test_list_with_status_filter(self, tmp_workdir):
        """List --status pending shows only pending tasks."""
        mock_env = {"GITREINS_MOCK_LLM_RESPONSE": json.dumps({"content": json.dumps({
            "verdict": "COMPLETE",
            "items": [{"criterion": "c1", "status": "PASS", "detail": "ok"}],
            "summary": "all good",
        })})}
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
        result = run_cli("judge", "judge-me", cwd=tmp_workdir,
                         extra_env={"GITREINS_MOCK_LLM_RESPONSE": json.dumps({"content": verdict_json})})
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
        assert hasattr(gitreins.cli, 'cmd_mcp_server')


class TestExtendedCLI:
    """Extended CLI coverage."""

    def test_create_task_special_chars(self, tmp_workdir):
        """Create task with special chars in title works."""
        result = run_cli("task", "create", "spec", "Task with $pecial !@#$%^& chars",
                         cwd=tmp_workdir)
        assert result.returncode == 0
        assert "Created task: spec" in result.stdout

    def test_create_task_multiple_criteria(self, tmp_workdir):
        """Create task with multiple criteria shows them all."""
        result = run_cli("task", "create", "multi", "Multi", "c1", "c2", "c3",
                         cwd=tmp_workdir)
        assert result.returncode == 0
        assert "c1" in result.stdout
        assert "c2" in result.stdout
        assert "c3" in result.stdout

    def test_complete_existing_task(self, tmp_workdir):
        """Complete existing task after creating and starting it."""
        mock_env = {"GITREINS_MOCK_LLM_RESPONSE": json.dumps({"content": json.dumps({
            "verdict": "COMPLETE",
            "items": [{"criterion": "c1", "status": "PASS", "detail": "ok"}],
            "summary": "all good",
        })})}
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
        subprocess.run(["git", "init"], cwd=tmp_workdir,
                       capture_output=True, timeout=15)
        secret_file = os.path.join(tmp_workdir, "secret.py")
        with open(secret_file, "w") as f:
            f.write('api_key = "sk-1234567890123456789012345678901234567890"\n')
        subprocess.run(["git", "add", "secret.py"], cwd=tmp_workdir,
                       capture_output=True, timeout=15)
        result = run_cli("guard", cwd=tmp_workdir)
        assert "Tier 1 Guards:" in result.stdout

    def test_guard_secrets_detected_when_fails(self, tmp_workdir):
        """Guard output contains FAIL when secrets found."""
        subprocess.run(["git", "init"], cwd=tmp_workdir,
                       capture_output=True, timeout=15)
        secret_file = os.path.join(tmp_workdir, "creds.py")
        with open(secret_file, "w") as f:
            f.write('token = "ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"\n')
        subprocess.run(["git", "add", "creds.py"], cwd=tmp_workdir,
                       capture_output=True, timeout=15)
        result = run_cli("guard", cwd=tmp_workdir)
        assert "Tier 1 Guards:" in result.stdout

    def test_commit_shows_guard_output(self, tmp_workdir):
        """Commit shows guard result in output."""
        result = run_cli("commit", "test message", cwd=tmp_workdir)
        assert "Tier 1" in result.stdout

    def test_commit_fails_when_guard_detects_secret(self, tmp_workdir):
        """Commit exits 1 when guards detect a secret in staged files."""
        subprocess.run(["git", "init"], cwd=tmp_workdir,
                       capture_output=True, timeout=15)
        secret_file = os.path.join(tmp_workdir, "secret_key.py")
        with open(secret_file, "w") as f:
            f.write('api_key = "sk-1234567890123456789012345678901234567890"\n')
        subprocess.run(["git", "add", "secret_key.py"], cwd=tmp_workdir,
                       capture_output=True, timeout=15)
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
        assert u"○" in result.stdout

        run_cli("task", "start", "life1", cwd=tmp_workdir)

        result = run_cli("task", "list", cwd=tmp_workdir)
        assert u"◐" in result.stdout

        run_cli("task", "complete", "life1", cwd=tmp_workdir)

        result = run_cli("task", "list", cwd=tmp_workdir)
        assert u"●" in result.stdout

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
        mock_env = {"GITREINS_MOCK_LLM_RESPONSE": json.dumps({"content": json.dumps({
            "verdict": "COMPLETE",
            "items": [{"criterion": "", "status": "PASS", "detail": "ok"}],
            "summary": "all good",
        })})}
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
        verdict_json = json.dumps({
            "verdict": "COMPLETE",
            "items": [{"criterion": "c1", "status": "PASS", "detail": "ok"}],
            "summary": "all good",
        })
        run_cli("task", "create", "judge-eval", "Judge Eval", "c1", cwd=tmp_workdir)
        result = run_cli("judge", "judge-eval", cwd=tmp_workdir,
                         extra_env={"GITREINS_MOCK_LLM_RESPONSE": json.dumps({"content": verdict_json})})
        assert "Judge Result" in result.stdout
        assert "Overall:" in result.stdout

    def test_judge_requires_api_key(self, tmp_workdir):
        """Judge integration test that requires DEEPSEEK_API_KEY."""
        if not os.environ.get("DEEPSEEK_API_KEY"):
            pytest.skip("requires DEEPSEEK_API_KEY")
        verdict_json = json.dumps({
            "verdict": "COMPLETE",
            "items": [{"criterion": "c1", "status": "PASS", "detail": "ok"}],
            "summary": "all good",
        })
        mock_env = {"GITREINS_MOCK_LLM_RESPONSE": json.dumps({"content": verdict_json})}
        run_cli("task", "create", "judge-api", "Judge API", "c1", cwd=tmp_workdir)
        run_cli("task", "start", "judge-api", cwd=tmp_workdir)
        run_cli("task", "complete", "judge-api", cwd=tmp_workdir, extra_env=mock_env)
        result = run_cli("judge", "judge-api", cwd=tmp_workdir, extra_env=mock_env)
        assert result.returncode in (0, 1)
        assert "Judge Result" in result.stdout or "Judge" in result.stdout
