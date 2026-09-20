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
    gitreins task worktree <id> [--tick <id>]
    gitreins worktree doctor
    gitreins worktree list
    gitreins worktree fleet <manifest.json> [--merge]
    gitreins worktree fresh --cmd "<command>" [--keep --timeout <seconds>]
    gitreins worktree repro --cmd "<command>" -k <N> [--concurrency <C>]
    gitreins worktree dogfood [--keep --skip-judge]
    gitreins worktree clean [--confirm-stale-orphan]
    gitreins worktree merge <id> [--force --actor <actor>]
    gitreins qa list [--json]
    gitreins qa record --project <name> [--verdict PASS|FAIL --cell <name>=<status> ...]
    gitreins guard run
    gitreins judge <id>
    gitreins commit <message>
    gitreins mcp-server
    gitreins serve [--repo <path>] [--port <port>] [--project <name>]
"""

import argparse
import json
import logging
import os
import shlex
import subprocess
import sys
import time
import yaml

from engine.version import __version__
from engine.repo_paths import (
    WorktreeResolutionError,
    resolve_worktree_paths,
)

INSTALL_DEFAULT_TEST_COMMAND = "pytest -x --tb=short"
GITREINS_GITIGNORE_ENTRIES = (
    ".gitreins/tasks.yaml",
    ".gitreins/config.yaml.bak",
    ".gitreins/usage.jsonl",
    # DF-018: guard run logs (one full log per guard run). Without this
    # entry every consumer repo that runs the guard would show an untracked
    # .gitreins/logs/ in `git status`.
    ".gitreins/logs/",
)

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
  max_concurrent_worktrees: 2             # bounded fleet concurrency
  worktree_venv_source: ".venv"            # shared source under canonical main
  worktree_venv_name: ".venv"              # destination name in each tree

# ── Disposable and fleet worktrees ───────────────────────────────
worktree_fleet:
  disk_ceiling_mb: 4096                   # <=0 means unlimited
guards:
  secrets: true
  lint: true
  tests: true
  test_mode: "full"                      # "full" or "diff" (smart)
  test_command: "pytest -x --tb=short"
  # TRUST-001: a run where a substantive gate (lint/tests/lsp) did no work is a
  # DEGRADED pass and exits 2 unless this is true. `init` writes true so the
  # first commit on a fresh repo is not blocked; set false to fail loud.
  allow_skips: true
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
# The gitreins command is PINNED at install time (DF-011): a bare
# `gitreins` resolves via PATH at commit time and can silently run a
# different version that skips guards. The command below is replaced
# with the absolute path (or `python -m gitreins`) of the binary that
# ran `gitreins install`.

# Skip cleanly if the repo hasn't been initialised with a config.
REPO_ROOT="$(git rev-parse --show-toplevel)"
if [ ! -f "$REPO_ROOT/.gitreins/config.yaml" ]; then
    exit 0
fi

cd "$REPO_ROOT"
__GITREINS_CMD__
exit $?
"""


def _resolve_gitreins_invocation() -> str | None:
    """Resolve the gitreins invocation to hardcode in the pre-commit hook.

    The hook must run the SAME installation that ran `install`. A bare
    `gitreins` resolves via PATH at commit time and can silently pick a
    different version (DF-011: a stale 0.8.1 earlier on PATH let real
    secrets through while the repo's .venv had an older patch release). Resolution
    order:

      1. ``sys.argv[0]`` — the script that actually launched this
         process (the binary that ran `install`). Immune to PATH
         shadowing at install time AND at commit time.
      2. ``sys.executable -m gitreins`` — the interpreter running this
         code imports the same installation by construction.
      3. ``None`` — caller falls back to bare `gitreins` with a warning.

    Returns the shell-quoted invocation (without the ``guard`` command).
    """
    argv0 = os.path.realpath(sys.argv[0])
    if (
        argv0
        and os.path.basename(argv0) == "gitreins"
        and os.path.isfile(argv0)
        and os.access(argv0, os.X_OK)
    ):
        return shlex.quote(argv0)
    if sys.executable:
        return f"{shlex.quote(sys.executable)} -m gitreins"
    return None


def _render_pre_commit_hook() -> str:
    """Render the pre-commit hook with the gitreins invocation pinned."""
    invocation = _resolve_gitreins_invocation()
    if invocation is None:
        return PRE_COMMIT_HOOK.replace(
            "__GITREINS_CMD__",
            "# WARNING: no gitreins binary or interpreter resolvable at install\n"
            "# time — falling back to PATH lookup (a different version may run).\n"
            "gitreins guard",
        )
    return PRE_COMMIT_HOOK.replace("__GITREINS_CMD__", f"{invocation} guard")


def load_config(workdir: str) -> dict:
    """Load .gitreins/config.yaml, returning {} if not found.

    WARNING: If the file exists but cannot be parsed (YAML syntax error,
    encoding issue, etc.), this function logs a warning and returns {}.
    Callers that write config back to disk (e.g. cmd_init) MUST check
    whether the original file existed before treating an empty return
    as "no config" — otherwise they will overwrite a broken-but-valuable
    config file with auto-generated defaults.
    """
    logger = logging.getLogger("gitreins")
    config_path = os.path.join(workdir, ".gitreins", "config.yaml")
    if not os.path.isfile(config_path):
        return {}
    try:
        with open(config_path, "r") as f:
            data = yaml.safe_load(f)
            if data is None:
                return {}
            return data
    except yaml.YAMLError as e:
        logger.warning(
            "Failed to parse %s: %s — config file exists but YAML is invalid. "
            "Callers: do NOT overwrite this file with defaults. "
            "User: fix the YAML syntax before running 'gitreins init'.",
            config_path,
            e,
        )
        return {}
    except Exception as e:
        logger.warning("Failed to load %s: %s", config_path, e)
        return {}


def _require_guard_config(workdir: str) -> str:
    """Refuse to run guards in a repo with no .gitreins/config.yaml.

    GR-GAP-051: every guard falls back to built-in defaults when the
    config file is absent, so ``gitreins guard`` printed a green
    "Tier 1 Guards: PASS" and ``gitreins commit`` committed unguarded —
    a false green light (see DF-GITREINS-POC-2). Fail loud instead.

    Returns the config path when it exists. Prints the fix to stderr and
    exits 1 when it does not. Deliberately NOT inside
    ``GuardManager.run_all()`` — library/MCP callers and unit-test
    fixtures construct ``GuardManager`` directly with ``config=None``
    and must keep working.
    """
    config_path = os.path.join(workdir, ".gitreins", "config.yaml")
    if not os.path.isfile(config_path):
        print(
            "no .gitreins/config.yaml — run `gitreins init` first",
            file=sys.stderr,
        )
        sys.exit(1)
    return config_path


