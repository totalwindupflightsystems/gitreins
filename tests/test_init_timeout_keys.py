"""DF-032 regression: smart init writes an explicit coherent timeout pair.

Before the fix, a fresh `gitreins install && gitreins init` repo carried no
guards.hook_timeout / guards.test_timeout keys, so GuardManager derived the
implicit budget (test_timeout default 1200, hook raised to test+120) and
printed a "hook_timeout=300s <= test_timeout=1200s" mismatch warning on
EVERY guard run. The fix makes init write the exact pair the derivation
produces, so the first three guard runs are warning-free and no
user-authored value is touched.
"""

from __future__ import annotations

import os
import subprocess

import yaml

from engine.guard_manager import GuardManager

# The constants exist only post-fix; import them lazily inside the tests that
# use them so the PRE-FIX red proof fails behaviorally (missing config keys /
# warning present) instead of dying at module collection.
from .test_cli import run_cli


def _read_guards(workdir: str) -> dict:
    with open(os.path.join(workdir, ".gitreins", "config.yaml")) as f:
        return yaml.safe_load(f)["guards"]


def _init_real_git_repo(tmp_path, name: str = "repo") -> str:
    """Real `git init` repo (hooks + manifest writes need it)."""
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "-C", repo, "config", "core.hooksPath", ".git/hooks"], check=True)
    subprocess.run(["git", "-C", repo, "config", "user.name", "GitReins Tests"], check=True)
    subprocess.run(
        ["git", "-C", repo, "config", "user.email", "gitreins-tests@example.invalid"],
        check=True,
    )
    return str(repo)


def _write_python_source(workdir: str) -> None:
    with open(os.path.join(workdir, "app.py"), "w") as f:
        f.write("print('hello')\n")
    os.makedirs(os.path.join(workdir, "tests"), exist_ok=True)
    with open(os.path.join(workdir, "tests", "test_app.py"), "w") as f:
        f.write("def test_app(): pass\n")


def _guard_stderr(workdir: str) -> str:
    """Construct the GuardManager exactly as a guard run would and return stderr.

    The mismatch warning is printed in GuardManager.__init__ from the loaded
    config, so constructing it with the repo's own config IS the first guard
    run's warning check (capsys catches the print(..., file=sys.stderr)).
    """
    from gitreins.cli import load_config

    config = load_config(workdir)
    import contextlib
    import io

    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        GuardManager(workdir, config)
    return err.getvalue()


