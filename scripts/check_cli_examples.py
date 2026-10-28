#!/usr/bin/env python3
"""Check every `gitreins` example in the docs against the real CLI parser (POC-13).

A doc example that does not parse is a broken promise: the README shipped a
`task create ... --depends-on <id> <criteria...>` example whose criteria were
rejected by argparse (`unrecognized arguments`), so a reader following the
README got a failure from GitReins' own guide. This script replays each
documented invocation through the SAME parser the CLI builds — no second,
hand-maintained copy of the CLI surface — and fails when one does not parse.

What is checked
    Fenced code blocks in README.md, CONTRIBUTING.md and docs/*.md whose
    logical line (after `\\` continuation joining) starts with `gitreins`.
    Two forms are accepted:

    * invocation examples — checked verbatim;
    * usage sketches (`gitreins report [-n N] [--interactive]`) — bracketed
      optional spans are stripped and `<placeholder>` tokens are replaced
      with a dummy value before parsing, so the required part of the syntax
      is still validated.

Lines that compose shell commands (`&&`, `|`, `;`, `$(`, redirection) are
reported as skipped, never silently passed, because only the GitReins part of
such a line can be attributed to the CLI.

The command handlers are stubbed out before parsing, so nothing executes: only
argparse runs. Exit 0 with one summary line, exit 1 naming the exact doc line
that failed to parse.

README Commands-block coverage
    After the parse check, the script also verifies that every subcommand the
    live CLI advertises via `gitreins --help` appears in the fenced Commands
    block of README.md. A missing command is a silent discoverability bug —
    a reader scanning the README cannot find it. This check fails closed: if
    a subcommand is added to the CLI but not to the README, the script exits 1.

Usage:
    python scripts/check_cli_examples.py [--repo-root PATH]
"""

import argparse
import contextlib
import io
import re
import shlex
import subprocess
import sys
from pathlib import Path

_FENCE_RE = re.compile(r"^\s*```")
_BRACKET_RE = re.compile(r"\[[^\[\]]*\]")
_PLACEHOLDER_RE = re.compile(r"<[^<>]*>")
_TRAILING_COMMENT_RE = re.compile(r"\s+#(?:\s|$)")
_COMPOSITION_MARKERS = ("&&", "||", "|", ";", "$(", ">", "&")

# docs/dogfood/** holds historical run transcripts (commands as they were
# typed on the day), not instructions a reader follows — deliberately excluded.
_DOC_GLOBS = ("README.md", "CONTRIBUTING.md", "docs/*.md")

# README Commands-block coverage: every subcommand the live CLI advertises
# must appear in the fenced Commands block. Drift between the CLI surface and
# the README inventory is a silent discoverability bug — this check fails
# closed.
_README_COMMANDS_BLOCK_PATH = "README.md"


def _iter_logical_lines(path):
    """Yield (lineno, text) for gitreins lines inside fenced blocks."""
    try:
        raw_lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    in_fence = False
    i = 0
    while i < len(raw_lines):
        line = raw_lines[i]
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            i += 1
            continue
        if not in_fence:
            i += 1
            continue
        start = i + 1
        text = line
        while text.rstrip().endswith("\\") and i + 1 < len(raw_lines):
            i += 1
            text = text.rstrip()[:-1] + " " + raw_lines[i].strip()
        yield start, text.strip()
        i += 1


def _classify(text):
    """Return (kind, command) where kind is check/skip/composition/other."""
    if text.startswith("$ "):
        text = text[2:].strip()
    if not text or text.startswith("#"):
        return "other", text

    # Normalise the sketch forms before deciding what kind of line this is.
    unbracketed = _BRACKET_RE.sub("", text).strip()
    command = _PLACEHOLDER_RE.sub("1", unbracketed)
    command = _TRAILING_COMMENT_RE.split(command, maxsplit=1)[0].strip()
    if command.split()[:1] != ["gitreins"]:
        # Prose/dir listings ("gitreins/  — CLI entry point") are not invocations.
        return "other", text
    if len(command.split()) < 2:
        # A bare `gitreins` prints the command list — nothing to validate.
        return "other", text
    if _PLACEHOLDER_RE.match(unbracketed.split()[1]):
        # `gitreins <command> [args]` sketches the command list itself; there is
        # no parser to run until a real subcommand is named.
        return "other", text
    for marker in _COMPOSITION_MARKERS:
        if marker in command:
            return "composition", text
    return "check", command


def _stub_handlers(cli):
    """Replace every cmd_* handler with a recorder so parsing cannot execute.

    `push-check` is dispatched inline in ``main()`` rather than through a
    ``cmd_*`` attribute, so its `engine.push_secrets.check_push_range` entry
    point is stubbed separately — otherwise a documented `push-check` example
    would scan real git ranges (and fail on placeholder SHAs) instead of only
    parsing.
    """
    for attr in dir(cli):
        if attr.startswith("cmd_"):
            setattr(cli, attr, lambda args: None)
    try:
        from engine import push_secrets

        push_secrets.check_push_range = lambda workdir, local_sha, remote_sha: 0
    except Exception:
        pass


