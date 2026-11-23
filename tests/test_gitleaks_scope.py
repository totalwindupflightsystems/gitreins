"""GR-244 — gitleaks scope containment.

The judge's full-tree secrets scan used to run
``gitleaks detect --source <caller-controlled> --no-git``: ``--no-git``
walks the FILESYSTEM with no ``git ls-files`` boundary, so a scan rooted
outside a repo (a lane run from a non-repo cwd, an explicit scan root
above the repo) read the entire server at ~1010% CPU. These tests pin the
fail-closed contract:

* a scan whose scope cannot be contained inside a repo root is REFUSED —
  gitleaks never runs, the refusal is named in the evidence, and the
  built-in cross-check still grades the tree;
* a contained scan sources an absolute, realpath-resolved path inside the
  repo root — never a bare ``.`` — and runs under a hard GOMAXPROCS cap;
* the staged path (``GITREINS_GUARD_STAGED=1`` → ``protect --staged``) is
  byte-identical: no cap, no detect;
* the generated config's allowlist (harness state + generated/vendor
  trees) binds in ``--no-git`` mode — verified live when gitleaks is
  installed.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from engine.guard_manager import (
    GITLEAKS_MAX_PROCS,
    generated_dir_allowlist_paths,
    harness_scan_gitleaks_config,
)
from engine.pipeline import _secrets_step_run


def _git(workdir: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=workdir, capture_output=True, check=True)


def _init_repo(workdir: Path) -> None:
    """A REAL git repo (rev-parse --show-toplevel must resolve)."""
    workdir.mkdir(parents=True, exist_ok=True)
    _git(workdir, "init", "-q")
    _git(workdir, "config", "user.email", "test@example.com")
    _git(workdir, "config", "user.name", "test")


def _fake_gitleaks(tmp_path: Path) -> tuple[Path, Path]:
    """A fake gitleaks that records its argv into $FAKE_GITLEAKS_ARGS."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    fake = fake_bin / "gitleaks"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$FAKE_GITLEAKS_ARGS"\nexit 0\n')
    fake.chmod(0o755)
    return fake_bin, tmp_path / "gitleaks-args.txt"


