"""GR-162: `gitreins doctor` answers repo health, not just the gitleaks allowlist.

The failure mode this pins: an install whose hooks git never runs (because
`core.hooksPath` points elsewhere) used to pass doctor with flying colours while
enforcing nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


from engine import doctor, install_manifest
from engine.install_manifest import Artifact

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK_BODY = "#!/bin/sh\n# gitreins-managed — test hook\nexit 0\n"


def _repo(tmp_path) -> str:
    workdir = tmp_path / "repo"
    (workdir / ".git" / "hooks").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=str(workdir), check=True)
    return str(workdir)


def _install(workdir: str, *, hooks_path: str | None = None) -> None:
    """A minimal healthy install: managed hooks, config, manifest."""
    if hooks_path:
        subprocess.run(["git", "config", "core.hooksPath", hooks_path], cwd=workdir, check=True)
    artifacts = []
    for name in install_manifest.HOOK_NAMES:
        path = install_manifest._hook_path(workdir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(HOOK_BODY)
        os.chmod(path, 0o755)
        artifacts.append(
            Artifact(os.path.relpath(path, workdir), "hook", install_manifest.sha256_file(path))
        )
    config = os.path.join(workdir, ".gitreins", "config.yaml")
    os.makedirs(os.path.dirname(config), exist_ok=True)
    with open(config, "w") as handle:
        handle.write("guards:\n  secrets: true\n")
    artifacts.append(
        Artifact(".gitreins/config.yaml", "config", install_manifest.sha256_file(config))
    )
    install_manifest.record_install(workdir, artifacts)


def _check(checks, name):
    return next(check for check in checks if check.name == name)


def test_not_installed_is_recognised(tmp_path) -> None:
    workdir = _repo(tmp_path)
    assert doctor.is_installed(workdir) is False


def test_healthy_install_passes_every_check(tmp_path) -> None:
    workdir = _repo(tmp_path)
    _install(workdir)
    checks = doctor.run_checks(workdir)
    assert not doctor.has_failures(checks), doctor.format_checks(checks)
    assert _check(checks, "config").status == "ok"
    assert _check(checks, "hooks").status == "ok"
    assert _check(checks, "manifest").status == "ok"


def test_missing_hook_fails(tmp_path) -> None:
    workdir = _repo(tmp_path)
    _install(workdir)
    os.remove(os.path.join(workdir, ".git", "hooks", "pre-commit"))
    checks = doctor.run_checks(workdir)
    assert _check(checks, "hooks").status == "fail"
    assert doctor.has_failures(checks)


def test_non_executable_hook_fails(tmp_path) -> None:
    workdir = _repo(tmp_path)
    _install(workdir)
    os.chmod(os.path.join(workdir, ".git", "hooks", "pre-push"), 0o644)
    checks = doctor.run_checks(workdir)
    assert _check(checks, "hooks").status == "fail"


def test_core_hookspath_pointing_at_an_empty_dir_fails(tmp_path) -> None:
    """The disarm-by-config case: hooks exist but git never reaches them."""
    workdir = _repo(tmp_path)
    _install(workdir)
    subprocess.run(["git", "config", "core.hooksPath", ".githooks"], cwd=workdir, check=True)
    os.makedirs(os.path.join(workdir, ".githooks"), exist_ok=True)

    checks = doctor.run_checks(workdir)

    hooks = _check(checks, "hooks")
    assert hooks.status == "fail"
    assert "never fire" in hooks.hint


def test_hookspath_with_our_hook_installed_there_passes(tmp_path) -> None:
    workdir = _repo(tmp_path)
    _install(workdir, hooks_path=".githooks")
    checks = doctor.run_checks(workdir)
    assert _check(checks, "hooks").status == "ok", _check(checks, "hooks").render()


def test_unreadable_config_key_warns(tmp_path) -> None:
    workdir = _repo(tmp_path)
    _install(workdir)
    with open(os.path.join(workdir, ".gitreins", "config.yaml"), "a") as handle:
        handle.write("  lintt: true\n")
    checks = doctor.run_checks(workdir)
    config = _check(checks, "config")
    assert config.status == "warn"
    assert "config-validate" in config.hint


def test_broken_yaml_config_fails(tmp_path) -> None:
    workdir = _repo(tmp_path)
    _install(workdir)
    with open(os.path.join(workdir, ".gitreins", "config.yaml"), "w") as handle:
        handle.write("guards: [oops\n")
    checks = doctor.run_checks(workdir)
    assert _check(checks, "config").status == "fail"


def test_manifest_reports_an_artifact_that_disappeared(tmp_path) -> None:
    workdir = _repo(tmp_path)
    _install(workdir)
    os.remove(os.path.join(workdir, ".gitreins", "config.yaml"))
    checks = doctor.run_checks(workdir)
    manifest = _check(checks, "manifest")
    assert manifest.status == "warn"
    assert "gone" in manifest.detail


def test_manifest_reports_disabled_state(tmp_path) -> None:
    workdir = _repo(tmp_path)
    _install(workdir)
    install_manifest.disable(workdir)
    checks = doctor.run_checks(workdir)
    assert "disabled" in _check(checks, "manifest").detail


def test_summary_counts_failures_and_warnings(tmp_path) -> None:
    workdir = _repo(tmp_path)
    _install(workdir)
    os.remove(os.path.join(workdir, ".git", "hooks", "pre-commit"))
    text = doctor.format_checks(doctor.run_checks(workdir))
    assert "Summary: 1 failing" in text


def test_cli_json_emits_checks_and_exit_zero_when_healthy(tmp_path) -> None:
    workdir = _repo(tmp_path)
    _install(workdir)
    env = dict(os.environ, PYTHONPATH=REPO_ROOT)
    result = subprocess.run(
        [sys.executable, "-m", "gitreins", "doctor", "--json"],
        cwd=workdir,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    names = {entry["name"] for entry in payload}
    assert {"config", "hooks", "tools", "manifest", "history"} <= names


def test_cli_fails_when_hooks_are_missing(tmp_path) -> None:
    workdir = _repo(tmp_path)
    _install(workdir)
    os.remove(os.path.join(workdir, ".git", "hooks", "pre-commit"))
    env = dict(os.environ, PYTHONPATH=REPO_ROOT)
    result = subprocess.run(
        [sys.executable, "-m", "gitreins", "doctor"],
        cwd=workdir,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 1
    assert "hooks" in result.stdout


def test_cli_keeps_the_gitleaks_report_for_uninstalled_repos(tmp_path) -> None:
    """A repo with no install still gets the original allowlist answer."""
    workdir = _repo(tmp_path)
    env = dict(os.environ, PYTHONPATH=REPO_ROOT)
    result = subprocess.run(
        [sys.executable, "-m", "gitreins", "doctor"],
        cwd=workdir,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0
    assert "nothing to check" in result.stdout
