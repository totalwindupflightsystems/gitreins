"""Hermetic tests for Check C of scripts/check_docs_drift.py (GR-141).

Check C: every "N tool(s)" claim about the evaluator in docs/architecture.md
AND docs/evaluator-loop.md must equal the number of tools defined by
EVALUATOR_TOOLS in engine/evaluator.py, which the script counts by
ast-parsing the source (no package import, no pytest spawn).

Same discipline as tests/test_docs_drift.py: never reads the live docs as a
check subject — every case builds a throwaway repo tree under tmp_path (fake
docs + a fake engine/evaluator.py with a known tool count) and points the
script at it via --repo-root, both as a subprocess and through the importable
functions.
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "check_docs_drift.py"

BANNER = (
    "> ✅ **v0.12.1** — release banner, {p} tests pass / {f} test files, verified by collection.\n"
)
TECH_STACK = "- **Test suite:** {p} tests across {f} test files (collection total).\n"


def _fake_evaluator(n_tools, body=None):
    """A fake engine/evaluator.py defining EVALUATOR_TOOLS with n_tools entries."""
    if body is not None:
        return body
    entries = "".join(
        '    {"type": "function", "function": {"name": "tool_%d", "description": "d"}},\n' % i
        for i in range(1, n_tools + 1)
    )
    return "# fake evaluator source\nEVALUATOR_TOOLS = [\n" + entries + "]\n"


def _write_repo(
    tmp_path,
    n_tools=3,
    evaluator_body=None,
    with_evaluator=True,
    architecture_body=None,
    loop_body=None,
    dirname="repo",
):
    """Build a throwaway repo: synced README/pyproject/tests + docs + engine.

    architecture_body / loop_body default to docs that claim the same tool
    count as the fake evaluator (a synced tree). Pass explicit bodies to seed
    drift. Returns the repo root.
    """
    root = tmp_path / dirname
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[build-system]\nrequires = ["setuptools"]\n[project]\nname = "gitreins"\nversion = "0.12.1"\n',
        encoding="utf-8",
    )
    (root / "README.md").write_text(
        BANNER.format(p=2, f=1) + "\n" + TECH_STACK.format(p=2, f=1), encoding="utf-8"
    )
    tests_dir = root / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_sample.py").write_text(
        "def test_one():\n    assert True\ndef test_two():\n    assert True\n",
        encoding="utf-8",
    )
    if architecture_body is None:
        architecture_body = (
            "# Architecture (fixture)\n"
            "\n"
            "### 2. MCP Server\n"
            "stdio transport exposing 13 tools:\n"
            "\n"
            "### 4. Agentic Evaluator\n"
            f"An LLM-powered agentic loop with {n_tools} tools (`engine/evaluator.py`):\n"
        )
    docs_dir = root / "docs"
    docs_dir.mkdir()
    (docs_dir / "architecture.md").write_text(architecture_body, encoding="utf-8")
    if loop_body is None:
        loop_body = (
            "## Evaluation Tools (%d)\n" % n_tools
            + f"\nAll {n_tools} tools are defined in `engine/evaluator.py` (`EVALUATOR_TOOLS`).\n"
            + "\n### Repo Inspection (2 tools)\n"
            + "\nOnly these tools; no MCP bridge tool.\n"
        )
    (docs_dir / "evaluator-loop.md").write_text(loop_body, encoding="utf-8")
    if with_evaluator:
        engine_dir = root / "engine"
        engine_dir.mkdir()
        (engine_dir / "evaluator.py").write_text(
            _fake_evaluator(n_tools, body=evaluator_body), encoding="utf-8"
        )
    return root


def _load_module():
    spec = importlib.util.spec_from_file_location("check_docs_drift", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_script(root, extra_args=()):
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--repo-root", str(root), *extra_args],
        capture_output=True,
        text=True,
    )


# ---------------------------------------------------------------------------
# match / mismatch / missing claim / malformed source — the four brief cases.
# ---------------------------------------------------------------------------


def test_tool_count_match_exits_zero(tmp_path):
    root = _write_repo(tmp_path, n_tools=3)
    proc = _run_script(root)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "evaluator tool claims match the 3 defined EVALUATOR_TOOLS" in proc.stdout

    code, message = _load_module().check_docs_drift(root)
    assert code == 0
    assert "3" in message


def test_tool_count_mismatch_fails_with_both_numbers(tmp_path):
    """Docs claim 7 tools, the fake evaluator defines 3 -> exit 1 naming the
    doc, the line, and both numbers."""
    root = _write_repo(tmp_path, n_tools=3)
    arch = root / "docs" / "architecture.md"
    arch.write_text(
        "# Architecture (fixture)\n"
        "\n"
        "### 4. Agentic Evaluator\n"
        "An LLM-powered agentic loop with 7 tools (`engine/evaluator.py`):\n",
        encoding="utf-8",
    )
    proc = _run_script(root)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    out = proc.stdout
    assert "docs/architecture.md:4" in out
    assert "7 tools" in out
    assert "3 EVALUATOR_TOOLS" in out

    code, message = _load_module().check_docs_drift(root)
    assert code == 1
    assert "7" in message and "3" in message
    assert "docs/architecture.md:4" in message


def test_mismatch_in_evaluator_loop_fails(tmp_path):
    root = _write_repo(
        tmp_path,
        n_tools=3,
        loop_body=(
            "## Evaluation Tools (3)\n"
            "\nAll 4 tools are defined in `engine/evaluator.py` (`EVALUATOR_TOOLS`).\n"
        ),
    )
    proc = _run_script(root)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "docs/evaluator-loop.md:3" in proc.stdout
    assert "4 tools" in proc.stdout and "3 EVALUATOR_TOOLS" in proc.stdout


def test_missing_tool_claim_passes(tmp_path):
    """No doc claims a tool count -> nothing to check -> exit 0."""
    root = _write_repo(
        tmp_path,
        n_tools=3,
        architecture_body="# Architecture\n\nNo counts claimed here at all.\n",
        loop_body="# Evaluator loop\n\nNo counts claimed here either.\n",
    )
    proc = _run_script(root)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_malformed_evaluator_source_fails(tmp_path):
    """Unparseable engine/evaluator.py -> FAIL, never green on doubt."""
    root = _write_repo(tmp_path, n_tools=3, evaluator_body="EVALUATOR_TOOLS = [\n")
    proc = _run_script(root)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "engine/evaluator.py" in proc.stdout
    assert "could not parse" in proc.stdout

    code, message = _load_module().check_docs_drift(root)
    assert code == 1
    assert "could not parse" in message


def test_missing_assignment_fails(tmp_path):
    root = _write_repo(tmp_path, n_tools=3, evaluator_body="OTHER_TOOLS = []\n")
    proc = _run_script(root)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "no EVALUATOR_TOOLS assignment" in proc.stdout


def test_empty_evaluator_tools_fails(tmp_path):
    """Zero parsed tools means the parser or the source is wrong -> FAIL."""
    root = _write_repo(tmp_path, n_tools=0)
    proc = _run_script(root)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "0 tools" in proc.stdout

    code, message = _load_module().check_evaluator_tool_claims(root)
    assert code == 1


# ---------------------------------------------------------------------------
# Scoping: MCP-server counts, subsection tallies, and "advertises N of them"
# must not be mistaken for evaluator-surface claims.
# ---------------------------------------------------------------------------


def test_mcp_server_tool_count_is_out_of_scope(tmp_path):
    """'exposing 13 tools' sits under the MCP Server heading, not the
    evaluator's — it must be ignored even though 13 != 3."""
    root = _write_repo(
        tmp_path,
        n_tools=3,
        architecture_body=(
            "# Architecture (fixture)\n"
            "\n"
            "### 2. MCP Server\n"
            "stdio transport exposing 13 tools:\n"
            "\n"
            "### 4. Agentic Evaluator\n"
            "An LLM-powered agentic loop, tools listed below.\n"
        ),
    )
    proc = _run_script(root)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_subsection_tallies_are_out_of_scope(tmp_path):
    """'### Repo Inspection (2 tools)' is a partition of the surface, not the
    surface size: only heading-level 'Evaluation Tools (N)' claims are checked."""
    root = _write_repo(
        tmp_path,
        n_tools=3,
        loop_body=(
            "## Evaluation Tools (3)\n"
            "\nAll 3 tools are defined in `engine/evaluator.py` (`EVALUATOR_TOOLS`).\n"
            "\n### Repo Inspection (2 tools)\n"
            "\n### Diagnostics (1 tools)\n"
        ),
    )
    proc = _run_script(root)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_heading_form_claim_is_checked(tmp_path):
    root = _write_repo(tmp_path, n_tools=3, loop_body="## Evaluation Tools (5)\n")
    proc = _run_script(root)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "docs/evaluator-loop.md:1" in proc.stdout
    assert "5" in proc.stdout and "3" in proc.stdout


