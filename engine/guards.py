"""Go-specific guard checks for GitReins."""

import logging
import os
import re
import subprocess
from dataclasses import dataclass

from engine import command_hygiene
from engine import scanner_nice
from engine.data_protection import DataProtectionPolicy
from engine.types import DataProtectionResult

logger = logging.getLogger("gitreins.guards.go")

# ── Data-protection lane (GR-146) ──────────────────────────────────────────
# The lane scans the SAME change scope the other Tier-1 lanes grade.  It reads
# file bodies, so it is bounded on both axes (file count and per-file bytes)
# and skips binary files rather than ever feeding raw NUL-laden bytes into the
# detector.  Synthetic fixtures only — nothing here reads real personal data.
DATA_PROTECTION_MAX_FILE_BYTES = 1_000_000
DATA_PROTECTION_MAX_FILES = 500
# Text-ish suffixes worth scanning; extension-less files (Dockerfile, .env
# variants) are also attempted and dropped if they look binary.
_DATA_PROTECTION_TEXT_SUFFIXES = frozenset(
    {
        ".py",
        ".pyi",
        ".md",
        ".rst",
        ".txt",
        ".json",
        ".jsonl",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
        ".cfg",
        ".conf",
        ".properties",
        ".csv",
        ".tsv",
        ".xml",
        ".html",
        ".htm",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".go",
        ".rs",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".sh",
        ".bash",
        ".sql",
        ".env",
        ".log",
        ".mako",
        ".tpl",
    }
)


def _data_protection_scope(workdir: str, changed_files: list[str] | None) -> list[str]:
    """Existing, scannable files in the caller's scope (or the staged index).

    ``None`` means the caller handed in no scope (staged runs): the lane does
    its own ``git diff --cached`` discovery, mirroring the Go lanes.
    """
    if changed_files is None:
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=workdir,
            env=_sanitized_env(),
        )
        candidates = [f for f in staged.stdout.strip().split("\n") if f]
    else:
        candidates = list(changed_files)
    scope: list[str] = []
    for path in candidates:
        if not path:
            continue
        absolute = os.path.join(workdir, path)
        if not os.path.isfile(absolute):
            continue
        suffix = os.path.splitext(path)[1].lower()
        name = os.path.basename(path)
        if suffix and suffix not in _DATA_PROTECTION_TEXT_SUFFIXES and not name.startswith(".env"):
            continue
        scope.append(path)
    return scope


def _looks_binary(path: str) -> bool:
    try:
        with open(path, "rb") as handle:
            chunk = handle.read(8192)
    except OSError:
        return True
    return b"\x00" in chunk


