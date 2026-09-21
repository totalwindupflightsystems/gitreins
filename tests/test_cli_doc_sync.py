"""Hermetic tests for scripts/check_cli_doc_sync.py (GR-GAP-056).

The doc under test is always a throwaway copy under ``tmp_path`` (built from
the LIVE CLI surface, then mutated per case) and is handed to the script via
``--doc``; the parser the script compares against is the real one — the CLI is
never stubbed or mirrored, which is the whole point of the check. Each scenario
that matters is exercised two ways: through the importable
``check_cli_doc_sync()`` function and through the real exit code (script run as
a subprocess with an argument list, no shell).

One case deliberately does NOT use a fixture: the repository's own
``docs/cli-reference.md`` must be in sync, so the acceptance for the fixed doc
is asserted on every test run.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "check_cli_doc_sync.py"
REPO_ROOT = SCRIPT_PATH.parent.parent


def _load_module():
    """Load the script as an importable module (importlib, by file path)."""
    spec = importlib.util.spec_from_file_location("check_cli_doc_sync", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _usage_line(command, options):
    """A fenced usage sketch naming every option the parser accepts."""
    parts = []
    for option in options:
        if option.startswith("--"):
            parts.append(f"[{option} <value>]")
        else:
            parts.append(f"[{option} <value>]")
    return f"gitreins {command} " + " ".join(parts)


def _write_synced_doc(tmp_path, module):
    """Build a minimal doc that mirrors the live surface (then cases mutate it)."""
    top_level, worktree_options, qa_options = module.live_surface(REPO_ROOT)
    lines = [
        "# Fixture CLI Reference",
        "",
        "## Global",
        "",
        f"There are **{len(top_level)} top-level subcommands**:",
        "",
        "| # | Command | Purpose |",
        "|---|---------|---------|",
    ]
    lines += [f"| {i} | `{name}` | fixture |" for i, name in enumerate(top_level, start=1)]
    lines += ["", "## 12. `gitreins worktree`", "", "| Subcommand | Purpose |", "|---|---|"]
    lines += [f"| `{sub}` | fixture |" for sub in sorted(worktree_options)]
    for sub, options in sorted(worktree_options.items()):
        lines += [
            "",
            f"### `worktree {sub}`",
            "",
            "```bash",
            _usage_line(f"worktree {sub}", sorted(options)),
            "```",
            "",
            f"Runs `worktree {sub}`.",
        ]
    lines += ["", "## 13. `gitreins qa`", ""]
    for sub, options in sorted(qa_options.items()):
        lines += [
            f"### `qa {sub}`",
            "",
            "```bash",
            _usage_line(f"qa {sub}", sorted(options)),
            "```",
            "",
            f"Reads or records QA rows (`qa {sub}`).",
            "",
        ]
    doc = tmp_path / "cli-reference.md"
    doc.write_text("\n".join(lines), encoding="utf-8")
    return doc


def _run_script(doc_path):
    return subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "--repo-root",
            str(REPO_ROOT),
            "--doc",
            str(doc_path),
        ],
        capture_output=True,
        text=True,
    )


def test_live_surface_pins_the_current_cli():
    """The truth the doc is compared against — pinned so a silent parser
    change shows up here rather than as a mysterious doc failure."""
    top_level, worktree_options, qa_options = _load_module().live_surface(REPO_ROOT)
    assert len(top_level) == 16
    assert "qa" in top_level
    assert "resolve" in top_level
    assert "preflight" in top_level
    assert sorted(worktree_options) == [
        "clean",
        "doctor",
        "dogfood",
        "fleet",
        "fresh",
        "list",
        "merge",
        "repro",
    ]
    assert worktree_options["clean"] == {"--confirm-stale-orphan"}
    assert worktree_options["fleet"] == {
        "--actor",
        "--force-merge",
        "--max-concurrent-worktrees",
        "--merge",
        "--tick",
    }
    assert "--cell" not in qa_options["list"]
    assert "--cell" in qa_options["record"]


def test_synced_fixture_doc_passes(tmp_path):
    doc = _write_synced_doc(tmp_path, _load_module())

    proc = _run_script(doc)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "16 subcommands" in proc.stdout

    code, message = _load_module().check_cli_doc_sync(REPO_ROOT, doc_path=doc)
    assert code == 0, message


def test_repository_doc_is_in_sync():
    """The fixed docs/cli-reference.md must pass its own check."""
    proc = subprocess.run(
        [sys.executable, str(SCRIPT_PATH)],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "16 subcommands" in proc.stdout


def test_dropped_subcommand_row_fails_naming_it(tmp_path):
    doc = _write_synced_doc(tmp_path, _load_module())
    text = doc.read_text(encoding="utf-8")
    assert "| 9 | `qa` | fixture |" in text
    doc.write_text(text.replace("| 9 | `qa` | fixture |\n", ""), encoding="utf-8")

    proc = _run_script(doc)
    assert proc.returncode == 1, proc.stdout
    assert "`qa` has no row" in proc.stdout
    assert "numbering is not 1..15" in proc.stdout


def test_stated_count_mismatch_fails(tmp_path):
    doc = _write_synced_doc(tmp_path, _load_module())
    text = doc.read_text(encoding="utf-8").replace(
        "There are **16 top-level subcommands**", "There are **15 top-level subcommands**"
    )
    doc.write_text(text, encoding="utf-8")

    code, message = _load_module().check_cli_doc_sync(REPO_ROOT, doc_path=doc)
    assert code == 1
    assert "stated count 15 != live count 16" in message


def test_missing_worktree_option_fails_naming_it(tmp_path):
    doc = _write_synced_doc(tmp_path, _load_module())
    text = doc.read_text(encoding="utf-8")
    assert "[--tick <value>]" in text
    doc.write_text(text.replace("[--tick <value>]", ""), encoding="utf-8")

    proc = _run_script(doc)
    assert proc.returncode == 1, proc.stdout
    assert "`worktree fleet` option `--tick` is undocumented" in proc.stdout


def test_flag_documented_under_the_wrong_subcommand_fails(tmp_path):
    """The historical defect: the exit-2 `--cell` contract sat under `qa list`."""
    doc = _write_synced_doc(tmp_path, _load_module())
    text = doc.read_text(encoding="utf-8")
    claim = "Reads or records QA rows (`qa list`)."
    assert claim in text
    doc.write_text(
        text.replace(claim, claim + "\n\nExit **2** when `--cell` is not `NAME=STATUS`."),
        encoding="utf-8",
    )

    proc = _run_script(doc)
    assert proc.returncode == 1, proc.stdout
    assert "`qa list` documents `--cell`" in proc.stdout


def test_extra_flag_in_a_usage_sketch_fails(tmp_path):
    """A flag claimed by a usage sketch counts too, not only a backticked one."""
    doc = _write_synced_doc(tmp_path, _load_module())
    text = doc.read_text(encoding="utf-8")
    list_line = next(line for line in text.splitlines() if line.startswith("gitreins qa list"))
    doc.write_text(text.replace(list_line, f"{list_line} [--cell <value>]"), encoding="utf-8")

    code, message = _load_module().check_cli_doc_sync(REPO_ROOT, doc_path=doc)
    assert code == 1
    assert "`qa list` documents `--cell`" in message


def test_dropped_worktree_table_row_fails(tmp_path):
    doc = _write_synced_doc(tmp_path, _load_module())
    text = doc.read_text(encoding="utf-8")
    assert "| `clean` | fixture |" in text
    doc.write_text(text.replace("| `clean` | fixture |\n", ""), encoding="utf-8")

    code, message = _load_module().check_cli_doc_sync(REPO_ROOT, doc_path=doc)
    assert code == 1
    assert "`worktree clean` has no row" in message


def test_missing_doc_fails_loudly(tmp_path):
    code, message = _load_module().check_cli_doc_sync(REPO_ROOT, doc_path=tmp_path / "absent.md")
    assert code == 1
    assert "not found" in message
