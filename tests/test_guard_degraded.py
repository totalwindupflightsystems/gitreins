"""TRUST-001 — a gate that did no work is a DEGRADED pass, never a silent green.

The dogfood verdict (POC-11) caught `gitreins guard` printing
``Tier 1 Guards: PASS`` with exit 0 on a clean tree while the persisted log
said ``lint: No Python files staged`` and ``tests: No files staged — skipped``.
CI and merge-back both consume the exit code as truth, so these tests pin the
whole contract: per-step skip reasons, the DEGRADED PASS line, the exit-code
policy (0 only with ``guards.allow_skips``), the ``init`` default, the judge's
runtime-skip record, and the fact that a staged tree keeps today's green output
untouched.
"""

import os
import shutil
import subprocess
import sys

import yaml

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLI_SCRIPT = os.path.join(PROJECT_ROOT, "gitreins", "cli.py")


def _run_cli(*args, cwd=None):
    env = os.environ.copy()
    env["PYTHONPATH"] = PROJECT_ROOT + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    cmd = [sys.executable, CLI_SCRIPT] + list(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=60, cwd=cwd, env=env)


def _init_repo(workdir):
    subprocess.run(["git", "init", "-q"], cwd=workdir, capture_output=True, check=True)
    subprocess.run(
        ["git", "config", "user.email", "trust-001@example.invalid"],
        cwd=workdir,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "TRUST-001"], cwd=workdir, capture_output=True, check=True
    )
    return workdir


def _repo(tmp_path, name="repo"):
    path = tmp_path / name
    path.mkdir()
    return str(_init_repo(path))


def _stage(workdir, relpath, content):
    full = os.path.join(workdir, relpath)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w") as f:
        f.write(content)
    subprocess.run(["git", "add", relpath], cwd=workdir, capture_output=True, check=True)


def _write_config(workdir, guards: dict):
    cfg_dir = os.path.join(workdir, ".gitreins")
    os.makedirs(cfg_dir, exist_ok=True)
    with open(os.path.join(cfg_dir, "config.yaml"), "w") as f:
        yaml.safe_dump({"guards": guards}, f)


def _manager(workdir, **guards):
    from engine.guard_manager import GuardManager

    guards.setdefault("test_command", "echo ok")
    return GuardManager(workdir, config={"guards": guards})


