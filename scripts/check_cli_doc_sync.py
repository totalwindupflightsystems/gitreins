#!/usr/bin/env python3
"""Fail when docs/cli-reference.md drifts from the live CLI surface (GR-GAP-056).

The 2026-09-18 hunt found the CLI reference contradicting the shipped CLI on
three counts at once: it claimed **13** top-level subcommands while
`python -m gitreins --help` listed 14 (`qa` had no row in the table and no
section number), section 12 documented no options at all for `worktree clean`
and `worktree fleet`, and the exit-2 `--cell` contract was filed under
`qa list` — which does not accept `--cell` at all.

`scripts/check_cli_examples.py` cannot catch any of that: it proves documented
examples PARSE, and every one of those examples parsed happily while the table
and the count around them were wrong. This check compares the doc's *claims*
against the SAME parser the CLI builds — no second, hand-maintained copy of
the CLI surface — and fails on a dropped subcommand, a wrong count, a missing
worktree option, or a subsection documenting a flag its own parser rejects.

What is checked
    1. the stated count — ``There are **N top-level subcommands**`` — equals
       the live parser's subcommand count;
    2. the numbered table under ``## Global`` lists exactly the live
       subcommands, numbered 1..N with no gaps or duplicates;
    3. every live ``worktree`` subcommand has a row in section 12's table;
    4. every option a ``worktree`` or ``qa`` subparser accepts is named in
       that subcommand's subsection;
    5. no subsection documents a long flag (``\\`--flag\\```) that its own
       subparser does not accept — the historical `qa list --cell` error.

Exit 0 with one summary line, exit 1 naming every drift found.

Usage:
    python scripts/check_cli_doc_sync.py [--repo-root PATH] [--doc PATH]
"""

import argparse
import contextlib
import io
import re
import sys
from pathlib import Path

DOC_RELPATH = Path("docs") / "cli-reference.md"

_COUNT_RE = re.compile(r"There are \*\*(\d+) top-level subcommands\*\*")
_TABLE_ROW_RE = re.compile(r"^\|\s*(\d+)\s*\|\s*`([A-Za-z][A-Za-z0-9_-]*)`\s*\|")
_PLAIN_TABLE_ROW_RE = re.compile(r"^\|\s*`([A-Za-z][A-Za-z0-9_-]*)(?:[^`]*)`\s*\|")
_HEADING_RE = re.compile(r"^(#{2,3})\s+(.*?)\s*$")
_FENCE_RE = re.compile(r"^\s*```")
_BACKTICKED_FLAG_RE = re.compile(r"`(--[a-z][a-z0-9-]*)`")


def _sections(text, level):
    """Map heading text -> body for headings of exactly ``level`` hashes.

    Fenced code blocks are skipped when looking for headings (a `#` comment
    inside a bash example is not a section), while the body keeps them.
    """
    sections = {}
    current = None
    body = []
    in_fence = False
    for line in text.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            if current is not None:
                body.append(line)
            continue
        match = None if in_fence else _HEADING_RE.match(line)
        if match and len(match.group(1)) == level:
            if current is not None:
                sections[current] = "\n".join(body)
            current = match.group(2)
            body = []
        elif current is not None:
            body.append(line)
    if current is not None:
        sections[current] = "\n".join(body)
    return sections


def _table_names(body):
    """Return [(number, name)] for numbered table rows, None for non-numbered."""
    rows = []
    for line in body.splitlines():
        match = _TABLE_ROW_RE.match(line)
        if match:
            rows.append((int(match.group(1)), match.group(2)))
    return rows


def _plain_table_names(body):
    """Return `name` for unnumbered table rows of the form ``| `name …` | … |``.

    The first token inside the backticks is the name, so a row that carries a
    positional placeholder (`| \\`fleet <manifest>\\` | … |`) still counts. Option
    rows (`| \\`--json\\` | … |`) never match: a name must start with a letter.
    """
    names = []
    for line in body.splitlines():
        match = _PLAIN_TABLE_ROW_RE.match(line)
        if match:
            names.append(match.group(1))
    return names


def _unquoted_heading(heading):
    """`worktree clean` -> worktree clean."""
    return heading.replace("`", "").strip()