def _run_step(cmd: str, cwd: Path, **env_extra: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.pop("GITREINS_GUARD_STAGED", None)
    env.update(env_extra)
    return subprocess.run(
        ["bash", "-c", cmd], cwd=str(cwd), env=env, capture_output=True, text=True, check=False
    )


class TestScopeRefusal:
    def test_non_repo_workdir_refuses_the_scan(self, tmp_path: Path) -> None:
        """A workdir with no containing repo root is refused: gitleaks never
        runs (its argv is never recorded by the shim) and the refusal is
        named in the evidence."""
        plain = tmp_path / "plain"
        plain.mkdir()  # NOT a git repo
        fake_bin, args_file = _fake_gitleaks(tmp_path)

        cmd = _secrets_step_run(str(plain), {})
        assert "gitleaks detect" not in cmd, "the un-bounded detect must not be built"
        assert "gitleaks: skipped (not a git repo — scope cannot be contained)" in cmd

        proc = _run_step(
            cmd,
            plain,
            PATH=f"{fake_bin}:{os.environ['PATH']}",
            FAKE_GITLEAKS_ARGS=str(args_file),
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert not args_file.exists(), (
            "gitleaks ran on an uncontained scope — the refusal did not bind"
        )
        assert "gitleaks: skipped (not a git repo — scope cannot be contained)" in proc.stdout

    def test_non_repo_scan_root_refuses_the_scan(self, tmp_path: Path) -> None:
        """An explicit scan_root outside any repo is refused too — the
        caller's cwd (the control root) is not the scope, the scan root is."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        outside = tmp_path / "outside"
        outside.mkdir()  # non-repo scan root, distinct from workdir
        fake_bin, args_file = _fake_gitleaks(tmp_path)

        cmd = _secrets_step_run(str(repo), {}, str(outside))
        assert "gitleaks detect" not in cmd

        proc = _run_step(
            cmd,
            repo,
            PATH=f"{fake_bin}:{os.environ['PATH']}",
            FAKE_GITLEAKS_ARGS=str(args_file),
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert not args_file.exists()
        assert "gitleaks: skipped (not a git repo — scope cannot be contained)" in proc.stdout

    def test_refused_scan_grades_as_not_run_without_claiming_clean(self, tmp_path: Path) -> None:
        """The refusal grades the step via the built-in cross-check only —
        and must NOT print the `gitleaks: clean` verdict for a scan that
        never ran (TRUST-003: the evidence cannot lie)."""
        plain = tmp_path / "plain"
        plain.mkdir()

        proc = _run_step(_secrets_step_run(str(plain), {}), plain)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "gitleaks: skipped (not a git repo — scope cannot be contained)" in proc.stdout
        assert "secrets: gitleaks: not run (scope cannot be contained)" in proc.stdout
        assert "secrets: gitleaks: clean" not in proc.stdout
        # The built-in cross-check still grades the same tree.
        assert "secrets: builtin cross-check" in proc.stdout

    def test_refused_scan_is_functional_end_to_end(self, tmp_path: Path) -> None:
        """End-to-end on a REAL repo-shaped tree: the judge step for a
        non-repo scan root must refuse even with real gitleaks on PATH."""
        if shutil.which("gitleaks") is None:
            pytest.skip("gitleaks not installed on this host")
        home_like = tmp_path / "homelike"
        home_like.mkdir()  # non-repo "home" stand-in — nothing is walked
        before = os.times()

        proc = _run_step(_secrets_step_run(str(home_like), {}), home_like)

        after = os.times()
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "gitleaks: skipped" in proc.stdout
        # The refusal cost ~nothing: no un-bounded walk happened (a real walk
        # would burn minutes of child CPU even on an empty-looking tree).
        assert (after.children_user - before.children_user) < 5.0


class TestContainedScope:
    def test_scan_source_is_absolute_inside_the_repo_root(self, tmp_path: Path) -> None:
        """A contained workdir sources its realpath — never a bare ``.`` that
        resolves wherever the step's cwd lands."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        cmd = _secrets_step_run(str(repo), {})
        assert f"--source {shlex.quote(str(repo))}" in cmd
        assert "--source ." not in cmd

    def test_scan_root_inside_a_repo_is_allowed(self, tmp_path: Path) -> None:
        """An explicit scan root that lives INSIDE a repo keeps grading that
        contained scope (a tracked subpath is a sanctioned source)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        sub = repo / "src"
        sub.mkdir()
        fake_bin, args_file = _fake_gitleaks(tmp_path)

        cmd = _secrets_step_run(str(repo), {}, str(sub))
        assert f"--source {shlex.quote(str(sub))}" in cmd

        proc = _run_step(
            cmd,
            repo,
            PATH=f"{fake_bin}:{os.environ['PATH']}",
            FAKE_GITLEAKS_ARGS=str(args_file),
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert args_file.exists(), "a contained explicit scan root must still reach gitleaks"


class TestParallelismCap:
    def test_detect_command_is_gomaxprocs_capped(self, tmp_path: Path) -> None:
        """The full-tree detect runs under `env GOMAXPROCS=<N>` — nice
        deprioritizes scheduling, it does not bound CPU (~1010% peak)."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        cmd = _secrets_step_run(str(repo), {})
        assert f"export GOMAXPROCS={GITLEAKS_MAX_PROCS};" in cmd
        assert f"gitleaks detect --source {shlex.quote(str(repo))} --no-git" in cmd
        # The cap is named in the evidence (TRUST-003 attribution).
        assert f"secrets: gitleaks parallelism=GOMAXPROCS={GITLEAKS_MAX_PROCS}" in cmd

    def test_staged_path_is_untouched(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AC-R4: the staged contract is byte-identical — `protect --staged`,
        no detect, and no parallelism cap injected."""
        monkeypatch.setenv("GITREINS_GUARD_STAGED", "1")
        repo = tmp_path / "repo"
        _init_repo(repo)
        cmd = _secrets_step_run(str(repo), {})
        assert 'gitleaks protect --staged --no-banner --no-color --config "$_glcfg"' in cmd
        assert "gitleaks detect" not in cmd
        assert "GOMAXPROCS" not in cmd

    def test_guard_full_tree_spawn_env_is_capped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard's own full-tree spawn passes the same GOMAXPROCS cap."""
        from engine import guard_manager

        repo = tmp_path / "repo"
        _init_repo(repo)
        captured: dict[str, str] = {}

        real_run = subprocess.run

        def spy_run(*args: object, **kwargs: object) -> object:
            env = kwargs.get("env")
            if isinstance(env, dict) and any(a == "gitleaks" for a in (args[0] if args else [])):
                captured.update(env)
            return real_run(*args, **kwargs)  # type: ignore[arg-type]

        fake_bin = tmp_path / "bin"
        fake_bin.mkdir(exist_ok=True)
        (fake_bin / "gitleaks").write_text("#!/bin/sh\nexit 0\n")
        (fake_bin / "gitleaks").chmod(0o755)

        gm = guard_manager.GuardManager(str(repo))
        monkeypatch.setattr(f"{guard_manager.__name__}.subprocess.run", spy_run)
        monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ['PATH']}")
        gm._check_secrets_full_tree()
        assert captured.get("GOMAXPROCS") == str(GITLEAKS_MAX_PROCS)


class TestGeneratedDirAllowlist:
    def test_config_allowlists_generated_dirs(self, tmp_path: Path) -> None:
        rendered = harness_scan_gitleaks_config(str(tmp_path))
        for pattern in generated_dir_allowlist_paths():
            assert f"'''{pattern}'''" in rendered
        # The harness-state entries still ride along.
        assert r"(^|/)\.gitreins/.*" in rendered

    def test_venv_family_pattern_matches_prefixes(self) -> None:
        import re

        venv_re = generated_dir_allowlist_paths()[-1]
        assert re.search(venv_re, ".venv/lib/site-packages/x.py")
        assert re.search(venv_re, "src/.venv312/lib/x.py")
        assert re.search(venv_re, "venvs/tools/x.py")
        assert not re.search(venv_re, "src/venvlike.txt")
        assert not re.search(venv_re, "src/oven/dish.py")

    @pytest.mark.skipif(
        shutil.which("gitleaks") is None, reason="gitleaks not installed on this host"
    )
    def test_allowlist_binds_live_in_no_git_mode(self, tmp_path: Path) -> None:
        """AC-R3, live: a tracked secret IS flagged; the same secret in a
        generated dir (.venv) and in harness state (.gitreins) is NOT —
        allowlisted paths are pruned from the walk itself. Tokens are
        runtime-constructed (never literals in source) and high-entropy —
        gitleaks does not flag low-entropy placeholder strings at all."""
        import hashlib

        def token(seed: str) -> str:
            return "ghp_" + hashlib.sha256(seed.encode()).hexdigest()[:36]

        repo = tmp_path / "repo"
        _init_repo(repo)
        (repo / ".venv" / "lib").mkdir(parents=True)
        (repo / ".gitreins" / "logs").mkdir(parents=True)
        (repo / "tracked.txt").write_text(f'token = "{token("tracked")}"\n')
        (repo / ".venv" / "lib" / "x.txt").write_text(f'token = "{token("venv")}"\n')
        (repo / ".gitreins" / "logs" / "guard.log").write_text(f'token = "{token("state")}"\n')
        _git(repo, "add", "tracked.txt")
        _git(repo, "commit", "-qm", "init")

        cfg_path = tmp_path / "gen.toml"
        cfg_path.write_text(harness_scan_gitleaks_config(str(repo)))
        proc = subprocess.run(
            [
                "gitleaks",
                "detect",
                "--source",
                str(repo),
                "--no-git",
                "--verbose",
                "--no-banner",
                "--no-color",
                "--config",
                str(cfg_path),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        flagged = {
            ln.split()[1]
            for ln in (proc.stdout + proc.stderr).splitlines()
            if ln.startswith("File:")
        }
        assert str(repo / "tracked.txt") in flagged, (
            f"the tracked secret must be flagged: {proc.stdout + proc.stderr}"
        )
        assert not any(".venv" in f for f in flagged), f".venv must not be graded: {flagged}"
        assert not any(".gitreins" in f for f in flagged), (
            f"harness state must not be graded: {flagged}"
        )
