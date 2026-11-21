"""Repo-hygiene gate: `.gitignore` may never hide a path that git TRACKS.

`.gitignore` governs untracked paths only, so a rule that covers a tracked path
is silent inside the checkout: `git status` stays clean, `git add` of that path
is refused, and every ignore-driven workflow built from HEAD quietly diverges
from HEAD. Measured on QA-GITREINS-11 (2026-11-13): the reconstruction
``git archive HEAD | tar -x`` + ``git init`` + ``git add -A`` staged 601 of the
606 tracked paths and dropped ``apps/github_app/`` entirely, because
``.gitignore`` carried a bare ``apps/`` (meant for Node bootstrap debris) while
``apps/`` is also this repository's tracked Python package directory.

``test_every_tracked_path_survives_archive_reconstruction`` reproduces that
workflow generically — the staged set of the reconstruction must equal
``git ls-tree -r --name-only HEAD`` exactly — so ANY future rule that hides a
tracked path fails here instead of in a consumer's archive.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: Variables that would retarget git at the OUTER repository (the pre-commit
#: hook runs this module with a scratch index, and CI may export a work tree).
_GIT_REDIRECT_VARS = (
    "GIT_DIR",
    "GIT_COMMON_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_NAMESPACE",
    "GIT_PREFIX",
    "GIT_CEILING_DIRECTORIES",
)


def _git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_REDIRECT_VARS}
    # Never let a commit hook / test harness spawn further hooks from our probes.
    env.pop("GIT_REFLOG_ACTION", None)
    return env


def _run(cmd: list[str], cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )


def _require_git_checkout() -> None:
    if shutil.which("git") is None:
        pytest.skip("git is unavailable")
    probe = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if probe.returncode != 0:
        pytest.skip(f"{REPO_ROOT} is not a git checkout: {probe.stderr.strip()}")


def test_every_tracked_path_survives_archive_reconstruction(tmp_path: Path) -> None:
    """`git archive HEAD` + `git init` + `git add -A` must stage exactly HEAD.

    This is the QA-GITREINS-11 reproduction, generalised: any ignore rule that
    hides a tracked path makes the reconstructed tree a different tree — it is
    what a sandbox/ship/test-fresh run ends up grading or shipping — and the
    mismatch is invisible in the source checkout itself.

    The working tree's `.gitignore` is overlaid onto the reconstruction on
    purpose: the pre-commit guard runs this module against the PARENT commit's
    tree, which still carries the pre-fix rules, so measuring that tree would
    fail the very commit that fixes it. Once the fix is committed the overlay is
    a no-op and this is byte-for-byte the QA row's command.
    """
    _require_git_checkout()
    if shutil.which("tar") is None:
        pytest.skip("tar is unavailable")

    env = _git_env()

    listing = _run(["git", "ls-tree", "-r", "--name-only", "-z", "HEAD"], REPO_ROOT, env)
    assert listing.returncode == 0, listing.stderr
    tracked = sorted(path for path in listing.stdout.split("\0") if path)
    assert tracked, "HEAD tracks no paths — the comparison below would be vacuous"

    archive = tmp_path / "head.tar"
    with open(archive, "wb") as handle:
        packed = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "archive", "HEAD"],
            env=env,
            stdout=handle,
            stderr=subprocess.PIPE,
            timeout=600,
        )
    assert packed.returncode == 0, packed.stderr.decode("utf-8", "replace")

    tree = tmp_path / "tree"
    tree.mkdir()
    extracted = _run(["tar", "-x", "-f", str(archive), "-C", str(tree)], tmp_path, env)
    assert extracted.returncode == 0, extracted.stderr

    # The reconstruction must be graded with THIS checkout's ignore rules.
    shutil.copyfile(REPO_ROOT / ".gitignore", tree / ".gitignore")

    # Hermetic git: a machine-global excludes file (or a site template dir) must
    # not hide tracked paths and turn a healthy tree into a red test.
    empty_ignore = tmp_path / "no-global-ignore"
    empty_ignore.write_text("", encoding="utf-8")
    empty_templates = tmp_path / "no-templates"
    empty_templates.mkdir()
    git = [
        "git",
        "-c",
        f"core.excludesFile={empty_ignore}",
        "-c",
        f"init.templateDir={empty_templates}",
    ]

    for cmd in (["init", "-q"], ["add", "-A"]):
        result = _run([*git, *cmd], tree, env)
        assert result.returncode == 0, f"git {' '.join(cmd)} failed: {result.stderr}"

    staged_out = _run([*git, "ls-files", "-z"], tree, env)
    assert staged_out.returncode == 0, staged_out.stderr
    staged = sorted(path for path in staged_out.stdout.split("\0") if path)

    dropped = sorted(set(tracked) - set(staged))
    invented = sorted(set(staged) - set(tracked))
    assert not dropped, (
        "the archive reconstruction staged "
        f"{len(staged)} of {len(tracked)} tracked paths — an ignore rule hides "
        f"them from `git add -A`: {dropped}"
    )
    assert not invented, (
        f"the archive reconstruction staged {len(staged)} paths but HEAD tracks "
        f"{len(tracked)} — unexpected paths: {invented}"
    )


def test_tracked_apps_package_is_not_ignored() -> None:
    """QA-GITREINS-11: the tracked ``apps/`` package must not be ignore-covered.

    ``git check-ignore`` consults the index, so it reports a TRACKED path as not
    ignored even while a rule covers it — the rule only refuses paths git does
    not already know. ``--no-index`` asks the question that matters here: would
    a fresh ``git add -A`` of this path be refused?
    """
    _require_git_checkout()
    env = _git_env()

    under_apps = _run(["git", "ls-files", "-z", "--", "apps"], REPO_ROOT, env)
    assert under_apps.returncode == 0, under_apps.stderr
    tracked = [path for path in under_apps.stdout.split("\0") if path]
    assert tracked, "nothing under apps/ is tracked — the repository moved it?"

    for path in tracked:
        verdict = _run(
            ["git", "-C", str(REPO_ROOT), "check-ignore", "-v", "--no-index", path],
            REPO_ROOT,
            env,
        )
        assert verdict.returncode == 1, (
            f"git refuses to add the TRACKED path {path!r} — an ignore rule "
            f"covers it ({verdict.stdout.strip() or verdict.stderr.strip()})"
        )
