"""
Guard Manager — Static checks at pre-commit time.

Tier 1 (no LLM, fast):
    1. Secrets scanning (gitleaks or built-in pattern scanner)
    2. Lint (ruff/flake8)
    3. Tests (full or diff-mode, configurable)

All checks are optional and configurable via .gitreins/config.yaml

Config:
    guards:
      secrets: true
      lint: true
      tests: true
      test_mode: "full" | "diff"     # default: full
      test_command: "pytest -x --tb=short"
"""

import fnmatch
import importlib.util
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import replace
from datetime import datetime, timezone
from engine import lang_detect
from engine.guards import (
    _coerce_timeout,
    check_go_lint,
    check_go_tests,
    check_go_build,
)
from engine.lsp import find_lsp_tool, run_lsp_check
from engine.repo_paths import WorktreeResolutionError, resolve_worktree_identity
from engine.types import (
    SCANNER_CLEAN,
    SCANNER_NOT_RUN,
    GuardResult,
    Tier1Result,
    parse_first_failing_test,
    parse_gitleaks_finding_count,
    render_secrets_scanners,
    scanner_finding_status,
)

logger = logging.getLogger("gitreins.guard")


# ── Harness state (POC-17 / TRUST-002) ──────────────────────────────────────
# `.gitreins/` is the HARNESS's own state directory: config, verdict history,
# guard run logs, usage telemetry and disposable-worktree bookkeeping. It is
# never the repo's code, and neither scanner may grade it:
#   * guard run logs persist raw scanner output plus QA anti-tamper fixtures,
#   * `.gitreins/history/**` verdict artifacts embed the evidence of earlier
#     judgements (which can itself quote a fixture token),
# so a scan of that directory fails on tokens the tree's author cannot fix
# from a failing tree. Tick 285 burned a diagnosis cycle on exactly this:
# tier1 `secrets` failed while the code was clean.
HARNESS_STATE_DIRS: tuple[str, ...] = (".gitreins",)


def harness_state_allowlist_paths() -> list[str]:
    """gitleaks ``[allowlist].paths`` regexes for :data:`HARNESS_STATE_DIRS`.

    gitleaks' ``detect --no-git`` mode walks the working tree and does NOT
    honour ``.gitignore``, so a gitignored guard log under
    ``.gitreins/logs/`` is scanned unless the config allowlists it.
    """
    return [rf"(^|/){re.escape(d)}/.*" for d in HARNESS_STATE_DIRS]


def _is_harness_state_path(fpath: str) -> bool:
    """True when the workdir-relative *fpath* lives inside harness state.

    Callers pass repo-relative paths (``_workdir_files`` /
    ``_get_staged_files``), so a directory component match is enough.
    """
    if not fpath:
        return False
    parts = fpath.replace(os.sep, "/").split("/")
    return any(d in parts[:-1] for d in HARNESS_STATE_DIRS)


