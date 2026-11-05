"""GR-159: `gitreins setup` is one guided path with a checkable definition of done.

Before this, getting a repo armed took four commands in a particular order, each
with its own preconditions, and none of them said when you were finished. "Done"
here means the health checks pass — i.e. the guard will actually run on the next
commit — not that the steps printed successfully.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _repo(tmp_path) -> str:
    workdir = tmp_path / "repo"
    workdir.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=str(workdir), check=True)
    return str(workdir)


def _run(workdir: str, *argv: str) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONPATH=REPO_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "gitreins", *argv],
        cwd=workdir,
        capture_output=True,
        text=True,
        env=env,
    )


def test_setup_arms_a_bare_repo_and_says_so(tmp_path) -> None:
    workdir = _repo(tmp_path)
    result = _run(workdir, "setup")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Setup complete" in result.stdout
    assert "guard will run on your next commit" in result.stdout

    assert os.path.isfile(os.path.join(workdir, ".gitreins", "config.yaml"))
    assert os.path.isfile(os.path.join(workdir, ".gitreins", "install-manifest.json"))
    assert os.path.isfile(os.path.join(workdir, ".git", "hooks", "pre-commit"))
    assert os.access(os.path.join(workdir, ".git", "hooks", "pre-commit"), os.X_OK)


def test_setup_ends_with_a_health_check_not_a_claim(tmp_path) -> None:
    """The verify section must actually run doctor's checks."""
    workdir = _repo(tmp_path)
    result = _run(workdir, "setup")
    assert "[verify]" in result.stdout
    assert "hooks" in result.stdout and "manifest" in result.stdout
    assert "All checks passed" in result.stdout or "healthy with" in result.stdout


def test_setup_is_idempotent(tmp_path) -> None:
    workdir = _repo(tmp_path)
    first = _run(workdir, "setup")
    assert first.returncode == 0, first.stdout + first.stderr
    with open(os.path.join(workdir, ".gitreins", "config.yaml")) as handle:
        config_after_first = handle.read()

    second = _run(workdir, "setup")
    assert second.returncode == 0, second.stdout + second.stderr
    with open(os.path.join(workdir, ".gitreins", "config.yaml")) as handle:
        assert handle.read() == config_after_first, "a second setup must not rewrite config"


def test_setup_preserves_an_explicit_setting(tmp_path) -> None:
    workdir = _repo(tmp_path)
    assert _run(workdir, "init").returncode == 0
    config_path = os.path.join(workdir, ".gitreins", "config.yaml")
    with open(config_path) as handle:
        text = handle.read()
    text = (
        text.replace("  secrets: true", "  secrets: false") if "  secrets: true" in text else text
    )
    with open(config_path, "w") as handle:
        handle.write(text)

    result = _run(workdir, "setup")
    assert result.returncode == 0, result.stdout + result.stderr
    with open(config_path) as handle:
        assert "  secrets: false" in handle.read(), "setup overwrote an explicit setting"


def test_setup_outside_git_exits_two(tmp_path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    result = _run(str(plain), "setup")
    assert result.returncode == 2
    assert "not a git repository" in result.stderr


def test_setup_json_reports_steps_and_checks(tmp_path) -> None:
    workdir = _repo(tmp_path)
    result = _run(workdir, "setup", "--json")
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert [step["step"] for step in payload["steps"]] == ["config", "hooks"]
    assert {check["name"] for check in payload["checks"]} >= {"config", "hooks", "manifest"}


def test_setup_json_failure_is_reported_not_raised(tmp_path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    result = _run(str(plain), "setup", "--json")
    assert result.returncode == 2
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert "not a git repository" in payload["error"]