class TestSkippedStepReasons:
    """Criterion 1: the tier1 result carries a skipped-step list with reasons."""

    def test_clean_tree_records_skip_reasons(self, tmp_path):
        repo = _repo(tmp_path)
        result = _manager(repo).run_all()

        assert result.passed is True, "a skip is not a failure"
        assert result.degraded is True
        assert result.skipped_steps == [
            {"step": "lint", "reason": "no staged files"},
            {"step": "tests", "reason": "no staged files"},
        ]
        assert result.skip_summary == "lint=no staged files, tests=no staged files"
        # extra carries the same facts for library/MCP callers.
        assert result.extra["degraded"] is True
        assert result.extra["skipped_steps"] == result.skipped_steps
        assert result.extra["allow_skips"] is False

    def test_summary_marks_skips_and_never_uses_a_checkmark(self, tmp_path):
        repo = _repo(tmp_path)
        summary = _manager(repo).run_all().summary

        assert "~ lint — skipped (no staged files)" in summary
        assert "~ tests — skipped (no staged files)" in summary
        assert "✓ lint" not in summary
        assert "✓ tests" not in summary
        assert "✓ secrets" in summary, "secrets really did run on this tree"

    def test_staged_tree_has_no_skip_markers(self, tmp_path):
        repo = _repo(tmp_path)
        _stage(repo, "clean.py", "x = 1\n")
        result = _manager(repo).run_all()

        assert result.passed is True
        assert result.degraded is False
        assert result.skipped_steps == []
        assert result.skip_summary == ""
        assert "~" not in result.summary
        assert "skipped" not in result.summary.lower()

    def test_missing_linter_is_a_skip_not_a_pass(self, tmp_path, monkeypatch):
        repo = _repo(tmp_path)
        _stage(repo, "clean.py", "x = 1\n")
        # Keep `git` reachable (staged-file discovery) while ruff/flake8 are
        # not — the lint gate cannot grade anything on this PATH.
        fake_bin = tmp_path / "bin-no-linters"
        fake_bin.mkdir()
        os.symlink(shutil.which("git"), fake_bin / "git")
        monkeypatch.setenv("PATH", str(fake_bin))
        result = _manager(repo).run_all()

        assert result.degraded is True
        lint = [s for s in result.skipped_steps if s["step"] == "lint"]
        assert lint == [{"step": "lint", "reason": "no linter on PATH"}]

    def test_config_disabled_guards_are_not_degradations(self, tmp_path):
        """A disabled gate never runs — only a gate that RAN and skipped counts."""
        repo = _repo(tmp_path)
        result = _manager(repo, lint=False, tests=False).run_all()

        assert result.skipped_steps == []
        assert result.degraded is False

    def test_absent_lsp_server_is_a_skip_not_a_clean_pass(self, tmp_path, monkeypatch):
        """`run_lsp_check` returns [] for a missing server — that is not "clean".

        Pinned with a patched tool lookup: CI installs python-lsp-server (a dev
        extra), so "pylsp is missing" is a property of the environment this test
        must not depend on.
        """
        repo = _repo(tmp_path)
        _stage(repo, "clean.py", "x = 1\n")
        monkeypatch.setattr("engine.guard_manager.find_lsp_tool", lambda tool: None)
        result = _manager(repo, lsp=True, lsp_tools=["pylsp"]).run_all()

        assert result.passed is True
        assert result.degraded is True
        assert result.skipped_steps == [
            {"step": "lsp", "reason": "no LSP tool on PATH (pylsp not installed)"}
        ]
        assert "~ lsp — skipped (no LSP tool on PATH (pylsp not installed))" in result.summary

    def test_installed_lsp_server_is_graded_not_skipped(self, tmp_path, monkeypatch):
        """With the server present the LSP gate grades normally (no skip marker)."""
        repo = _repo(tmp_path)
        _stage(repo, "clean.py", "x = 1\n")
        gm = _manager(repo, lsp=True, lsp_tools=["pylsp"])
        monkeypatch.setattr("engine.guard_manager.find_lsp_tool", lambda tool: f"/fake/bin/{tool}")
        monkeypatch.setattr("engine.guard_manager.run_lsp_check", lambda *a, **kw: [])
        result = gm.run_all()

        assert [s for s in result.skipped_steps if s["step"] == "lsp"] == []
        assert result.degraded is False


class TestConsoleAndExitCode:
    """Criterion 2 + 4: DEGRADED PASS line, exit 0 only with allow_skips."""

    def test_clean_tree_with_allow_skips_prints_degraded_and_exits_0(self, tmp_path):
        repo = _repo(tmp_path)
        _write_config(repo, {"test_command": "echo ok", "allow_skips": True})

        result = _run_cli("guard", cwd=repo)

        assert result.returncode == 0, f"stdout={result.stdout} stderr={result.stderr}"
        assert "Tier 1: DEGRADED PASS (skips: lint=no staged files, tests=no staged files)" in (
            result.stdout
        )
        assert "~ lint — skipped (no staged files)" in result.stdout
        assert "Tier 1 Guards: PASS" not in result.stdout, (
            "a degraded run must never print the green header"
        )

    def test_clean_tree_without_allow_skips_exits_2(self, tmp_path):
        repo = _repo(tmp_path)
        _write_config(repo, {"test_command": "echo ok"})

        result = _run_cli("guard", cwd=repo)

        assert result.returncode == 2, f"stdout={result.stdout} stderr={result.stderr}"
        assert "DEGRADED PASS" in result.stdout
        assert "guards.allow_skips" in result.stderr

    def test_staged_tree_still_prints_the_green_header(self, tmp_path):
        repo = _repo(tmp_path)
        _write_config(repo, {"test_command": "echo ok"})
        _stage(repo, "clean.py", "x = 1\n")

        result = _run_cli("guard", cwd=repo)

        assert result.returncode == 0, f"stdout={result.stdout} stderr={result.stderr}"
        assert "Tier 1 Guards: PASS" in result.stdout
        assert "skipped" not in result.stdout.lower()


