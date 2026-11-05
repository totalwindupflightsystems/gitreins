"""GR-161: install records what it owns; disable/enable/uninstall act on that record.

Owner rulings encoded here:
  * `uninstall` removes only the hook and the config;
  * `--purge` is the ONLY way history is removed;
  * `disable` disarms enforcement without deleting config or history, and is
    reversible with `enable`.
"""

from __future__ import annotations

import os
import subprocess
import sys


from engine import install_manifest
from engine.install_manifest import Artifact

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK_BODY = "#!/bin/sh\n# gitreins-managed — test hook\n"
USER_HOOK = "#!/bin/sh\necho user hook\n"


def _repo(tmp_path) -> str:
    workdir = tmp_path / "repo"
    (workdir / ".git" / "hooks").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=str(workdir), check=True)
    return str(workdir)


def _install_artifacts(workdir: str, *, backup: str | None = None) -> None:
    """Stand in for `gitreins install` without running its full side effects."""
    hook = os.path.join(workdir, ".git", "hooks", "pre-commit")
    with open(hook, "w") as handle:
        handle.write(HOOK_BODY)
    os.chmod(hook, 0o755)
    config = os.path.join(workdir, ".gitreins", "config.yaml")
    os.makedirs(os.path.dirname(config), exist_ok=True)
    if not os.path.isfile(config):
        with open(config, "w") as handle:
            handle.write("guards:\n  secrets: true\n")
    install_manifest.record_install(
        workdir,
        [
            Artifact(".git/hooks/pre-commit", "hook", install_manifest.sha256_file(hook), backup),
            Artifact(".gitreins/config.yaml", "config", install_manifest.sha256_file(config)),
        ],
    )


def test_install_manifest_records_owned_artifacts(tmp_path) -> None:
    workdir = _repo(tmp_path)
    _install_artifacts(workdir)
    manifest = install_manifest.read_manifest(workdir)
    paths = {a["path"] for a in manifest["artifacts"]}
    assert paths == {".git/hooks/pre-commit", ".gitreins/config.yaml"}
    assert manifest["disabled"] is False


def test_disable_removes_the_managed_hook_and_keeps_config_and_history(tmp_path) -> None:
    workdir = _repo(tmp_path)
    _install_artifacts(workdir)
    history = os.path.join(workdir, ".gitreins", "history")
    os.makedirs(history, exist_ok=True)

    steps = install_manifest.disable(workdir)

    assert not os.path.isfile(os.path.join(workdir, ".git/hooks/pre-commit"))
    assert os.path.isfile(os.path.join(workdir, ".gitreins", "config.yaml"))
    assert os.path.isdir(history)
    assert install_manifest.read_manifest(workdir)["disabled"] is True
    assert any("removed hook" in step for step in steps)


def test_disable_is_idempotent(tmp_path) -> None:
    workdir = _repo(tmp_path)
    _install_artifacts(workdir)
    install_manifest.disable(workdir)
    steps = install_manifest.disable(workdir)
    assert steps == ["already disabled — nothing to do"]


def test_disable_restores_a_displaced_user_hook(tmp_path) -> None:
    """The user's own hook comes back rather than being deleted with ours."""
    workdir = _repo(tmp_path)
    hook = os.path.join(workdir, ".git", "hooks", "pre-commit")
    with open(hook, "w") as handle:
        handle.write(USER_HOOK)
    backup_rel = install_manifest.backup_displaced(workdir, hook, "pre-commit")
    assert backup_rel == os.path.join(".gitreins", "backups", "pre-commit.pre-gitreins")
    _install_artifacts(workdir, backup=backup_rel)

    install_manifest.disable(workdir)

    with open(hook) as handle:
        assert handle.read() == USER_HOOK


def test_backup_displaced_never_buries_an_earlier_original(tmp_path) -> None:
    workdir = _repo(tmp_path)
    hook = os.path.join(workdir, ".git", "hooks", "pre-commit")
    with open(hook, "w") as handle:
        handle.write(USER_HOOK)
    first = install_manifest.backup_displaced(workdir, hook, "pre-commit")
    with open(hook, "w") as handle:
        handle.write("#!/bin/sh\necho second\n")
    second = install_manifest.backup_displaced(workdir, hook, "pre-commit")
    assert second != first
    with open(os.path.join(workdir, first)) as handle:
        assert handle.read() == USER_HOOK


def test_backup_displaced_ignores_its_own_hook(tmp_path) -> None:
    workdir = _repo(tmp_path)
    hook = os.path.join(workdir, ".git", "hooks", "pre-commit")
    with open(hook, "w") as handle:
        handle.write(HOOK_BODY)
    assert install_manifest.backup_displaced(workdir, hook, "pre-commit") is None