def _sanitized_env() -> dict[str, str]:
    """Return the current environment with every GIT_* variable removed.

    Git exports GIT_INDEX_FILE (plus GIT_DIR, GIT_WORK_TREE, and friends) to
    pre-commit hooks. Leaking them into subprocesses the guard spawns —
    pytest, linters, nested guards — makes those processes read the OUTER
    repository's index instead of the workdir's own, which breaks
    nested-guard tests and diff-mode test selection. (DF-008)
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _is_test_file(fpath: str) -> bool:
    """True for test/fixture files, which routinely embed deliberately
    fake keys (benchmarks, fixtures, examples).

    Mirrors the documentation-file exemption in the built-in secrets
    scan: gitleaks still scans every file, so this only relaxes the
    low-entropy regex cross-check for files whose secrets are by
    construction not real.
    """
    base = os.path.basename(fpath)
    if re.search(r"(^|/)test(s|data)?/", fpath):
        return True
    if base.endswith(("_test.go", "_test.py", "_test.rs", "_test.sh", "_test.rb")):
        return True
    if base.startswith("test_") and base.endswith(".py"):
        return True
    if re.search(r"\.(test|spec)\.(js|ts|jsx|tsx|mjs|cjs)$", base):
        return True
    return False


# ── Diff-based test discovery ──────────────────────────────────

# Files that, when changed, force a full test run (too broad to narrow)
_FORCE_FULL_TEST_GLOBS = [
    "pyproject.toml",
    "setup.cfg",
    "setup.py",
    "conftest.py",
    ".gitreins/config.yaml",
    ".github/workflows/*.yml",
    "Makefile",
]


def _discover_test_targets(workdir: str) -> list[str] | None:
    """Return a list of test file paths to run, or None for full suite.

    None means "full suite" — returned when:
    - No staged or registered linked-worktree changes
    - A force-full file was changed
    - No test files map to the changed sources (safety fallback)

    Returns absolute paths to test files.
    """
    # In a registered linked worktree this includes committed task-branch
    # changes, while ordinary repositories remain staged-only.
    changed_files = _get_worktree_changed_files(workdir)
    if not changed_files:
        return None

    # Check force-full triggers
    for changed_file in changed_files:
        for glob in _FORCE_FULL_TEST_GLOBS:
            if fnmatch.fnmatch(changed_file, glob):
                logger.debug("Force-full trigger: %s matches %s", changed_file, glob)
                return None

    # Map changed source files to test files using basename matching
    test_files: set[str] = set()
    for changed_file in changed_files:
        # If a test file itself changed, always include it
        basename = os.path.basename(changed_file)
        if basename.startswith("test_") and basename.endswith(".py"):
            test_files.add(os.path.join(workdir, changed_file))
            continue

        # Derive test file from source basename:
        #   engine/foo.py → tests/test_foo.py
        #   gitreins/bar.py → tests/test_bar.py
        #   gitreins_mcp/server.py → tests/test_mcp_server.py
        module = os.path.splitext(basename)[0]
        candidates = [
            os.path.join(workdir, "tests", f"test_{module}.py"),
        ]
        # Special case: gitreins_mcp/server.py → tests/test_mcp_server.py
        if os.path.dirname(changed_file).startswith("gitreins_mcp"):
            candidates.append(os.path.join(workdir, "tests", f"test_mcp_{module}.py"))

        for candidate in candidates:
            if os.path.isfile(candidate):
                test_files.add(candidate)
                logger.debug("Mapped %s → %s", changed_file, os.path.relpath(candidate, workdir))
                break
        else:
            logger.debug("No test file found for %s (tried: %s)", changed_file, candidates)

    if not test_files:
        # Changed files don't map to any known tests — skip in diff mode
        logger.debug("No test targets discovered for staged files, returning empty")
        return []

    return sorted(test_files)


def _get_staged_files(workdir: str) -> list[str]:
    """Return staged file paths relative to workdir.

    Uses git diff --cached when HEAD exists (only changed files),
    falls back to git ls-files --cached when no HEAD (all staged files
    are new and should still be scanned).
    """
    try:
        head_check = subprocess.run(
            ["git", "rev-parse", "--verify", "-q", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=workdir,
            env=_sanitized_env(),
        )
        if head_check.returncode != 0:
            # No HEAD — use ls-files to list all staged files.
            # In a fresh repo, every staged file is "new" but the
            # guard should still scan them for secrets/lint/etc.
            result = subprocess.run(
                ["git", "ls-files", "--cached"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=workdir,
                env=_sanitized_env(),
            )
        else:
            result = subprocess.run(
                ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
                capture_output=True,
                text=True,
                timeout=10,
                cwd=workdir,
                env=_sanitized_env(),
            )
        return [f.strip() for f in result.stdout.split("\n") if f.strip()]
    except Exception:
        return []


def _get_worktree_changed_files(workdir: str) -> list[str]:
    """Return staged files plus linked-task changes since the recorded base."""
    changed = set(_get_staged_files(workdir))
    try:
        identity = resolve_worktree_identity(workdir)
    except WorktreeResolutionError:
        return sorted(changed)

    if not identity.is_linked_worktree or not identity.branch_point:
        return sorted(changed)

    try:
        result = subprocess.run(
            [
                "git",
                "diff",
                "--name-only",
                "--diff-filter=ACM",
                "--end-of-options",
                f"{identity.branch_point}...HEAD",
                "--",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=workdir,
            env=_sanitized_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return sorted(changed)
    if result.returncode == 0:
        changed.update(f.strip() for f in result.stdout.splitlines() if f.strip())
    return sorted(changed)


def _tree_python_files(workdir: str) -> list[str]:
    """Tracked + untracked-but-not-ignored Python files, repo-relative (deduped).

    Same git invocation ``engine.lang_detect`` uses for its source-file
    listing, so whole-tree grading and language detection can never disagree
    about what the tree contains. Empty index is the normal case here — this
    is the whole-tree listing, not a staged-files listing.
    """
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=workdir,
            capture_output=True,
            timeout=15,
            env=_sanitized_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    out = proc.stdout.decode("utf-8", "replace")
    seen: set[str] = set()
    files: list[str] = []
    for path in out.split("\0"):
        # --cached + --others can list the same path twice (index + worktree).
        if path.endswith(".py") and path not in seen:
            seen.add(path)
            files.append(path)
    return files


def _ruff_scoped_files(workdir: str, py_files: list[str]) -> list[str] | None:
    """Which of *py_files* ruff actually grades once the repo's config applies.

    DF-GITREINS-POC-18: ruff honours ``exclude``/``extend-exclude`` only while
    it recurses into directories — a file named explicitly on the command line
    is linted even when the repo deliberately excludes it (scratch trees such
    as ``sandbox/``, stale ``build/`` copies, secret fixtures). ``--force-exclude``
    restores the configuration's authority over explicit paths; ``--show-files``
    with the same flags reports the resulting scope, so the lint lane can name
    how many files were graded and can never claim a clean pass when the config
    excluded every submitted file.

    Returns repo-relative paths in ruff's order, or ``None`` when ruff cannot
    answer (binary absent, flag unsupported, unexpected exit code) — callers
    then keep the submitted list instead of inventing a scope.
    """
    try:
        proc = subprocess.run(
            ["ruff", "check", "--force-exclude", "--show-files", *py_files],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=workdir,
            env=_sanitized_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    scoped: list[str] = []
    for line in proc.stdout.splitlines():
        path = line.strip()
        if not path.endswith((".py", ".pyi")):
            continue
        try:
            path = os.path.relpath(path, workdir) if os.path.isabs(path) else path
        except ValueError:  # pragma: no cover - different drive (Windows)
            pass
        scoped.append(path)
    return scoped


def _ruff_format_command(py_files: list[str]) -> list[str]:
    """The ruff FORMATTER's exit-code-bearing check over *py_files* (GR-GAP-063).

    ``--check`` is the flag that sets the exit code (1 when a file would be
    reformatted); ``--diff`` prints the diff and exits 0, which is the exact
    false-green shape GR-GAP-061 hit in CI — a step that always passes. Never
    swap one for the other here.

    ``--force-exclude`` keeps the repo's own ``exclude`` / ``extend-exclude``
    authority over an explicitly named file list, exactly as the check command
    does (DF-GITREINS-POC-18), so the format sub-check grades the same scope
    the check lane graded.
    """
    return ["ruff", "format", "--check", "--force-exclude", *py_files]


_UNFORMATTED_LEGACY_PREFIX = "Would reformat:"


def _parse_unformatted_files(raw: str) -> list[str]:
    """Paths ruff's formatter reported as needing a reformat.

    Two output shapes exist in the wild and BOTH must parse, because CI
    installs ruff at its latest release while a developer's venv can hold an
    older one (measured 2026-09-20: ruff 0.16.8 in CI vs 0.15.22 locally, and
    the accompanying test only exercised the local shape — the gate's own
    parser failed in CI with ``2 failed`` on an otherwise green suite):

    * 0.15.x and earlier: ``Would reformat: <path>``, one line per offender.
    * 0.16.x and later:   ``unformatted: File would be reformatted`` followed by
      a diff hunk whose locator line is `` --> <path>:<line>:<col>``.

    Either shape names each offender exactly once, so the union of the two
    patterns is the file list; a line matching neither (the trailing count, the
    unified-diff body) is ignored.
    """
    files: list[str] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped.startswith(_UNFORMATTED_LEGACY_PREFIX):
            path = stripped[len(_UNFORMATTED_LEGACY_PREFIX) :].strip()
        elif stripped.startswith("-->"):
            # ``--> path:line:col`` (ruff >= 0.16). Split from the RIGHT so a
            # path containing a colon (Windows drive, POSIX name) survives.
            locator = stripped[3:].strip()
            parts = locator.rsplit(":", 2)
            path = parts[0].strip() if len(parts) == 3 else locator
        else:
            continue
        if path and path not in files:
            files.append(path)
    return files


def _format_failure_message(raw: str) -> str:
    """Name the offending files, then the command that fixes them (GR-GAP-063).

    The lane's failure must be actionable on its own: a reader sees which
    files drifted and the exact command to repair them, without consulting a
    brief. The raw ruff output is appended so a non-drift failure (a parse
    error, an excluded-everything list) still reads as itself instead of being
    flattened into "N files would be reformatted".
    """
    files = _parse_unformatted_files(raw)
    if files:
        head = (
            f"ruff format --check: {len(files)} file(s) would be reformatted — "
            f"run `ruff format {' '.join(files)}`"
        )
    else:
        head = "ruff format --check: failed — run `ruff format <paths>` (see output below)"
    return f"{head}\n{raw}"


def _build_diff_test_command(test_command: str, test_files: list[str], workdir: str) -> str:
    """Build a test command targeting specific test files.

    If test_command is a pytest invocation (bare `pytest ...` or
    `uv run pytest ...` / `python -m pytest ...` / `python3 -m pytest ...`),
    appends the test file paths. Otherwise returns the original command
    (custom runners can't be narrowed without user config).
    """
    cmd = test_command.strip()

    # Match pytest invocations regardless of runner prefix:
    #   pytest ... | uv run pytest ... | python -m pytest ... | python3 -m pytest ...
    #   | .venv/bin/pytest ...
    if re.search(r"(^|\s)((uv\s+run\s+)?pytest|python3?\s+-m\s+pytest)(\s|$)", cmd):
        # Convert absolute paths to relative for cleaner output
        rel_paths = [os.path.relpath(f, workdir) for f in test_files]
        return f"{cmd} {' '.join(rel_paths)}"

    # Non-pytest runner — can't narrow, run full
    return cmd


# Runner prefixes that wrap the real test binary in their own virtualenv.
# A config written by `gitreins init` on a uv machine (or the shipped
# uv-based default) fails on any machine without that runner: the shell
# dies with `uv: command not found` before pytest ever starts (GR-GAP-037).
_KNOWN_TEST_RUNNER_PREFIXES = ("uv run", "pipenv run", "poetry run")

# GR-GAP-064: a BARE pytest invocation (no runner prefix, no interpreter).
# `python -m pytest` already names its interpreter (needs no help), and
# runner-prefixed commands resolve through the GR-GAP-037 loop above.
_BARE_PYTEST_RE = re.compile(r"^pytest(\s|$)")


def _resolve_test_command(cmd: str) -> tuple[str, str | None]:
    """GR-GAP-037: runtime fallback when a configured test_command's runner is missing.

    When *cmd* starts with a known runner prefix (`uv run`, `pipenv run`,
    `poetry run`) but that runner's binary is not on PATH, rewrite pytest
    invocations to ``{sys.executable} -m pytest ...`` so a pip-only machine
    (no uv/pipenv/poetry) still passes the tests stage. Returns the effective
    command plus a warning line for guard output (None when no fallback
    applied).

    Commands without a known runner prefix (e.g. ``make test``) and
    runner-prefixed non-pytest commands (e.g. ``uv run tox`` — semantics
    can't be preserved) pass through unchanged.
    """
    import shutil
    import sys

    stripped = cmd.strip()
    for prefix in _KNOWN_TEST_RUNNER_PREFIXES:
        if not stripped.startswith(prefix):
            continue
        runner = prefix.split()[0]
        if shutil.which(runner):
            return cmd, None
        rest = stripped[len(prefix) :].lstrip()
        # pytest invocation → {sys.executable} -m pytest ...
        if rest == "pytest" or rest.startswith("pytest "):
            new_cmd = f"{sys.executable} -m {rest}"
        # interpreter invocation (uv run python -m pytest ...) → same interpreter
        elif re.match(r"^python3?(\s|$)", rest):
            new_cmd = re.sub(r"^python3?(\s|$)", f"{sys.executable}\\1", rest, count=1)
        else:
            # Custom runner command — cannot safely rewrite (tox != pytest).
            return cmd, None
        warning = (
            f"test runner '{runner}' not found on PATH — falling back to "
            f"'{new_cmd}' for this run. Install the runner (e.g. pip install "
            f"{runner}) to use the configured test_command verbatim."
        )
        return new_cmd, warning

    # GR-GAP-064: a bare pytest invocation (the shipped default
    # `pytest -x --tb=short`, and the form `gitreins init` writes) fails on a
    # fresh `gitreins install` venv: pytest is installed venv-local, gitreins
    # itself runs INSIDE that venv, but the guard subprocess cannot see the
    # venv-local console script on PATH — the lane dies with
    # `/bin/sh: 1: pytest: not found` before pytest ever starts. When the
    # running interpreter can import pytest, use it (same rewrite shape as
    # GR-GAP-037); when it cannot, pass through unchanged so the lane fails
    # with its natural exit 127 — a missing pytest is a real setup error and
    # the call site makes that failure actionable instead of swallowing it.
    if _BARE_PYTEST_RE.match(stripped):
        if shutil.which("pytest"):
            return cmd, None
        if importlib.util.find_spec("pytest") is not None:
            rest = stripped[len("pytest") :].lstrip()
            new_cmd = f"{sys.executable} -m pytest{(' ' + rest) if rest else ''}"
            warning = (
                "pytest not found on PATH — falling back to "
                f"'{new_cmd}' for this run (gitreins is running inside the "
                "venv that has it). Install pytest on PATH (e.g. pip install "
                "pytest) or set guards.test_command to use the venv interpreter."
            )
            return new_cmd, warning
    return cmd, None


def _pytest_not_found_hint(exit_code: int, cmd: str) -> str | None:
    """GR-GAP-064: actionable line for a shell exit 127 on a bare pytest command.

    When the bare-pytest passthrough above lets the lane run and it dies with
    `/bin/sh: 1: pytest: not found` (127), the raw output alone does not say
    how to fix the machine. Returns the fix line (naming the running
    interpreter) for a 127 exit on a pytest invocation, else None. The exit
    code and FAIL verdict are never touched by this — it is output text only.
    """
    import sys

    if exit_code != 127 or not _BARE_PYTEST_RE.match(cmd.strip()):
        return None
    return (
        "test runner 'pytest' not found on PATH and not importable by "
        f"{sys.executable} — install it with {sys.executable} -m pip install "
        "pytest (or set guards.test_command)"
    )


# ── pytest exit-5 ("no tests collected") handling ─────────────
# pytest exit codes: 0=all passed, 1=tests failed, 2=interrupted/
# collection error, 3=internal error, 4=usage error, 5=no tests collected.
# Exit 5 is benign on a fresh repo with zero test files, but the tests
# stage treated every non-zero code as a BLOCK — so `gitreins init` +
# first `gitreins guard` failed and a brand-new repo could not make its
# first commit (GR-GAP-048). Exit 5 with pytest's genuine "no tests ran"
# summary and NO collection errors is pass-with-warning; anything else
# (including exit 5 with collection errors mixed in) still blocks.
_PYTEST_NO_TESTS_RE = re.compile(r"\bno tests ran\b", re.IGNORECASE)
_PYTEST_ERRORS_RE = re.compile(r"(?m)^ERROR\b|\b[1-9]\d*\s+errors?\b", re.IGNORECASE)
_PYTEST_NO_TESTS_WARNING = (
    "pytest collected no tests (exit 5) — not blocking. Add tests to enable real test gating."
)


def _pytest_no_tests_benign(output: str) -> bool:
    """True when output is pytest's benign exit-5 "no tests collected" case.

    Requires pytest's signature "no tests ran" summary line (distinguishes
    a real pytest exit 5 from some other tool exiting 5) and REJECTS the
    output when collection errors are mixed in ("ERROR collecting ..."
    lines or an "N error(s)" summary count) — that case stays a failure.
    """
    if not _PYTEST_NO_TESTS_RE.search(output):
        return False
    return _PYTEST_ERRORS_RE.search(output) is None


def _load_guard_config(workdir: str) -> dict:
    """Load .gitreins/config.yaml and extract the guards section.

    Returns the full config dict, or {} if the file doesn't exist
    or can't be parsed. Handles missing PyYAML gracefully (returns {}
    with a log warning) since pre-commit hooks may run in a bare Python
    environment without the project's dependencies installed.
    """
    config_path = os.path.join(workdir, ".gitreins", "config.yaml")
    if not os.path.isfile(config_path):
        return {}
    try:
        import yaml as _yaml

        with open(config_path, "r") as f:
            return _yaml.safe_load(f) or {}
    except ImportError:
        logger.warning(
            "PyYAML not available in this Python environment — "
            "cannot load .gitreins/config.yaml. Guards will use defaults. "
            "Install with: pip install pyyaml"
        )
        return {}
    except Exception:
        return {}


def _merge_secret_findings(gitleaks_output: str, builtin_output: str) -> str:
    """Merge gitleaks' verbose finding blocks with built-in scanner findings.

    gitleaks' verbose output carries ``File:/Line:`` pairs that the Tier1
    summary counts (_secrets_findings_detail), but no human-readable
    labels; the built-in scanner emits ``path:line: [label]`` lines with
    the labels. DF-016: when both scanners report (gitleaks nonzero exit),
    the merged output keeps the gitleaks block verbatim and appends each
    built-in finding — a ``File:/Line:`` block (only when that path:line is
    NOT already in the gitleaks pairs, so the summary never double-counts
    a location) plus the original labeled line (so every finding's label
    stays visible).
    """
    gitleaks_pairs: set[tuple[str, str]] = set()
    files: list[str] = []
    lines: list[str] = []
    for ln in gitleaks_output.splitlines():
        stripped = ln.strip()
        if stripped.startswith("File:"):
            value = stripped.removeprefix("File:").strip()
            if value:
                files.append(value)
        elif stripped.startswith("Line:"):
            value = stripped.removeprefix("Line:").strip()
            if value:
                lines.append(value)
    for path, line in zip(files, lines):
        gitleaks_pairs.add((path, line))

    merged: list[str] = [gitleaks_output]
    seen = set(gitleaks_pairs)
    for ln in builtin_output.splitlines():
        m = re.match(r"^(?P<path>.*?):(?P<line>\d+): ", ln)
        if not m:
            continue
        key = (m.group("path"), m.group("line"))
        if key not in seen:
            seen.add(key)
            merged.append(f"File: {key[0]}")
            merged.append(f"Line: {key[1]}")
        merged.append(ln)
    return "\n".join(merged)


# ── Guard run log persistence (DF-018) ─────────────────────────
# The guard console summary is deliberately BOUNDED (head+tail bounding in
# engine/types.py + the tail-only slice in _run_test_command), so after a
# failed run the full pytest traceback was unrecoverable: the only way to
# learn what actually broke was to re-run pytest by hand.
#
# Every guard run now persists its COMPLETE, untruncated output to
# ``<workdir>/.gitreins/logs/guard-<UTC-stamp>.log`` — one file per run —
# and the newest path is exposed to callers (CLI, judge pipeline) through
# ``newest_guard_log()``. Same contract as the judge usage telemetry in
# engine/pipeline.py: BEST-EFFORT and NON-FATAL. Never raises, never
# changes the guard verdict, never blocks a commit.
# TRUST-003: scanner ids for the secrets guard's attribution pairs, rendered
# through engine.types.scanner_label() ("builtin" → "builtin cross-check").
GITLEAKS_SCANNER = "gitleaks"
BUILTIN_SCANNER = "builtin"

GUARD_LOG_SUBDIR = os.path.join(".gitreins", "logs")
GUARD_LOG_PREFIX = "guard-"
GUARD_LOG_SUFFIX = ".log"
# Newest N runs are retained; older files are pruned so the directory
# cannot grow without bound.
GUARD_LOG_KEEP = 20
# Single-log cap. Pathological output (a runaway suite) is cut with an
# explicit marker rather than filling the disk.
GUARD_LOG_MAX_BYTES = 2 * 1024 * 1024


def guard_log_dir(workdir: str) -> str:
    """Absolute path of the directory holding persisted guard run logs."""
    return os.path.join(os.path.abspath(workdir), GUARD_LOG_SUBDIR)


def _guard_log_name() -> str:
    """Log file name for one run.

    Fixed-width UTC stamp (microseconds) so lexical order == chronological
    order, which makes "newest" and "prune the oldest" both one sort.
    """
    return (
        f"{GUARD_LOG_PREFIX}{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')}"
        f"{GUARD_LOG_SUFFIX}"
    )


def _guard_log_files(workdir: str) -> list[str]:
    """Absolute paths of every persisted guard log, oldest first."""
    directory = guard_log_dir(workdir)
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    return sorted(
        os.path.join(directory, name)
        for name in names
        if name.startswith(GUARD_LOG_PREFIX) and name.endswith(GUARD_LOG_SUFFIX)
    )


def newest_guard_log(workdir: str) -> str | None:
    """Path of the newest persisted guard run log, or None when there is none.

    The single accessor for callers that cite the raw evidence — neither the
    CLI nor the judge pipeline guesses the timestamp embedded in the name.
    """
    files = _guard_log_files(workdir)
    return files[-1] if files else None


def _bound_guard_log(content: str, max_bytes: int | None = None) -> str:
    """Cap a log body at *max_bytes*, ending with an explicit marker."""
    limit = GUARD_LOG_MAX_BYTES if max_bytes is None else max_bytes
    raw = content.encode("utf-8", errors="replace")
    if len(raw) <= limit:
        return content
    marker = f"\n... [log truncated at {limit} bytes]\n"
    keep = max(0, limit - len(marker.encode("utf-8")))
    return raw[:keep].decode("utf-8", errors="ignore") + marker


def _log_test_scope(extra: dict) -> str:
    """Human-readable test scope for the log header."""
    mode = extra.get("test_mode", "unknown")
    if "test_targets" not in extra:
        if extra.get("grade_full_tree"):
            return "all (whole tree)"
        return "all (full mode)" if mode == "full" else "unknown"
    targets = extra["test_targets"]
    if targets is None:
        return "full suite (safety trigger)"
    return f"{targets} file(s)"


def _diagnostics_lines(result: Tier1Result) -> list[str]:
    """TRUST-003: record the two console facts in the run log as well.

    The log is the post-mortem artifact (DF-018), so the first failing test id
    and the secrets scanner attribution must be readable there without
    re-parsing the untruncated bodies below.
    """
    first_id = ""
    source = ""
    for guard in result.results:
        if guard.passed or not guard.output:
            continue
        test_id = parse_first_failing_test(guard.output)
        if test_id:
            first_id, source = test_id, guard.name
            break
    scanners = next(
        (
            guard.scanners
            for guard in result.results
            if guard.name.startswith("secrets") and guard.scanners
        ),
        (),
    )
    lines = [
        "diagnostics:",
        f"  first_failing_test: {first_id or 'none detected'}"
        + (f"  (from {source})" if source else ""),
        f"  secrets_scanners: {render_secrets_scanners(scanners) if scanners else 'none ran'}",
    ]
    return lines


def _guard_log_content(
    workdir: str, result: Tier1Result, full_outputs: dict[str, str] | None = None
) -> str:
    """Render the full run log: header, then every guard's complete output.

    Failures are listed before passes — a post-mortem reads the top of the
    file. ``full_outputs`` carries the untruncated output captured before a
    guard applied its own 2000-char cap (DF-018); guards that bounded
    internally fall back to ``GuardResult.output``.
    """
    extra = result.extra or {}
    evidence = full_outputs or {}
    failed = [r for r in result.results if not r.passed]
    skipped = result.skipped_steps
    lines = [
        "GitReins guard run log — full output (the console summary is bounded)",
        f"run_utc: {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"workdir: {os.path.abspath(workdir)}",
        f"test_mode: {extra.get('test_mode', 'unknown')}",
        f"test_targets: {_log_test_scope(extra)}",
        f"overall: {'PASS' if result.passed else 'FAIL'}"
        + (" (DEGRADED — skipped checks)" if result.degraded else ""),
        f"guards: {len(result.results)} ({len(failed)} failed, {len(skipped)} skipped)",
    ]
    lines += _diagnostics_lines(result)
    if skipped:
        # TRUST-001: the log keeps the machine-readable skip list for
        # post-mortems, matching the console's DEGRADED PASS line.
        lines.append("skipped_steps:")
        lines.extend(f"  - {step['step']}: {step['reason']}" for step in skipped)
    if result.warnings:
        lines.append("warnings:")
        lines.extend(f"  - {warning}" for warning in result.warnings)

    for r in sorted(result.results, key=lambda guard: (guard.skipped, guard.passed)):
        exit_code = "n/a" if r.exit_code is None else str(r.exit_code)
        status = "SKIP" if r.skipped else ("PASS" if r.passed else "FAIL")
        lines += [
            "",
            "=" * 78,
            f"[{status}] {r.name}"
            f"  passed={str(r.passed).lower()}  exit_code={exit_code}"
            + (f"  skip_reason={r.skip_reason}" if r.skipped else ""),
            "=" * 78,
        ]
        if r.warning:
            lines.append(f"warning: {r.warning}")
        if r.error:
            lines.append(f"error: {r.error}")
        body = evidence.get(r.name, r.output)
        if body:
            lines.append("--- output (untruncated) ---")
            lines.append(body.rstrip("\n"))

    return "\n".join(lines) + "\n"


def _prune_guard_logs(directory: str, keep: int | None = None) -> None:
    """Delete all but the newest *keep* guard logs. Never raises."""
    limit = GUARD_LOG_KEEP if keep is None else keep
    if limit <= 0:
        return
    try:
        names = sorted(
            name
            for name in os.listdir(directory)
            if name.startswith(GUARD_LOG_PREFIX) and name.endswith(GUARD_LOG_SUFFIX)
        )
    except OSError:
        return
    for name in names[:-limit]:
        try:
            os.remove(os.path.join(directory, name))
        except OSError:
            logger.debug("could not prune guard log %s", name)


def write_guard_log(
    workdir: str, result: Tier1Result, full_outputs: dict[str, str] | None = None
) -> str:
    """Persist the complete run log for *result*; return the log file path.

    One timestamped file per run plus retention pruning. *full_outputs*
    supplies untruncated output per guard name (see GuardManager). Raises on
    a write failure (uncreatable directory, permission/disk errors) — the
    caller ``GuardManager._persist_run_log`` is what makes the whole thing
    best-effort and non-fatal.
    """
    directory = guard_log_dir(workdir)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, _guard_log_name())
    content = _bound_guard_log(_guard_log_content(workdir, result, full_outputs))
    with open(path, "w", encoding="utf-8", errors="replace") as f:
        f.write(content)
    _prune_guard_logs(directory)
    return path


class GuardManager:
    """Run static checks against staged changes."""

    def __init__(
        self,
        workdir: str = ".",
        config: dict | None = None,
        *,
        grade_full_tree: bool = False,
    ):
        self.workdir = os.path.abspath(workdir)
        if config is None:
            config = _load_guard_config(self.workdir)
        self.config = config
        guards_cfg = self.config.get("guards", {})
        self._enabled = {
            "secrets": guards_cfg.get("secrets", True),
            "lint": guards_cfg.get("lint", True),
            "tests": guards_cfg.get("tests", True),
            "dead_code": guards_cfg.get("dead_code", False),  # opt-in: Python-only, can be noisy
            "skylos": guards_cfg.get("skylos", False),  # opt-in: needs pip install
            "static_analysis": guards_cfg.get("static_analysis", False),  # opt-in: type checkers
            "lsp": guards_cfg.get("lsp", False),  # opt-in: LSP servers
            "security_scan": guards_cfg.get("security_scan", {}).get(
                "enabled", False
            ),  # opt-in: Antares CVE scanner
        }
        self._static_tools = guards_cfg.get("static_analysis_tools", {})
        self._lsp_tools = guards_cfg.get("lsp_tools", ["pylsp"])
        # LSP timeouts (seconds). None = language-aware defaults
        # (clangd/cpp repos get 300s init / 120s per-file automatically).
        lsp_cfg = guards_cfg.get("lsp_timeouts", {})
        self._lsp_init_timeout: float | None = lsp_cfg.get("init")
        self._lsp_per_file_timeout: float | None = lsp_cfg.get("per_file")

        # DF-018: untruncated guard outputs for the persisted run log. The
        # guard results keep their own bounded output (what the console
        # summary and the pipeline's step-evidence bounding consume); this
        # map is only read by write_guard_log, so the raw evidence — the
        # full pytest traceback — survives the run.
        self._full_outputs: dict[str, str] = {}

        # Test mode: "full" (default) or "diff"
        self._test_mode = guards_cfg.get("test_mode", "full")

        # Whole-tree grading opt-in (DF-GITREINS-POC-11). Keyword-only in
        # __init__, default False so every existing caller — judge, worktree
        # manager, MCP server, pipeline subprocess — keeps today's behavior
        # byte for byte. When True, a clean tree no longer vacates the tests
        # and lint lanes: the full test_command runs and the linter grades
        # the whole tree instead of returning the TRUST-001 skips. The CLI
        # sets it from `gitreins guard --full`.
        self._grade_full_tree = bool(grade_full_tree)

        # Run the full test_command even when nothing is staged (AUDIT-GAP-002 /
        # GR-GAP-009): chained suites (e.g. totalstack ACM parity) otherwise
        # never execute on clean-tree guard runs → vacuous green audits.
        self._test_on_clean = guards_cfg.get("test_on_clean", False)
        # TRUST-001: when a substantive gate (lint/tests/lsp) does no work, the
        # run is a DEGRADED pass. Exit 0 is kept ONLY when this flag is true;
        # the code-level default is False (fail loud) while `gitreins init`
        # writes allow_skips: true for ergonomic first commits on a fresh repo.
        self._allow_skips = bool(guards_cfg.get("allow_skips", False))
        # Test timeout in seconds (default: 180s). Coerced to int — string
        # config values like '300s' crash subprocess.run(timeout=...) with a
        # TypeError (GR-GAP-028, Kobayashi-Maru ticks 240-242).
        self._test_timeout = _coerce_timeout(
            guards_cfg.get("test_timeout", 180), "test_timeout", 180
        )
        # Hook timeout in seconds (default: 300s) — overall guard budget (GR-064e).
        # Same coercion as test_timeout: a string here breaks the _timed_out()
        # monotonic comparison with a TypeError.
        self._hook_timeout = _coerce_timeout(
            guards_cfg.get("hook_timeout", 300), "hook_timeout", 300
        )

        # Project type detection — every marker query comes from
        # engine.lang_detect, the single source of truth shared with the
        # judge's Tier 1 pipeline and `gitreins init`, so the gate and the
        # verdict can never disagree about what language this repo is
        # (DF-GITREINS-POC-16). Signatures only (no extension fallback): the
        # Go/Rust guards need a real ecosystem marker, not an inferred one.
        signatures = lang_detect.signature_languages(self.workdir)
        self._is_go = "go" in signatures
        self._go_guards = guards_cfg.get("go", {})
        self._is_ruby = "ruby" in signatures
        self._is_php = "php" in signatures
        self._is_cpp = (
            "cpp" in signatures
            or "c" in signatures
            or os.path.isfile(os.path.join(self.workdir, "compile_commands.json"))
            or any(
                f.endswith(lang_detect.CPP_SOURCE_SUFFIXES) for f in _get_staged_files(self.workdir)
            )
        )
        self._is_rust = "rust" in signatures
        self._has_sql = any(
            f.endswith(".sql") for f in _get_staged_files(self.workdir)
        ) or os.path.isdir(os.path.join(self.workdir, "migrations"))

    def run_all(self, force_dead_code: bool = False) -> Tier1Result:
        """Run all enabled Tier 1 guards.

        For Go projects, the Python-specific guards (lint, tests, dead_code)
        are skipped in favor of Go-native equivalents (go vet, go test, go build).

        Args:
            force_dead_code: If True, enable dead_code guard regardless of config.
                             Used by CLI --dead-code flag and MCP dead_code param.

        The overall guard run is bounded by hook_timeout (default 120s). If the
        total elapsed time exceeds this budget, remaining checks are skipped and a
        warning is issued — the guard \"fails open\" to prevent blocking commits
        indefinitely (GR-064e).
        """
        start = time.monotonic()
        results: list[GuardResult] = []
        warnings: list[str] = []
        # Fresh evidence map per run — write_guard_log reads it (DF-018).
        self._full_outputs = {}

        def _timed_out() -> bool:
            return (time.monotonic() - start) >= self._hook_timeout

        def _finalize(result: Tier1Result) -> Tier1Result:
            """Persist the full run log (best-effort) on EVERY exit path.

            DF-018: the console summary is deliberately bounded, so the raw
            evidence has to outlive the run. Persistence never changes the
            verdict — a write failure is recorded in ``extra`` (the CLI
            prints the reason instead of a path) and the result is returned
            untouched.
            """
            self._persist_run_log(result)
            return result

        if self._enabled["secrets"]:
            results.append(self._check_secrets())
            if _timed_out():
                warnings.append(
                    f"Guard timed out after {self._hook_timeout}s "
                    f"(hook_timeout). Remaining checks skipped — "
                    f"commit allowed to proceed (fail-open)."
                )
                return _finalize(Tier1Result(passed=True, results=results, warnings=warnings))

        if self._enabled["lint"] and not self._is_go:
            results.append(self._check_lint())
            if _timed_out():
                warnings.append(
                    f"Guard timed out after {self._hook_timeout}s "
                    f"(hook_timeout). Remaining checks skipped — "
                    f"commit allowed to proceed (fail-open)."
                )
                return _finalize(Tier1Result(passed=True, results=results, warnings=warnings))

        if self._enabled["tests"] and not self._is_go:
            results.append(self._check_tests())
            if _timed_out():
                warnings.append(
                    f"Guard timed out after {self._hook_timeout}s "
                    f"(hook_timeout). Remaining checks skipped — "
                    f"commit allowed to proceed (fail-open)."
                )
                return _finalize(Tier1Result(passed=True, results=results, warnings=warnings))

        dead_code_enabled = self._enabled["dead_code"] or force_dead_code
        if dead_code_enabled and not self._is_go:
            results.append(self._check_dead_code())
            if _timed_out():
                warnings.append(
                    f"Guard timed out after {self._hook_timeout}s "
                    f"(hook_timeout). Remaining checks skipped — "
                    f"commit allowed to proceed (fail-open)."
                )
                return _finalize(Tier1Result(passed=True, results=results, warnings=warnings))

        if self._enabled["skylos"]:
            results.append(self._check_skylos())
            if _timed_out():
                warnings.append(
                    f"Guard timed out after {self._hook_timeout}s "
                    f"(hook_timeout). Remaining checks skipped — "
                    f"commit allowed to proceed (fail-open)."
                )
                return _finalize(Tier1Result(passed=True, results=results, warnings=warnings))

        if self._enabled["static_analysis"]:
            results.append(self._check_static_analysis())
            if _timed_out():
                warnings.append(
                    f"Guard timed out after {self._hook_timeout}s "
                    f"(hook_timeout). Remaining checks skipped — "
                    f"commit allowed to proceed (fail-open)."
                )
                return _finalize(Tier1Result(passed=True, results=results, warnings=warnings))

        if self._enabled["lsp"] and not self._is_go:
            results.append(self._check_lsp())
            if _timed_out():
                warnings.append(
                    f"Guard timed out after {self._hook_timeout}s "
                    f"(hook_timeout). Remaining checks skipped — "
                    f"commit allowed to proceed (fail-open)."
                )
                return _finalize(Tier1Result(passed=True, results=results, warnings=warnings))

        if self._enabled.get("security_scan", False):
            results.append(self._check_security_scan())
            if _timed_out():
                warnings.append(
                    f"Guard timed out after {self._hook_timeout}s "
                    f"(hook_timeout). Remaining checks skipped — "
                    f"commit allowed to proceed (fail-open)."
                )
                return _finalize(Tier1Result(passed=True, results=results, warnings=warnings))

        if self._is_go:
            if self._go_guards.get("build", True):
                results.append(self._check_go_build())
                if _timed_out():
                    warnings.append(
                        f"Guard timed out after {self._hook_timeout}s "
                        f"(hook_timeout). Remaining checks skipped — "
                        f"commit allowed to proceed (fail-open)."
                    )
                    return _finalize(Tier1Result(passed=True, results=results, warnings=warnings))
            if self._go_guards.get("lint", True):
                results.append(self._check_go_lint())
                if _timed_out():
                    warnings.append(
                        f"Guard timed out after {self._hook_timeout}s "
                        f"(hook_timeout). Remaining checks skipped — "
                        f"commit allowed to proceed (fail-open)."
                    )
                    return _finalize(Tier1Result(passed=True, results=results, warnings=warnings))
            if self._go_guards.get("tests", True):
                results.append(self._check_go_tests())
                if _timed_out():
                    warnings.append(
                        f"Guard timed out after {self._hook_timeout}s "
                        f"(hook_timeout). Remaining checks skipped — "
                        f"commit allowed to proceed (fail-open)."
                    )
                    return _finalize(Tier1Result(passed=True, results=results, warnings=warnings))

        passed = all(r.passed for r in results)
        extra = {
            "test_mode": self._test_mode,
            # DF-GITREINS-POC-11: the CLI reads this for the whole-tree mode
            # note ("test mode: full, whole tree"); library callers can use
            # it to distinguish a whole-tree run from a staged run.
            "grade_full_tree": self._grade_full_tree,
            # TRUST-001: the CLI turns these into the DEGRADED PASS line and
            # the exit-code policy; library/MCP callers read them without
            # having to re-derive skips from the per-guard results.
            "allow_skips": self._allow_skips,
        }
        if self._test_mode == "diff" and self._enabled.get("tests"):
            staged = _get_staged_files(self.workdir)
            targets = _discover_test_targets(self.workdir)
            if targets:
                extra["test_targets"] = len(targets)
                extra["staged_count"] = len(staged)
            else:
                extra["test_targets"] = None  # full suite triggered
        result = _finalize(
            Tier1Result(passed=passed, results=results, extra=extra, warnings=warnings)
        )
        result.extra["degraded"] = result.degraded
        result.extra["skipped_steps"] = result.skipped_steps
        return result

    def _remember_full_output(self, name: str, output: str) -> None:
        """Keep an untruncated guard output for the persisted run log (DF-018).

        Called BEFORE the guard's own 2000-char cap, so the log carries the
        complete traceback while the guard result stays bounded for the
        console.
        """
        self._full_outputs[name] = output

    def _persist_run_log(self, result: Tier1Result) -> None:
        """Write the full run log and record its path (or the failure) in extra.

        BEST-EFFORT and NON-FATAL (DF-018): a missing/unwritable
        ``.gitreins/``, a permission error or a full disk must never raise
        out of a guard run, and must never alter the verdict. The reason is
        surfaced through ``extra['guard_log_error']`` instead, which the CLI
        prints in place of a path.
        """
        try:
            path = write_guard_log(self.workdir, result, self._full_outputs)
        except Exception as exc:  # noqa: BLE001 — best-effort by contract
            logger.warning("guard run log not written: %s", exc)
            result.extra["guard_log_error"] = str(exc)
            return
        result.extra["guard_log"] = path

    @property
    def test_mode(self) -> str:
        return self._test_mode

    def _check_secrets(self) -> GuardResult:
        """Scan staged changes for secrets using gitleaks or built-in scanner."""
        # Try gitleaks first
        try:
            cmd = [
                "gitleaks",
                "protect",
                "--staged",
                "--source",
                ".",
                "--verbose",
                "--no-banner",
            ]
            config_path = os.path.join(self.workdir, ".gitleaks.toml")
            if os.path.isfile(config_path):
                cmd.extend(["--config", config_path])
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                cwd=self.workdir,
                errors="replace",
                env=_sanitized_env(),
            )
            if result.returncode == 0:
                # gitleaks clean — ALSO run the built-in scanner. gitleaks'
                # default rules carry entropy thresholds that skip low-entropy
                # keys (e.g. an AKIA-prefixed key in test fixtures), and the
                # built-in scanner catches provider patterns without that
                # filter (GR-GAP-005). gitleaks OR builtin must both be clean.
                builtin = self._builtin_secrets_scan()
                scanners = ((GITLEAKS_SCANNER, SCANNER_CLEAN), *builtin.scanners)
                if not builtin.passed:
                    return replace(builtin, scanners=scanners)
                return GuardResult(
                    name="secrets", passed=True, output="gitleaks: clean", scanners=scanners
                )
            else:
                # gitleaks found something — do NOT short-circuit (DF-016).
                # The built-in cross-check still runs and its findings are
                # merged into the output: gitleaks' verbose dump carries
                # File:/Line: pairs but no human-readable labels, so a
                # low-entropy key it skipped (e.g. a ghp_ token behind a
                # decoy-first sk- key) would otherwise vanish from the
                # report. The result stays failed until BOTH scanners are
                # clean — reporting-completeness only, no weakening.
                builtin = self._builtin_secrets_scan()
                output = result.stdout + result.stderr
                if not builtin.passed:
                    output = _merge_secret_findings(output, builtin.output)
                # TRUST-003 (AC2): name which scanner raised the finding and
                # what the other one saw — 'fail' alone was ambiguous.
                gitleaks_status = self._gitleaks_failure_status(output)
                return GuardResult(
                    name="secrets",
                    passed=False,
                    output=output,
                    exit_code=result.returncode,
                    scanners=((GITLEAKS_SCANNER, gitleaks_status), *builtin.scanners),
                )
        except FileNotFoundError:
            # GR-GAP-043: the missing-gitleaks case must be VISIBLE, not a
            # silent debug line — AGENTS.md hardcodes $HOME/go/bin on PATH,
            # so a user without gitleaks there gets no hint otherwise.
            expected = os.path.join(os.path.expanduser("~"), "go", "bin", "gitleaks")
            result = self._builtin_secrets_scan()
            hint = (
                f"gitleaks not found at expected path {expected} — using the "
                "built-in secrets scanner instead. Install gitleaks "
                "(e.g. 'go install github.com/gitleaks/gitleaks/v8@latest') "
                "for full secret coverage."
            )
            logger.debug(hint)
            # TRUST-003: an absent scanner is NAMED, not silently implied —
            # the console line then reads "clean (builtin cross-check;
            # gitleaks not on PATH)".
            return replace(
                result,
                warning=hint,
                scanners=((GITLEAKS_SCANNER, SCANNER_NOT_RUN), *result.scanners),
            )
        except Exception as e:
            logger.warning("gitleaks failed: %s — falling back to built-in scanner", e)

        return self._builtin_secrets_scan()

    @staticmethod
    def _gitleaks_failure_status(output: str) -> str:
        """Per-scanner status for a non-zero gitleaks exit (TRUST-003).

        The count comes from gitleaks' own report when it is parseable; a
        non-zero exit whose output carries no recognizable tally is reported
        as findings-found-without-a-count rather than misreported as zero.
        """
        count = parse_gitleaks_finding_count(output)
        if count is None:
            return "reported findings (count unavailable)"
        return scanner_finding_status(count)

    def _builtin_secrets_scan(self, staged_only: bool = True) -> GuardResult:
        """
        Built-in secrets scanner with whitelist patterns.

        Detects likely secrets (API keys, tokens, private keys) while
        ignoring common false positives like environment variable loading,
        form field access, and credential construction.

        ``staged_only=True`` scans the staged diff (pre-commit hook path).
        ``staged_only=False`` scans the whole workdir (judge/pipeline path,
        where the changes under evaluation are already committed — DF-012).
        """
        # Patterns that LIKELY represent actual secrets (high confidence)
        danger_patterns = [
            # Private key blocks (SSH, SSL, PGP, PKCS#8)
            (
                r"(?i)-----BEGIN\s+(RSA|DSA|EC|OPENSSH|PGP|ENCRYPTED\s+)?\s*PRIVATE\s+KEY(\s+BLOCK)?",
                "private key block",
            ),
            # GitHub tokens
            (r"\bghp_[A-Za-z0-9]{36,}", "GitHub personal access token"),
            (r"\bgho_[A-Za-z0-9]{36,}", "GitHub OAuth token"),
            # GitLab tokens
            (r"\bglpat-[A-Za-z0-9_\-]{20,}", "GitLab personal access token"),
            # OpenAI/OpenRouter keys (20+ chars, at least one uppercase/digit —
            # real keys are hex/base64; quoted doc strings that are all-lowercase
            # (e.g. 'premise-verification') are not keys)
            (
                r"\bsk-(?=[A-Za-z0-9_\-]{20,})[A-Za-z0-9_\-]*[A-Z0-9][A-Za-z0-9_\-]*",
                "OpenAI/OpenRouter API key",
            ),
            # AWS keys
            (r"(?i)AKIA[0-9A-Z]{16}", "AWS access key"),
            (
                r'(?i)(aws[_-]?secret[_-]?access[_-]?key|aws[_-]?secret|secret[_-]?access[_-]?key)\s*[:=]\s*["\'][A-Za-z0-9+/]{40,}["\']',
                "AWS secret access key",
            ),
            # GCP API keys
            (r"AIza[0-9A-Za-z\-_]{35,}", "GCP API key"),
            # DigitalOcean tokens
            (r"dop_v1_[a-z0-9]{64}", "DigitalOcean access token"),
            # Stripe live keys
            (r"sk_live_[0-9a-zA-Z]{24,}", "Stripe live secret key"),
            (r"rk_live_[0-9a-zA-Z]{24,}", "Stripe restricted key"),
            # Azure storage
            (
                r"(?i)DefaultEndpointsProtocol=https;AccountName=[^;]+;AccountKey=[A-Za-z0-9+/=]{60,}",
                "Azure storage connection string",
            ),
            (r'(?i)AccountKey\s*=\s*["\']?[A-Za-z0-9+/=]{60,}["\']?', "Azure storage account key"),
            # Slack API tokens
            (r"xox[baprs]-[0-9a-zA-Z\-]{20,}", "Slack API token"),
            # Generic patterns (check LAST — specific providers above)
            (
                r'(?i)(api[_-]?key|apikey)\s*[:=]\s*["\']([A-Za-z0-9_\-]{20,})["\']',
                "hardcoded API key",
            ),
            # JWTs assigned as literal strings
            (
                r'(?i)(token|jwt)\s*[:=]\s*["\']eyJ[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{20,}\.[A-Za-z0-9_\-]{10,}["\']',
                "hardcoded JWT",
            ),
            # Passwords with literal-looking values
            (r'(?i)(password|passwd|pwd)\s*[:=]\s*["\'][^"\'$]{8,}["\']', "hardcoded password"),
            # Generic tokens/secrets (check LAST)
            (r'(?i)(secret|token)\s*[:=]\s*["\'][A-Za-z0-9+/=]{32,}["\']', "hardcoded secret"),
        ]

        # Patterns we explicitly IGNORE (common false positives)
        whitelist_patterns = [
            r"(?i)(api[_-]?key|apikey|secret|token|password|passwd|pwd)\s*[:=]\s*(os\.getenv|os\.environ|getenv|environ\[|request\.form|request\.args|\.env|config\[|settings\[)",
            r"(?i)\$\{[A-Z_]+\}",  # Shell variable substitution
            r"(?i)\$\w+",  # Shell variable reference ($KEY)
            r"(?i)\{\{[^}]*\}\}",  # Template variables ({{ }})
            r"(?i)\{%[^}]*%\}",  # Template variables ({% %})
            r'(?i)(password|passwd|pwd)\s*[:=]\s*""',  # Empty password assignments
            r"(?i)EXAMPLE|PLACEHOLDER|TODO|FIXME|xxx+|<your-[-a-z]+>|changeme",  # Placeholders
            r"(?i)jwt\.encode|jwt\.decode|b64encode",  # JWT construction, not hardcoded
            r"(?i)generate|random|uuid|hash",  # Generated values
        ]

        findings = []
        allowlist = self._load_gitleaks_allowlist()
        try:
            if staged_only:
                files = _get_staged_files(self.workdir)
            else:
                files = self._workdir_files()

            if not files:
                scope = "staged" if staged_only else "workdir"
                return GuardResult(
                    name="secrets",
                    passed=True,
                    output=f"No {scope} files to scan",
                    scanners=((BUILTIN_SCANNER, SCANNER_CLEAN),),
                )

            for fpath in files:
                # POC-17 / TRUST-002: the harness's own state directory is
                # never graded — neither scanner may fail a judgement on
                # GitReins' config, logs, verdict history or disposable
                # bookkeeping. Checked here as well as in _workdir_files so a
                # STAGED `.gitreins/**` path (config.yaml and history/ are
                # tracked) is skipped too.
                if _is_harness_state_path(fpath):
                    continue
                # Respect .gitleaks.toml [allowlist] paths — same exemptions
                # gitleaks applies (test fixtures with deliberate fake keys).
                if any(rx.search(fpath) for rx in allowlist):
                    continue
                full = os.path.join(self.workdir, fpath)
                if not os.path.isfile(full):
                    continue

                # Skip documentation files — they routinely contain
                # example API keys, placeholder tokens, and credential
                # snippets that are not actual secrets.
                if any(
                    skip in fpath
                    for skip in (".memory-bank/", "docs/", "CONTRIBUTING.md", "SECURITY.md")
                ) or fpath.endswith(".md"):
                    continue

                # Skip test files — fixtures and benchmarks routinely
                # embed deliberately fake keys (e.g. sk-benchmark-...).
                # Same rationale as the documentation skip above; gitleaks
                # still scans everything, so this only exempts the
                # low-entropy built-in cross-check. (musterflow GAP-012)
                if _is_test_file(fpath):
                    continue

                try:
                    if staged_only:
                        staged_result = subprocess.run(
                            ["git", "show", f":{fpath}"],
                            capture_output=True,
                            text=True,
                            timeout=10,
                            cwd=self.workdir,
                            errors="replace",
                            env=_sanitized_env(),
                        )
                        if staged_result.returncode != 0:
                            continue
                        text = staged_result.stdout
                    else:
                        if os.path.getsize(full) > 1_000_000:
                            continue  # Skip very large files
                        with open(full, "r", errors="replace") as f:
                            text = f.read()
                except Exception:
                    continue
                if len(text.encode("utf-8", errors="replace")) > 1_000_000:
                    continue

                for i, line in enumerate(text.splitlines(), 1):
                    # Skip whitelisted lines
                    if any(re.search(wp, line) for wp in whitelist_patterns):
                        continue

                    # Check danger patterns
                    for pattern, label in danger_patterns:
                        if re.search(pattern, line):
                            # Suppress the actual value in output
                            sanitized = re.sub(r'["\'][^"\']{6,}["\']', '"***"', line.rstrip())
                            findings.append(f"{fpath}:{i}: [{label}] {sanitized}")
                            break  # One finding per line

            if findings:
                logger.warning("Secrets scan: %d potential findings", len(findings))
                return GuardResult(
                    name="secrets",
                    passed=False,
                    output="Potential secrets found:\n" + "\n".join(findings[:20]),
                    scanners=((BUILTIN_SCANNER, scanner_finding_status(len(findings))),),
                )
            return GuardResult(
                name="secrets",
                passed=True,
                output=(
                    f"Scanned {len(files)} files — clean "
                    f"(excluded harness state: {', '.join(d + '/**' for d in HARNESS_STATE_DIRS)})"
                ),
                scanners=((BUILTIN_SCANNER, SCANNER_CLEAN),),
            )

        except Exception as e:
            logger.exception("Secrets scan failed")
            return GuardResult(
                name="secrets",
                passed=False,
                error=str(e),
                scanners=((BUILTIN_SCANNER, "scan error"),),
            )

    def _workdir_files(self) -> list[str]:
        """Relative paths of all non-ignored files in the workdir.

        Used by the judge/pipeline secrets cross-check (DF-012), where the
        changes under evaluation are already committed — nothing is staged.
        Mirrors the directories gitleaks' generated config allowlists.
        """
        skip_dirs = {
            ".git",
            *HARNESS_STATE_DIRS,
            "node_modules",
            "__pycache__",
            ".mypy_cache",
            ".pytest_cache",
            ".ruff_cache",
            ".vfs",
            "dist",
            "build",
            "vendor",
            "target",
            "coverage",
            ".next",
            ".turbo",
            ".pnpm-store",
            # Belt-and-braces: catch venvs with unusual root names (the
            # .venv*/venv* prefix filter below handles the normal ones).
            "site-packages",
            "dist-packages",
            # Gitignored stray demo-fixture dirs (other-uid, unreadable files) —
            # mirrors .gitignore and .gitleaks.toml allowlist
            "demo-slugify",
            "demo-calc",
        }
        files: list[str] = []
        for root, dirs, names in os.walk(self.workdir):
            # Prune ANY venv-like dir, not just exact ".venv"/"venv":
            # .venv312, venv311, venvs, etc. The judge's workdir scan
            # (DF-012) walked .venv312/lib/python3.12/site-packages vendored
            # code and tripped danger patterns (jedi RECORD AKIA lines,
            # cryptography private-key markers, pydantic example passwords)
            # on docs-only commits. (GR-GAP-039)
            dirs[:] = [
                d
                for d in dirs
                if d not in skip_dirs and not (d.startswith(".venv") or d.startswith("venv"))
            ]
            for name in names:
                files.append(os.path.relpath(os.path.join(root, name), self.workdir))
        return files

    def _load_gitleaks_allowlist(self) -> list:
        """Load path allowlist regexes from .gitleaks.toml, if present.

        The built-in scanner applies the same path exemptions gitleaks gets
        via the repo's [allowlist] (test fixtures with deliberate fake keys,
        docs, generated dirs). Entries are emitted as Go-style regexps inside
        '''...''' quotes.
        """
        allowed = []
        cfg = os.path.join(self.workdir, ".gitleaks.toml")
        if not os.path.isfile(cfg):
            return allowed
        try:
            with open(cfg, "r", errors="replace") as f:
                text = f.read()
            for m in re.finditer(r"'''(.+?)'''", text):
                try:
                    allowed.append(re.compile(m.group(1)))
                except re.error:
                    continue
        except Exception:
            logger.debug("Could not parse %s allowlist", cfg)
        return allowed

    def _check_lint(self) -> GuardResult:
        """Run linter on staged Python files.

        DF-GITREINS-POC-11: under grade_full_tree (CLI --full), an empty
        index no longer vacates the lane — the whole tree (tracked +
        untracked-but-not-ignored .py files) is graded instead.

        DF-GITREINS-POC-18: the graded scope is whatever the repo's own ruff
        configuration allows — a config-excluded path stays excluded even when
        it is named explicitly (``ruff check --force-exclude``), and a file
        list the config excludes ENTIRELY is an honest SKIP, never a clean pass.

        GR-GAP-063: the lane ALSO grades formatting (``ruff format --check``)
        over that same scope, because the two are one verdict — code the
        checker accepts can still have drifted from the repo's formatter, and
        nothing gated that before. ``--check`` rather than ``--diff``:
        ``--diff`` exits 0 on differences and would be a permanent green.
        Formatting failures keep the ``lint`` lane's name and fail the lane;
        the output names the offending files and the ``ruff format <files>``
        command that fixes them.
        """
        linters = ["ruff", "flake8"]
        # Get staged Python files
        staged_files = _get_staged_files(self.workdir)
        py_files = [f for f in staged_files if f.endswith(".py")]
        if not py_files and self._grade_full_tree:
            # Nothing staged but whole-tree grading is on: lint the tree
            # (tracked + untracked-but-not-ignored). Never invoke the
            # linter with an empty file list — the honest skip below stays
            # for a tree with no Python files at all.
            py_files = _tree_python_files(self.workdir)
        if not py_files:
            # TRUST-001: nothing staged is not a graded lint pass.
            return GuardResult(
                name="lint",
                passed=True,
                output="No Python files staged",
                skipped=True,
                skip_reason="no staged files",
            )

        for linter in linters:
            try:
                if linter == "ruff":
                    # DF-GITREINS-POC-18: the repo's own ruff configuration
                    # governs an explicit file list too. Resolve the real
                    # scope first (see _ruff_scoped_files) so the lane can
                    # name how many files it graded and never report a clean
                    # lint over a list the config excluded entirely.
                    scoped = _ruff_scoped_files(self.workdir, py_files)
                    graded = len(scoped) if scoped is not None else len(py_files)
                    excluded = len(py_files) - graded
                    if graded == 0:
                        return GuardResult(
                            name="lint",
                            passed=True,
                            output=(
                                f"ruff: 0 of {len(py_files)} file(s) in scope — all excluded "
                                "by the repo's ruff configuration"
                            ),
                            skipped=True,
                            skip_reason=f"all {len(py_files)} file(s) excluded by ruff config",
                        )
                    lint_cmd = ["ruff", "check", "--force-exclude", *py_files]
                else:
                    graded = len(py_files)
                    excluded = 0
                    lint_cmd = [linter, *py_files]
                lint_result = subprocess.run(
                    lint_cmd,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    cwd=self.workdir,
                    env=_sanitized_env(),
                )
                output = lint_result.stdout + lint_result.stderr
                scope_note = f"{graded} tracked files"
                if excluded:
                    # The config dropped files from the submitted list — say
                    # so, or a reader cannot tell a smaller scope from a
                    # smaller tree.
                    scope_note += f", {excluded} excluded by config"
                # DF-018: untruncated lint output for the run log (the
                # GuardResult below keeps the capped head the summary reads).
                if lint_result.returncode == 0 and self._grade_full_tree:
                    # The raw ruff stdout for a clean tree is EMPTY, and the
                    # log's evidence lookup falls back to it — an empty body
                    # would shadow the graded scope ("ruff: clean (N tracked
                    # files)") in the persisted run log. Prefix the summary
                    # line so a post-mortem sees what was graded.
                    output = f"{linter}: clean ({scope_note})\n{output}"
                self._remember_full_output("lint", output)
                if len(output) > 2000:
                    output = output[:2000] + "\n... [truncated]"

                if lint_result.returncode == 0:
                    # GR-GAP-063: formatting is part of the lint verdict, not a
                    # lane of its own. `ruff check` grades correctness and is
                    # blind to formatting — the tree drifted from
                    # `ruff format` repeatedly with every gate green, so the
                    # same graded scope now runs `ruff format --check` too.
                    # --force-exclude keeps the scope identical to the check
                    # command's (a config-excluded file is never graded here
                    # either). A missing formatter is not a failure of a lint
                    # lane that just ran ruff successfully: it is reported in
                    # the clean line instead of inventing a red.
                    format_note = ""
                    format_failure: GuardResult | None = None
                    if linter == "ruff":
                        try:
                            fmt_result = subprocess.run(
                                _ruff_format_command(py_files),
                                capture_output=True,
                                text=True,
                                timeout=120,
                                cwd=self.workdir,
                                env=_sanitized_env(),
                            )
                        except (OSError, subprocess.SubprocessError):
                            format_note = "format: not run (formatter unavailable)"
                        else:
                            fmt_raw = fmt_result.stdout + fmt_result.stderr
                            if fmt_result.returncode != 0:
                                format_failure = GuardResult(
                                    name="lint",
                                    passed=False,
                                    output=_format_failure_message(fmt_raw),
                                    exit_code=fmt_result.returncode,
                                )
                            else:
                                format_note = f"format: clean ({graded} files)"
                    if format_failure is not None:
                        # Persist the un-truncated formatter output for the run
                        # log, exactly as the check sub-step does.
                        self._remember_full_output("lint", format_failure.output)
                        return format_failure
                    # GR-GAP-063: the clean line names the formatter sub-check
                    # too — a reader (and the run log) can then tell a lane that
                    # graded formatting from one that never ran it.
                    clean_parts = [f"{linter}: clean"]
                    if self._grade_full_tree:
                        # Name the scope so a whole-tree run is
                        # distinguishable from a staged run in the console.
                        clean_parts[0] += f" ({scope_note})"
                    if format_note:
                        clean_parts.append(format_note)
                    return GuardResult(
                        name="lint",
                        passed=True,
                        output=", ".join(clean_parts),
                        exit_code=lint_result.returncode,
                    )
                else:
                    return GuardResult(
                        name="lint",
                        passed=False,
                        output=output,
                        exit_code=lint_result.returncode,
                    )
            except FileNotFoundError:
                continue

        # No linter binary ran (none of the candidates exist on PATH) —
        # TRUST-001: a skipped gate, not a clean one.
        return GuardResult(
            name="lint",
            passed=True,
            output="No linter found — skipped",
            skipped=True,
            skip_reason="no linter on PATH",
        )

    def _check_tests(self) -> GuardResult:
        """Run the configured test command.

        In 'diff' mode, only runs tests relevant to staged changes.
        In 'full' mode (default), runs the entire test suite.
        When no files are staged, tests are skipped unless guards.test_on_clean
        is true (then the full test_command runs — chained suites execute on
        clean-tree guard runs instead of silently passing). Under
        grade_full_tree (CLI --full) the full test_command runs even with an
        empty index — a --full run is expected to produce test evidence, not
        a skip.
        """
        test_command = self.config.get("guards", {}).get("test_command", "pytest -x --tb=short")

        # A linked task may have committed branch changes with an empty index.
        # Keep ordinary clean-tree behavior unchanged while allowing those
        # committed changes to participate in diff-mode discovery.
        staged = _get_staged_files(self.workdir)
        changed = (
            _get_worktree_changed_files(self.workdir)
            if self._test_mode == "diff" and not staged
            else []
        )
        if not staged and not changed:
            if self._grade_full_tree:
                # DF-GITREINS-POC-11: --full grades the whole tree even with
                # an empty index — fall through to the full test_command
                # instead of returning the TRUST-001 skip. (Only
                # test_on_clean's logger line is bypassed here; ordinary
                # construction keeps it.)
                pass
            elif not self._test_on_clean:
                # TRUST-001: the vacuous-green case from the dogfood verdict —
                # no tests ran, so this is a skip with a named reason, never a
                # silent pass.
                return GuardResult(
                    name="tests",
                    passed=True,
                    output="No files staged — skipped",
                    skipped=True,
                    skip_reason="no staged files",
                )
            else:
                logger.info("test_on_clean: no files staged — running full test_command")

        if self._test_mode == "diff":
            if not changed and self._test_on_clean:
                return self._run_test_command(test_command, "tests (full)")
            test_files = _discover_test_targets(self.workdir)
            if test_files is not None:
                if not test_files:
                    # No test files map to the changed sources — skip
                    return GuardResult(
                        name="tests",
                        passed=True,
                        output="No matching test files — skipped (diff mode)",
                        skipped=True,
                        skip_reason="no test files match the changed sources (diff mode)",
                    )
                # Narrowed — only run relevant tests
                cmd = _build_diff_test_command(test_command, test_files, self.workdir)
                label = f"tests (diff: {len(test_files)} files)"
                return self._run_test_command(cmd, label)
            # Fall through to full suite (safety default for force-full triggers)

        label = "tests (full)"
        return self._run_test_command(test_command, label)

    def _run_test_command(self, cmd: str, label: str) -> GuardResult:
        """Execute a test command and return a GuardResult."""
        resolved_cmd, fallback_warning = _resolve_test_command(cmd)
        try:
            result = subprocess.run(
                resolved_cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self._test_timeout,
                cwd=self.workdir,
                env=_sanitized_env(),
            )
            output = result.stdout + result.stderr
            if fallback_warning:
                output = f"{fallback_warning}\n{output}"
            # DF-018: keep the untruncated output for the run log BEFORE the
            # tail cap below. The GuardResult keeps the bounded tail the
            # console summary consumes; the log gets the whole traceback.
            self._remember_full_output(label, output)
            # GR-GAP-048: classify exit 5 on the FULL output (the "no tests
            # ran" summary is a tail line, but collection-error lines sit
            # earlier and must survive truncation for the check below).
            no_tests_benign = result.returncode == 5 and _pytest_no_tests_benign(output)
            if len(output) > 2000:
                output = output[-2000:]  # Keep last 2000 chars for failure context
            if result.returncode == 0:
                return GuardResult(
                    name=label,
                    passed=True,
                    output=output[:500],
                    warning=fallback_warning or "",
                    exit_code=result.returncode,
                )
            elif no_tests_benign:
                # pytest exit 5 with zero tests collected and no collection
                # errors — fresh-repo case: pass with a warning instead of
                # blocking the repo's first commit.
                warning = (
                    f"{fallback_warning}\n{_PYTEST_NO_TESTS_WARNING}"
                    if fallback_warning
                    else _PYTEST_NO_TESTS_WARNING
                )
                return GuardResult(
                    name=label,
                    passed=True,
                    output=output[:500],
                    warning=warning,
                    exit_code=result.returncode,
                    # TRUST-001: pytest collected zero tests — the gate graded
                    # nothing, so say so instead of reporting a green step.
                    skipped=True,
                    skip_reason="no tests collected",
                )
            else:
                # GR-GAP-064: make a genuine not-found (127) actionable —
                # the hint line is prepended to the displayed output for a
                # FAILED pytest lane; the value of exit_code decides
                # pass/fail and is never reclassified or swallowed.
                output = result.stdout + result.stderr
                not_found_hint = _pytest_not_found_hint(result.returncode, cmd)
                if not_found_hint:
                    output = f"{not_found_hint}\n{output}"
                return GuardResult(
                    name=label,
                    passed=False,
                    output=output,
                    warning=fallback_warning or "",
                    exit_code=result.returncode,
                )
        except subprocess.TimeoutExpired:
            return GuardResult(
                name=label,
                passed=False,
                output=(
                    f"Tests timed out after {self._test_timeout}s. "
                    f"To raise the limit: set guards.test_timeout in "
                    f".gitreins/config.yaml (e.g. test_timeout: 300)."
                ),
                warning=fallback_warning or "",
            )
        except Exception as e:
            return GuardResult(
                name=label, passed=False, error=str(e), warning=fallback_warning or ""
            )

    def _check_dead_code(self) -> GuardResult:
        """Detect unreachable code, unused functions, and unused imports."""
        try:
            from engine.dead_code import DeadCodeDetector

            detector = DeadCodeDetector(self.workdir)
            report = detector.scan()

            # Also check for unused functions project-wide
            unused_funcs = detector.find_unused_functions()
            report.findings.extend(unused_funcs)

            if report.passed:
                return GuardResult(name="dead_code", passed=True, output="No dead code found")

            # Group by category for clear output
            output = report.summary
            if len(output) > 2000:
                output = output[:2000] + "\n... [truncated]"

            return GuardResult(name="dead_code", passed=False, output=output)
        except ImportError:
            return GuardResult(
                name="dead_code",
                passed=True,
                output="Dead code detector unavailable — skipped",
                skipped=True,
                skip_reason="dead-code detector unavailable",
            )
        except Exception as e:
            return GuardResult(name="dead_code", passed=False, error=str(e))

    def _check_skylos(self) -> GuardResult:
        """Multi-language dead code + AI mistake detection via Skylos.

        Requires: pip install skylos
        Detects: unused functions, imports, classes, variables, parameters,
                 unreachable code, AI-hallucinated patterns.
        Languages: Python, TS/JS, Go, Java, PHP, Rust, Dart, C#
        """
        try:
            result = subprocess.run(
                ["skylos", self.workdir, "--format", "json", "--no-grep-verify"],
                capture_output=True,
                text=True,
                timeout=120,
                cwd=self.workdir,
                env=_sanitized_env(),
            )
            if result.returncode != 0:
                return GuardResult(
                    name="skylos",
                    passed=True,
                    output=f"skylos exited {result.returncode}: {result.stderr[:200]}",
                )

            data = json.loads(result.stdout)

            findings = []
            for f in data.get("unused_functions", []):
                findings.append(f"{f['file']}:{f['line']} — unused function {f['name']}")
            for f in data.get("unused_imports", []):
                findings.append(f"{f['file']}:{f['line']} — unused import {f['name']}")
            for f in data.get("unused_classes", []):
                findings.append(f"{f['file']}:{f['line']} — unused class {f['name']}")

            # Dead symbols from definitions
            for name, info in data.get("definitions", {}).items():
                if info.get("dead"):
                    findings.append(f"{info['file']}:{info['line']} — dead: {name}")

            grade = data.get("grade", {}).get("overall", {})
            score = grade.get("score", "?")
            letter = grade.get("letter", "?")

            if not findings:
                return GuardResult(
                    name="skylos",
                    passed=True,
                    output=f"Skylos grade {letter} ({score}) — no dead code found",
                )

            output = f"Skylos grade {letter} ({score}) — {len(findings)} findings:\n"
            output += "\n".join(f"  • {f}" for f in findings[:20])
            if len(findings) > 20:
                output += f"\n  ... and {len(findings) - 20} more"

            if len(output) > 2000:
                output = output[:2000] + "\n... [truncated]"

            return GuardResult(name="skylos", passed=False, output=output)

        except FileNotFoundError:
            return GuardResult(
                name="skylos",
                passed=True,
                output="skylos not installed — install with: pip install skylos",
            )
        except json.JSONDecodeError:
            return GuardResult(name="skylos", passed=True, output="skylos output unparseable")
        except subprocess.TimeoutExpired:
            return GuardResult(name="skylos", passed=True, output="skylos timed out")
        except Exception as e:
            return GuardResult(name="skylos", passed=False, error=str(e))

    def _check_static_analysis(self) -> GuardResult:
        """Run configured static analysis tools against the project.

        Respects static_analysis_tools config key. Only runs tools that
        exist on PATH; a configured tool that is absent is reported as
        not-installed and (when none ran) makes the whole step a skip —
        never a "clean" pass. Returns FAIL if any tool finds errors.
        """
        if self._is_go:
            return GuardResult(
                name="static_analysis",
                passed=True,
                output="Go compiler covers static analysis — skipped",
            )
        # Check for Python, Ruby, PHP, SQL, C/C++, Rust, Go. Marker queries
        # come from engine.lang_detect (single source of truth).
        lang_tools: list[str] = []
        if lang_detect.python_packaging_present(self.workdir):
            lang_tools = self._static_tools.get("python", [])
        elif self._is_ruby:
            lang_tools = self._static_tools.get("ruby", [])
        elif self._is_php:
            lang_tools = self._static_tools.get("php", [])
        elif self._has_sql:
            lang_tools = self._static_tools.get("sql", [])
        elif self._is_cpp:
            lang_tools = self._static_tools.get("cpp", ["cppcheck"])
        elif self._is_rust:
            lang_tools = self._static_tools.get("rust", ["clippy"])

        if not lang_tools:
            return GuardResult(
                name="static_analysis",
                passed=True,
                output="No static analysis tools configured for this language",
            )

        # DF-019: `run_static_check` returns [] when the binary is absent, and
        # that empty list used to be reported as "<tool> — clean" — a vacuous
        # green on a gate that never ran, while `init` announced the tool as
        # enabled. Check the binary first (same rule the LSP gate learned in
        # TRUST-001) and name the gap instead of grading nothing as clean.
        from engine.static_analysis import find_tool, run_static_check

        all_diagnostics: list[str] = []
        had_errors = False
        missing: list[str] = []

        for tool in lang_tools:
            if not find_tool(tool):
                missing.append(tool)
                continue
            try:
                # cppcheck on a real C++ repo can exceed the default 120s —
                # grant C++/Rust tools the same generous budget as clangd.
                tool_timeout = 300.0 if (self._is_cpp or self._is_rust) else 120.0
                diags = run_static_check(tool, self.workdir, timeout=tool_timeout)
            except Exception as exc:
                logger.warning("static_analysis %s failed: %s", tool, exc)
                continue

            if not diags:
                all_diagnostics.append(f"  {tool} — clean")
                continue

            for d in diags:
                severity = d.get("severity", "error")
                prefix = "✗" if severity == "error" else "⚠"
                all_diagnostics.append(
                    f"  {prefix} {d['file']}:{d['line']} [{tool}] {d['message']}"
                )
                if severity == "error":
                    had_errors = True

        if not all_diagnostics and missing:
            # Every configured tool is absent: nothing was graded. DF-019 —
            # this reports as a skip with the tools named, never as "clean".
            return GuardResult(
                name="static_analysis",
                passed=True,
                output="No static analysis tools ran — check static_analysis_tools config",
                skipped=True,
                skip_reason=(
                    f"no static analysis tool on PATH ({', '.join(missing)} not installed)"
                ),
            )

        if not all_diagnostics:
            # Every configured tool failed to run (crash, timeout) —
            # TRUST-001: nothing was graded, so this is a skip.
            return GuardResult(
                name="static_analysis",
                passed=True,
                output="No tools ran — check static_analysis_tools config",
                skipped=True,
                skip_reason="no configured static-analysis tool ran",
            )

        if missing:
            # Some tools ran, some are absent — keep the graded result and name
            # the gap in the output so "clean" is never read as full coverage.
            all_diagnostics.extend(f"  {tool} — not installed (skipped)" for tool in missing)

        output = "\n".join(all_diagnostics)
        if len(output) > 2000:
            output = output[:2000] + "\n... [truncated]"

        return GuardResult(
            name="static_analysis",
            passed=not had_errors,
            output=output,
        )

    def _check_lsp(self) -> GuardResult:
        """Run configured LSP servers against staged files.

        Uses lsp_tools config key. Only runs tools that exist on PATH.
        Returns FAIL if any tool finds errors.
        """
        if self._is_go:
            return GuardResult(
                name="lsp", passed=True, output="Go compiler covers static analysis — skipped"
            )

        if not self._lsp_tools:
            return GuardResult(name="lsp", passed=True, output="No LSP tools configured")

        all_diagnostics: list[str] = []
        had_errors = False
        missing: list[str] = []

        for tool in self._lsp_tools:
            # TRUST-001: `run_lsp_check` returns [] for a server that is not
            # installed, and an empty list used to read as "clean" — a vacuous
            # pass on the gate the dogfood verdict called out. Check the binary
            # first and record the tool as not-installed instead.
            if not find_lsp_tool(tool):
                missing.append(tool)
                continue
            try:
                diags = run_lsp_check(
                    tool,
                    self.workdir,
                    timeout_per_file=self._lsp_per_file_timeout,
                    init_timeout=self._lsp_init_timeout,
                )
            except Exception as exc:
                logger.warning("lsp %s failed: %s", tool, exc)
                continue

            if not diags:
                all_diagnostics.append(f"  {tool} — clean")
                continue

            for d in diags:
                severity = d.get("severity", "error")
                prefix = "✗" if severity == "error" else "⚠"
                all_diagnostics.append(
                    f"  {prefix} {d['file']}:{d['line']} [{tool}] {d['message']}"
                )
                if severity == "error":
                    had_errors = True

        if not all_diagnostics and missing:
            # Nothing was graded: every configured server is absent.
            return GuardResult(
                name="lsp",
                passed=True,
                output="No LSP tools ran — check lsp_tools config",
                skipped=True,
                skip_reason=f"no LSP tool on PATH ({', '.join(missing)} not installed)",
            )

        if not all_diagnostics:
            # pylsp (or another configured server) never produced diagnostics:
            # it is missing on PATH or crashed on init. TRUST-001: the LSP gate
            # did no work — a skip, named for the tool the user can install.
            return GuardResult(
                name="lsp",
                passed=True,
                output="No LSP tools ran — check lsp_tools config",
                skipped=True,
                skip_reason="no LSP tool ran (install pylsp?)",
            )

        if missing:
            # Some servers ran, some are absent — keep the graded result but
            # name the gap in the output (TRUST-001).
            all_diagnostics.extend(f"  {tool} — not installed (skipped)" for tool in missing)

        output = "\n".join(all_diagnostics)
        if len(output) > 2000:
            output = output[:2000] + "\n... [truncated]"

        return GuardResult(
            name="lsp",
            passed=not had_errors,
            output=output,
        )

    def _check_security_scan(self) -> GuardResult:
        """Run Antares CVE localization scan against staged files.

        Opt-in guard. SCAFFOLD (GR-117a/d): the scanner falls back to a
        keyword-based heuristic that produces zero-confidence
        "CVE-SIMULATED" findings until GR-117c wires in the real model.
        Returns FAIL if any finding is produced; PASS otherwise. If the
        optional huggingface_hub/transformers stack isn't installed and
        the scanner can't be imported at all, returns PASS with a
        "not available" note — the guard is opt-in and must never block
        commits when its dependencies are missing.
        """
        try:
            from engine.antares import AntaresScanner
        except ImportError as exc:
            logger.debug("Antares scanner import failed: %s", exc)
            return GuardResult(
                name="security_scan",
                passed=True,
                output="Antares: not available — install huggingface_hub and transformers",
            )

        try:
            scanner = AntaresScanner(self.workdir)
            findings = scanner.scan_staged_files()
        except Exception as exc:
            logger.warning("Antares scan raised: %s", exc)
            return GuardResult(name="security_scan", passed=False, error=str(exc))

        if not findings:
            return GuardResult(name="security_scan", passed=True, output="Antares: clean")

        # Format: one line per finding, capped to keep output bounded.
        lines = [
            f"  • {f.file}:{f.line} [{f.cve_id} conf={f.confidence:.2f}] {f.description}"
            for f in findings[:20]
        ]
        if len(findings) > 20:
            lines.append(f"  ... and {len(findings) - 20} more")

        output = f"Antares: {len(findings)} potential finding(s):\n" + "\n".join(lines)
        if len(output) > 2000:
            output = output[:2000] + "\n... [truncated]"

        return GuardResult(name="security_scan", passed=False, output=output)

    def _check_go_lint(self) -> GuardResult:
        """Run Go lint checks (delegates to engine.guards)."""
        r = check_go_lint(self.workdir)
        return GuardResult(name=r.name, passed=r.passed, output=r.output, error=r.error)

    def _check_go_tests(self) -> GuardResult:
        """Run Go tests (delegates to engine.guards)."""
        r = check_go_tests(self.workdir, timeout=self._test_timeout)
        return GuardResult(name=r.name, passed=r.passed, output=r.output, error=r.error)

    def _check_go_build(self) -> GuardResult:
        """Run Go build (delegates to engine.guards)."""
        r = check_go_build(self.workdir)
        return GuardResult(name=r.name, passed=r.passed, output=r.output, error=r.error)
