"""PROF-002-adjacent guard fix: the Tier 1 secrets scan could run away.

The runaway observed on 2026-11-11 (gitleaks at ~1010% CPU for 10+ minutes
during routine lane commits) is structural, not a one-off: the step builds
``gitleaks detect --source . --no-git``, which walks EVERY file on disk in the
worktree at EVERY commit and every judge pass. Measured on this host: mischief
4 s, guard 1 s, trouble 17 s, hermes-dagger 48 s (409 MB) — and the fleet lands
concurrent commits, so several 16-core scans stack (load 35 observed).

Two bounds, both fail-closed:

1. In pre-commit context the hook sets ``GITREINS_GUARD_STAGED=1`` and the step
   scans the STAGED DIFF (``gitleaks protect --staged``) instead of the whole
   tree — O(changed files) instead of O(worktree). The judge's pass (no env)
   keeps the full-tree ``detect`` scan: it grades the committed tree, where
   staged-only would be the wrong scope.
2. Every external scanner run is wrapped in ``timeout``
   (``guards.scanner_timeout_s`` / ``GITREINS_SCANNER_TIMEOUT_S``, default 600,
   0 = off). A scan that hits the bound exits non-zero — a killed scan is a
   FAIL with evidence, never a silent pass, because a secrets scan that did not
   finish proves nothing.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from engine.pipeline import _secrets_step_run


def _fake_gitleaks(tmp_path: Path, body: str) -> tuple[Path, Path]:
    """A fake gitleaks that records its argv into $FAKE_GITLEAKS_ARGS."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    fake = fake_bin / "gitleaks"
    fake.write_text(body)
    fake.chmod(0o755)
    args_file = tmp_path / "gitleaks-args.txt"
    return fake_bin, args_file


def _run(cmd: str, cwd: Path, env_extra: dict[str, str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("GITREINS_GUARD_STAGED", None)
    env.pop("GITREINS_SCANNER_TIMEOUT_S", None)
    env.update(env_extra)
    return subprocess.run(
        ["bash", "-c", cmd], cwd=str(cwd), env=env, capture_output=True, text=True, check=False
    )


class TestStagedMode:
    def test_staged_env_scans_the_staged_diff_not_the_tree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GITREINS_GUARD_STAGED=1 → ``protect --staged``, never a tree-wide detect.

        The env is read at BUILD time, matching the real flow: the hook exports
        it and then invokes `gitreins guard` in the same environment.
        """
        monkeypatch.setenv("GITREINS_GUARD_STAGED", "1")
        fake_bin, _ = _fake_gitleaks(
            tmp_path, '#!/bin/sh\nprintf "%s\\n" "$@" > "$FAKE_GITLEAKS_ARGS"\nexit 0\n'
        )
        workdir = tmp_path / "repo"
        workdir.mkdir()
        cmd = _secrets_step_run(str(workdir), {})
        proc = _run(
            cmd,
            workdir,
            {
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "GITREINS_GUARD_STAGED": "1",
                "FAKE_GITLEAKS_ARGS": str(tmp_path / "gitleaks-args.txt"),
            },
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        args = (tmp_path / "gitleaks-args.txt").read_text()
        assert "protect" in args, f"staged mode did not run `protect`: {args}"
        assert "--staged" in args, f"staged mode did not pass --staged: {args}"
        assert "detect" not in args, f"staged mode still walked the tree: {args}"

    def test_default_mode_keeps_the_full_tree_scan(self) -> None:
        """No env → the judge's full-tree scope is unchanged."""
        cmd = _secrets_step_run("/tmp")
        assert "gitleaks detect" in cmd
        assert "protect --staged" not in cmd


class TestScannerTimeout:
    def test_default_command_carries_a_timeout_bound(self) -> None:
        """The scanner is wrapped in `timeout` by default — a runaway scan
        cannot outlive the knob."""
        import re

        cmd = _secrets_step_run("/tmp")
        assert re.search(r"timeout 600 (?:\S+ )*?gitleaks", cmd), cmd[:400]

    def test_zero_disables_the_bound(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITREINS_SCANNER_TIMEOUT_S", "0")
        cmd = _secrets_step_run("/tmp")
        assert not re.search(r"timeout \d+ (?:\S+ )*?gitleaks", cmd), cmd[:400]
        assert "gitleaks detect" in cmd or "gitleaks protect" in cmd

    def test_timeout_knob_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GITREINS_SCANNER_TIMEOUT_S", "45")
        cmd = _secrets_step_run("/tmp")
        assert re.search(r"timeout 45 (?:\S+ )*?gitleaks", cmd), cmd[:400]

    def test_a_hung_scan_fails_closed_within_the_bound(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A scanner that never finishes exits non-zero fast — a scan that did
        not run proves nothing, and grading it green would be a false pass."""
        monkeypatch.setenv("GITREINS_SCANNER_TIMEOUT_S", "2")
        fake_bin, _ = _fake_gitleaks(tmp_path, "#!/bin/sh\nsleep 300\nexit 0\n")
        workdir = tmp_path / "repo"
        workdir.mkdir()
        cmd = _secrets_step_run(str(workdir), {})
        proc = _run(
            cmd,
            workdir,
            {
                "PATH": f"{fake_bin}:{os.environ['PATH']}",
                "GITREINS_SCANNER_TIMEOUT_S": "2",
            },
        )
        assert proc.returncode != 0, "a hung scanner graded green"
        combined = proc.stdout + proc.stderr
        assert "timeout" in combined.lower(), (
            f"the timeout is not named in the evidence: {combined[:300]}"
        )