def test_advertises_n_of_them_is_not_a_claim(tmp_path):
    root = _write_repo(
        tmp_path,
        n_tools=3,
        loop_body=(
            "## Evaluation Tools (3)\n"
            "\nAll 3 tools are defined in `engine/evaluator.py` (`EVALUATOR_TOOLS`).\n"
            "The judge advertises **2** of them by default.\n"
        ),
    )
    proc = _run_script(root)
    assert proc.returncode == 0, proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# Missing authority + mode coverage.
# ---------------------------------------------------------------------------


def test_missing_evaluator_source_without_claims_skips(tmp_path):
    root = _write_repo(
        tmp_path,
        with_evaluator=False,
        architecture_body="# Architecture\n\nNo tool counts claimed.\n",
        loop_body="# Loop\n\nNone here either.\n",
    )
    proc = _run_script(root)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "skipped" in proc.stdout


def test_missing_evaluator_source_with_claims_fails(tmp_path):
    """Claims exist but the authority for the count is gone -> FAIL."""
    root = _write_repo(tmp_path, with_evaluator=False)
    proc = _run_script(root)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "engine/evaluator.py" in proc.stdout and "missing" in proc.stdout


def test_check_c_runs_in_static_mode(tmp_path):
    root = _write_repo(tmp_path, n_tools=3)
    arch = root / "docs" / "architecture.md"
    arch.write_text(
        "# Architecture (fixture)\n"
        "\n"
        "### 4. Agentic Evaluator\n"
        "An LLM-powered agentic loop with 7 tools (`engine/evaluator.py`):\n",
        encoding="utf-8",
    )
    proc = _run_script(root, extra_args=("--static",))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "7 tools" in proc.stdout and "3 EVALUATOR_TOOLS" in proc.stdout


def test_count_evaluator_tools_parses_fake_source(tmp_path):
    root = _write_repo(tmp_path, n_tools=3)
    count, reason = _load_module().count_evaluator_tools(root)
    assert reason is None
    assert count == 3