class TestInitDefault:
    """Criterion 3: fresh init writes allow_skips: true."""

    def test_init_writes_allow_skips_true(self, tmp_path):
        repo = _repo(tmp_path)
        with open(os.path.join(repo, "app.py"), "w") as f:
            f.write("x = 1\n")

        result = _run_cli("init", cwd=repo)
        assert result.returncode == 0, f"stdout={result.stdout} stderr={result.stderr}"

        with open(os.path.join(repo, ".gitreins", "config.yaml")) as f:
            config = yaml.safe_load(f)

        assert config["guards"]["allow_skips"] is True

    def test_init_fill_missing_keeps_existing_choice(self, tmp_path):
        """An explicit false is user-authored and must survive a re-init."""
        from gitreins.cli import _detect_language, _fill_missing_guards

        lang = _detect_language(".")
        guards = {"allow_skips": False}
        _fill_missing_guards(guards, lang, "pytest", [])

        assert guards["allow_skips"] is False


class TestJudgeRuntimeSkips:
    """Criterion 5: a runtime skip reaches the stage record / verdict.json."""

    def test_runtime_skip_marks_the_stage(self, tmp_path, monkeypatch):
        from engine.pipeline import Pipeline, _lint_step_run, degradation_warning

        workdir = str(tmp_path / "tree")
        os.makedirs(workdir)
        # `sh` must stay reachable for the step to run at all; ruff must not.
        fake_bin = tmp_path / "bin-no-linters"
        fake_bin.mkdir()
        for tool in ("sh", "bash"):
            resolved = shutil.which(tool)
            if resolved:
                os.symlink(resolved, fake_bin / tool)
        monkeypatch.setenv("PATH", str(fake_bin))

        config = {
            "pipeline": {
                "stages": [
                    {
                        "id": "tier1",
                        "parallel": True,
                        "on": ["pre-eval"],
                        "steps": [
                            {"id": "lint", "type": "script", "run": _lint_step_run("ruff check .")}
                        ],
                    }
                ]
            }
        }
        result = Pipeline(config, workdir).run(
            {"id": "T", "title": "t", "criteria": []}, trigger="pre-eval"
        )
        tier1 = result["stages"]["tier1"]

        assert result["passed"] is True, "a skip must not flip the verdict"
        assert tier1["degraded"] is True
        assert tier1["skipped_steps"] == ["lint"]
        assert "skipped at runtime" in tier1["degradation_reason"]
        assert "lint" in degradation_warning(tier1)

    def test_no_sentinel_no_degradation(self, tmp_path):
        from engine.pipeline import Pipeline

        workdir = str(tmp_path / "tree2")
        os.makedirs(workdir)
        config = {
            "pipeline": {
                "stages": [
                    {
                        "id": "tier1",
                        "parallel": True,
                        "on": ["pre-eval"],
                        "steps": [{"id": "tests", "type": "script", "run": "echo 3 passed"}],
                    }
                ]
            }
        }
        result = Pipeline(config, workdir).run(
            {"id": "T", "title": "t", "criteria": []}, trigger="pre-eval"
        )

        assert "degraded" not in result["stages"]["tier1"]

    def test_sentinel_parser_ignores_ordinary_output(self):
        from engine.pipeline import parse_skip_sentinels

        assert parse_skip_sentinels("3 passed in 1.2s") == []
        assert parse_skip_sentinels("GITREINS_SKIP: lint=no linter on PATH") == [
            ("lint", "no linter on PATH")
        ]
        assert parse_skip_sentinels("junk GITREINS_SKIP: tests") == [
            ("tests", "reason not recorded")
        ]