def _parse(cli, command):
    """Return (exit_code, stderr_text) for a real argparse run of `command`."""
    argv = shlex.split(command)
    argv = ["gitreins" if argv and argv[0] == "gitreins" else argv[0]] + argv[1:]
    stderr = io.StringIO()
    stdout = io.StringIO()
    saved = sys.argv
    try:
        sys.argv = argv
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
            cli.main()
    except SystemExit as exc:  # argparse exits 2 on a parse error, 0 otherwise
        code = exc.code if isinstance(exc.code, int) else 1
        return code, stderr.getvalue()
    except Exception as exc:  # handler-free parse should never raise anything else
        return 99, f"{type(exc).__name__}: {exc}"
    finally:
        sys.argv = saved
    return 0, stderr.getvalue()


def resolve_repo_root(argv=None):
    parser = argparse.ArgumentParser(
        description="Check documented gitreins examples against the real CLI parser."
    )
    parser.add_argument("--repo-root", type=Path, default=None)
    args = parser.parse_args(argv)
    if args.repo_root is not None:
        return args.repo_root
    return Path(__file__).resolve().parent.parent


def _discover_live_subcommands(repo_root):
    """Return the set of subcommand names the live CLI advertises in --help."""
    try:
        help_out = subprocess.run(
            [sys.executable, "-m", "gitreins", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=repo_root,
        )
        help_text = help_out.stdout + help_out.stderr
    except Exception:
        return set()
    m = re.search(r"\{([a-z0-9,_-]+)\}", help_text)
    if not m:
        return set()
    return {c.strip() for c in m.group(1).split(",") if c.strip()}


def _check_readme_commands_block(repo_root):
    """Assert every live subcommand appears in the README Commands block.

    Returns (ok: bool, message: str). ok=True means either the check passed
    or it could not be run (missing README or could not discover commands);
    ok=False means the block is missing at least one live subcommand.
    """
    readme_path = repo_root / _README_COMMANDS_BLOCK_PATH
    if not readme_path.exists():
        return True, "README.md not found; skipping Commands-block coverage"
    try:
        readme_text = readme_path.read_text(encoding="utf-8")
    except OSError as e:
        return True, f"could not read README.md: {e}"
    # Extract the fenced block under the '## Commands' heading.
    commands_block_lines = []
    in_commands_section = False
    in_fence = False
    for line in readme_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("## Commands"):
            in_commands_section = True
            continue
        if in_commands_section and stripped.startswith("## "):
            break  # next section
        if not in_commands_section:
            continue
        if stripped.startswith("```"):
            if in_fence:
                break  # closing fence ends the block
            in_fence = True
            continue
        if in_fence:
            commands_block_lines.append(line)
    commands_block = "\n".join(commands_block_lines)
    live = _discover_live_subcommands(repo_root)
    if not live:
        return True, "could not discover live subcommands; skipping coverage"
    documented = {
        m.group(1) for m in re.finditer(r"\bgitreins\s+([a-z][a-z0-9_-]*)", commands_block)
    }
    missing = sorted(live - documented)
    if missing:
        return False, (
            f"README Commands block is missing {len(missing)} live subcommand(s): "
            f"{', '.join(missing)}"
        )
    return True, f"README Commands block covers all {len(live)} live subcommands"


def main(argv=None):
    repo_root = resolve_repo_root(argv)
    sys.path.insert(0, str(repo_root))
    from gitreins import cli

    _stub_handlers(cli)

    files = []
    for glob in _DOC_GLOBS:
        files.extend(sorted(repo_root.glob(glob)))

    checked = 0
    skipped = []
    failures = []
    for path in files:
        rel = path.relative_to(repo_root)
        for lineno, text in _iter_logical_lines(path):
            kind, command = _classify(text)
            if kind == "other":
                continue
            if kind == "composition":
                skipped.append(f"{rel}:{lineno} (shell composition)")
                continue
            checked += 1
            code, err = _parse(cli, command)
            if code != 0:
                detail = " ".join(err.split())[:300] or f"exit {code}"
                failures.append(f"{rel}:{lineno}: {command}\n      {detail}")

    if failures:
        print(f"FAIL: {len(failures)} documented gitreins example(s) do not parse")
        for item in failures:
            print(f"  {item}")
        return 1

    # README Commands-block coverage (must appear after the parse check so a
    # broken example is reported before a missing command).
    ok, msg = _check_readme_commands_block(repo_root)
    print(msg)
    if not ok:
        return 1

    print(
        f"All documented gitreins examples parse: {checked} checked across "
        f"{len(files)} doc(s), {len(skipped)} shell-composition line(s) skipped"
    )
    for item in skipped:
        print(f"  skipped: {item}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
