#!/usr/bin/env python3
"""
GitReins CLI — Human-usable command line.

Usage:
    gitreins install
    gitreins task create <id> <title> [criteria...]
    gitreins task start <id>
    gitreins task complete <id>
    gitreins task list [--status pending|in_progress|complete]
    gitreins task delete <id>
    gitreins guard run
    gitreins judge <id>
    gitreins commit <message>
    gitreins mcp-server
"""

DEFAULT_GITREINS_CONFIG = """\
# GitReins Configuration

# ── Global defaults (overrides engine.config.GitReinsDefaults) ─────
defaults:
  # model: deepseek-v4-flash            # default LLM
  max_iterations: 100                   # LLM turns (-1 = unlimited)
  # max_time: "30m"                     # wall clock
  max_input_tokens: "10M"               # 10 million
  max_output_tokens: "1M"               # 1 million
  tool_call_weight: 0.1                 # fraction per tool call
  check_for_updates: true               # check PyPI on each run
  update_check_ttl: "24h"               # re-check after this period

# ── Guards (Tier 1 static checks) ─────────────────────────────────
guards:
  secrets: true
  lint: true
  tests: true
  test_mode: "full"                      # "full" or "diff" (smart)
  test_command: "pytest -x --tb=short"
  # dead_code: true    # opt-in: Python dead-code detection (AST-based)
  # skylos: true       # opt-in: multi-language dead code + AI mistake detection

# ── Evaluator caps ───────────────────────────────────────────────
evaluator:
  max_iterations: 100

# ── Verdict history persistence ──────────────────────────────────
history:
  enabled: true           # false = don't save verdicts
  # path: ".gitreins/history"   # where to store (relative to repo)
  storage: "git"          # "git" = auto-commit to gitreins branch
                          # "filesystem" = write files only, no git
  max_verdicts: 1000      # auto-prune old entries past this limit
"""

PRE_COMMIT_HOOK = """\
#!/usr/bin/env bash
# GitReins pre-commit hook — runs Tier 1 guards on staged changes.

# Skip cleanly if the repo hasn't been initialised with a config.
REPO_ROOT="$(git rev-parse --show-toplevel)"
if [ ! -f "$REPO_ROOT/.gitreins/config.yaml" ]; then
    exit 0
fi

cd "$REPO_ROOT"
gitreins guard
exit $?
"""

import argparse
import logging
import os
import sys
import yaml

from engine.version import __version__


