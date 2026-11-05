"""DF-2026-20: the secrets step must never blame its informational banner.

The five duckbrain verdicts of 2026-10-30 (55043021/761c8c9f/3786436f/
ed0feed3/a38df962) and DF-2026-10's own b54dd019/2d680b63 were all narrated
as ``tier1 secrets FAIL: harness state excluded from gitleaks scope
(.gitreins/**)`` because the echo ran unconditionally as the FIRST output
line and ``_summarize_stage``'s no-pytest-line fallback takes the first
non-blank line. The real failure was the builtin credential-store path class
on the repo's tracked ``.npmrc``/``.env.example`` while gitleaks was clean —
both scanners already agree on harness-state scope. Pinned here:

1. the PASS path prints no harness-state message at all;
2. the gitleaks-FAILURE diagnosis keeps the exclusion note (TRUST-002) but
   prints it AFTER the findings evidence;
3. a failing step's one-line summary names the finding, not the banner;
4. the two scanners demonstrably agree on ``.gitreins/**`` scope: the
   generated gitleaks config excludes it AND the builtin skips it — proven
   as a red/blue pair (same canary: ignored under ``.gitreins/``,
   caught in a source path).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from engine.guard_manager import GuardManager
from engine.pipeline import (
    Pipeline,
    StageResult,
    StepResult,
    _secrets_step_run,
    harness_scan_gitleaks_config,
)

_CANARY = 'token = "ghp_' + "D" * 40 + '"\n'


def _git(workdir: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=workdir, check=True, capture_output=True)


def _init_repo(workdir: Path) -> None:
    _git(workdir, "init", "-q")
    _git(workdir, "config", "user.email", "test@example.com")
    _git(workdir, "config", "user.name", "test")


def _fake_gitleaks(tmp_path: Path, body: str) -> dict[str, str]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    fake = fake_bin / "gitleaks"
    fake.write_text(f"#!/bin/sh\n{body}\n")
    fake.chmod(0o755)
    return dict(os.environ, PATH=f"{fake_bin}:{os.environ['PATH']}")


def _run_step(workdir: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", _secrets_step_run(str(workdir))],
        cwd=workdir,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


class TestSecretsStepScopeMessage:
    def test_pass_output_carries_no_harness_state_message(self, tmp_path: Path) -> None:
        """DF-2026-20 AC2: a clean run must not print the scope line."""
        workdir = tmp_path / "repo"
        workdir.mkdir()
        _init_repo(workdir)
        (workdir / "clean.py").write_text("x = 1\n")
        _git(workdir, "add", "-A")
        _git(workdir, "commit", "-qm", "init")

        proc = _run_step(workdir, _fake_gitleaks(tmp_path, "exit 0"))

        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "harness state excluded" not in proc.stdout

    def test_gitleaks_failure_keeps_the_exclusion_note_after_evidence(self, tmp_path: Path) -> None:
        """TRUST-002 preserved: the diagnosis reads the exclusion — after the
        findings evidence, never as the leading line the summary would pick."""
        workdir = tmp_path / "repo"
        workdir.mkdir()
        _init_repo(workdir)
        (workdir / "clean.py").write_text("x = 1\n")
        _git(workdir, "add", "-A")
        _git(workdir, "commit", "-qm", "init")

        proc = _run_step(workdir, _fake_gitleaks(tmp_path, "exit 1"))

        assert proc.returncode != 0
        findings_at = proc.stdout.index("secrets: gitleaks: findings found (exit 1)")
        exclusion_at = proc.stdout.index("secrets: harness state excluded from gitleaks scope")
        assert findings_at < exclusion_at
        assert not proc.stdout.lstrip().startswith("secrets: harness state excluded")


class TestFailingSecretsSummaryNamesTheFinding:
    def test_summary_prefers_sensitive_path_line_over_banner(self, tmp_workdir: str) -> None:
        """DF-2026-20: the banner is informational — the finding is the reason."""
        banner = "secrets: harness state excluded from gitleaks scope (.gitreins/**)\n"
        step_output = (
            banner
            + "secrets: scanners=gitleaks+builtin cross-check\n"
            + "secrets: gitleaks: clean\n"
            + "Secrets scan: 2 potential findings\n"
            + "secrets: builtin cross-check: Potential secrets found:\n"
            + ".npmrc: SENSITIVE PATH (credential-store path class) — blocked regardless of content.\n"
            + ".env.example: SENSITIVE PATH (credential-store path class) — blocked.\n"
        )
        stage = StageResult(
            id="tier1",
            passed=False,
            any_failed=True,
            steps=[StepResult(id="secrets", type="script", passed=False, output=step_output)],
        )
        p = Pipeline({"pipeline": {"stages": []}}, tmp_workdir)
        summary = p._summarize_stage(stage)
        assert "SENSITIVE PATH (credential-store path class)" in summary
        assert "harness state excluded" not in summary

    def test_secrets_finding_regex_matches_both_scanners(self) -> None:
        from engine.types import _SECRETS_FINDING_LINE

        assert _SECRETS_FINDING_LINE.search(
            ".npmrc: SENSITIVE PATH (credential-store path class) — x"
        )
        assert _SECRETS_FINDING_LINE.search("Finding: .env:3: aws-key (severity=high)")
        assert not _SECRETS_FINDING_LINE.search("secrets: gitleaks: clean")


class TestHarnessStateScopeAgreement:
    """DF-2026-20 AC1 (agreement branch): both shipped scanners exclude
    ``.gitreins/**`` — proven as a red/blue pair on the same canary."""

    def test_generated_gitleaks_config_excludes_harness_state(self, tmp_path: Path) -> None:
        rendered = harness_scan_gitleaks_config(str(tmp_path))
        assert "allowlist" in rendered
        assert r"\.gitreins" in rendered

    def test_builtin_skips_gitreins_canary_but_catches_source_canary(self, tmp_path: Path) -> None:
        workdir = tmp_path / "repo"
        workdir.mkdir()
        _init_repo(workdir)
        (workdir / "src").mkdir()
        (workdir / "src" / "clean.py").write_text("x = 1\n")
        history = workdir / ".gitreins" / "history" / "2026-11-02"
        history.mkdir(parents=True)
        (history / "verdict.json").write_text(_CANARY)
        _git(workdir, "add", "-A")
        _git(workdir, "commit", "-qm", "init")

        blue = GuardManager(str(workdir))._builtin_secrets_scan(staged_only=False)
        assert blue.passed, blue.output
        # The clean label may NAME the exclusion (".gitreins/**") but must
        # never grade the artifact path itself.
        assert ".gitreins/history" not in blue.output
        assert "ghp_" not in blue.output

        (workdir / "src" / "leak.py").write_text(_CANARY)
        red = GuardManager(str(workdir))._builtin_secrets_scan(staged_only=False)
        assert not red.passed
        assert "src/leak.py" in red.output
