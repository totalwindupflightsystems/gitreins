"""Hermetic tests for scripts/check_docs_drift.py (GR-GAP-052).

Never reads the live README.md or pyproject.toml as a check subject: every
case builds its own throwaway repo tree under tmp_path and points the script
at it via --repo-root. Each scenario is exercised two ways — through the
importable functions (module loaded by path) and through the real exit code
(script run as a subprocess with a list of args, no shell).
"""

import importlib.util
import subprocess
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "check_docs_drift.py"

BANNER = "> ✅ **v{version}** — release banner, {p} tests pass / {f} test files, verified by collection.\n"
TECH_STACK = "- **Test suite:** {p} tests across {f} test files (collection total).\n"


def _write_repo(
    tmp_path,
    banner_version="0.12.1",
    pyproject_version="0.12.1",
    readme_body=None,
    pyproject_body=None,
):
    """Create a throwaway repo tree with README.md + pyproject.toml."""
    root = tmp_path / "repo"
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
    return root


def _load_module():
    """Load the script as an importable module (importlib, by file path)."""
    spec = importlib.util.spec_from_file_location("check_docs_drift", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_script(root):
    """Run the script as a subprocess against a fixture tree."""
    return subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--repo-root", str(root)],
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