def test_enable_rearms_after_disable(tmp_path) -> None:
    workdir = _repo(tmp_path)
    _install_artifacts(workdir)
    install_manifest.disable(workdir)
    install_manifest.enable(workdir, {"pre-commit": HOOK_BODY, "pre-push": HOOK_BODY})

    hook = os.path.join(workdir, ".git", "hooks", "pre-commit")
    assert os.path.isfile(hook)
    assert os.access(hook, os.X_OK)
    assert install_manifest.read_manifest(workdir)["disabled"] is False


def test_uninstall_is_a_dry_run_unless_applied(tmp_path) -> None:
    workdir = _repo(tmp_path)
    _install_artifacts(workdir)
    steps = install_manifest.uninstall(workdir, apply=False)
    assert os.path.isfile(os.path.join(workdir, ".git/hooks/pre-commit"))
    assert os.path.isfile(os.path.join(workdir, ".gitreins/config.yaml"))
    assert any(step.startswith("would remove") for step in steps)


def test_uninstall_removes_hook_and_config_but_keeps_history(tmp_path) -> None:
    workdir = _repo(tmp_path)
    _install_artifacts(workdir)
    history = os.path.join(workdir, ".gitreins", "history")
    os.makedirs(history, exist_ok=True)

    install_manifest.uninstall(workdir, apply=True)

    assert not os.path.isfile(os.path.join(workdir, ".git/hooks/pre-commit"))
    assert not os.path.isfile(os.path.join(workdir, ".gitreins/config.yaml"))
    assert os.path.isdir(history), "history must survive a plain uninstall"


def test_purge_is_the_only_way_history_is_removed(tmp_path) -> None:
    workdir = _repo(tmp_path)
    _install_artifacts(workdir)
    history = os.path.join(workdir, ".gitreins", "history")
    os.makedirs(history, exist_ok=True)

    install_manifest.uninstall(workdir, purge=True, apply=True)

    assert not os.path.isdir(os.path.join(workdir, ".gitreins"))


def test_uninstall_keeps_a_config_the_user_edited(tmp_path) -> None:
    workdir = _repo(tmp_path)
    _install_artifacts(workdir)
    config = os.path.join(workdir, ".gitreins", "config.yaml")
    with open(config, "a") as handle:
        handle.write("  lint: false\n")

    steps = install_manifest.uninstall(workdir, apply=True)

    assert os.path.isfile(config), "an edited config is the user's, not the harness's"
    assert any("KEEP config" in step for step in steps)


def test_uninstall_restores_a_displaced_hook(tmp_path) -> None:
    workdir = _repo(tmp_path)
    hook = os.path.join(workdir, ".git", "hooks", "pre-commit")
    with open(hook, "w") as handle:
        handle.write(USER_HOOK)
    backup_rel = install_manifest.backup_displaced(workdir, hook, "pre-commit")
    _install_artifacts(workdir, backup=backup_rel)

    install_manifest.uninstall(workdir, apply=True)

    with open(hook) as handle:
        assert handle.read() == USER_HOOK


def test_cli_disable_and_enable_round_trip(tmp_path) -> None:
    workdir = _repo(tmp_path)
    _install_artifacts(workdir)
    env = dict(os.environ, PYTHONPATH=REPO_ROOT)

    disabled = subprocess.run(
        [sys.executable, "-m", "gitreins", "disable"],
        cwd=workdir,
        capture_output=True,
        text=True,
        env=env,
    )
    assert disabled.returncode == 0, disabled.stdout + disabled.stderr
    assert not os.path.isfile(os.path.join(workdir, ".git/hooks/pre-commit"))

    enabled = subprocess.run(
        [sys.executable, "-m", "gitreins", "enable"],
        cwd=workdir,
        capture_output=True,
        text=True,
        env=env,
    )
    assert enabled.returncode == 0, enabled.stdout + enabled.stderr
    assert os.path.isfile(os.path.join(workdir, ".git/hooks/pre-commit"))


def test_cli_uninstall_defaults_to_dry_run(tmp_path) -> None:
    workdir = _repo(tmp_path)
    _install_artifacts(workdir)
    env = dict(os.environ, PYTHONPATH=REPO_ROOT)
    result = subprocess.run(
        [sys.executable, "-m", "gitreins", "uninstall"],
        cwd=workdir,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Dry run" in result.stdout
    assert os.path.isfile(os.path.join(workdir, ".gitreins/config.yaml"))
