"""Version identity tests — GR-GAP-030.

Asserts the single source of truth: gitreins.__version__ and the CLI --version
output both match the version declared in pyproject.toml (so the test does not
rot on the next version bump).
"""

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib  # type: ignore[import-not-found]
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)["project"]["version"]


def test_gitreins_module_reexports_version():
    """gitreins.__version__ matches the pyproject.toml version."""
    import gitreins

    assert gitreins.__version__ == _pyproject_version()


def test_cli_version_flag_matches_pyproject():
    """`gitreins --version` prints the pyproject.toml version."""
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", "")
    if str(PROJECT_ROOT) not in env["PYTHONPATH"]:
        env["PYTHONPATH"] = str(PROJECT_ROOT) + (
            ":" + env["PYTHONPATH"] if env["PYTHONPATH"] else ""
        )
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "gitreins" / "cli.py"), "--version"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"gitreins {_pyproject_version()}"
