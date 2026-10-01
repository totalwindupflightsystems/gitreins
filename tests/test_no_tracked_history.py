"""Repo-hygiene gate: no .gitreins/history path may be git-tracked.

`.gitignore` ignores `.gitreins/history/` (verdict files are runtime
artifacts whose canonical home is the verdict ref), and the docs promise a
fresh clone has no local `.gitreins/history/` directory. Two legacy verdict
directories predated the ignore rule and stayed tracked, silently breaking
that promise (DF-GITREINS-POC-58); this test refuses to let them (or any
successor) come back.
"""

import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_no_gitreins_history_paths_are_tracked():
    if shutil.which("git") is None:
        import pytest

        pytest.skip("git is unavailable")
    out = subprocess.run(
        ["git", "ls-files", ".gitreins/history"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert out.returncode == 0, out.stderr
    tracked = [line for line in out.stdout.splitlines() if line.strip()]
    assert tracked == [], (
        "git-tracked files under .gitreins/history/ break the fresh-clone "
        f"guarantee in README.md; untrack them (git rm --cached): {tracked}"
    )