def _mentions_option(text, option):
    """True when ``option`` appears in ``text`` as a standalone CLI flag."""
    if option.startswith("--"):
        return option in text
    # A short option must not match inside a long one: `-k` is not `--keep`.
    return re.search(r"(?<!-)" + re.escape(option) + r"(?![\w-])", text) is not None


def _fenced_blocks(body):
    """Yield the text of each fenced code block in ``body``."""
    block = []
    in_fence = False
    for line in body.splitlines():
        if _FENCE_RE.match(line):
            if in_fence:
                yield "\n".join(block)
                block = []
            in_fence = not in_fence
            continue
        if in_fence:
            block.append(line)


def _claimed_flags(body):
    """Flags a subsection claims for its own subcommand.

    Two claim forms count: a backticked flag in prose (``\\`--cell\\```) and any
    long flag inside a fenced usage sketch. An unbackticked mention in prose (a
    cross-reference like "use `worktree merge --force`") is not a claim and is
    deliberately ignored.
    """
    flags = set(_BACKTICKED_FLAG_RE.findall(body))
    for block in _fenced_blocks(body):
        flags.update(re.findall(r"(?<![\w-])(--[a-z][a-z0-9-]*)", block))
    return flags


def _subparser_map(parent_parser):
    """Return {name: parser} for the subparsers of an argparse parser."""
    import argparse as _argparse

    for action in parent_parser._actions:
        if isinstance(action, _argparse._SubParsersAction):
            return dict(action._name_parser_map)
    return {}


def _options_of(parser, *, long_only=False):
    """Every option string a parser accepts, minus the implicit --help/-h."""
    options = set()
    for action in parser._actions:
        for option_string in action.option_strings or ():
            if option_string in ("-h", "--help"):
                continue
            if long_only and not option_string.startswith("--"):
                continue
            options.add(option_string)
    return options


def live_surface(repo_root):
    """Read the live CLI surface from the parser ``gitreins.cli.main`` builds.

    Handlers are stubbed and ``main()`` is driven with ``--help`` so nothing
    executes — only argparse runs. Returns
    ``(top_level_names, {worktree_sub: options}, {qa_sub: options})``.
    """
    import argparse as _argparse

    repo_root = str(repo_root)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    from gitreins import cli

    for attr in dir(cli):
        if attr.startswith("cmd_"):
            setattr(cli, attr, lambda args: None)

    created = []
    original = _argparse.ArgumentParser.add_subparsers

    def _spy(self, *args, **kwargs):
        action = original(self, *args, **kwargs)
        created.append(action)
        return action

    _argparse.ArgumentParser.add_subparsers = _spy
    saved_argv = sys.argv
    try:
        sys.argv = ["gitreins", "--help"]
        with contextlib.redirect_stdout(io.StringIO()):
            cli.main()
    except SystemExit:
        pass
    finally:
        _argparse.ArgumentParser.add_subparsers = original
        sys.argv = saved_argv

    top = next(
        (
            action
            for action in created
            if hasattr(action, "_name_parser_map")
            and {"install", "guard"} <= set(action._name_parser_map)
        ),
        None,
    )
    if top is None:
        raise RuntimeError("could not locate the top-level subparser built by gitreins.cli.main")

    parsers = dict(top._name_parser_map)
    worktree = {name: _options_of(p) for name, p in _subparser_map(parsers["worktree"]).items()}
    qa = {name: _options_of(p) for name, p in _subparser_map(parsers["qa"]).items()}
    return sorted(parsers), worktree, qa