class TestInitWritesExplicitTimeoutPair:
    """DF-032: the pair init writes matches the guard's derivation."""

    def test_install_then_init_writes_coherent_pair(self, tmp_path) -> None:
        """Fresh `install && init` writes test_timeout/hook_timeout explicitly.

        The values must be exactly the derivation's numbers (1200 default,
        hook = test + 120 slack), and constructing the GuardManager from the
        persisted config must print NO hook_timeout warning.
        """
        from engine.guard_manager import DEFAULT_TEST_TIMEOUT, HOOK_TIMEOUT_SLACK

        repo = _init_real_git_repo(tmp_path)
        _write_python_source(repo)
        assert run_cli("install", cwd=repo).returncode == 0
        assert run_cli("init", cwd=repo).returncode == 0

        guards = _read_guards(repo)
        assert guards["test_timeout"] == DEFAULT_TEST_TIMEOUT
        assert guards["hook_timeout"] == DEFAULT_TEST_TIMEOUT + HOOK_TIMEOUT_SLACK

        assert "WARN: hook_timeout" not in _guard_stderr(repo)

    def test_derived_constants_match_guard_derivation(self, tmp_workdir: str, capsys) -> None:
        """Structural tie: the writer's constants ARE the deriver's numbers.

        A config-less GuardManager derives hook = default_test + slack and
        warns; pinning the constants to that behavior makes any future drift
        between the writer and the deriver a failing test.
        """
        from engine.guard_manager import DEFAULT_TEST_TIMEOUT, HOOK_TIMEOUT_SLACK

        gm = GuardManager(tmp_workdir, {})
        err = capsys.readouterr().err
        assert gm._test_timeout == DEFAULT_TEST_TIMEOUT
        assert gm._hook_timeout == DEFAULT_TEST_TIMEOUT + HOOK_TIMEOUT_SLACK
        # The config-less path still warns (nothing can silence it there);
        # this test only pins that the constants match the derivation.
        assert "WARN: hook_timeout=300s <= test_timeout=1200s" in err

    def test_first_three_guard_runs_are_warning_free(self, tmp_path) -> None:
        """FAILIF acceptance shape: three consecutive guard constructions warn nothing."""
        repo = _init_real_git_repo(tmp_path)
        _write_python_source(repo)
        assert run_cli("install", cwd=repo).returncode == 0
        assert run_cli("init", cwd=repo).returncode == 0
        for _ in range(3):
            assert "WARN: hook_timeout" not in _guard_stderr(repo)

    def test_init_preserves_user_authored_timeout_values(self, tmp_path) -> None:
        """A repo that already set both keys keeps its exact values."""
        repo = _init_real_git_repo(tmp_path)
        _write_python_source(repo)
        assert run_cli("install", cwd=repo).returncode == 0
        config_path = os.path.join(repo, ".gitreins", "config.yaml")
        with open(config_path) as f:
            config = yaml.safe_load(f)
        config["guards"]["test_timeout"] = 900
        config["guards"]["hook_timeout"] = 700  # explicit short budget, honored
        with open(config_path, "w") as f:
            yaml.safe_dump(config, f, sort_keys=False)

        assert run_cli("init", cwd=repo).returncode == 0
        guards = _read_guards(repo)
        assert guards["test_timeout"] == 900
        assert guards["hook_timeout"] == 700

    def test_init_fills_missing_hook_from_existing_test_timeout(self, tmp_path) -> None:
        """User set test_timeout only → hook is derived from THAT value.

        The written hook must equal test_timeout + slack so the pair stays
        coherent (and warning-free), not the 1200-based default.
        """
        from engine.guard_manager import HOOK_TIMEOUT_SLACK

        repo = _init_real_git_repo(tmp_path)
        _write_python_source(repo)
        assert run_cli("install", cwd=repo).returncode == 0
        config_path = os.path.join(repo, ".gitreins", "config.yaml")
        with open(config_path) as f:
            config = yaml.safe_load(f)
        config["guards"]["test_timeout"] = 900
        with open(config_path, "w") as f:
            yaml.safe_dump(config, f, sort_keys=False)

        assert run_cli("init", cwd=repo).returncode == 0
        guards = _read_guards(repo)
        assert guards["test_timeout"] == 900
        assert guards["hook_timeout"] == 900 + HOOK_TIMEOUT_SLACK
        assert "WARN: hook_timeout" not in _guard_stderr(repo)

    def test_init_leaves_non_numeric_test_timeout_untouched(self, tmp_path) -> None:
        """A user-authored string test_timeout ("300s") is data: write nothing."""
        repo = _init_real_git_repo(tmp_path)
        _write_python_source(repo)
        assert run_cli("install", cwd=repo).returncode == 0
        config_path = os.path.join(repo, ".gitreins", "config.yaml")
        with open(config_path) as f:
            config = yaml.safe_load(f)
        config["guards"]["test_timeout"] = "300s"
        with open(config_path, "w") as f:
            yaml.safe_dump(config, f, sort_keys=False)

        assert run_cli("init", cwd=repo).returncode == 0
        guards = _read_guards(repo)
        assert guards["test_timeout"] == "300s"
        assert "hook_timeout" not in guards


class TestUnitApplyDefaultTimeoutPair:
    """Direct unit coverage of the writer helper."""

    def test_adds_both_keys_when_missing(self) -> None:
        from engine.guard_manager import DEFAULT_TEST_TIMEOUT, HOOK_TIMEOUT_SLACK
        from gitreins.cli import _apply_default_timeout_pair

        guards: dict = {}
        added = _apply_default_timeout_pair(guards)
        assert added == ["test_timeout", "hook_timeout"]
        assert guards["test_timeout"] == DEFAULT_TEST_TIMEOUT
        assert guards["hook_timeout"] == DEFAULT_TEST_TIMEOUT + HOOK_TIMEOUT_SLACK

    def test_existing_hook_timeout_is_never_overwritten(self) -> None:
        from gitreins.cli import _apply_default_timeout_pair

        guards = {"hook_timeout": 5}
        added = _apply_default_timeout_pair(guards)
        assert added == ["test_timeout"]
        assert guards["hook_timeout"] == 5

    def test_noop_when_pair_complete(self) -> None:
        from gitreins.cli import _apply_default_timeout_pair

        guards = {"test_timeout": 600, "hook_timeout": 620}
        assert _apply_default_timeout_pair(guards) == []
        assert guards == {"test_timeout": 600, "hook_timeout": 620}