def load_config(workdir: str) -> dict:
    """Load .gitreins/config.yaml, returning {} if not found."""
    config_path = os.path.join(workdir, ".gitreins", "config.yaml")
    if not os.path.isfile(config_path):
        return {}
    try:
        with open(config_path, "r") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def get_workdir() -> str:
    """Find the git repo root."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout.strip()
    except Exception:
        return os.getcwd()


def _check_for_updates():
    """Check PyPI for a newer version. Prints notice to stderr if available."""
    try:
        from engine.config import check_for_update
        msg = check_for_update(workdir=get_workdir())
        if msg:
            print(f"  \033[33m{msg}\033[0m", file=sys.stderr)
    except Exception:
        pass  # never block on update check failures


def cmd_install(args):
    """One-command GitReins activation for the current repo.

    Creates:
      - .gitreins/config.yaml   (default config if missing)
      - .git/hooks/pre-commit   (runs `gitreins guard` on staged changes)
      - .gitignore              (adds .gitreins/tasks.yaml if not already present)

    For smarter auto-detection, use: gitreins init
    """
    import subprocess

    workdir = get_workdir()
    git_dir = os.path.join(workdir, ".git")
    hooks_dir = os.path.join(git_dir, "hooks")
    gitreins_dir = os.path.join(workdir, ".gitreins")
    config_path = os.path.join(gitreins_dir, "config.yaml")
    hook_path = os.path.join(hooks_dir, "pre-commit")
    gitignore_path = os.path.join(workdir, ".gitignore")
    tasks_entry = ".gitreins/tasks.yaml"

    if not os.path.isdir(git_dir):
        print(f"Error: {workdir} is not a git repository (no .git directory).")
        print("Run `git init` first, then re-run `gitreins install`.")
        sys.exit(1)

    created = []
    skipped = []

    # 1. .gitreins/config.yaml
    os.makedirs(gitreins_dir, exist_ok=True)
    if os.path.isfile(config_path):
        skipped.append(config_path)
    else:
        with open(config_path, "w") as f:
            f.write(DEFAULT_GITREINS_CONFIG)
        created.append(config_path)

    # 2. .git/hooks/pre-commit
    os.makedirs(hooks_dir, exist_ok=True)
    hook_existed = os.path.isfile(hook_path)
    with open(hook_path, "w") as f:
        f.write(PRE_COMMIT_HOOK)
    os.chmod(hook_path, 0o755)
    created.append(hook_path + ("" if not hook_existed else " (overwritten)"))

    # 3. .gitignore — add .gitreins/tasks.yaml if not present
    existing_gitignore = ""
    if os.path.isfile(gitignore_path):
        with open(gitignore_path, "r") as f:
            existing_gitignore = f.read()
    already_present = any(
        line.strip() == tasks_entry
        for line in existing_gitignore.splitlines()
    )
    if already_present:
        skipped.append(f"{tasks_entry} already in .gitignore")
    else:
        with open(gitignore_path, "a") as f:
            if existing_gitignore and not existing_gitignore.endswith("\n"):
                f.write("\n")
            f.write(tasks_entry + "\n")
        created.append(f".gitignore (added {tasks_entry})")

    # 4. Success summary
    print(f"GitReins installed in {workdir}")
    print()
    print("Created:")
    for path in created:
        print(f"  + {path}")
    if skipped:
        print()
        print("Skipped:")
        for path in skipped:
            print(f"  - {path}")
    print()
    print("Next steps:")
    print("  - Run smart init:  gitreins init")
    print("  - Create a task:  gitreins task create <id> <title> [criteria...]")
    print("  - Run guards:     gitreins guard")
    print("  - Try the hook:   make a change, git add ., git commit -m 'test'")


def cmd_init(args):
    """Smart project initialization — detects language, size, and optimal config.

    Re-runnable: never overwrites existing config values, only adds missing sections.
    Use to upgrade config when new GitReins features ship.
    """
    workdir = get_workdir()
    gitreins_dir = os.path.join(workdir, ".gitreins")
    config_path = os.path.join(gitreins_dir, "config.yaml")

    # Detect project characteristics
    lang_info = _detect_language(workdir)
    test_cmd = _detect_test_command(workdir, lang_info)
    size = _detect_project_size(workdir, lang_info)

    # Load existing config or start fresh
    existing = load_config(workdir)
    if not existing:
        existing = {}

    # Build or update sections
    changed = []

    # Guards section
    if "guards" not in existing or args.reset:
        existing["guards"] = _build_guards_section(lang_info, test_cmd)
        changed.append("guards")
    else:
        # Fill in missing guard keys
        guards = existing.setdefault("guards", {})
        updates = _fill_missing_guards(guards, lang_info, test_cmd)
        if updates:
            changed.append(f"guards (+{', '.join(updates)})")

    # Evaluator section — size-appropriate caps
    if "evaluator" not in existing or args.reset:
        existing["evaluator"] = _build_evaluator_section(size)
        changed.append("evaluator")
    else:
        evaluator = existing.setdefault("evaluator", {})
        if "max_iterations" not in evaluator:
            evaluator["max_iterations"] = size["max_iterations"]
            changed.append("evaluator.max_iterations")

    # History section
    if "history" not in existing or args.reset:
        existing["history"] = {
            "enabled": True,
            "storage": "git",
            "max_verdicts": 1000,
        }
        changed.append("history")

    # Write config
    os.makedirs(gitreins_dir, exist_ok=True)
    with open(config_path, "w") as f:
        yaml.dump(existing, f, default_flow_style=False, sort_keys=False)

    # Ensure pre-commit hook exists
    hook_path = os.path.join(workdir, ".git", "hooks", "pre-commit")
    if not os.path.isfile(hook_path) or args.reset:
        with open(hook_path, "w") as f:
            f.write(PRE_COMMIT_HOOK)
        os.chmod(hook_path, 0o755)
        changed.append("pre-commit hook")

    # Summary
    print(f"GitReins init: {workdir}")
    print(f"  Language:    {lang_info['name']}")
    print(f"  Packages:    {size['packages']}")
    print(f"  Test cmd:    {test_cmd}")
    print(f"  Test mode:   {existing['guards'].get('test_mode', 'full')}")
    print(f"  Eval cap:    {existing['evaluator'].get('max_iterations', 100)} iterations")
    print(f"  History:     {existing.get('history', {}).get('enabled', True) and 'enabled' or 'disabled'}")
    print()
    if changed:
        print(f"Updated: {', '.join(changed)}")
    else:
        print("No changes needed — config is up to date.")


def _detect_language(workdir: str) -> dict:
    """Detect project language(s). Returns {name, type, is_go, is_python, is_ts}."""
    info = {"name": "unknown", "type": "unknown", "is_go": False, "is_python": False, "is_ts": False}

    if os.path.isfile(os.path.join(workdir, "go.mod")):
        info["type"] = "go"
        info["is_go"] = True
        info["name"] = "Go"
    elif os.path.isfile(os.path.join(workdir, "pyproject.toml")) or \
         os.path.isfile(os.path.join(workdir, "setup.py")) or \
         os.path.isfile(os.path.join(workdir, "setup.cfg")):
        info["type"] = "python"
        info["is_python"] = True
        info["name"] = "Python"
    elif os.path.isfile(os.path.join(workdir, "package.json")):
        info["type"] = "typescript"
        info["is_ts"] = True
        info["name"] = "TypeScript"

    return info


def _detect_test_command(workdir: str, lang: dict) -> str:
    """Detect the right test command for the project."""
    if lang["is_go"]:
        # Check for Makefile first
        makefile = os.path.join(workdir, "Makefile")
        if os.path.isfile(makefile):
            with open(makefile) as f:
                content = f.read()
            if "go test" in content:
                return "go test -short -count=1 ./..."
        return "go test -short -count=1 ./..."
    elif lang["is_python"]:
        return "pytest -x --tb=short"
    elif lang["is_ts"]:
        # Check package.json for test script
        pkg = os.path.join(workdir, "package.json")
        if os.path.isfile(pkg):
            try:
                import json
                with open(pkg) as f:
                    data = json.load(f)
                if data.get("scripts", {}).get("test"):
                    return "npm test"
            except Exception:
                pass
        return "npx vitest run"
    return "pytest -x --tb=short"


def _detect_project_size(workdir: str, lang: dict) -> dict:
    """Estimate project size for evaluator cap recommendations."""
    packages = 0
    if lang["is_go"]:
        # Count Go packages
        import glob
        go_files = set()
        for root, dirs, files in os.walk(workdir):
            # Skip vendor, .git, node_modules
            dirs[:] = [d for d in dirs if d not in (".git", "vendor", "node_modules", ".gitreins")]
            for f in files:
                if f.endswith(".go"):
                    go_files.add(os.path.relpath(os.path.dirname(os.path.join(root, f)), workdir))
        packages = len(go_files)
    elif lang["is_python"]:
        import glob
        py_pkgs = set()
        for root, dirs, files in os.walk(workdir):
            dirs[:] = [d for d in dirs if d not in (".git", ".venv", "node_modules", ".gitreins", "__pycache__")]
            if "__init__.py" in files:
                py_pkgs.add(os.path.relpath(root, workdir))
        packages = len(py_pkgs) or 1

    # Cap recommendations
    if packages <= 3:
        max_iter = 15
    elif packages <= 10:
        max_iter = 25
    elif packages <= 25:
        max_iter = 50
    else:
        max_iter = 100

    return {
        "packages": packages,
        "max_iterations": max_iter,
        "test_mode": "full" if packages <= 5 else "diff",
    }


def _build_guards_section(lang: dict, test_cmd: str) -> dict:
    """Build guards section optimized for the detected language."""
    if lang["is_go"]:
        return {
            "secrets": True,
            "lint": False,
            "tests": False,
            "test_mode": "full",
            "go": {"build": True, "lint": True, "tests": True},
        }
    elif lang["is_python"]:
        return {
            "secrets": True,
            "lint": True,
            "tests": True,
            "test_mode": "full",
            "test_command": test_cmd,
        }
    elif lang["is_ts"]:
        return {
            "secrets": True,
            "lint": False,
            "tests": True,
            "test_mode": "full",
            "test_command": test_cmd,
        }
    else:
        return {
            "secrets": True,
            "lint": True,
            "tests": True,
            "test_mode": "full",
            "test_command": test_cmd,
        }


def _fill_missing_guards(guards: dict, lang: dict, test_cmd: str) -> list[str]:
    """Fill in missing guard keys without overwriting existing values. Returns keys added."""
    added = []
    defaults = _build_guards_section(lang, test_cmd)

    for key, val in defaults.items():
        if key not in guards:
            guards[key] = val
            added.append(key)

    # For Go projects, ensure go: section exists
    if lang["is_go"] and "go" not in guards:
        guards["go"] = {"build": True, "lint": True, "tests": True}
        added.append("go")

    return added


def _build_evaluator_section(size: dict) -> dict:
    """Build evaluator section with size-appropriate caps."""
    return {
        "max_iterations": size["max_iterations"],
    }


def cmd_task_create(args):
    from engine.task_manager import TaskManager
    tm = TaskManager(get_workdir())
    criteria = args.criteria if args.criteria else []
    depends_on = args.depends_on if hasattr(args, "depends_on") and args.depends_on else []
    task = tm.create(args.id, args.title, criteria, depends_on=depends_on)
    print(f"Created task: {task.id} — {task.title}")
    if task.depends_on:
        print(f"  Depends on: {', '.join(task.depends_on)}")
    for i, c in enumerate(task.criteria, 1):
        print(f"  {i}. {c}")


def cmd_task_start(args):
    from engine.task_manager import TaskManager
    tm = TaskManager(get_workdir())
    task = tm.start(args.id)
    print(f"Started: {task.id} → {task.status}")


def cmd_task_complete(args):
    from engine.task_manager import TaskManager, DependencyError
    from engine.llm import LLMClient
    from engine.judge import Judge
    from engine.persist import VerdictPersister

    workdir = get_workdir()
    tm = TaskManager(workdir)

    force = getattr(args, "force", False)

    # Check dependencies (unless forced)
    if not force:
        blocked = tm.check_dependencies(args.id)
        if blocked:
            print(f"Cannot complete '{args.id}' — depends on incomplete tasks: {', '.join(blocked)}")
            print("Complete those tasks first, or use --force to skip dependency checks.")
            sys.exit(1)

    task = tm.complete(args.id, force=force)
    print(f"Completed: {task.id} → {task.status}")

    print("\nEvaluating...")
    llm = LLMClient()
    judge = Judge(llm, workdir)
    result = judge.evaluate_task(task)
    print(result.summary)

    # Persist verdict
    _persist_result(workdir, task, result)


def cmd_task_list(args):
    from engine.task_manager import TaskManager
    tm = TaskManager(get_workdir())
    tasks = tm.list_tasks(args.status)
    if not tasks:
        print("No tasks found.")
        return
    for t in tasks:
        status_icon = {"pending": "○", "in_progress": "◐", "complete": "●"}.get(t.status, "?")
        print(f"  {status_icon} {t.id:<20} {t.title}")


def cmd_task_delete(args):
    from engine.task_manager import TaskManager
    tm = TaskManager(get_workdir())
    tm.delete(args.id)
    print(f"Deleted: {args.id}")


def _persist_result(workdir: str, task, result) -> None:
    """Save evaluation verdict to history. Non-fatal — logs on failure."""
    try:
        from engine.persist import VerdictPersister

        persister = VerdictPersister(workdir)
        if not persister.enabled:
            return

        # Build verdict data from result
        verdict_data = {
            "task_id": task.id,
            "task_title": task.title,
            "task_criteria": task.criteria,
            "passed": result.passed,
        }

        # Extract items from verdict or pipeline result
        if result.verdict and hasattr(result.verdict, "items"):
            verdict_data["items"] = [
                {"criterion": item.criterion, "status": item.status, "detail": item.detail}
                for item in result.verdict.items
            ]
        else:
            verdict_data["items"] = []

        # Pipeline stages
        if result.pipeline_result:
            verdict_data["stages"] = result.pipeline_result.get("stages", {})

        # Summary text
        verdict_data["summary"] = result.summary

        commit_hash = persister.persist(task.id, verdict_data)
        if commit_hash == "disabled":
            pass  # user opted out
        elif commit_hash == "dry-run":
            print("  ⚠ Verdict saved to disk but not committed (git unavailable)", file=sys.stderr)
        else:
            print(f"  📋 Verdict saved: {commit_hash}")

    except Exception:
        print("  ⚠ Failed to persist verdict (non-fatal)", file=sys.stderr)


def cmd_report(args):
    """Show recent verdict history."""
    from engine.persist import build_report

    workdir = get_workdir()
    n = args.n if hasattr(args, "n") else 10

    # Interactive TUI mode
    if args.interactive:
        _cmd_report_tui(workdir, n)
        return

    report = build_report(workdir, n=n)
    print(report)


def _cmd_report_tui(workdir: str, n: int = 20):
    """Interactive TUI for verdict browsing (requires textual)."""
    from engine.persist import build_report, VerdictPersister

    try:
        import textual
    except ImportError:
        print("Interactive mode requires 'textual'. Install with: pip install textual")
        print("Falling back to text mode...")
        print()
        print(build_report(workdir, n=n))
        return

    persister = VerdictPersister(workdir)

    if not persister.enabled:
        print("History is disabled (history.enabled = false in config).")
        return

    entries = persister.list_verdicts(n=n)
    if not entries:
        print("No verdict history found.")
        return

    # Textual TUI
    from textual.app import App, ComposeResult
    from textual.containers import VerticalScroll
    from textual.widgets import Header, Footer, Static

    verdict_lines = []
    for entry in entries:
        icon = "✓" if entry.get("passed") else "✗"
        task_id = entry.get("task_id", "?")
        date = entry.get("_date", "?")
        title = entry.get("task_title", task_id)
        items = entry.get("items", [])
        criteria = ""
        if items:
            parts = []
            for item in items:
                if isinstance(item, dict):
                    s = "✓" if item.get("status") == "PASS" else "✗"
                else:
                    s = "✓" if getattr(item, "status", None) == "PASS" else "✗"
                parts.append(s)
            criteria = f" [{''.join(parts)}]"
        verdict_lines.append(f"{icon} {task_id:<24} {date}  {criteria}")
        if title and title != task_id:
            verdict_lines.append(f"   {title}")

    class VerdictApp(App):
        CSS = """
        Screen { background: #0d1117; }
        Static { color: #c9d1d9; }
        Static.green { color: #3fb950; }
        Static.red { color: #f85149; }
        """

        def compose(self) -> ComposeResult:
            yield Header()
            yield VerticalScroll(Static("\n".join(verdict_lines), markup=False))
            yield Footer()

    app = VerdictApp()
    app.run()


def cmd_guard_run(args):
    _check_for_updates()
    from engine.guard_manager import GuardManager
    workdir = get_workdir()
    config = load_config(workdir)
    gm = GuardManager(workdir, config=config)
    result = gm.run_all()

    # Build mode note
    mode = gm.test_mode
    extra = result.extra
    mode_note = f"  (test mode: {mode}"
    if extra.get("test_targets"):
        mode_note += f", {extra['test_targets']} test file(s)"
    elif extra.get("test_targets") is None and mode == "diff":
        mode_note += ", full suite — safety trigger"
    mode_note += ")"

    print(f"Tier 1 Guards: {'PASS' if result.passed else 'FAIL'}{mode_note}")
    print(result.summary)

    if not result.passed:
        print()
        print("Fix the issues above and re-run: gitreins guard")


def cmd_judge(args):
    _check_for_updates()
    from engine.task_manager import TaskManager
    from engine.llm import LLMClient
    from engine.judge import Judge
    from engine.persist import VerdictPersister

    workdir = get_workdir()
    tm = TaskManager(workdir)
    task = tm.get(args.id)
    if not task:
        print(f"Task not found: {args.id}")
        sys.exit(1)

    llm = LLMClient()
    judge = Judge(llm, workdir)
    result = judge.evaluate_task(task)
    print(result.summary)

    # Persist verdict
    _persist_result(workdir, task, result)


def cmd_commit(args):
    import subprocess
    from engine.guard_manager import GuardManager

    workdir = get_workdir()
    config = load_config(workdir)
    gm = GuardManager(workdir, config=config)
    tier1 = gm.run_all()

    if not tier1.passed:
        print("Tier 1 FAILED — cannot commit:")
        print(tier1.summary)
        sys.exit(1)

    print("Tier 1 PASSED — committing...")
    result = subprocess.run(
        ["git", "commit", "-m", args.message],
        capture_output=True, text=True,
        cwd=workdir,
    )
    print(result.stdout + result.stderr)


def cmd_mcp_server(args):
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from gitreins_mcp.server import GitReinsMCPServer
    server = GitReinsMCPServer(get_workdir())
    server.run_stdio()


def main():
    parser = argparse.ArgumentParser(description="GitReins — Git-Native Agent Co-Harness")
    parser.add_argument("--version", action="version", version=f"gitreins {__version__}")
    sub = parser.add_subparsers(dest="command")

    # install
    sub.add_parser("install", help="Install GitReins hooks and config in the current repo")

    # init
    init_p = sub.add_parser("init", help="Smart init — detect language, size, optimal config")
    init_p.add_argument("--reset", action="store_true", help="Reset config to smart defaults")

    # task
    task_p = sub.add_parser("task", help="Task management")
    task_sub = task_p.add_subparsers(dest="subcommand")

    create_p = task_sub.add_parser("create", help="Create a task")
    create_p.add_argument("id")
    create_p.add_argument("title")
    create_p.add_argument("criteria", nargs="*")
    create_p.add_argument("--depends-on", action="append", default=[], help="Task ID that must complete first (repeatable)")

    start_p = task_sub.add_parser("start", help="Start a task")
    start_p.add_argument("id")

    complete_p = task_sub.add_parser("complete", help="Complete and evaluate a task")
    complete_p.add_argument("id")
    complete_p.add_argument("--force", "-f", action="store_true", help="Skip dependency checks")

    list_p = task_sub.add_parser("list", help="List tasks")
    list_p.add_argument("--status", choices=["pending", "in_progress", "complete"])

    delete_p = task_sub.add_parser("delete", help="Delete a task")
    delete_p.add_argument("id")

    # guard
    sub.add_parser("guard", help="Run Tier 1 guards")

    # judge
    judge_p = sub.add_parser("judge", help="Evaluate a task")
    judge_p.add_argument("id")

    # commit
    commit_p = sub.add_parser("commit", help="Commit with guard checks")
    commit_p.add_argument("message")

    # mcp-server
    sub.add_parser("mcp-server", help="Run MCP stdio server")

    # report
    report_p = sub.add_parser("report", help="Show verdict history")
    report_p.add_argument("-n", type=int, default=10, help="Number of recent verdicts to show")
    report_p.add_argument("--interactive", "-i", action="store_true", help="Interactive TUI mode")

    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=logging.WARNING,  # Only show warnings+ by default
        format="%(name)s: %(levelname)s: %(message)s",
    )

    if args.command == "install":
        cmd_install(args)
    elif args.command == "init":
        cmd_init(args)
    elif args.command == "task":
        if args.subcommand == "create":
            cmd_task_create(args)
        elif args.subcommand == "start":
            cmd_task_start(args)
        elif args.subcommand == "complete":
            cmd_task_complete(args)
        elif args.subcommand == "list":
            cmd_task_list(args)
        elif args.subcommand == "delete":
            cmd_task_delete(args)
        else:
            parser.print_help()
    elif args.command == "guard":
        cmd_guard_run(args)
    elif args.command == "judge":
        cmd_judge(args)
    elif args.command == "commit":
        cmd_commit(args)
    elif args.command == "mcp-server":
        cmd_mcp_server(args)
    elif args.command == "report":
        cmd_report(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