def check_cli_doc_sync(repo_root, doc_path=None):
    """Return (exit_code, message) for the doc at ``doc_path``."""
    repo_root = Path(repo_root)
    doc = Path(doc_path) if doc_path is not None else repo_root / DOC_RELPATH
    if not doc.is_file():
        return 1, f"FAIL: {doc} not found"

    text = doc.read_text(encoding="utf-8")
    top_level, worktree_options, qa_options = live_surface(repo_root)
    problems = []

    # 1 + 2 — the stated count and the numbered table under `## Global`.
    global_body = _sections(text, 2).get("Global")
    if global_body is None:
        problems.append("no `## Global` section (the subcommand table lives there)")
        global_body = ""
    count_match = _COUNT_RE.search(global_body)
    if count_match is None:
        problems.append(
            "the `## Global` section no longer states the subcommand count "
            "(expected `There are **N top-level subcommands**:`)"
        )
    elif int(count_match.group(1)) != len(top_level):
        problems.append(
            f"stated count {count_match.group(1)} != live count {len(top_level)} "
            f"(live: {', '.join(top_level)})"
        )

    rows = _table_names(global_body)
    doc_names = [name for _num, name in rows]
    for name in top_level:
        if name not in doc_names:
            problems.append(f"live subcommand `{name}` has no row in the `## Global` table")
    for name in doc_names:
        if name not in top_level:
            problems.append(f"table documents `{name}`, which the live CLI does not provide")
    seen = set()
    for name in doc_names:
        if name in seen:
            problems.append(f"duplicate table row for `{name}`")
        seen.add(name)
    numbers = [num for num, _name in rows]
    if numbers and numbers != list(range(1, len(numbers) + 1)):
        problems.append(
            f"table numbering is not 1..{len(numbers)} with no gaps/duplicates (got {numbers})"
        )

    # 3 + 4 + 5 — section 12 (`worktree`) and section 13 (`qa`) subsections.
    # The section numbers are pinned to the shipped doc's layout, NOT to the
    # table numbering under `## Global` (the table groups commands logically:
    # worktree is row 4, qa row 14, while their sections sit at 12 and 13).
    # A new subcommand appended to the table does not renumber them.
    sections_l2 = _sections(text, 2)
    for section_prefix, options_by_sub in (("12.", worktree_options), ("13.", qa_options)):
        heading = next((h for h in sections_l2 if h.startswith(section_prefix)), None)
        if heading is None:
            problems.append(f"no `## {section_prefix}` section")
            continue
        section_body = sections_l2[heading]
        quoted = _unquoted_heading(heading)
        command = quoted.split(".", 1)[1].strip().strip("`")
        command = command.removeprefix("gitreins ").strip()
        table_names = _plain_table_names(section_body)
        subsections = _sections(section_body, 3)

        if table_names:
            for listed in table_names:
                if listed not in options_by_sub:
                    problems.append(
                        f"{section_prefix} table lists `{command} {listed}`, "
                        "which the live CLI does not provide"
                    )

        for sub, options in sorted(options_by_sub.items()):
            if table_names and sub not in table_names:
                problems.append(f"`{command} {sub}` has no row in the {section_prefix} table")
            body = next(
                (
                    text_body
                    for sub_heading, text_body in subsections.items()
                    if _unquoted_heading(sub_heading) == f"{command} {sub}"
                ),
                None,
            )
            if body is None:
                problems.append(
                    f"no `### `{command} {sub}`` subsection in section {section_prefix}"
                )
                continue
            for option in sorted(options):
                if not _mentions_option(body, option):
                    problems.append(f"`{command} {sub}` option `{option}` is undocumented")
            for documented in sorted(_claimed_flags(body)):
                if documented not in options:
                    problems.append(
                        f"`{command} {sub}` documents `{documented}`, "
                        "which its parser does not accept"
                    )

    if problems:
        return 1, "FAIL: docs/cli-reference.md drifts from the live CLI:\n" + "\n".join(
            f"  - {problem}" for problem in problems
        )

    return 0, (
        f"docs/cli-reference.md matches the live CLI: {len(top_level)} subcommands, "
        f"{len(worktree_options)} worktree subcommand(s) with every option documented, "
        f"{len(qa_options)} qa subcommand(s) checked"
    )


def resolve_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Check docs/cli-reference.md against the live CLI parser."
    )
    parser.add_argument("--repo-root", type=Path, default=None)
    parser.add_argument(
        "--doc",
        type=Path,
        default=None,
        help="Override the doc under test (default: <repo-root>/docs/cli-reference.md)",
    )
    args = parser.parse_args(argv)
    if args.repo_root is None:
        args.repo_root = Path(__file__).resolve().parent.parent
    return args


def main(argv=None):
    args = resolve_args(argv)
    code, message = check_cli_doc_sync(args.repo_root, doc_path=args.doc)
    print(message)
    return code


if __name__ == "__main__":
    sys.exit(main())
