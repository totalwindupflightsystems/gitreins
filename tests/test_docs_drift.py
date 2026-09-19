"""Hermetic tests for scripts/check_docs_drift.py (GR-GAP-052, GR-GAP-061).

Never reads the live README.md or pyproject.toml as a check subject: every
case builds its own throwaway repo tree under tmp_path — including a REAL
tests/ directory whose collection the script measures by running pytest as a
subprocess (the collector is never stubbed in tests that claim to prove live
behaviour) — and points the script at it via --repo-root. Each scenario is
exercised two ways where it matters: through the importable functions (module
loaded by path) and through the real exit code (script run as a subprocess
with a list of args, no shell).
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "check_docs_drift.py"

BANNER = "> ✅ **v{version}** — release banner, {p} tests pass / {f} test files, verified by collection.\n"
TECH_STACK = "- **Test suite:** {p} tests across {f} test files (collection total).\n"
CONTRIBUTING_CLAIM = "All tests must pass. Currently **{p} tests across {f}\ntest files** (canonical count in README).\n"


def _write_repo(
    tmp_path,
    banner_version="0.12.1",
    pyproject_version="0.12.1",
    readme_body=None,
    pyproject_body=None,
    n_tests=12,
    n_files=3,
    contributing_body=None,
    dirname="repo",
    with_tests=True,
):
    """Create a throwaway repo tree: README.md + pyproject.toml + a real tests/.

    The tests/ directory holds exactly ``n_tests`` trivial test functions
    spread over ``n_files`` files, so the script's live collection measures a
    known number. Defaults match the README claims (12 / 3) so a plain fixture
    is a synced tree.
    """
    root = tmp_path / dirname
    root.mkdir()
    if pyproject_body is None:
        pyproject_body = (
            "[build-system]\n"
            'requires = ["setuptools"]\n'
            "[project]\n"
            'name = "gitreins"\n'
            f'version = "{pyproject_version}"\n'
        )
    if readme_body is None:
        readme_body = (
            BANNER.format(version=banner_version, p=12, f=3) + "\n" + TECH_STACK.format(p=12, f=3)
        )
    (root / "pyproject.toml").write_text(pyproject_body, encoding="utf-8")
    (root / "README.md").write_text(readme_body, encoding="utf-8")
    if contributing_body is not None:
        (root / "CONTRIBUTING.md").write_text(contributing_body, encoding="utf-8")
    if with_tests:
        tests_dir = root / "tests"
        tests_dir.mkdir()
        per_file = [n_tests // n_files] * n_files
        for i in range(n_tests % n_files):
            per_file[i] += 1
        for i, count in enumerate(per_file, start=1):
            body = "".join(f"def test_{j}():\n    assert True\n" for j in range(count))
            (tests_dir / f"test_sample{i}.py").write_text(body, encoding="utf-8")
    return root


def _load_module():
    """Load the script as an importable module (importlib, by file path)."""
    spec = importlib.util.spec_from_file_location("check_docs_drift", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_script(root, extra_args=()):
    """Run the script as a subprocess against a fixture tree."""
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--repo-root", str(root), *extra_args],
        capture_output=True,
        text=True,
    )


def test_matching_version_and_counts_exit_zero(tmp_path):
    root = _write_repo(tmp_path)
    proc = _run_script(root)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "OK" in proc.stdout
    assert "0.12.1" in proc.stdout

    code, message = _load_module().check_docs_drift(root)
    assert code == 0
    assert "0.12.1" in message


def test_banner_version_mismatch_fails_naming_both(tmp_path):
    root = _write_repo(tmp_path, banner_version="9.9.9", pyproject_version="0.12.1")
    proc = _run_script(root)
    assert proc.returncode == 1, proc.stdout
    assert "9.9.9" in proc.stdout
    assert "0.12.1" in proc.stdout
    assert "README.md" in proc.stdout

    code, message = _load_module().check_docs_drift(root)
    assert code == 1
    assert "9.9.9" in message and "0.12.1" in message


def test_conflicting_tests_pass_claims_fail(tmp_path):
    readme = (
        BANNER.format(version="0.12.1", p=12, f=3)
        + "\n"
        + "Historical notes: 15 tests pass across the old suite.\n"
    )
    root = _write_repo(tmp_path, readme_body=readme)
    proc = _run_script(root)
    assert proc.returncode == 1, proc.stdout
    assert "12" in proc.stdout and "15" in proc.stdout
    assert "tests pass" in proc.stdout

    code, message = _load_module().check_docs_drift(root)
    assert code == 1
    assert "12" in message and "15" in message


def test_conflicting_test_files_claims_fail(tmp_path):
    readme = (
        BANNER.format(version="0.12.1", p=12, f=3)
        + "\n"
        + "Historical notes: 12 tests across 7 test files.\n"
    )
    root = _write_repo(tmp_path, readme_body=readme)
    proc = _run_script(root)
    assert proc.returncode == 1, proc.stdout
    assert "3" in proc.stdout and "7" in proc.stdout
    assert "test files" in proc.stdout


def test_missing_banner_fails(tmp_path):
    readme = "# Title\n\n12 tests pass / 3 test files.\n\n" + TECH_STACK.format(p=12, f=3)
    root = _write_repo(tmp_path, readme_body=readme)
    proc = _run_script(root)
    assert proc.returncode == 1, proc.stdout
    assert "banner" in proc.stdout

    code, message = _load_module().check_docs_drift(root)
    assert code == 1
    assert "banner" in message


def test_missing_pyproject_version_fails(tmp_path):
    pyproject = '[project]\nname = "gitreins"\ndescription = "no version here"\n'
    root = _write_repo(tmp_path, pyproject_body=pyproject)
    proc = _run_script(root)
    assert proc.returncode == 1, proc.stdout
    assert "version" in proc.stdout

    code, message = _load_module().check_docs_drift(root)
    assert code == 1
    assert "version" in message


def test_tests_across_disagreeing_with_tests_pass_fails(tmp_path):
    readme = BANNER.format(version="0.12.1", p=12, f=3) + "\n" + TECH_STACK.format(p=14, f=3)
    root = _write_repo(tmp_path, readme_body=readme)
    proc = _run_script(root)
    assert proc.returncode == 1, proc.stdout
    assert "12" in proc.stdout and "14" in proc.stdout
    assert "tests across" in proc.stdout


# ---------------------------------------------------------------------------
# GR-GAP-061: the script itself must run the live collection and fail on
# documented counts that disagree with it — the defect was a green "counts
# consistent" without ever measuring.
# ---------------------------------------------------------------------------


def test_live_synced_fixture_exits_zero_naming_measured_numbers(tmp_path):
    """2 tests / 1 file in the fixture, docs claim the same -> exit 0, message
    names the numbers the script actually measured."""
    readme = BANNER.format(version="0.12.1", p=2, f=1) + "\n" + TECH_STACK.format(p=2, f=1)
    root = _write_repo(tmp_path, readme_body=readme, n_tests=2, n_files=1)
    proc = _run_script(root)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "2 tests" in proc.stdout
    assert "1 test file" in proc.stdout
    assert "live collection" in proc.stdout


def test_live_stale_readme_count_fails_naming_line_and_both_numbers(tmp_path):
    """Docs claim 7 tests, the fixture really collects 2 -> exit 1 naming
    README.md, the line number, the stale 7 and the measured 2."""
    readme = BANNER.format(version="0.12.1", p=7, f=1) + "\n" + TECH_STACK.format(p=7, f=1)
    root = _write_repo(tmp_path, readme_body=readme, n_tests=2, n_files=1)
    proc = _run_script(root)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    out = proc.stdout
    assert "README.md:1" in out
    assert "7" in out and "2" in out

    code, message = _load_module().check_docs_drift(root)
    assert code == 1
    assert "README.md" in message and "1" in message


def test_live_stale_contributing_count_fails_naming_contributing_line(tmp_path):
    """README synced, only CONTRIBUTING.md stale -> exit 1 names CONTRIBUTING.md
    with its line number (CONTRIBUTING.md claims used to be unchecked)."""
    contributing = "# Contributing\n\n" + CONTRIBUTING_CLAIM.format(p=7, f=1)
    root = _write_repo(
        tmp_path,
        readme_body=BANNER.format(version="0.12.1", p=2, f=1) + "\n" + TECH_STACK.format(p=2, f=1),
        contributing_body=contributing,
        n_tests=2,
        n_files=1,
    )
    proc = _run_script(root)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    out = proc.stdout
    assert "CONTRIBUTING.md:3" in out
    assert "7" in out and "2" in out


def test_newline_split_claim_is_still_compared(tmp_path):
    """The CONTRIBUTING.md:35 shape: the claim phrase wraps across a newline.
    A literal-space regex silently skips it; the gate must not."""
    synced = "# Contributing\n\n" + CONTRIBUTING_CLAIM.format(p=2, f=1)
    root = _write_repo(
        tmp_path,
        readme_body=BANNER.format(version="0.12.1", p=2, f=1) + "\n" + TECH_STACK.format(p=2, f=1),
        contributing_body=synced,
        n_tests=2,
        n_files=1,
        dirname="synced",
    )
    proc = _run_script(root)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    stale = "# Contributing\n\n" + CONTRIBUTING_CLAIM.format(p=7, f=1)
    root2 = _write_repo(
        tmp_path,
        readme_body=BANNER.format(version="0.12.1", p=2, f=1) + "\n" + TECH_STACK.format(p=2, f=1),
        contributing_body=stale,
        n_tests=2,
        n_files=1,
        dirname="stale",
    )
    proc2 = _run_script(root2)
    assert proc2.returncode == 1, proc2.stdout + proc2.stderr
    assert "CONTRIBUTING.md:3" in proc2.stdout


def test_unmeasurable_collection_fails_never_green(tmp_path):
    """No tests/ directory at all: collection cannot be measured -> exit 1 and
    the message must not certify anything (no 'consistent')."""
    root = _write_repo(
        tmp_path,
        readme_body=BANNER.format(version="0.12.1", p=2, f=1) + "\n" + TECH_STACK.format(p=2, f=1),
        with_tests=False,
    )
    proc = _run_script(root)
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "consistent" not in proc.stdout.lower()
    assert "could not be measured" in proc.stdout

    code, message = _load_module().check_docs_drift(root)
    assert code == 1
    assert "consistent" not in message.lower()


def test_static_flag_skips_live_and_says_so(tmp_path):
    """--static on a stale-count fixture exits 0, never says 'consistent', and
    says the live collection was NOT compared."""
    readme = BANNER.format(version="0.12.1", p=7, f=1) + "\n" + TECH_STACK.format(p=7, f=1)
    root = _write_repo(tmp_path, readme_body=readme, n_tests=2, n_files=1)
    proc = _run_script(root, extra_args=("--static",))
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout
    assert "consistent" not in out.lower()
    assert "not compared" in out.lower()

    code, message = _load_module().check_docs_drift(root, static_only=True)
    assert code == 0
    assert "consistent" not in message.lower()
    assert "not compared" in message.lower()


def test_static_mode_still_catches_sibling_claim_conflict(tmp_path):
    """Without a measurement, the static path keeps the internal-agreement
    check: two claims in one doc that disagree still fail."""
    readme = (
        BANNER.format(version="0.12.1", p=12, f=3)
        + "\n"
        + "Historical notes: 15 tests pass across the old suite.\n"
    )
    root = _write_repo(tmp_path, readme_body=readme, with_tests=False)
    proc = _run_script(root, extra_args=("--static",))
    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert "12" in proc.stdout and "15" in proc.stdout


def test_collect_live_counts_parses_fixture_tree(tmp_path):
    """The collector returns (count, files) measured from a real subprocess
    pytest run: 5 tests over 2 files."""
    root = _write_repo(tmp_path, n_tests=5, n_files=2)
    live, reason = _load_module().collect_live_counts(root)
    assert reason is None
    assert live == (5, 2)