def check_data_protection(
    workdir: str,
    changed_files: list[str] | None = None,
    policy: DataProtectionPolicy | None = None,
) -> DataProtectionResult:
    """Scan the change scope for PII / network identifiers per *policy* (GR-146).

    Returns a :class:`DataProtectionResult`.  A disabled policy (or one whose
    categories are all ``off``) yields a SKIP — the lane did no work and says
    so, exactly like ``_no_go_files_result``; it is not a substantive gate, so
    the skip never degrades a run.  When a category is ``block``, any finding
    fails the lane; ``warn`` findings pass but are reported.

    Values in ``output`` are already scrubbed for the resolved handling:
    ``redact`` emits ``[REDACTED:<category>]``, ``replace`` emits the
    class-specific placeholder, and only an explicit ``preserve`` shows the
    raw value (report-only by policy).
    """
    policy = policy or DataProtectionPolicy.default()
    if not policy.enabled:
        return DataProtectionResult(
            skipped=True,
            skip_reason="data_protection disabled (data_protection.enabled: false)",
            output="data_protection: disabled",
        )
    if not policy.active:
        return DataProtectionResult(
            skipped=True,
            skip_reason="all data_protection categories are off",
            output="data_protection: all categories off",
        )

    scope = _data_protection_scope(workdir, changed_files)
    if not scope:
        return DataProtectionResult(
            skipped=True,
            skip_reason="no scannable files in scope",
            output="data_protection: no scannable files in scope",
        )

    findings: list = []
    scanned = 0
    skipped_files: list[str] = []
    for path in scope[:DATA_PROTECTION_MAX_FILES]:
        absolute = os.path.join(workdir, path)
        try:
            if os.path.getsize(absolute) > DATA_PROTECTION_MAX_FILE_BYTES:
                skipped_files.append(f"{path} (too large)")
                continue
        except OSError:
            continue
        if _looks_binary(absolute):
            skipped_files.append(f"{path} (binary)")
            continue
        try:
            with open(absolute, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            continue
        scanned += 1
        result = policy.scan(text, path=path)
        for finding in result.findings:
            findings.append((path, finding))

    blocked = [f for _, f in findings if f.blocked]
    warned = [f for _, f in findings if f.warned]
    suppressed = [f for _, f in findings if f.suppressed]

    lines: list[str] = []
    for path, finding in findings[:50]:
        if finding.suppressed:
            continue
        lines.append(f"  {path}:{finding.line}: {finding.render(reveal=False)}")
    if len(findings) > 50:
        lines.append(f"  ... and {len(findings) - 50} more finding(s)")

    summary = (
        f"data_protection: {len(blocked)} blocked, {len(warned)} warned, "
        f"{len(suppressed)} suppressed across {scanned} file(s)"
    )
    if skipped_files:
        summary += f"; skipped: {', '.join(skipped_files[:5])}"
    output = "\n".join([summary, *lines]) if lines else summary
    if len(output) > 2000:
        output = output[:2000] + "\n... [truncated]"

    warning = ""
    if warned and not blocked:
        warning = (
            f"data_protection: {len(warned)} finding(s) detected with detection=warn "
            "(reported, not blocked)"
        )
    return DataProtectionResult(
        passed=not blocked,
        output=output,
        warning=warning,
        findings=tuple(f.to_dict() for _, f in findings),
        blocked_count=len(blocked),
        warned_count=len(warned),
        suppressed_count=len(suppressed),
        scanned_files=scanned,
    )


# ── missing-binary diagnostics (DF-GITREINS-POC-46) ────────────────────────
# A Go lane whose toolchain binary is absent from PATH returned
# ``GoGuardResult(passed=False, error=<raw OSError>)`` and nothing rendered
# ``error``: the console showed a bare ``✗`` while the cause
# (``[Errno 2] No such file or directory: 'go'``) reached only the run log.
# The Python lane solved this class twice on purpose (``_resolve_test_command``,
# GR-GAP-037, names the missing runner; ``_pytest_not_found_hint`` names the
# interpreter), so the gap was lane-local: name the binary and the fix.
_BINARY_INSTALL_HINTS: dict[str, str] = {
    "go": "install the Go toolchain: https://go.dev/doc/install",
    "golangci-lint": "install it: https://golangci-lint.run/usage/install/",
}
# ``run_bounded`` surfaces ``str(OSError)`` from a failed Popen; a missing
# binary reads ``[Errno 2] No such file or directory: '<program>'``, where the
# program is the argv[0] it tried to exec. ``scanner_nice.argv_prefix`` withholds
# the nice prefix for a tool that does not resolve, so this names the tool
# itself and never a ``nice`` wrapper.
_ENOENT_PROGRAM_RE = re.compile(r"No such file or directory: '([^']+)'")


def _missing_binary_message(program: str) -> str:
    """``'go' is not on PATH — install the Go toolchain: https://...``.

    The hint is per-binary: the Go toolchain and golangci-lint have different
    install stories. An unknown program still gets a usable line (named, with
    the configured-runner escape) rather than a bare errno string.
    """
    hint = _BINARY_INSTALL_HINTS.get(program)
    if hint is None:
        hint = (
            "install it and put it on PATH (or point guards.test_command at a "
            "runner this machine has)"
        )
    return f"'{program}' is not on PATH — {hint}"


def _spawn_error_message(err: str) -> str:
    """Name the missing binary in an ENOENT spawn failure; else pass *err* through.

    Only the missing-binary shape is rewritten. A permission error, a bad
    interpreter or a refused busy-wait keeps its own words — claiming "not on
    PATH" for those would misdirect the reader to the wrong fix.
    """
    match = _ENOENT_PROGRAM_RE.search(err or "")
    if not match:
        return err
    return _missing_binary_message(os.path.basename(match.group(1)))


def _sanitized_env() -> dict[str, str]:
    """Return the current environment with every GIT_* variable removed.

    Git exports GIT_INDEX_FILE (plus GIT_DIR, GIT_WORK_TREE, and friends) to
    pre-commit hooks. Leaking them into `go test` subprocesses breaks tests
    that exec git in temp repos or worktrees — the relative GIT_INDEX_FILE
    resolves against the wrong directory and `git worktree add` fails with
    `fatal: .git/index: index file open failed: Not a directory`. Same
    class as DF-008 (guard_manager.py, c24f29e) — the Go guards missed it.
    """
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _coerce_timeout(value: object, name: str, default: int) -> int:
    """Coerce a guard timeout config value to a positive int of seconds.

    YAML durations are commonly written with a unit suffix
    (``test_timeout: 300s``). Passing that string straight into
    subprocess.run(timeout=...) raises TypeError — not a clean
    TimeoutExpired — which crashed the full-suite go_tests stage and judge
    tier1 fleet-wide (Kobayashi-Maru ticks 240-242, GR-GAP-028).

    Leading digits are parsed ('300s' -> 300, '300' -> 300); None/missing
    -> default; garbage (no leading digits, <= 0, bool) -> ValueError with
    a message naming the config key.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        raise ValueError(
            f"guards.{name} must be a positive number of seconds (e.g. {name}: 300), got {value!r}"
        )
    if isinstance(value, str):
        match = re.match(r"^\s*(\d+)", value)
        if not match:
            raise ValueError(
                f"guards.{name} must be a positive number of seconds "
                f"(e.g. {name}: 300), got {value!r}"
            )
        value = int(match.group(1))
    else:
        try:
            value = int(value)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(
                f"guards.{name} must be a positive number of seconds "
                f"(e.g. {name}: 300), got {value!r}"
            ) from None
    if value <= 0:
        raise ValueError(
            f"guards.{name} must be a positive number of seconds (e.g. {name}: 300), got {value!r}"
        )
    return value


@dataclass
class GoGuardResult:
    name: str
    passed: bool
    output: str = ""
    error: str = ""
    # DF-GITREINS-POC-46: a non-fatal note, rendered by ``GuardResult.summary``
    # as its own ``⚠ <text>`` console line — the same field the Python lanes
    # carry for their GR-GAP-037 runner fallback. Used when a lane was graded
    # by a FALLBACK tool (golangci-lint absent → go vet): the pass is honest
    # but the missing tool and its install hint must still be visible.
    warning: str = ""
    # DF-GITREINS-POC-42: a Go lane that graded no file did no work. It keeps
    # ``passed=True`` (the toolchain is not at fault) but says so, mirroring
    # ``GuardResult`` — without the signal the DEGRADED-PASS machinery
    # (TRUST-001) could not tell "no Go files" from "Go files, all clean".
    skipped: bool = False
    skip_reason: str = ""
    # DF-GITREINS-POC-55: the nice(1) prefix this lane's scanner was spawned
    # with, or why it could not be applied — the same evidence line
    # ``GuardResult.nice_note`` carries. Empty when the knob is off or the
    # spawn happened through a shell (whose in-band probe echoes it instead).
    nice_note: str = ""


def is_go_project(workdir: str) -> bool:
    """Return True if go.mod exists in workdir."""
    return os.path.isfile(os.path.join(workdir, "go.mod"))


def _changed_go_files(workdir: str, changed_files: list[str] | None) -> list[str]:
    """Go files to grade under the caller's change scope.

    ``changed_files`` is the caller's collected scope (the guard's
    ``--scope working-tree`` set) — paths that no longer exist are dropped,
    since nothing can compile or lint them. ``None`` means "no scope was
    handed in": the guards then run their own historical staged discovery,
    unchanged, wording included.
    """
    if changed_files is None:
        staged = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=workdir,
            env=_sanitized_env(),
        )
        return [f for f in staged.stdout.strip().split("\n") if f.endswith(".go")]
    return [
        f for f in changed_files if f.endswith(".go") and os.path.isfile(os.path.join(workdir, f))
    ]


def _no_go_files(changed_files: list[str] | None) -> str:
    """The skip message for the scope that produced no Go file."""
    return "No Go files staged" if changed_files is None else "No Go files in scope"


def _no_go_files_result(name: str, changed_files: list[str] | None) -> GoGuardResult:
    """The honest SKIP for a lane that graded no Go file (DF-GITREINS-POC-42).

    ``passed=True`` because the lane itself did not fail, plus ``skipped`` /
    ``skip_reason`` so the run is a DEGRADED pass rather than an
    indistinguishable green (the message names the reason: the index was the
    scope, or a working-tree/whole-tree scope held no Go file).
    """
    message = _no_go_files(changed_files)
    return GoGuardResult(name=name, passed=True, output=message, skipped=True, skip_reason=message)


def check_go_lint(
    workdir: str, changed_files: list[str] | None = None, nice_level: int = 0
) -> GoGuardResult:
    """Run go vet for the selected change scope. Fall back to golangci-lint if available.

    ``nice_level`` is the DF-GITREINS-POC-55 scanner nice level (0 = off): both
    the linter and the ``go vet`` fallback are spawned external scanners, so both
    run behind the shared policy.
    """
    go_files = _changed_go_files(workdir, changed_files)
    if not go_files:
        return _no_go_files_result("go_lint", changed_files)

    # GR-LINT-001: golangci-lint with an explicit FILE list analyses only that
    # file-set, so package-level symbols declared in sibling files go
    # unresolvable and every reference typechecks as ``undefined`` — false
    # findings that block single-file Go commits (proven 3x on 2026-09-24).
    # Scope by package DIRECTORY instead: the linter then loads the whole
    # package and cross-file symbols resolve. One invocation per distinct
    # package dir keeps multi-package change sets honest; all invocations must
    # pass for the lane to pass.
    package_dirs = sorted({os.path.dirname(f) or "." for f in go_files})
    lint_prefix, lint_note = scanner_nice.argv_prefix(nice_level, "golangci-lint")
    lint_results: list[dict] = []
    for pkg_dir in package_dirs:
        result = command_hygiene.run_bounded(
            [
                *lint_prefix,
                "golangci-lint",
                "run",
                "--new-from-rev=HEAD~1",
                *([] if pkg_dir == "." else [pkg_dir]),
            ],
            cwd=workdir,
            timeout=60,
            env=_sanitized_env(),
        )
        lint_results.append(result)
        if "exit_code" not in result:
            break
    # GR-LINT-001 follow-up (judge probe, verdict edabaa0e): an invocation that
    # COULD NOT RUN must not reach the go-vet fallback when an EARLIER package
    # already returned exit 1 — the vet fallback would erase that real
    # finding. And the fallback must never claim a lane the linter did not
    # fully cover: if a mid-loop spawn failure left any package dir UNGRADED
    # (fewer results than dirs), the lane fails loudly instead of passing on
    # a partial go-vet fallback (verdict 89a79c5d probe).
    lint_failed = any(r.get("exit_code") not in (0, None) for r in lint_results)
    if "exit_code" not in result and not lint_failed and len(lint_results) == len(package_dirs):
        # DF-GITREINS-POC-43: the linter never RAN — every shape run_bounded
        # can return without a verdict: spawn failure ({"error": ...}, no
        # exit_code) or a refused busy-wait ({"refused", "reason"}). Only
        # this may fall through to go vet; exit_code=1 (incl. a timed-out
        # kill's -9) means the process ran and its verdict must be graded.
        detail = result.get("error") or result.get("reason") or "linter did not run"
        note = f"golangci-lint unavailable ({detail}); "
        # DF-GITREINS-POC-46: the fallback keeps the lane green when go vet can
        # grade the tree, but a green whose linter was ABSENT must still name
        # the missing tool and its install hint on the console — otherwise the
        # operator reads `✓ go_lint — ok` on a box that never linted with
        # golangci-lint. A refused busy-wait (a misconfiguration) says so in
        # its own words instead and gets no "not on PATH" claim.
        fallback_warning = ""
        missing = _ENOENT_PROGRAM_RE.search(detail or "")
        if missing and os.path.basename(missing.group(1)) == "golangci-lint":
            fallback_warning = (
                f"{_missing_binary_message('golangci-lint')} "
                "(this run was graded by go vet instead)"
            )
        vet_prefix, vet_note = scanner_nice.argv_prefix(nice_level, "go")
        vet = command_hygiene.run_bounded(
            [*vet_prefix, "go", "vet", "./..."],
            cwd=workdir,
            timeout=60,
            env=_sanitized_env(),
        )
        if "error" in vet and "exit_code" not in vet:
            # Spawn failure (e.g. go itself missing) — surfaced in error,
            # matching the old except-Exception contract, now naming the
            # missing binary and its install hint (DF-GITREINS-POC-46).
            return GoGuardResult(
                name="go_lint",
                passed=False,
                error=_spawn_error_message(vet["error"]),
                warning=fallback_warning,
            )
        output = vet.get("output") or ""
        if len(output) > 2000:
            output = output[:2000] + "\n... [truncated]"
        if vet.get("exit_code") == 0:
            return GoGuardResult(
                name="go_lint",
                passed=True,
                output=f"{note}graded by go vet: clean",
                warning=fallback_warning,
                nice_note=vet_note,
            )
        return GoGuardResult(
            name="go_lint",
            passed=False,
            output=f"{note}graded by go vet:\n{output}",
            warning=fallback_warning,
            nice_note=vet_note,
        )
    # The linter ran for every package dir: the verdicts are authoritative. A
    # real exit 1 with findings must never masquerade as "ok" via a clean go
    # vet (DF-GITREINS-POC-43 false-PASS). GR-LINT-001: one invocation per
    # package dir — the lane passes only when ALL of them pass.
    outputs: list[str] = []
    failed_dirs: list[str] = []
    for pkg_dir, res in zip(package_dirs, lint_results):
        out = res.get("output") or ""
        if len(out) > 2000:
            out = out[:2000] + "\n... [truncated]"
        if res.get("exit_code") != 0:
            failed_dirs.append(pkg_dir)
            outputs.append(f"[{pkg_dir}] {out}" if len(package_dirs) > 1 else out)
    if failed_dirs:
        return GoGuardResult(
            name="go_lint",
            passed=False,
            output="\n".join(outputs),
            nice_note=lint_note,
        )
    label = (
        "golangci-lint: clean"
        if len(package_dirs) == 1
        else f"golangci-lint: clean ({len(package_dirs)} packages)"
    )
    return GoGuardResult(name="go_lint", passed=True, output=label, nice_note=lint_note)


def _resolve_go_test_argv(test_command: str | None) -> tuple[list[str] | str, str | None]:
    """Resolve the Go tests lane's invocation (DF-GITREINS-POC-45).

    Returns ``(cmd, warning)`` where *cmd* is an argv LIST for the historical
    default and a shell STRING for a configured command
    (``command_hygiene.run_bounded`` runs a list with ``shell=False`` and a
    string through the shell).

    ``guards.test_command`` was promised by ``gitreins init``'s printed
    ``Test cmd:`` line and by the docs ("the configured guards.test_command")
    but only the PYTHON tests lane ever read it: this lane hard-coded
    ``go test -count=1 -short ./...`` whatever the config said. A knob that is
    documented, advertised and silently ignored is worse than a missing knob —
    it makes the documented configuration path a dead end.

    Precedence: a present, non-empty configured command wins and is executed
    through ``/bin/sh`` (so ``go test -race ./...`` and other shell-shaped
    commands work, matching the Python lane, which runs its ``test_command``
    as a string); otherwise the historical argv list is returned UNCHANGED, so
    every config without the key behaves exactly as before.
    """
    configured = (test_command or "").strip() if isinstance(test_command, str) else ""
    if not configured:
        return ["go", "test", "-count=1", "-short", "./..."], None
    return configured, f"go_tests graded by the configured guards.test_command: {configured}"


def check_go_tests(
    workdir: str,
    timeout: int | str = 180,
    changed_files: list[str] | None = None,
    test_command: str | None = None,
    nice_level: int = 0,
) -> GoGuardResult:
    """Run go test for the selected change scope.

    timeout is configurable so large Go projects (slow integration
    suites) can raise it via guards.test_timeout in .gitreins/config.yaml.
    ``changed_files`` carries the caller's scope (see :func:`_changed_go_files`).
    ``test_command`` is the repo's configured ``guards.test_command``
    (DF-GITREINS-POC-45): when present and non-empty it drives the lane,
    otherwise the historical argv runs unchanged.

    ``nice_level`` (DF-GITREINS-POC-55): the historical argv is spawned
    directly and takes the nice prefix as argv words; a configured SHELL
    command is wrapped as a whole so a chain is covered, and the in-band probe
    echoes the note into the lane's own output.
    """
    # Belt-and-braces: consumers may pass a raw string config value (e.g.
    # '300s'); subprocess.run(timeout='300s') raises TypeError instead of
    # timing out (GR-GAP-028). GuardManager already coerces at init — this
    # protects direct callers.
    timeout = _coerce_timeout(timeout, "test_timeout", 180)
    go_files = _changed_go_files(workdir, changed_files)
    if not go_files:
        return _no_go_files_result("go_tests", changed_files)

    cmd, configured_note = _resolve_go_test_argv(test_command)
    nice_note = ""
    if isinstance(cmd, list):
        prefix, nice_note = scanner_nice.argv_prefix(nice_level, cmd[0])
        cmd = [*prefix, *cmd] if prefix else cmd
    else:
        cmd = scanner_nice.shell_wrap(cmd, nice_level)
    result = command_hygiene.run_bounded(
        cmd,
        cwd=workdir,
        timeout=timeout,
        env=_sanitized_env(),
    )
    if result.get("timed_out"):
        return GoGuardResult(
            name="go_tests",
            passed=False,
            output=f"Tests timed out after {timeout}s (guards.test_timeout). "
            "Raise it in .gitreins/config.yaml — e.g. test_timeout: 900 for "
            "large projects with slow integration suites.",
        )
    if result.get("refused"):
        # A configured command that is a busy-wait is a misconfiguration, not
        # a test failure — the reason text already names the right primitive.
        return GoGuardResult(name="go_tests", passed=False, error=result["reason"])
    if "error" in result and "exit_code" not in result:
        # DF-GITREINS-POC-46: a spawn failure is a missing binary (the runner
        # is not on PATH) — name it and the fix instead of the raw errno.
        return GoGuardResult(
            name="go_tests", passed=False, error=_spawn_error_message(result["error"])
        )
    output = result.get("output") or ""
    if len(output) > 2000:
        output = output[-2000:]
    prefix = f"{configured_note}\n" if configured_note else ""
    if result.get("exit_code") == 0:
        return GoGuardResult(
            name="go_tests",
            passed=True,
            output=(prefix + output)[:500],
            nice_note=nice_note,
        )
    return GoGuardResult(name="go_tests", passed=False, output=prefix + output, nice_note=nice_note)


def check_go_build(
    workdir: str, changed_files: list[str] | None = None, nice_level: int = 0
) -> GoGuardResult:
    """Run go build for the selected change scope to catch compile errors."""
    go_files = _changed_go_files(workdir, changed_files)
    if not go_files:
        return _no_go_files_result("go_build", changed_files)

    prefix, nice_note = scanner_nice.argv_prefix(nice_level, "go")
    result = command_hygiene.run_bounded(
        [*prefix, "go", "build", "-buildvcs=false", "./..."],
        cwd=workdir,
        timeout=120,
        env=_sanitized_env(),
    )
    if "error" in result and "exit_code" not in result:
        # Spawn failure (e.g. go missing) — old except-Exception contract, now
        # naming the missing binary and its install hint (DF-GITREINS-POC-46).
        return GoGuardResult(
            name="go_build", passed=False, error=_spawn_error_message(result["error"])
        )
    output = result.get("output") or ""
    if len(output) > 2000:
        output = output[:2000] + "\n... [truncated]"
    if result.get("exit_code") == 0:
        return GoGuardResult(
            name="go_build", passed=True, output="go build: ok", nice_note=nice_note
        )
    return GoGuardResult(name="go_build", passed=False, output=output, nice_note=nice_note)