def _safe_overwrite(path: str, content_func) -> str | None:
    """Write content to path, backing up the original if it exists.

    Args:
        path: Absolute or relative path to write.
        content_func: Callable that writes to an open file handle, e.g.
            lambda f: f.write(text) or lambda f: yaml.dump(data, f).

    Creates parent directories if missing. Creates a .bak copy of the
    original file before overwriting. The caller is responsible for
    ensuring the content is valid before calling — this is a write
    safety net, not a content validator.

    Returns the bak_path if a backup was created, else None.
    """
    import io
    import shutil

    # Serialize to a buffer first so the write can be skipped entirely when
    # the content is unchanged — keeps 'gitreins init' truly idempotent
    # (DF-022): a second run on an up-to-date repo is a no-op (no rewrite,
    # no backup churn) and reports "No changes needed".
    buf = io.StringIO()
    content_func(buf)
    new_content = buf.getvalue().encode("utf-8")

    bak_path = None
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        with open(path, "rb") as f:
            old_content = f.read()
        if old_content == new_content:
            return None
        bak_path = path + ".bak"
        shutil.copy2(path, bak_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(new_content)
    return bak_path


def get_workdir() -> str:
    """Find the git repo root."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
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


def _ensure_gitignore_entry(workdir: str, entry: str) -> tuple[bool, str]:
    """Ensure `entry` is present in <workdir>/.gitignore (create if absent).

    Returns (changed, message). Shared by cmd_install and cmd_init so both
    activation paths protect GitReins' local runtime files from accidental
    commits (GR-GAP-025, DF-GITREINS-POC-3).
    """
    gitignore_path = os.path.join(workdir, ".gitignore")
    existing = ""
    if os.path.isfile(gitignore_path):
        with open(gitignore_path, "r") as f:
            existing = f.read()
    already_present = any(line.strip() == entry for line in existing.splitlines())
    if already_present:
        return False, f"{entry} already in .gitignore"
    with open(gitignore_path, "a") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(entry + "\n")
    return True, f".gitignore (added {entry})"


def _gitignore_entries_for_project(workdir: str, lang: dict | None = None) -> tuple[str, ...]:
    """Return the GitReins-generated files to protect in this project.

    The universal entries cover GitReins state and telemetry. Python projects
    also get the standard interpreter cache exclusion; other languages should
    not receive a language-specific rule just because ``install`` was run.
    """
    if lang is None:
        lang = _detect_language(workdir)
    entries = list(GITREINS_GITIGNORE_ENTRIES)
    if lang.get("is_python"):
        entries.append("__pycache__/")
    return tuple(entries)


def _ensure_gitignore_entries(workdir: str, entries: tuple[str, ...]) -> list[str]:
    """Append missing entries and return messages for entries that changed it."""
    changed = []
    for entry in entries:
        entry_changed, message = _ensure_gitignore_entry(workdir, entry)
        if entry_changed:
            changed.append(message)
    return changed


def cmd_install(args):
    """One-command GitReins activation for the current repo.

    Creates:
      - .gitreins/config.yaml   (default config if missing)
      - .git/hooks/pre-commit   (runs `gitreins guard` on staged changes)
      - .gitignore              (adds GitReins runtime entries, plus
                                 `__pycache__/` for Python projects)

    For smarter auto-detection, use: gitreins init
    """

    workdir = get_workdir()
    git_dir = os.path.join(workdir, ".git")
    hooks_dir = os.path.join(git_dir, "hooks")
    gitreins_dir = os.path.join(workdir, ".gitreins")
    config_path = os.path.join(gitreins_dir, "config.yaml")
    hook_path = os.path.join(hooks_dir, "pre-commit")

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
        f.write(_render_pre_commit_hook())
    os.chmod(hook_path, 0o755)
    created.append(hook_path + ("" if not hook_existed else " (overwritten)"))

    # 3. .gitignore — protect GitReins state and runtime artifacts
    for gitignore_msg in _ensure_gitignore_entries(
        workdir, _gitignore_entries_for_project(workdir)
    ):
        created.append(gitignore_msg)

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
    static_tools = _detect_static_analysis_tools(workdir, lang_info)

    # Load existing config or start fresh.
    # CRITICAL: load_config returns {} for BOTH "file doesn't exist" AND
    # "YAML parse error". We must NOT overwrite a broken-but-existent config
    # with auto-generated defaults — that silently nukes user settings.
    config_exists = os.path.isfile(config_path) and os.path.getsize(config_path) > 0
    existing = load_config(workdir)
    if not existing:
        if config_exists:
            print(
                f"Error: {config_path} exists but could not be parsed.\n"
                f"Fix the YAML syntax in that file, then re-run 'gitreins init'.\n"
                f"To start fresh (discarding existing config), use --reset.",
                file=sys.stderr,
            )
            sys.exit(1)
        existing = {}

    # Build or update sections
    changed = []

    # Guards section
    if "guards" not in existing or args.reset:
        existing["guards"] = _build_guards_section(lang_info, test_cmd, static_tools)
        # TRUST-001: fresh repos get allow_skips: true — the first `gitreins
        # guard` on a clean tree is a DEGRADED pass, and a brand-new repo has
        # nothing to stage yet. The code-level default stays False (fail loud)
        # for configs that predate this key.
        existing["guards"].setdefault("allow_skips", True)
        changed.append("guards")
    else:
        # Fill in missing guard keys, then upgrade only the exact default that
        # `install` wrote. Any other value is user-authored and stays intact.
        guards = existing.setdefault("guards", {})
        updates = _fill_missing_guards(guards, lang_info, test_cmd, static_tools)
        if _upgrade_install_default_test_command(guards, test_cmd):
            updates.append("test_command")
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

    # Disposable/fleet worktree policy — additive so existing settings stay intact.
    fleet = existing.setdefault("worktree_fleet", {})
    if not isinstance(fleet, dict):
        fleet = {}
        existing["worktree_fleet"] = fleet
        changed.append("worktree_fleet")
    if "disk_ceiling_mb" not in fleet:
        fleet["disk_ceiling_mb"] = 4096
        changed.append("worktree_fleet.disk_ceiling_mb")

    # Write config
    os.makedirs(gitreins_dir, exist_ok=True)
    bak = _safe_overwrite(
        config_path,
        lambda f: yaml.dump(
            existing,
            f,
            default_flow_style=False,
            sort_keys=False,
        ),
    )
    if bak:
        changed.append(f"backup: {os.path.basename(bak)}")

    # Ensure pre-commit hook exists.  ``.git`` is a file in linked and
    # detached worktrees, so resolve Git's common hooks path instead of
    # assuming a directory under the checkout.
    hooks_result = subprocess.run(
        ["git", "-C", workdir, "rev-parse", "--git-path", "hooks"],
        capture_output=True,
        text=True,
        check=False,
    )
    hooks_dir = hooks_result.stdout.strip() or os.path.join(workdir, ".git", "hooks")
    if not os.path.isabs(hooks_dir):
        hooks_dir = os.path.join(workdir, hooks_dir)
    hook_path = os.path.join(hooks_dir, "pre-commit")
    if not os.path.isfile(hook_path) or args.reset:
        os.makedirs(hooks_dir, exist_ok=True)
        with open(hook_path, "w") as f:
            f.write(_render_pre_commit_hook())
        os.chmod(hook_path, 0o755)
        changed.append("pre-commit hook")

    # Generate .gitleaks.toml if missing (prevents scanning node_modules/.venv/vendor)
    gitleaks_path = os.path.join(workdir, ".gitleaks.toml")
    if not os.path.isfile(gitleaks_path):
        _generate_gitleaks_config(workdir, lang_info, gitleaks_path)
        changed.append(".gitleaks.toml")

    # Ensure GitReins state and runtime artifacts stay local. This mirrors
    # cmd_install so either activation path is safe (GR-GAP-025).
    changed.extend(
        _ensure_gitignore_entries(workdir, _gitignore_entries_for_project(workdir, lang_info))
    )

    # Summary
    print(f"GitReins init: {workdir}")
    print(f"  Language:    {lang_info['name']}")
    if lang_info["name"] == "unknown":
        # GR-GAP-026: empty/source-less repos can't be detected — warn loudly
        # instead of silently writing a gutted config the user discovers later.
        print(
            "  Warning: no source files detected — language detection was\n"
            "  inconclusive, so static_analysis stays disabled and language-specific\n"
            "  guards (ruff, mypy, go vet, ...) will NOT be configured.\n"
            "  Add your source files, then re-run 'gitreins init'.",
            file=sys.stderr,
        )
    print(f"  Packages:    {size['packages']}")
    persisted_test_cmd = existing["guards"].get("test_command", test_cmd)
    print(f"  Test cmd:    {persisted_test_cmd}")
    if persisted_test_cmd == "python3 -m pytest -x --tb=short":
        print(
            "  Note: using 'python3 -m pytest' — root module/package layout without pytest pythonpath "
            'config; add [tool.pytest.ini_options] pythonpath = ["."] to pyproject.toml '
            "to use bare pytest"
        )
    print(f"  Test mode:   {existing['guards'].get('test_mode', 'full')}")
    print(f"  Eval cap:    {existing['evaluator'].get('max_iterations', 100)} iterations")
    print(
        f"  History:     {existing.get('history', {}).get('enabled', True) and 'enabled' or 'disabled'}"
    )
    print(f"  Static analysis: {_static_analysis_status(existing['guards'], lang_info)}")
    if existing["guards"].get("static_analysis", False):
        # DF-019: warn loudly instead of letting "enabled (…)" imply the tools
        # run. Static analysers are an opt-in install; `pip install gitreins`
        # does not bring them.
        absent_static = _missing_static_analysis_tools(existing["guards"])
        if absent_static:
            print(
                "  Warning: static analysis is enabled, but these configured tools are not\n"
                "  installed — the guard reports the step as skipped and grades nothing until\n"
                "  they are:\n" + "\n".join(_static_analysis_install_lines(absent_static)) + "\n"
                "  (run 'gitreins setup-tools' to re-check availability)",
                file=sys.stderr,
            )
    print()
    if changed:
        print(f"Updated: {', '.join(changed)}")
    else:
        print("No changes needed — config is up to date.")


# Canonical language token (engine.lang_detect) -> (cli flag, display name,
# legacy `type` token). The tokens are produced by engine.lang_detect, the
# single source of truth for language detection (DF-GITREINS-POC-16); this
# table only translates them into the dict shape `init`/`install` consume.
_LANG_INFO: dict[str, tuple[str, str, str]] = {
    "go": ("is_go", "Go", "go"),
    "python": ("is_python", "Python", "python"),
    "js": ("is_ts", "TypeScript", "typescript"),
    "ruby": ("is_ruby", "Ruby", "ruby"),
    "php": ("is_php", "PHP", "php"),
    "rust": ("is_rust", "Rust", "rust"),
    "java": ("", "Java", "java"),
    "kotlin": ("", "Kotlin", "kotlin"),
    "csharp": ("", "C#", "csharp"),
    "scala": ("", "Scala", "scala"),
    "c": ("", "C", "c"),
    "cpp": ("", "C++", "cpp"),
}


def _detect_language(workdir: str) -> dict:
    """Detect project language(s). Returns {name, type, is_go, is_python, is_ts, ...}.

    Delegates entirely to engine.lang_detect — the signature-file table, the
    source-extension fallback and the per-language command map live there and
    are shared with the judge's Tier 1 pipeline and the guard, so `init`,
    `guard` and `judge` can no longer disagree about what this repo is
    (DF-GITREINS-POC-16). Multi-language repos report every detected language:
    'type' is the primary (first) one, 'name' aggregates all found.
    """
    from engine import lang_detect

    info = {
        "name": "unknown",
        "type": "unknown",
        "is_go": False,
        "is_python": False,
        "is_ts": False,
        "is_ruby": False,
        "is_php": False,
        "is_rust": False,
        "has_sql": False,
    }
    langs_found: list[str] = []

    for token in lang_detect.detect_languages(workdir):
        flag, display, type_token = _LANG_INFO.get(token, ("", token.title(), token))
        if flag:
            info[flag] = True
        if display not in langs_found:
            langs_found.append(display)
        if info["type"] == "unknown":
            info["type"] = type_token

    # SQL detection runs regardless of other languages; it selects static
    # analysis tooling only (no lint/test command pair exists for SQL).
    if lang_detect.has_sql_sources(workdir):
        info["has_sql"] = True
        langs_found.append("SQL")

    if langs_found:
        info["name"] = " + ".join(langs_found)
    return info


def _detect_static_analysis_tools(workdir: str, lang: dict) -> list[str]:
    """Return list of installed static analysis tools for all detected languages."""
    from engine.static_analysis import list_available_tools

    tools: list[str] = []
    if lang["is_python"]:
        tools.extend(list_available_tools("python"))
    if lang["is_ruby"]:
        tools.extend(list_available_tools("ruby"))
    if lang["is_php"]:
        tools.extend(list_available_tools("php"))
    if lang["has_sql"]:
        tools.extend(list_available_tools("sql"))
    if lang["is_ts"]:
        tools.extend(list_available_tools("typescript"))
    if lang["is_rust"]:
        tools.extend(list_available_tools("rust"))
    return tools


def _detect_root_import_layout(workdir: str) -> bool:
    """True when the repo root has top-level Python packages or modules.

    A top-level Python package is a directory directly under the repo root
    that contains __init__.py; a top-level Python module is a *.py file
    directly under the repo root. Well-known non-package dirs (tests/,
    .venv, node_modules, .git, .gitreins, __pycache__) are excluded, as are
    well-known non-importable top-level build/bootstrap scripts
    (setup.py, conftest.py).

    DF-017: pytest 9 importlib mode leaves the repo root off sys.path, so
    tests importing a root package dir OR a root module file (e.g.
    weather.py) only work under `python3 -m pytest` (which prepends CWD);
    uv's `uv run pytest` entry point has the same blind spot.
    """
    # setup.py / conftest.py are top-level *.py build/bootstrap scripts that
    # tests never import from the repo root; a src/ (non-root) layout whose
    # only root .py files are these must not be misclassified as a root-import
    # layout (DF-017), otherwise uv run pytest is wrongly replaced by module pytest.
    excluded = {
        "tests",
        ".venv",
        "node_modules",
        ".git",
        ".gitreins",
        "__pycache__",
        "setup.py",
        "conftest.py",
    }
    try:
        with os.scandir(workdir) as it:
            for entry in it:
                try:
                    if entry.name in excluded:
                        continue
                    if entry.is_dir():
                        if os.path.isfile(os.path.join(workdir, entry.name, "__init__.py")):
                            return True
                    elif entry.is_file() and entry.name.endswith(".py"):
                        return True
                except OSError:
                    # Entry vanished or became unreadable between scandir and
                    # the probe read -- skip it rather than failing detection.
                    continue
    except OSError:
        return False
    return False


def _has_pytest_pythonpath_config(workdir: str) -> bool:
    """True when the project already configures pytest's pythonpath.

    Checks the standard locations pytest reads: pyproject.toml
    ([tool.pytest.ini_options] pythonpath), pytest.ini / tox.ini
    ([pytest] pythonpath), and setup.cfg ([tool:pytest] pythonpath).
    """
    pyproject = os.path.join(workdir, "pyproject.toml")
    if os.path.isfile(pyproject):
        try:
            try:
                import tomllib  # Python 3.11+
            except ImportError:  # pragma: no cover — Python 3.10 fallback
                import tomli as tomllib  # type: ignore[no-redef]

            with open(pyproject, "rb") as f:
                data = tomllib.load(f)
            ini_options = data.get("tool", {}).get("pytest", {}).get("ini_options", {})
            if "pythonpath" in ini_options:
                return True
        except Exception:
            pass  # unparseable pyproject — treat as unconfigured

    import configparser

    for path, section in (
        (os.path.join(workdir, "pytest.ini"), "pytest"),
        (os.path.join(workdir, "tox.ini"), "pytest"),
        (os.path.join(workdir, "setup.cfg"), "tool:pytest"),
    ):
        if not os.path.isfile(path):
            continue
        try:
            parser = configparser.ConfigParser()
            parser.read(path)
            if parser.has_option(section, "pythonpath"):
                return True
        except Exception:
            pass
    return False


def _needs_python_module_pytest(workdir: str) -> bool:
    """True when bare `pytest` cannot import the project's root modules/packages.

    pytest 9 importlib mode no longer adds the repo root to sys.path, so a
    root-module/package layout (top-level *.py module files or __init__.py
    dirs) with a tests/ dir only works under `python3 -m pytest` — unless
    the user already configured pytest's pythonpath (pyproject.toml /
    pytest.ini / setup.cfg). Checked ahead of _detect_python_runner so uv
    can never override the module-pytest decision (DF-017).
    """
    if not os.path.isdir(os.path.join(workdir, "tests")):
        return False
    if not _detect_root_import_layout(workdir):
        return False
    if _has_pytest_pythonpath_config(workdir):
        return False
    return True


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
        if _needs_python_module_pytest(workdir):
            return "python3 -m pytest -x --tb=short"
        # GR-GAP-024: prefer a runner the user actually has installed —
        # bare `pytest` fails on layouts where the runner isn't on PATH
        # (uv/pipenv/poetry virtualenvs install pytest into their own env).
        runner = _detect_python_runner(workdir)
        if runner:
            return f"{runner} pytest -x --tb=short"
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
    # Languages `init` has no runner heuristics for (rust/java/c/cpp/ruby/php/
    # kotlin/csharp/scala) use the shared language default, so the config init
    # writes is the same command the judge's Tier 1 runs for that language
    # (DF-GITREINS-POC-16). 'unknown' keeps the documented pytest default.
    from engine import lang_detect

    shared = lang_detect.lint_test_commands(lang.get("type"))
    if shared:
        return shared[1]
    return "pytest -x --tb=short"


def _detect_python_runner(workdir: str) -> str | None:
    """Detect a Python test runner available on PATH (GR-GAP-024).

    Returns e.g. "uv run" / "pipenv run" / "poetry run", or None when no
    supported runner is installed (callers fall back to bare `pytest`).
    Runner binaries are checked via shutil.which so virtualenv-only
    installs (uv/pipenv/poetry put pytest in their own env, not on PATH)
    produce a command that actually works.
    """
    import shutil

    if shutil.which("uv"):
        return "uv run"
    if shutil.which("pipenv") and os.path.isfile(os.path.join(workdir, "Pipfile")):
        return "pipenv run"
    if shutil.which("poetry") and os.path.isfile(os.path.join(workdir, "pyproject.toml")):
        pyproject = os.path.join(workdir, "pyproject.toml")
        try:
            with open(pyproject) as f:
                if "[tool.poetry]" in f.read():
                    return "poetry run"
        except OSError:
            pass
    return None


def _detect_project_size(workdir: str, lang: dict) -> dict:
    """Estimate project size for evaluator cap recommendations."""
    packages = 0
    if lang["is_go"]:
        # Count Go packages
        go_files = set()
        for root, dirs, files in os.walk(workdir):
            # Skip vendor, .git, node_modules
            dirs[:] = [d for d in dirs if d not in (".git", "vendor", "node_modules", ".gitreins")]
            for f in files:
                if f.endswith(".go"):
                    go_files.add(os.path.relpath(os.path.dirname(os.path.join(root, f)), workdir))
        packages = len(go_files)
    elif lang["is_python"]:
        py_pkgs = set()
        for root, dirs, files in os.walk(workdir):
            dirs[:] = [
                d
                for d in dirs
                if d not in (".git", ".venv", "node_modules", ".gitreins", "__pycache__")
            ]
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


def _build_guards_section(lang: dict, test_cmd: str, static_tools: list[str] | None = None) -> dict:
    """Build guards section optimized for the detected language."""
    if static_tools is None:
        static_tools = []

    # Canonical default static-analysis tools per language (DF-022). PATH
    # detection is preferred, but when it finds nothing installed we still
    # write a non-empty list: static_analysis: true WITHOUT tools makes the
    # guard silently no-op, which is worse than a noisy failure.
    canonical_tools = {
        "python": ["mypy", "pyright"],
        "ruby": ["sorbet"],
        "php": ["phpstan"],
        "sql": ["sqlfluff"],
    }

    section: dict
    if lang["is_go"]:
        return {
            "secrets": True,
            "lint": False,
            "tests": False,
            "test_mode": "full",
            "go": {"build": True, "lint": True, "tests": True},
        }
    elif lang["is_python"]:
        section = {
            "secrets": True,
            "lint": True,
            "tests": True,
            "test_mode": "full",
            "test_command": test_cmd,
            "static_analysis": True,  # ON: dynamic language, no compiler
        }
        section["static_analysis_tools"] = {"python": static_tools or canonical_tools["python"]}
        return section
    elif lang["is_ts"]:
        return {
            "secrets": True,
            "lint": False,
            "tests": True,
            "test_mode": "full",
            "test_command": test_cmd,
            "static_analysis": False,  # OFF: tsc --noEmit covers this
        }
    elif lang["is_ruby"]:
        return {
            "secrets": True,
            "lint": False,
            "tests": False,
            "test_mode": "full",
            "test_command": "bundle exec rspec",
            "static_analysis": True,  # ON: dynamic language, no compiler
            "static_analysis_tools": {"ruby": static_tools or canonical_tools["ruby"]},
        }
    elif lang["is_php"]:
        return {
            "secrets": True,
            "lint": False,
            "tests": False,
            "test_mode": "full",
            "test_command": "vendor/bin/phpunit",
            "static_analysis": True,  # ON: dynamic language, no compiler
            "static_analysis_tools": {"php": static_tools or canonical_tools["php"]},
        }
    elif lang["is_rust"]:
        return {
            "secrets": True,
            "lint": False,
            "tests": False,
            "test_mode": "full",
            "test_command": "cargo test",
            "static_analysis": False,  # OFF: cargo check covers this
        }
    elif lang["has_sql"]:
        return {
            "secrets": True,
            "lint": False,
            "tests": False,
            "test_mode": "full",
            "test_command": "echo 'No SQL test runner configured'",
            "static_analysis": True,  # ON: no compiler for SQL
            "static_analysis_tools": {"sql": static_tools or canonical_tools["sql"]},
        }
    else:
        return {
            "secrets": True,
            "lint": True,
            "tests": True,
            "test_mode": "full",
            "test_command": test_cmd,
        }


def _upgrade_install_default_test_command(guards: dict, detected_test_cmd: str) -> bool:
    """Upgrade only the untouched test command created by ``install``.

    ``install`` has no user input and writes ``INSTALL_DEFAULT_TEST_COMMAND``.
    Treating that exact value as the baseline lets smart init tailor it while
    preserving every custom command on subsequent runs.
    """
    if (
        guards.get("test_command") == INSTALL_DEFAULT_TEST_COMMAND
        and detected_test_cmd != INSTALL_DEFAULT_TEST_COMMAND
    ):
        guards["test_command"] = detected_test_cmd
        return True
    return False


def _configured_static_analysis_tools(guards: dict) -> list[str]:
    """Flatten configured static-analysis tools for user-facing status output."""
    configured = guards.get("static_analysis_tools", {})
    values = configured.values() if isinstance(configured, dict) else [configured]
    tools = []
    for value in values:
        if isinstance(value, str):
            candidates = [value]
        elif isinstance(value, (list, tuple)):
            candidates = value
        else:
            candidates = []
        for tool in candidates:
            if isinstance(tool, str) and tool not in tools:
                tools.append(tool)
    return tools


def _missing_static_analysis_tools(guards: dict) -> list[str]:
    """Configured static-analysis tools that are NOT installed on this machine.

    DF-019: `init` announced "Static analysis: enabled (mypy, pyright)" for
    tools that were absent, so a fresh user believed mypy ran while the guard
    skipped the step. Anything the status line claims must be resolvable by the
    same lookup the guard uses.
    """
    from engine.static_analysis import find_tool

    return [tool for tool in _configured_static_analysis_tools(guards) if not find_tool(tool)]


def _static_analysis_install_lines(missing: list[str]) -> list[str]:
    """Human-readable install instructions for absent static-analysis tools."""
    from engine.static_analysis import _install_help

    return [f"    {tool} — install: {_install_help(tool)}" for tool in missing]


def _static_analysis_status(guards: dict, lang: dict) -> str:
    """Describe the persisted static-analysis toggle and configured tools."""
    enabled = guards.get("static_analysis", False)
    tools = _configured_static_analysis_tools(guards)
    if enabled and tools:
        # DF-019: name only what can actually run. Announcing a configured but
        # absent tool as enabled is the lie this row was filed about.
        missing = set(_missing_static_analysis_tools(guards))
        if not missing:
            return f"enabled ({', '.join(tools)})"
        if len(missing) == len(tools):
            return f"enabled ({', '.join(tools)}; none installed — nothing will run)"
        absent = [tool for tool in tools if tool in missing]
        return f"enabled ({', '.join(tools)}; not installed: {', '.join(absent)})"
    if enabled:
        install_hints = []
        if lang["is_python"]:
            install_hints.append("pip install mypy")
        elif lang["is_ruby"]:
            install_hints.append("gem install sorbet && srb init")
        elif lang["is_php"]:
            install_hints.append("composer require --dev phpstan/phpstan")
        elif lang["has_sql"]:
            install_hints.append("pip install sqlfluff")
        hint = "; ".join(install_hints) if install_hints else "see docs for install instructions"
        return f"enabled (no tools configured — nothing will run; install: {hint})"
    if tools:
        return f"disabled (explicitly off; configured tools: {', '.join(tools)})"
    return "disabled (compiled language or explicitly off)"


def _fill_missing_guards(
    guards: dict, lang: dict, test_cmd: str, static_tools: list[str] | None = None
) -> list[str]:
    """Fill in missing guard keys without overwriting existing values. Returns keys added."""
    added = []
    defaults = _build_guards_section(lang, test_cmd, static_tools)
    # TRUST-001: the generated default config accepts zero-work skips; a repo
    # that wants DEGRADED runs to exit 2 removes the key or sets it false.
    defaults.setdefault("allow_skips", True)

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
        "static_analysis_diagnostics": False,  # OFF by default, visible for opt-in
    }


def _glob_to_regex(path: str) -> str:
    """Convert a plain-glob path into a valid Go (RE2) regexp.

    gitleaks compiles every [allowlist] paths entry as a Go regexp, so bare
    globs like '*.log' panic with 'missing argument to repetition operator'
    (DF-001). Glob '*' becomes regex '.*' and literal regex metacharacters are
    escaped: '.venv/' -> '\\.venv/', '*.egg-info/' -> '.*\\.egg-info/',
    'apps/*/node_modules/' -> 'apps/.*/node_modules/'.
    """
    out: list[str] = []
    for ch in path:
        if ch == "*":
            out.append(".*")
        elif ch in ".+()[]{}^$|\\?":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _generate_gitleaks_config(workdir: str, lang: dict, target_path: str) -> None:
    """Generate a .gitleaks.toml with sensible path exclusions.

    Prevents gitleaks from scanning dependency directories (node_modules,
    .venv, vendor, etc.) which can cause 30-second timeouts on large projects.
    Never overwrites existing .gitleaks.toml — caller checks before invoking.
    Allowlist path entries are emitted as Go regexps (via _glob_to_regex) —
    gitleaks compiles each entry as a regexp, not a glob.
    """
    # Universal exclusions for all projects
    paths = [
        ".git/",
        ".gitreins/",
        ".gitreins/history/",
        "*.log",
    ]

    # Language/ecosystem-specific exclusions
    if lang["is_python"]:
        paths.extend(
            [
                ".venv/",
                "venv/",
                "__pycache__/",
                "*.egg-info/",
                "dist/",
                "build/",
                ".mypy_cache/",
                ".pytest_cache/",
                ".ruff_cache/",
            ]
        )
    elif lang["is_ts"]:
        paths.extend(
            [
                "node_modules/",
                ".pnpm-store/",
                "apps/*/node_modules/",
                "packages/*/node_modules/",
                "apps/*/dist/",
                "packages/*/dist/",
                "dist/",
                "build/",
                ".turbo/",
                ".next/",
                "coverage/",
            ]
        )
    elif lang["is_go"]:
        paths.extend(
            [
                "vendor/",
            ]
        )
    elif lang["is_ruby"]:
        paths.extend(
            [
                "vendor/bundle/",
                ".bundle/",
            ]
        )
    elif lang["is_rust"]:
        paths.extend(
            [
                "target/",
            ]
        )

    # Documentation/spec files (often contain example keys)
    paths.extend(
        [
            "specs/",
            "docs/",
            "*.spec.md",
            "*.md",
        ]
    )

    # Build TOML
    lines = [
        "# GitReins — gitleaks configuration",
        "# Auto-generated by 'gitreins init'. Edit to add project-specific exclusions.",
        "# See: https://github.com/gitleaks/gitleaks#configuration",
        "",
        "# Extend the default gitleaks ruleset so AWS/GitHub/GitLab/etc. patterns still run",
        "[extend]",
        "useDefault = true",
        "",
        "# Catch all sk- prefixed API keys (OpenAI, OpenRouter, DeepSeek, etc.)",
        "# 20+ chars — lower than gitleaks' default 32-char minimum",
        "[[rules]]",
        'id = "sk-api-key"',
        'description = "OpenAI/OpenRouter/DeepSeek API key (sk- prefix)"',
        "regex = '''(?i)sk-[a-zA-Z0-9_-]{20,}'''",
        "",
        "# GitHub personal access tokens — gitleaks' default rules missed",
        "# ghp_... PATs in the 2026-08-14 dogfood (DF-012)",
        "[[rules]]",
        'id = "gitreins-github-pat"',
        'description = "GitHub personal access token (ghp_ prefix)"',
        "regex = '''\\bghp_[A-Za-z0-9]{36,}'''",
        "",
        "# GitLab personal access tokens",
        "[[rules]]",
        'id = "gitreins-gitlab-pat"',
        'description = "GitLab personal access token (glpat- prefix)"',
        "regex = '''\\bglpat-[A-Za-z0-9_\\-]{20,}'''",
        "",
        "# GCP API keys",
        "[[rules]]",
        'id = "gitreins-gcp-api-key"',
        'description = "GCP API key (AIza prefix)"',
        "regex = '''\\bAIza[0-9A-Za-z\\_-]{35,}'''",
        "",
        "[allowlist]",
        'description = "Auto-generated by gitreins init — skip dependency dirs, docs, specs"',
        "",
        "paths = [",
    ]
    for p in paths:
        lines.append(f"  '''{_glob_to_regex(p)}''',")
    lines.append("]")
    lines.append("")

    toml_content = "\n".join(lines) + "\n"

    # Use safe overwrite (creates .bak)
    _safe_overwrite(target_path, lambda f: f.write(toml_content))


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
    _require_task(tm, args.id)
    task = tm.start(args.id)
    print(f"Started: {task.id} → {task.status}")


def _require_task(tm, task_id: str):
    """Return the task, or exit 1 with the clean message ``judge`` prints.

    DF-GITREINS-POC-14: ``task start`` / ``task complete`` / ``task delete``
    let TaskManager's ``KeyError("Task not found: <id>")`` escape, so an
    unknown id dumped a Python traceback (rc 1) while ``judge`` printed a
    single line and exited. Same id, same repo, two different failure
    surfaces. The hint goes to stderr so a scripted stdout read stays
    greppable.
    """
    task = tm.get(task_id)
    if task is None:
        print(f"Task not found: {task_id}")
        print("Run `gitreins task list` to see known task ids.", file=sys.stderr)
        raise SystemExit(1)
    return task


def _print_tier2_recovery(llm, task_id: str) -> None:
    """Name the resolved LLM config and the way forward after a Tier 2 that
    judged nothing (DF-GITREINS-POC-14).

    The evaluator returns INCOMPLETE + an error summary when it cannot reach
    the provider; the CLI used to exit 1 on that without saying which
    credential/endpoint was tried or how to retry.
    """
    print("", file=sys.stderr)
    print(
        "Tier 2 judged nothing — the FAIL above is an infrastructure error, "
        "not a verdict on the work.",
        file=sys.stderr,
    )
    if llm is not None:
        print(f"  resolved: {llm.describe()}", file=sys.stderr)
    print(
        "  fix the credential/endpoint (GITREINS_LLM_API_KEY, "
        "GITREINS_LLM_BASE_URL, GITREINS_LLM_MODEL), then re-run:",
        file=sys.stderr,
    )
    print(f"    gitreins task complete {task_id} --force", file=sys.stderr)
    print(
        f"  or grade Tier 1 alone:  gitreins task complete {task_id} --skip-tier2",
        file=sys.stderr,
    )
    print("  see docs/onboarding.md (T5)", file=sys.stderr)


def cmd_task_complete(args):
    from engine.evaluator import LLM_FAILURE_SUMMARY_PREFIX
    from engine.task_manager import TaskManager
    from engine.llm import LLMClient
    from engine.judge import Judge

    workdir = get_workdir()
    tm = TaskManager(workdir)

    force = getattr(args, "force", False)
    skip_tier2 = getattr(args, "skip_tier2", False)

    # DF-GITREINS-POC-14: resolve the id BEFORE the credential check, so an
    # unknown id reports "Task not found" instead of a credential complaint.
    _require_task(tm, args.id)

    # Check dependencies (unless forced)
    if not force:
        blocked = tm.check_dependencies(args.id)
        if blocked:
            print(
                f"Cannot complete '{args.id}' — depends on incomplete tasks: {', '.join(blocked)}"
            )
            print("Complete those tasks first, or use --force to skip dependency checks.")
            sys.exit(1)

    # Resolve credentials before changing the task state.  The evaluator's
    # fallback chain is owned by LLMClient, so inspect its resolved key rather
    # than duplicating provider selection in the CLI.
    llm = None
    if not skip_tier2:
        llm = LLMClient()
        if not llm.api_key:
            print(
                "Cannot complete task: Tier 2 evaluation requires an LLM credential.",
                file=sys.stderr,
            )
            print(
                "Configure GITREINS_LLM_API_KEY (or a supported provider API key).", file=sys.stderr
            )
            print("You may also set GITREINS_LLM_BASE_URL and GITREINS_LLM_MODEL.", file=sys.stderr)
            print(
                "For Tier 1-only evaluation, run: gitreins task complete --skip-tier2 <id>",
                file=sys.stderr,
            )
            sys.exit(1)

    task = tm.complete(args.id, force=force)
    print(f"Completed: {task.id} → {task.status}")

    print("\nEvaluating...")
    judge = Judge(llm, workdir)
    result = judge.evaluate_task(task, skip_tier2=skip_tier2)
    print(result.summary)

    # Persist verdict
    _persist_result(workdir, task, result)
    if not result.passed:
        # DF-GITREINS-POC-14: an INCOMPLETE verdict that never reached the
        # provider judged nothing — print the resolved config and the way
        # forward instead of exiting on a bare FAIL.
        if LLM_FAILURE_SUMMARY_PREFIX in (result.summary or ""):
            _print_tier2_recovery(llm, task.id)
        sys.exit(1)


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
    _require_task(tm, args.id)
    tm.delete(args.id)
    print(f"Deleted: {args.id}")


def cmd_task_worktree(args):
    """Create (or idempotently reuse) a task's isolated git worktree."""
    from engine.worktree_manager import WorktreeError, WorktreeManager

    try:
        manager = WorktreeManager(get_workdir())
        brief_path = manager.worker_brief_path(args.id)
        record, created = manager.create(
            args.id,
            brief_path=str(brief_path),
            tick=getattr(args, "tick", None),
        )
    except (WorktreeError, KeyError) as exc:
        print(f"task worktree: failed\nError: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    verb = "Created" if created else "Reused"
    print(f"✓ {verb} worktree {record.path} (branch {record.branch})")
    print("✓ task filed in registry — main checkout .gitreins/worktrees.json")
    print("✓ board linked to main checkout — one shared truth")
    print(f"✓ worker brief path: {record.brief_path}")
    if record.tick:
        print(f"✓ tick: {record.tick}")
    print("Merge-back armed on judge PASS (worktree merge-back lands in WORKTREE-003).")


def cmd_worktree_merge(args):
    """Judge-gated fast-forward merge of a task worktree into canonical main."""
    from engine.repo_paths import WorktreeResolutionError
    from engine.worktree_manager import WorktreeError, WorktreeManager

    try:
        result = WorktreeManager(get_workdir()).merge(
            args.id,
            force=bool(getattr(args, "force", False)),
            actor=getattr(args, "actor", None),
            reason=getattr(args, "reason", "explicit judge-gate override"),
        )
    except (WorktreeError, WorktreeResolutionError) as exc:
        print(f"worktree merge: refused\nError: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    print(
        f"Merged {result['task_id']} ({result['mode']}) into canonical main "
        f"at {result['destination_commit'][:12]}"
    )
    print(f"  branch: {result['branch']}")
    print(f"  worktree reaped: {result['worktree']}")


def _format_age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def cmd_worktree_list(args):
    """List registered task worktrees with reconciled state and age."""
    import time as _time

    from engine.config import load_defaults
    from engine.worktree_manager import WorktreeError, WorktreeManager

    try:
        manager = WorktreeManager(get_workdir())
        records = manager.list_records()
        cap = load_defaults(str(manager.main_root)).max_concurrent_worktrees
    except (WorktreeError, ValueError) as exc:
        print(f"worktree list: failed\nError: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    if not records:
        print("No worktrees registered.")
        print(f"Fleet cap: {cap}")
        return

    now = _time.time()
    print(f"Fleet cap: {cap}")
    print(f"{'TASK':<24} {'STATE':<10} {'PHASE':<10} {'AGE':<8} BRANCH")
    for record in records:
        age = _format_age(now - record.created_at)
        phase = record.lane_phase or record.state
        print(f"  {record.task_id:<22} {record.state:<10} {phase:<10} {age:<8} {record.branch}")
        if record.exit_code is not None:
            print(f"    exit: {record.exit_code}")
        if record.error:
            print(f"    error: {record.error}")
        if record.output:
            evidence = " ".join(record.output.split())
            print(f"    evidence: {evidence[:240]}")
        print(f"    path: {record.path}")


def cmd_worktree_fleet(args):
    """Run an explicit bounded fleet manifest and print its tick report."""
    import json as _json

    from engine.worktree_fleet import FleetValidationError, WorktreeFleet, load_fleet_manifest
    from engine.worktree_manager import WorktreeError

    try:
        lanes = load_fleet_manifest(args.manifest)
        fleet = WorktreeFleet(
            get_workdir(),
            max_concurrent_worktrees=getattr(args, "max_concurrent_worktrees", None),
        )
        report = fleet.run(
            lanes,
            tick=getattr(args, "tick", None),
            merge=bool(getattr(args, "merge", False)),
            force_merge=bool(getattr(args, "force_merge", False)),
            merge_actor=getattr(args, "actor", None),
        )
    except (FleetValidationError, WorktreeError, ValueError) as exc:
        print(f"worktree fleet: failed\nError: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    print(_json.dumps(report, indent=2, sort_keys=True))


def _write_worktree_json(path: str | None, payload: dict) -> None:
    """Write disposable evidence as UTF-8 JSON when requested."""
    from engine.worktree_manager import WorktreeError

    if path is None:
        return
    try:
        json_path = os.path.abspath(path)
        parent = os.path.dirname(json_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(json_path, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2)
            stream.write("\n")
    except OSError as exc:
        raise WorktreeError(f"could not write evidence JSON {path}: {exc}") from exc


def _record_qa_run(kind: str, report: dict, *, command: str | None = None) -> None:
    """Record a QA-run outcome in the ledger; never fail the run it records.

    ``None`` return from the ledger means recording is switched off
    (``qa_ledger.enabled: false``), which is stated rather than silent.
    """
    from engine.qa_ledger import record_run

    try:
        stored = record_run(get_workdir(), kind, report, command=command)
    except (OSError, TypeError, ValueError) as exc:
        print(f"qa ledger: {kind} run not recorded ({exc})", file=sys.stderr)
        return
    if stored is None:
        print(f"qa ledger: {kind} run not recorded (qa_ledger.enabled is false)", file=sys.stderr)


def cmd_worktree_fresh(args):
    """Run one shell command in a disposable detached worktree."""
    from engine.worktree_disposable import DisposableWorktreeManager
    from engine.worktree_manager import WorktreeError

    try:
        verifier = DisposableWorktreeManager(get_workdir())
        result = verifier.run(
            args.cmd,
            timeout=getattr(args, "timeout", None),
            keep=bool(getattr(args, "keep", False)),
        )
        _write_worktree_json(getattr(args, "json_path", None), result)
    except WorktreeError as exc:
        print(f"worktree fresh: infrastructure failure\nError: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    _record_qa_run("fresh", result, command=args.cmd)
    kept = " (kept at " + result["tree"] + ")" if result["kept"] else ""
    print(f"fresh: exit {result['exit_code']} in {result['duration_s']:.3f}s{kept}")
    if result["output"]:
        print(result["output"])
    if result["exit_code"]:
        raise SystemExit(result["exit_code"])


def cmd_worktree_repro(args):
    """Run a command repeatedly in bounded disposable worktrees."""
    from engine.worktree_disposable import DisposableWorktreeManager
    from engine.worktree_manager import WorktreeError

    try:
        verifier = DisposableWorktreeManager(get_workdir())
        report = verifier.repro(
            args.cmd,
            args.k,
            concurrency=getattr(args, "concurrency", None),
            timeout=getattr(args, "timeout", None),
            keep_failures=bool(getattr(args, "keep_failures", False)),
        )
        _write_worktree_json(getattr(args, "json_path", None), report)
    except WorktreeError as exc:
        print(f"worktree repro: infrastructure failure\nError: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    _record_qa_run("repro", report, command=args.cmd)
    kept = [run["tree"] for run in report["runs"] if run["kept"]]
    suffix = f" — {len(kept)} failure(s) kept at {', '.join(kept)}" if kept else ""
    print(
        f"repro: {report['passes']}/{report['k']} passed "
        f"(pass rate {report['pass_rate']:.2f}) — {report['failures']} failure(s){suffix}"
    )
    if report["failures"]:
        raise SystemExit(1)


def cmd_worktree_dogfood(args):
    """Exercise init, task, guard, and judge in a disposable tree."""
    from engine.worktree_disposable import DisposableWorktreeManager
    from engine.worktree_manager import WorktreeError

    try:
        verifier = DisposableWorktreeManager(get_workdir())
        report = verifier.dogfood(
            keep=bool(getattr(args, "keep", False)),
            skip_judge=bool(getattr(args, "skip_judge", False)),
            test_command=getattr(args, "test_command", None),
            timeout=getattr(args, "timeout", None),
        )
        _write_worktree_json(getattr(args, "json_path", None), report)
    except WorktreeError as exc:
        print(f"worktree dogfood: infrastructure failure\nError: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    _record_qa_run("dogfood", report)
    print(
        f"dogfood: {sum(step['status'] == 'passed' for step in report['steps'])}/"
        f"{len(report['steps'])} steps passed; judge {report['judge']['status']}"
    )
    if report["exit_code"]:
        raise SystemExit(report["exit_code"])


def cmd_worktree_clean(args):
    """Reap merged task worktrees and finished disposable runs."""
    from engine.worktree_disposable import DisposableWorktreeManager
    from engine.worktree_manager import PROTECTED_STATES, WorktreeError, WorktreeManager

    confirm = bool(getattr(args, "confirm_stale_orphan", False))
    try:
        manager = WorktreeManager(get_workdir())
        report = manager.clean(confirm_stale_orphan=confirm)
        disposable_removed = DisposableWorktreeManager(manager.main_root).reap()
    except WorktreeError as exc:
        print(f"worktree clean: failed\nError: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    removed = report["removed"]
    if removed:
        print(f"Reaped {len(removed)} worktree(s): {', '.join(removed)}")
        for branch in report["branches_deleted"]:
            print(f"  branch deleted (merged): {branch}")
    if disposable_removed:
        print(
            f"Reaped {len(disposable_removed)} disposable run(s): {', '.join(disposable_removed)}"
        )
    if not removed and not disposable_removed:
        print("Nothing to reap.")

    kept = report["kept"]
    if kept:
        print(f"Kept {len(kept)} worktree(s):")
        for task_id, state in kept:
            line = f"  {task_id} [{state}]"
            if state in PROTECTED_STATES and not confirm:
                line += " — reaping requires --confirm-stale-orphan"
            print(line)


def _persist_result(workdir: str, task, result) -> None:
    """Save evaluation verdict to history. Non-fatal — logs on failure.

    Thin wrapper over ``engine.persist.persist_evaluation`` (the single shared
    implementation the MCP server also calls) that keeps the CLI's console
    behaviour: the saved-verdict line, the dry-run warning and the non-fatal
    warning all stay here, never in the shared helper.
    """
    try:
        from engine.persist import VerdictPersister, persist_evaluation

        persister = VerdictPersister(workdir)
        if not persister.enabled:
            return

        # Worker execution evidence (JVIEW-005): the worker brief, the driver
        # log tail and the graded patch are copied into the verdict directory,
        # so a verdict stays readable after /tmp is cleaned. Best-effort — the
        # collector swallows its own failures and the persister ignores a hook
        # that raises, because evidence must never fail a verdict.
        def _collect_evidence(entry_dir: str) -> dict:
            from engine.evidence import collect_evidence

            source_commit = ""
            try:
                commit_result = subprocess.run(
                    ["git", "rev-parse", "--verify", "HEAD"],
                    capture_output=True,
                    text=True,
                    cwd=workdir,
                    timeout=5,
                    check=False,
                )
                if commit_result.returncode == 0:
                    source_commit = commit_result.stdout.strip()
            except (OSError, subprocess.TimeoutExpired):
                pass
            return collect_evidence(workdir, entry_dir, commit=source_commit, task_id=task.id)

        commit_hash = persist_evaluation(workdir, task, result, collect_evidence=_collect_evidence)
        if commit_hash == "disabled":
            pass  # user opted out
        elif commit_hash == "dry-run":
            print("  ⚠ Verdict saved to disk but not committed (git unavailable)", file=sys.stderr)
        elif commit_hash == "error":
            print("  ⚠ Failed to persist verdict (non-fatal)", file=sys.stderr)
        else:
            print(f"  📋 Verdict saved: {commit_hash}")

    except Exception:
        print("  ⚠ Failed to persist verdict (non-fatal)", file=sys.stderr)


def cmd_qa_list(args):
    """Show recorded QA run outcomes from the QA ledger."""
    from engine.qa_ledger import format_rows, list_rows

    workdir = get_workdir()
    n = args.n if hasattr(args, "n") else 20
    if getattr(args, "as_json", False):
        print(json.dumps(list_rows(workdir, n), indent=2))
        return
    print(format_rows(workdir, n=n))


def cmd_qa_record(args):
    """Record a QA run outcome produced outside the harness.

    Rows carry the fleet QA-ledger keys, so pointing ``GITREINS_QA_LEDGER`` at a
    fleet ledger appends a row that a fleet discovery can read.
    """
    from engine.qa_ledger import qa_ledger_path, record_external

    workdir = get_workdir()
    cells: dict[str, str] = {}
    for entry in getattr(args, "cell", None) or []:
        name, separator, value = entry.partition("=")
        if not separator or not name.strip() or not value.strip():
            print(f"qa record: --cell expects NAME=STATUS (got {entry!r})", file=sys.stderr)
            raise SystemExit(2)
        cells[name.strip()] = value.strip()

    findings = []
    for entry in getattr(args, "finding", None) or []:
        finding_id, _separator, title = entry.partition(":")
        findings.append({"id": finding_id.strip(), "title": title.strip()})

    try:
        row = record_external(
            workdir,
            project=getattr(args, "project", None) or None,
            status=getattr(args, "status", None) or None,
            kind=getattr(args, "kind", None) or "lane",
            cells=cells,
            findings=findings,
            evidence=getattr(args, "evidence", None) or "",
            note=getattr(args, "note", None) or "",
            agent=getattr(args, "agent", None) or "",
            server=getattr(args, "server", None) or "",
            commit=getattr(args, "commit", None),
            verdict=getattr(args, "verdict", None),
            exit_code=getattr(args, "exit_code", None),
            ts=getattr(args, "ts", None) or None,
        )
    except (OSError, TypeError, ValueError) as exc:
        print(f"qa record: not recorded ({exc})", file=sys.stderr)
        raise SystemExit(1) from exc
    if row is None:
        print("qa record: not recorded (qa_ledger.enabled is false)", file=sys.stderr)
        raise SystemExit(1)
    print(
        f"qa ledger: recorded {row['kind']} {row['project']} {row['verdict']} "
        f"in {qa_ledger_path(workdir)}"
    )


def cmd_report(args):
    """Show recent verdict history."""
    from engine.persist import build_report
    from engine.qa_ledger import format_report_section

    workdir = get_workdir()
    n = args.n if hasattr(args, "n") else 10

    # Interactive TUI mode
    if args.interactive:
        _cmd_report_tui(workdir, n)
        return

    report = build_report(workdir, n=n)
    print(report)

    # QA verdicts are not task verdicts, so they are reported alongside the
    # task history rather than mixed into it.
    qa_section = format_report_section(workdir, n=min(max(n, 1), 10))
    if qa_section:
        print()
        print(qa_section)


def cmd_worktree_doctor(args):
    """Show and validate the shared board resolution for this checkout."""
    try:
        paths = resolve_worktree_paths()
    except WorktreeResolutionError as exc:
        print(f"worktree doctor: invalid\nError: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    local_status = "present" if paths.local_board_exists else "absent"
    print(f"Invoking worktree root: {paths.invoking_worktree_root}")
    print(f"Git common dir: {paths.git_common_dir}")
    print(f"Canonical main checkout/root: {paths.canonical_main_root}")
    print(f"Canonical board path: {paths.canonical_board}")
    print(f"Ignored local worktree board copy: {local_status} ({paths.local_board})")
    print("Resolution: valid")


def cmd_serve(args):
    """Run the local judgment-browser web server."""
    from gitreins.serve import ServeArgumentError, resolve_workdir, serve

    try:
        workdir = resolve_workdir(getattr(args, "repo", None), get_workdir())
    except ServeArgumentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        sys.exit(2)
    serve(
        workdir,
        host=args.host,
        port=args.port,
        project=args.project or "",
        open_browser=args.open,
    )


def _cmd_report_tui(workdir: str, n: int = 20):
    """Interactive TUI for verdict browsing (requires textual)."""
    from engine.persist import build_report, VerdictPersister

    try:
        from importlib.util import find_spec

        if not find_spec("textual"):
            raise ImportError
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
    _require_guard_config(workdir)
    config = load_config(workdir)
    # GR-GAP-043: --staged-only / --full override config guards.test_mode
    # ('diff' / 'full'). If both are passed, --staged-only wins (diff is the
    # narrower scope); neither → config value (default: 'full').
    # DF-GITREINS-POC-11: --full additionally grades the whole tree when the
    # index is empty (tests run, lint grades tracked+untracked files) —
    # --staged-only must not.
    grade_full_tree = False
    if getattr(args, "staged_only", False):
        config.setdefault("guards", {})["test_mode"] = "diff"
    elif getattr(args, "full", False):
        config.setdefault("guards", {})["test_mode"] = "full"
        grade_full_tree = True
    gm = GuardManager(workdir, config=config, grade_full_tree=grade_full_tree)
    result = gm.run_all(force_dead_code=getattr(args, "dead_code", False))

    # Build mode note
    mode = gm.test_mode
    extra = result.extra
    mode_note = f"  (test mode: {mode}"
    if extra.get("grade_full_tree"):
        mode_note += ", whole tree"
    if extra.get("test_targets"):
        mode_note += f", {extra['test_targets']} test file(s)"
    elif extra.get("test_targets") is None and mode == "diff":
        mode_note += ", full suite — safety trigger"
    mode_note += ")"

    # TRUST-001: a run where a substantive gate (lint/tests/lsp) did no work is
    # a DEGRADED PASS, and it never prints the green "Tier 1 Guards: PASS"
    # header — grepping that string is now proof the gates actually ran. The
    # exit code is 0 only when the repo opted in via guards.allow_skips.
    if not result.passed:
        print(f"Tier 1 Guards: FAIL{mode_note}")
    elif result.degraded:
        print(f"Tier 1: DEGRADED PASS (skips: {result.skip_summary}){mode_note}")
    else:
        print(f"Tier 1 Guards: PASS{mode_note}")
    print(result.summary)

    # DF-018: name the persisted run log (the complete, untruncated output)
    # on BOTH pass and fail — the bounded summary above is not enough to
    # diagnose a failure after the fact. When persistence failed, print the
    # reason instead of a path.
    if extra.get("guard_log"):
        print(f"  guard log: {extra['guard_log']}")
    else:
        print(f"  guard log: not written — {extra.get('guard_log_error') or 'unknown reason'}")

    if result.warnings:
        print()
        for warning in result.warnings:
            print(f"\033[33m⚠ {warning}\033[0m", file=sys.stderr)

    if not result.passed:
        print()
        print("Fix the issues above and re-run: gitreins guard")
        sys.exit(1)

    # TRUST-001: a degraded pass exits 0 ONLY with guards.allow_skips: true.
    # Exit 2 (not 1) keeps "a gate failed" distinct from "a gate never ran".
    if result.degraded and not result.extra.get("allow_skips", False):
        print()
        print(
            "\033[33m⚠ DEGRADED PASS: "
            f"{result.skip_summary} — these gates did not run, so this run is not \n"
            "  evidence the tree passes. Stage the files you want graded (git add), or \n"
            "  set guards.allow_skips: true in .gitreins/config.yaml to accept skips \n"
            "  on zero-work runs (gitreins init writes it for fresh repos).\033[0m",
            file=sys.stderr,
        )
        sys.exit(2)


def cmd_judge(args):
    """Evaluate a task — sync (default), or dispatch a background job.

    ``--async`` detaches a worker process and returns a job id; the job
    record lives in the shared disk store, so it survives this CLI
    exiting and can be polled with ``gitreins judge --status <job_id>``
    (or the MCP ``judge.status`` tool). ``--run-job`` is the internal
    worker mode executed by the detached child.
    """
    if getattr(args, "status", False):
        _cmd_judge_status(args.id)
        return
    if getattr(args, "run_job", False):
        _cmd_judge_worker(args.id)
        return
    if getattr(args, "async_dispatch", False):
        _cmd_judge_async(args.id)
        return

    _check_for_updates()
    from engine.task_manager import TaskManager
    from engine.llm import LLMClient
    from engine.judge import Judge

    workdir = get_workdir()
    tm = TaskManager(workdir)
    task = tm.get(args.id)
    if not task:
        print(f"Task not found: {args.id}")
        sys.exit(1)

    # Single-flight (GR-GAP-046): while a background evaluation for this
    # task is genuinely in flight (live pid — e.g. an MCP-dispatched job),
    # don't start a second evaluation inline. Point the user at the
    # running job instead. Only a LIVE pid blocks: a running record whose
    # owner died is an orphan and a sync run supersedes it.
    from engine.job_store import find_running_job, pid_alive

    running = find_running_job(args.id, workdir)
    if running is not None and pid_alive(running.get("pid")):
        print(f"Evaluation already in progress for {args.id} (job {running['id']})")
        print(f"  poll:    gitreins judge --status {running['id']}")
        return

    if getattr(args, "skip_tier2", False):
        print("Tier 2 skipped (--skip-tier2 flag)")

    llm = LLMClient()
    config = load_config(workdir)
    judge = Judge(llm, workdir, guard_config=config)
    result = judge.evaluate_task(task, skip_tier2=getattr(args, "skip_tier2", False))
    print(result.summary)

    # Persist verdict
    _persist_result(workdir, task, result)

    # DF-GITREINS-POC-16: a FAIL verdict must reach the shell. Printing
    # "Overall: FAIL" while exiting 0 lets a caller (script, CI step, agent)
    # treat a red gate as success — the same silent-pass class this task is
    # about. `gitreins guard` already exits 1 on the same tree.
    if not result.passed:
        sys.exit(1)


def _cmd_judge_async(task_id: str) -> None:
    """Dispatch the evaluation as a detached background job (DF-006).

    The worker is a detached child process that survives this CLI
    exiting; the job record lives in the shared disk store, so it can be
    polled with ``gitreins judge --status <job_id>`` and from the MCP
    server's ``judge.status`` tool.

    Single-flight (GR-GAP-046): if a job for the same (task_id, workdir)
    key is already ``running`` in the shared disk store, it is reused —
    no second worker is spawned, so concurrent dispatches (CLI + MCP, or
    two CLIs) yield ONE running evaluation. The child pid is captured
    BEFORE the job record is first persisted, so there is no window where
    the disk record claims a dead/None pid and a poll would resume a job
    that is about to be owned by a live child.
    """
    import subprocess

    from engine.job_store import find_running_job, job_log_path, make_job, new_job_id, save_job
    from engine.task_manager import TaskManager

    workdir = get_workdir()
    tm = TaskManager(workdir)
    task = tm.get(task_id)
    if not task:
        print(f"Task not found: {task_id}")
        sys.exit(1)

    existing = find_running_job(task_id, workdir)
    if existing is not None:
        print(f"Async job already running: {existing['id']}")
        print(f"  task:    {task_id}")
        print(f"  workdir: {workdir}")
        print(f"  poll:    gitreins judge --status {existing['id']}")
        return

    log_path = job_log_path(new_job_id())
    try:
        logf = open(log_path, "ab")
    except OSError as e:
        print(f"Could not open job log {log_path}: {e}")
        sys.exit(1)

    # Spawn the child FIRST: the job record is published with the child's
    # real pid in the same save that creates it (no pid=None window).
    job_id = new_job_id()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "gitreins.cli", "judge", "--run-job", job_id],
            cwd=workdir,
            start_new_session=True,
            stdout=logf,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
    except OSError as e:
        logf.close()
        print(f"Could not start background job: {e}")
        sys.exit(1)

    job = make_job(task_id, workdir)
    job["id"] = job_id
    job["pid"] = proc.pid
    save_job(job)
    print(f"Async job dispatched: {job['id']}")
    print(f"  task:    {task_id}")
    print(f"  workdir: {workdir}")
    print(f"  log:     {log_path}")
    print(f"  poll:    gitreins judge --status {job['id']}")


def _cmd_judge_worker(job_id: str) -> None:
    """Internal worker mode: run the evaluation for a disk job record.

    Executed by the detached child spawned from ``judge --async`` (and
    directly by tests). Writes the result back to the job record and
    persists the verdict exactly like a synchronous run.
    """
    from engine.job_store import load_job, save_job
    from engine.task_manager import TaskManager
    from engine.llm import LLMClient
    from engine.judge import Judge, judge_result_to_dict

    job = load_job(job_id)
    if job is None:
        # The dispatcher publishes the record AFTER spawning this child
        # (GR-GAP-046: the record is never visible with a dead/None pid).
        # A fast-starting child can therefore race the parent's save —
        # retry briefly before giving up.
        for _ in range(50):
            time.sleep(0.02)
            job = load_job(job_id)
            if job is not None:
                break
    if job is None:
        print(f"Job not found: {job_id}")
        sys.exit(1)
    if job["status"] != "running":
        print(f"Job {job_id} already {job['status']} — nothing to do")
        sys.exit(0)

    job["pid"] = os.getpid()
    save_job(job)

    wd = job["workdir"]
    tm = TaskManager(wd)
    task = tm.get(job["task_id"])
    if not task:
        job["status"] = "error"
        job["error"] = f"task {job['task_id']} not found in {wd}"
        job["finished_at"] = time.time()
        save_job(job)
        print(job["error"])
        sys.exit(1)

    try:
        llm = LLMClient()
        config = load_config(wd)
        judge = Judge(llm, wd, guard_config=config)
        result = judge.evaluate_task(task)
        job["result"] = judge_result_to_dict(job["task_id"], wd, result)
        job["status"] = "complete"
        job["finished_at"] = time.time()
        save_job(job)
        # Same verdict persistence as a synchronous run
        _persist_result(wd, task, result)
        print(result.summary)
    except Exception as e:
        job["status"] = "error"
        job["error"] = str(e)
        job["finished_at"] = time.time()
        save_job(job)
        print(f"Evaluation failed: {e}", file=sys.stderr)
        sys.exit(1)


def _cmd_judge_status(job_id: str) -> None:
    """Print the status/result of a background job.

    Exit codes: 0 complete, 2 still running, 1 error/not found.
    """
    from engine.job_store import job_log_path, load_job

    job = load_job(job_id)
    if job is None:
        print(f"Job not found: {job_id}")
        sys.exit(1)

    status = job["status"]
    print(f"Job:      {job['id']}")
    print(f"Status:   {status}")
    print(f"Task:     {job['task_id']}")
    print(f"Workdir:  {job['workdir']}")
    if job.get("pid"):
        print(f"Pid:      {job['pid']}")
    if job.get("started_at"):
        print("Started:  " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(job["started_at"])))
    if job.get("finished_at"):
        print("Finished: " + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(job["finished_at"])))
    print()

    if status == "running":
        print(f"Job is still running (pid {job.get('pid') or 'unknown'}). Poll again later.")
        sys.exit(2)
    if status == "error":
        print(f"Error: {job.get('error')}")
        log = job_log_path(job_id)
        if os.path.exists(log):
            print(f"Log: {log}")
        sys.exit(1)

    result = job.get("result") or {}
    print(f"Result:   {'PASS ✓' if result.get('passed') else 'FAIL ✗'}")
    if result.get("tier1_passed") is not None:
        print(f"Tier 1:   {'PASS' if result['tier1_passed'] else 'FAIL'}")
    if result.get("verdict"):
        print(f"Verdict:  {result['verdict']}")
        for item in result.get("items", []):
            mark = "✓" if item.get("status") == "PASS" else "✗"
            print(f"  {mark} {item.get('criterion')}: {item.get('detail')}")
    if result.get("summary"):
        print()
        print(result["summary"])
    sys.exit(0)


def _git_nul_paths(workdir: str, *args: str) -> set[bytes]:
    """Return Git pathnames from a NUL-delimited command result.

    Git's ``-z`` output is deliberately kept as bytes until display time so
    spaces, newlines, and non-UTF-8 filenames cannot corrupt the path set.
    """
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        cwd=workdir,
    )
    if result.returncode != 0:
        detail = (result.stderr or b"").decode(errors="replace").strip()
        raise RuntimeError(detail or f"git {' '.join(args)} failed")
    return {path for path in result.stdout.split(b"\0") if path}


def _snapshot_staged_paths(workdir: str) -> set[bytes]:
    """Snapshot all staged paths, including both sides of renames."""
    return _git_nul_paths(
        workdir,
        "diff",
        "--cached",
        "--name-only",
        "-z",
        "--no-renames",
        "--diff-filter=ACDMRTUXB",
    )


def _git_head(workdir: str) -> bytes | None:
    """Return HEAD's object ID, or None for a repository with no commits."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        capture_output=True,
        cwd=workdir,
    )
    if result.returncode != 0:
        return None
    head = result.stdout.strip()
    return head or None


def _commit_paths(workdir: str, commit: bytes) -> set[bytes]:
    """Return every path represented by a commit, without rename detection."""
    return _git_nul_paths(
        workdir,
        "diff-tree",
        "--root",
        "--no-commit-id",
        "--name-only",
        "-z",
        "--no-renames",
        "-r",
        commit.decode("ascii"),
    )


def _display_git_path(path: bytes) -> str:
    """Render a pathname without allowing control characters to hide it."""
    return repr(os.fsdecode(path))


def cmd_commit(args):
    from engine.guard_manager import GuardManager

    workdir = get_workdir()
    # GR-GAP-051: never commit unguarded — same refusal as `gitreins guard`.
    _require_guard_config(workdir)
    config = load_config(workdir)
    gm = GuardManager(workdir, config=config)
    tier1 = gm.run_all()

    if not tier1.passed:
        print("Tier 1 FAILED — cannot commit:")
        print(tier1.summary)
        sys.exit(1)

    try:
        staged_paths = _snapshot_staged_paths(workdir)
        previous_head = _git_head(workdir)
    except RuntimeError as exc:
        print(f"COMMIT INTEGRITY CHECK FAILED — cannot snapshot Git state: {exc}", file=sys.stderr)
        sys.exit(1)

    if getattr(args, "skip_tier2", False):
        print("Tier 1 PASSED — Tier 2 skipped (--skip-tier2 flag) — committing...")
    else:
        print("Tier 1 PASSED — committing...")
    result = subprocess.run(
        ["git", "commit", "-m", args.message],
        capture_output=True,
        text=True,
        cwd=workdir,
    )
    print(result.stdout + result.stderr)
    if result.returncode != 0:
        sys.exit(result.returncode)

    try:
        new_head = _git_head(workdir)
        committed_paths = _commit_paths(workdir, new_head) if new_head else set()
    except RuntimeError as exc:
        print(f"COMMIT INTEGRITY CHECK FAILED — cannot verify Git state: {exc}", file=sys.stderr)
        sys.exit(1)

    if new_head is None or new_head == previous_head:
        print(
            "COMMIT INTEGRITY CHECK FAILED — git reported success, but HEAD did not advance.",
            file=sys.stderr,
        )
        sys.exit(1)

    missing_paths = sorted(staged_paths - committed_paths)
    if missing_paths:
        print(
            "COMMIT INTEGRITY CHECK FAILED — the new commit omits staged paths:",
            file=sys.stderr,
        )
        for path in missing_paths:
            print(f"  {_display_git_path(path)}", file=sys.stderr)
        sys.exit(1)

    print(
        f"Commit completeness confirmed: {len(staged_paths)} staged path(s) represented in {new_head.decode('ascii')[:12]}."
    )
    for path in sorted(staged_paths):
        print(f"  {_display_git_path(path)}")


def cmd_commit_audit(args):
    """Validate a commit message against the staged diff using LLM.

    Reads the message from ``args.message`` or falls back to the git
    commit message file.  Runs the commit_audit pipeline stage and
    exits non-zero if configured to block on bad messages.
    """
    import os
    from engine.pipeline import Pipeline, load_pipeline_config
    from engine.llm import LLMClient

    workdir = get_workdir()
    message = args.message or ""

    # Fallback: read from git COMMIT_EDITMSG
    if not message:
        msg_path = os.path.join(workdir, ".git", "COMMIT_EDITMSG")
        if os.path.exists(msg_path):
            with open(msg_path, "r") as f:
                raw = f.read().strip()
            message = "\n".join(
                line for line in raw.split("\n") if not line.startswith("#")
            ).strip()

    if not message:
        print("No commit message to audit.")
        sys.exit(0)

    # Check for gitreins.skip-tier2 trailer before running audit
    from engine.commit_audit import has_skip_tier2_trailer

    if has_skip_tier2_trailer(message):
        print("Commit audit skipped (gitreins.skip-tier2 trailer)")
        sys.exit(0)

    # Load config and run commit_audit stage
    config = load_pipeline_config(workdir)
    llm = LLMClient()
    pipeline = Pipeline(config, workdir, llm=llm)

    task = {
        "id": "_commit_msg",
        "title": "Commit message audit",
        "criteria": [],
        "commit_message": message,
    }

    result = pipeline.run(task, trigger="commit-msg")

    # Check if audit stage blocked
    audit_stage = result.get("stages", {}).get("commit_audit", {})
    if audit_stage and not audit_stage.get("passed", True):
        print("\n" + audit_stage.get("summary", "Commit message rejected."))
        sys.exit(1)

    if audit_stage:
        print(audit_stage.get("summary", "✓ Commit message OK."))
    sys.exit(0)


def cmd_security_scan(args):
    """Run the Antares CVE localization scanner (GR-117f).

    Default target: staged Python files (``git diff --cached``). With
    ``--directory DIR`` the scanner recurses into DIR instead. Output
    is human-readable by default; ``--output json`` emits a
    machine-readable report. ``--force-ml`` requires the optional
    ``huggingface_hub``/``transformers`` stack — if either is missing
    the command exits non-zero instead of falling back to the
    keyword heuristic.

    Exit codes:
        0  — clean (no findings)
        1  — one or more findings produced
        2  — forced ML mode but dependencies missing
    """
    import json as _json

    workdir = get_workdir()
    config = load_config(workdir)
    security_cfg = (config or {}).get("defaults", {}).get("security_scan", {}) or {}

    force_ml = bool(getattr(args, "force_ml", False))
    output_fmt = getattr(args, "output", "text") or "text"
    directory = getattr(args, "directory", None)

    try:
        from engine.antares import AntaresScanner
    except ImportError as exc:
        print(f"Antares scanner unavailable: {exc}", file=sys.stderr)
        if force_ml:
            sys.exit(2)
        sys.exit(1)

    model_map = {
        "antares-1b": "fdtn-ai/antares-1b",
        "antares-350m": "fdtn-ai/antares-350m",
    }
    model_id = model_map.get(security_cfg.get("model", "antares-1b"), "fdtn-ai/antares-1b")

    if force_ml:
        # When ML is required, both download and inference deps must
        # be present. We check huggingface_hub (for the snapshot) and
        # transformers (for inference). The scanner itself enforces
        # this contract; here we just pre-flight a friendlier error.
        for mod_name, install_hint in (
            ("huggingface_hub", "pip install huggingface_hub"),
            ("transformers", "pip install transformers"),
        ):
            try:
                __import__(mod_name)
            except ImportError as exc:
                print(
                    f"Antares ML mode requires {mod_name}: {exc}\nInstall with: {install_hint}",
                    file=sys.stderr,
                )
                sys.exit(2)

    scanner = AntaresScanner(workdir, model_id=model_id, use_ml=force_ml)

    try:
        if directory:
            findings = scanner.scan_directory(directory)
        else:
            findings = scanner.scan_staged_files()
    except ImportError as exc:
        # Raised by the scanner when force_ml=True and the ML stack
        # couldn't be reached. Already covered above, but stay safe
        # in case the failure happens after pre-flight.
        print(f"Antares ML dependencies missing: {exc}", file=sys.stderr)
        if force_ml:
            sys.exit(2)
        findings = []
    except Exception as exc:  # noqa: BLE001
        logger = logging.getLogger("gitreins")
        logger.warning("Antares scan failed: %s", exc)
        print(f"Antares scan failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if output_fmt == "json":
        payload = [
            f.to_dict()
            if hasattr(f, "to_dict")
            else {
                "file": getattr(f, "file", ""),
                "line": getattr(f, "line", 0),
                "cve_id": getattr(f, "cve_id", ""),
                "confidence": getattr(f, "confidence", 0.0),
                "description": getattr(f, "description", ""),
            }
            for f in findings
        ]
        print(_json.dumps(payload, indent=2))
    else:
        if not findings:
            target = directory or "staged files"
            print(f"Antares: clean — no findings in {target}")
        else:
            target = directory or "staged files"
            print(f"Antares: {len(findings)} potential finding(s) in {target}:")
            for f in findings:
                print(f"  • {f.file}:{f.line} [{f.cve_id} conf={f.confidence:.2f}] {f.description}")

    sys.exit(1 if findings else 0)


def cmd_setup_tools(args):
    """Show available static analysis tools and install instructions for missing ones."""
    from engine.static_analysis import find_tool, _TOOL_INSTALL_GUIDE

    workdir = get_workdir()
    lang = _detect_language(workdir)

    lang_tools_map = {
        "python": ["mypy", "pyright"],
        "ruby": ["sorbet"],
        "sql": ["sqlfluff"],
        "php": ["phpstan"],
    }
    tools = lang_tools_map.get(lang["type"], [])

    if not tools:
        print(f"No static analysis tools are tracked for {lang['name']}.")
        return

    print(f"Static Analysis Tools for {lang['name']}:")
    found = 0
    missing = 0
    for tool in tools:
        path = find_tool(tool)
        if path:
            found += 1
            display = path.split("/")[-1] if "/" in path else path
            print(f"  {tool:<12} ✓ found  ({display})")
        else:
            missing += 1
            install = _TOOL_INSTALL_GUIDE.get(
                tool,
                f"Install {tool} from your package manager",
            )
            print(f"  {tool:<12} ✗ not installed — install: {install}")

    print()
    print(f"{found} tools available, {missing} missing.")


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
    create_p.add_argument(
        "--depends-on",
        action="append",
        default=[],
        help="Task ID that must complete first (repeatable)",
    )

    start_p = task_sub.add_parser("start", help="Start a task")
    start_p.add_argument("id")

    complete_p = task_sub.add_parser(
        "complete",
        help="Complete and evaluate a task",
        description="Complete a task and run Tier 1 plus the Tier 2 LLM evaluator.",
        epilog=(
            "Tier 2 requires GITREINS_LLM_API_KEY (or a supported provider key); "
            "configure GITREINS_LLM_BASE_URL and GITREINS_LLM_MODEL as needed. "
            "Use --skip-tier2 for an explicit Tier 1-only evaluation."
        ),
    )
    complete_p.add_argument("id")
    complete_p.add_argument("--force", "-f", action="store_true", help="Skip dependency checks")
    complete_p.add_argument(
        "--skip-tier2", action="store_true", help="Skip Tier 2 LLM evaluation; Tier 1 guards only"
    )

    list_p = task_sub.add_parser("list", help="List tasks")
    list_p.add_argument("--status", choices=["pending", "in_progress", "complete"])

    delete_p = task_sub.add_parser("delete", help="Delete a task")
    delete_p.add_argument("id")

    # worktree diagnostics + lifecycle
    worktree_p = sub.add_parser(
        "worktree", help="Git worktree diagnostics and task worktree lifecycle"
    )
    worktree_sub = worktree_p.add_subparsers(dest="subcommand")
    worktree_sub.add_parser("doctor", help="Validate the shared canonical board resolution")

    worktree_sub.add_parser(
        "list", help="List registered task worktrees (task, branch, state, phase, age, cap)"
    )

    worktree_fleet_p = worktree_sub.add_parser(
        "fleet",
        help="Run explicit task lanes concurrently in isolated worktrees",
    )
    worktree_fleet_p.add_argument("manifest", help="JSON/YAML manifest containing a lanes list")
    worktree_fleet_p.add_argument(
        "--max-concurrent-worktrees",
        type=int,
        default=None,
        help="Override configured fleet cap for this run (positive integer)",
    )
    worktree_fleet_p.add_argument("--tick", default=None, help="Optional tick/job id")
    worktree_fleet_p.add_argument(
        "--merge", action="store_true", help="Apply successful lanes serially after execution"
    )
    worktree_fleet_p.add_argument(
        "--force-merge", action="store_true", help="Bypass verdict gates when used with --merge"
    )
    worktree_fleet_p.add_argument(
        "--actor", default=None, help="Identity required by --force-merge"
    )

    worktree_fresh_p = worktree_sub.add_parser(
        "fresh", help="Run one shell command in a fresh detached worktree"
    )
    worktree_fresh_p.add_argument("--cmd", required=True, help="Shell command executed with sh -c")
    worktree_fresh_p.add_argument(
        "--json", dest="json_path", help="Write evidence JSON to this path"
    )
    worktree_fresh_p.add_argument("--keep", action="store_true", help="Retain the disposable tree")
    worktree_fresh_p.add_argument("--timeout", type=float, help="Command timeout in seconds")

    worktree_repro_p = worktree_sub.add_parser(
        "repro", help="Run a command repeatedly in fresh detached worktrees"
    )
    worktree_repro_p.add_argument("--cmd", required=True, help="Shell command executed with sh -c")
    worktree_repro_p.add_argument("-k", type=int, required=True, help="Number of repetitions")
    worktree_repro_p.add_argument(
        "--concurrency", type=int, help="Maximum concurrent runs (default: configured cap)"
    )
    worktree_repro_p.add_argument("--timeout", type=float, help="Per-run timeout in seconds")
    worktree_repro_p.add_argument(
        "--keep-failures", action="store_true", help="Retain failed trees"
    )
    worktree_repro_p.add_argument(
        "--json", dest="json_path", help="Write evidence JSON to this path"
    )

    worktree_dogfood_p = worktree_sub.add_parser(
        "dogfood", help="Exercise init, task, guard, and judge in a throwaway tree"
    )
    worktree_dogfood_p.add_argument(
        "--keep", action="store_true", help="Retain the disposable tree"
    )
    worktree_dogfood_p.add_argument(
        "--skip-judge", action="store_true", help="Skip Tier 2 deterministically"
    )
    worktree_dogfood_p.add_argument("--test-command", help="Override the guard test command")
    worktree_dogfood_p.add_argument("--timeout", type=float, help="Per-step timeout in seconds")
    worktree_dogfood_p.add_argument(
        "--json", dest="json_path", help="Write evidence JSON to this path"
    )

    worktree_clean_p = worktree_sub.add_parser(
        "clean",
        help="Reap merged worktrees immediately; stale/orphan only with confirmation",
    )
    worktree_clean_p.add_argument(
        "--confirm-stale-orphan",
        dest="confirm_stale_orphan",
        action="store_true",
        help=(
            "Also remove stale (>24h without heartbeat) and orphan trees. "
            "Without this flag they are reported and kept."
        ),
    )

    worktree_merge_p = worktree_sub.add_parser(
        "merge",
        help="Judge-gated fast-forward merge of a task worktree into canonical main",
    )
    worktree_merge_p.add_argument("id", help="Task id whose registered worktree should be merged")
    worktree_merge_p.add_argument(
        "--force",
        action="store_true",
        help="Bypass only the verdict gate; all Git safety checks still apply",
    )
    worktree_merge_p.add_argument(
        "--actor",
        help="Required identity recorded when --force bypasses the verdict gate",
    )
    worktree_merge_p.add_argument(
        "--reason",
        default="explicit judge-gate override",
        help="Reason recorded for a --force override",
    )

    task_wt_p = task_sub.add_parser(
        "worktree",
        help="Create (or idempotently reuse) a task's isolated worktree",
    )
    task_wt_p.add_argument("id", help="Task id — also names the branch and worktree directory")
    task_wt_p.add_argument(
        "--tick",
        dest="tick",
        default=None,
        help="Optional tick/job id to record in the registry entry",
    )

    # guard
    guard_p = sub.add_parser("guard", help="Run Tier 1 guards")
    guard_p.add_argument(
        "--dead-code",
        action="store_true",
        help="Enable Python dead-code detection (overrides config)",
    )
    guard_p.add_argument(
        "--staged-only",
        dest="staged_only",
        action="store_true",
        help=(
            "Run tests in diff mode — only packages with staged changes "
            "(overrides config guards.test_mode)"
        ),
    )
    guard_p.add_argument(
        "--full",
        dest="full",
        action="store_true",
        help=(
            "Run the full test suite (overrides config guards.test_mode; "
            "default when neither flag is given). Grades the whole tree "
            "even with an empty index: the tests lane runs and lint covers "
            "tracked+untracked Python files instead of skipping."
        ),
    )

    # judge
    judge_p = sub.add_parser("judge", help="Evaluate a task")
    judge_p.add_argument("id")
    judge_p.add_argument(
        "--skip-tier2", action="store_true", help="Skip Tier 2 LLM evaluation; Tier 1 guards only"
    )
    judge_p.add_argument(
        "--async",
        dest="async_dispatch",
        action="store_true",
        help="Dispatch the evaluation as a detached background job (survives this CLI exiting); poll with `gitreins judge --status <job_id>`",
    )
    judge_p.add_argument(
        "--status",
        action="store_true",
        help="Show the status/result of a background job (id = job id, not task id)",
    )
    judge_p.add_argument(
        "--run-job",
        dest="run_job",
        action="store_true",
        help=argparse.SUPPRESS,
    )

    # commit
    commit_p = sub.add_parser("commit", help="Commit with guard checks")
    commit_p.add_argument("message")
    commit_p.add_argument(
        "--skip-tier2", action="store_true", help="Skip any Tier 2 processing; Tier 1 guards only"
    )

    # commit-audit
    audit_p = sub.add_parser(
        "commit-audit", help="Validate commit message against staged diff (commit-msg hook)"
    )
    audit_p.add_argument(
        "message", nargs="?", help="Commit message (reads from COMMIT_EDITMSG if omitted)"
    )

    # mcp-server
    sub.add_parser(
        "mcp-server",
        help="Run MCP stdio server",
        description="Run the MCP stdio server (no flags required).",
        epilog=(
            "Configuration is via environment variables:\n"
            "  GITREINS_LLM_API_KEY   API key for the LLM provider\n"
            "  GITREINS_LLM_BASE_URL  Base URL of the LLM API (default: https://api.openai.com/v1)\n"
            "  GITREINS_LLM_MODEL     Model name (default varies by provider)\n"
            "  GITREINS_LLM_REASONING Reasoning mode: 'enabled' or 'disabled' (default: disabled)\n"
            "\n"
            "The MCP tool mcp_gitreins_configure can hot-reload the LLM config at runtime.\n"
            "\n"
            "Example:\n"
            "  export GITREINS_LLM_API_KEY=sk-...\n"
        ),
    )

    # security-scan
    security_p = sub.add_parser(
        "security-scan",
        help="Run the Antares CVE localization scanner (opt-in)",
    )
    security_p.add_argument(
        "--directory",
        "-d",
        help="Scan a directory recursively instead of staged files",
    )
    security_p.add_argument(
        "--output",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    security_p.add_argument(
        "--force-ml",
        action="store_true",
        help="Require ML inference (fails if huggingface_hub/transformers missing)",
    )

    # setup-tools
    sub.add_parser(
        "setup-tools", help="Show available static analysis tools and install instructions"
    )

    # qa — QA run ledger
    qa_p = sub.add_parser(
        "qa",
        help="QA run ledger — record and read QA run outcomes",
        description=(
            "QA verdicts are recorded in a QA ledger so the harness record covers QA\n"
            "runs, not only foreman/dev task verdicts:\n"
            "  * `gitreins worktree fresh|repro|dogfood` records its own outcome;\n"
            "  * `gitreins qa record` accepts an outcome produced outside the harness.\n"
            "Rows carry the fleet QA-ledger keys (ts, project, status, cells, findings,\n"
            "evidence, note) plus harness extras (kind, verdict, run_id, exit_code,\n"
            "commit, harness_version, detail)."
        ),
        epilog=(
            "Ledger path: GITREINS_QA_LEDGER (file or directory) > qa_ledger.path in\n"
            ".gitreins/config.yaml > <repo>/.gitreins/qa-ledger.jsonl. Recording is off\n"
            "when qa_ledger.enabled is false; qa_ledger.max_entries keeps the newest N.\n"
        ),
    )
    qa_sub = qa_p.add_subparsers(dest="qa_command")
    qa_list_p = qa_sub.add_parser("list", help="Show recorded QA run outcomes")
    qa_list_p.add_argument("-n", type=int, default=20, help="Number of recent runs to show")
    qa_list_p.add_argument(
        "--json", dest="as_json", action="store_true", help="Emit ledger rows as JSON"
    )
    qa_record_p = qa_sub.add_parser(
        "record", help="Record a QA run outcome produced outside the harness"
    )
    qa_record_p.add_argument(
        "--project", help="Project id (default: this repository's directory name)"
    )
    qa_record_p.add_argument(
        "--kind", default="lane", help="Run kind, e.g. lane, bunker, dogfood (default: lane)"
    )
    qa_record_p.add_argument(
        "--status", help="Fleet-ledger status word (default: pass/fail from the verdict)"
    )
    qa_record_p.add_argument("--verdict", choices=["PASS", "FAIL"], help="Explicit verdict")
    qa_record_p.add_argument(
        "--exit-code", dest="exit_code", type=int, help="Exit code of the audited run"
    )
    qa_record_p.add_argument(
        "--cell", action="append", metavar="NAME=STATUS", help="Cell outcome (repeatable)"
    )
    qa_record_p.add_argument(
        "--finding", action="append", metavar="ID:TITLE", help="Finding id and title (repeatable)"
    )
    qa_record_p.add_argument("--evidence", help="Path to the run's evidence file")
    qa_record_p.add_argument("--note", help="Free-form note stored with the row")
    qa_record_p.add_argument("--agent", help="Agent id that ran the audit")
    qa_record_p.add_argument("--server", help="Host or bunker the audit ran on")
    qa_record_p.add_argument("--commit", help="Commit audited (default: this repository's HEAD)")
    qa_record_p.add_argument("--ts", help="ISO timestamp of the run (default: now, UTC)")

    # report
    report_p = sub.add_parser("report", help="Show verdict history")
    report_p.add_argument("-n", type=int, default=10, help="Number of recent verdicts to show")
    report_p.add_argument("--interactive", "-i", action="store_true", help="Interactive TUI mode")

    serve_p = sub.add_parser(
        "serve", help="Live judgment browser — local web server (Ctrl-C to stop)"
    )
    serve_p.add_argument("--port", type=int, default=8616, help="Port to bind (default 8616)")
    serve_p.add_argument("--host", default="127.0.0.1", help="Bind address (default 127.0.0.1)")
    serve_p.add_argument(
        "--repo",
        default=None,
        help="Browse another checkout's judgments by path (default: the repository you run from)",
    )
    serve_p.add_argument(
        "--project",
        default="",
        help="Scheduler project name for the tick ledger (e.g. gitreins-poc)",
    )
    serve_p.add_argument("--open", action="store_true", help="Open the browser automatically")

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
        # QA-GITREINS-POC-6: a task write can refuse to clobber state that could
        # not be read (and could not be preserved). Surface that as one clean
        # line + exit 1 instead of a traceback, like the other task paths do.
        from engine.task_manager import TaskStateCorruptError

        try:
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
            elif args.subcommand == "worktree":
                cmd_task_worktree(args)
            else:
                parser.print_help()
        except TaskStateCorruptError as exc:
            print(f"error: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
    elif args.command == "worktree":
        if args.subcommand == "doctor":
            cmd_worktree_doctor(args)
        elif args.subcommand == "list":
            cmd_worktree_list(args)
        elif args.subcommand == "fleet":
            cmd_worktree_fleet(args)
        elif args.subcommand == "fresh":
            cmd_worktree_fresh(args)
        elif args.subcommand == "repro":
            cmd_worktree_repro(args)
        elif args.subcommand == "dogfood":
            cmd_worktree_dogfood(args)
        elif args.subcommand == "clean":
            cmd_worktree_clean(args)
        elif args.subcommand == "merge":
            cmd_worktree_merge(args)
        else:
            parser.print_help()
    elif args.command == "guard":
        cmd_guard_run(args)
    elif args.command == "judge":
        cmd_judge(args)
    elif args.command == "commit":
        cmd_commit(args)
    elif args.command == "commit-audit":
        cmd_commit_audit(args)
    elif args.command == "mcp-server":
        cmd_mcp_server(args)
    elif args.command == "security-scan":
        cmd_security_scan(args)
    elif args.command == "setup-tools":
        cmd_setup_tools(args)
    elif args.command == "qa":
        if args.qa_command == "list":
            cmd_qa_list(args)
        elif args.qa_command == "record":
            cmd_qa_record(args)
        else:
            parser.print_help()
    elif args.command == "report":
        cmd_report(args)
    elif args.command == "serve":
        cmd_serve(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
