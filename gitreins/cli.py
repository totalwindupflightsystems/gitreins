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

guards:
  secrets: true
  lint: true
  tests: true
  test_command: "pytest -x --tb=short"
  # dead_code: true    # opt-in: Python dead-code detection (AST-based)
  # skylos: true       # opt-in: multi-language dead code + AI mistake detection

evaluator:
  max_iterations: 15
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


def cmd_install(args):
    """One-command GitReins activation for the current repo.

    Creates:
      - .gitreins/config.yaml   (default config if missing)
      - .git/hooks/pre-commit   (runs `gitreins guard` on staged changes)
      - .gitignore              (adds .gitreins/tasks.yaml if not already present)
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
    print("  - Create a task:  gitreins task create <id> <title> [criteria...]")
    print("  - Run guards:     gitreins guard")
    print("  - Try the hook:   make a change, git add ., git commit -m 'test'")


def cmd_task_create(args):
    from engine.task_manager import TaskManager
    tm = TaskManager(get_workdir())
    criteria = args.criteria if args.criteria else []
    task = tm.create(args.id, args.title, criteria)
    print(f"Created task: {task.id} — {task.title}")
    for i, c in enumerate(task.criteria, 1):
        print(f"  {i}. {c}")


def cmd_task_start(args):
    from engine.task_manager import TaskManager
    tm = TaskManager(get_workdir())
    task = tm.start(args.id)
    print(f"Started: {task.id} → {task.status}")


def cmd_task_complete(args):
    from engine.task_manager import TaskManager
    from engine.llm import LLMClient
    from engine.judge import Judge

    workdir = get_workdir()
    tm = TaskManager(workdir)
    task = tm.complete(args.id)
    print(f"Completed: {task.id} → {task.status}")

    print("\nEvaluating...")
    llm = LLMClient()
    judge = Judge(llm, workdir)
    result = judge.evaluate_task(task)
    print(result.summary)


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


def cmd_guard_run(args):
    from engine.guard_manager import GuardManager
    workdir = get_workdir()
    config = load_config(workdir)
    gm = GuardManager(workdir, config=config)
    result = gm.run_all()
    print(f"Tier 1 Guards: {'PASS' if result.passed else 'FAIL'}")
    print(result.summary)


def cmd_judge(args):
    from engine.task_manager import TaskManager
    from engine.llm import LLMClient
    from engine.judge import Judge

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

    # task
    task_p = sub.add_parser("task", help="Task management")
    task_sub = task_p.add_subparsers(dest="subcommand")

    create_p = task_sub.add_parser("create", help="Create a task")
    create_p.add_argument("id")
    create_p.add_argument("title")
    create_p.add_argument("criteria", nargs="*")

    start_p = task_sub.add_parser("start", help="Start a task")
    start_p.add_argument("id")

    complete_p = task_sub.add_parser("complete", help="Complete and evaluate a task")
    complete_p.add_argument("id")

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

    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(
        level=logging.WARNING,  # Only show warnings+ by default
        format="%(name)s: %(levelname)s: %(message)s",
    )

    if args.command == "install":
        cmd_install(args)
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
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
