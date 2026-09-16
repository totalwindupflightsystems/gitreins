"""DF-GITREINS-POC-16 — language detection and judge/guard Tier 1 parity.

Hermetic: synthetic trees under tmp_path, no network, no LLM, no reliance on
the host layout beyond the repo's own interpreter. The two tests that shell out
to pytest carry a hard timeout.
"""

import os
import shutil
import subprocess
import sys

import pytest

from engine import lang_detect
from engine.pipeline import (
    Pipeline,
    _default_tier1_steps,
    _lint_step_run,
    degradation_warning,
    load_pipeline_config,
    tier1_plan,
)

PYTEST_RUN_TIMEOUT = 90


def _write_config(workdir, test_command: str | None = None, lint: bool = True) -> None:
    """Write a minimal .gitreins/config.yaml (no pipeline block)."""
    config_dir = os.path.join(str(workdir), ".gitreins")
    os.makedirs(config_dir, exist_ok=True)
    lines = [
        "defaults:",
        "  check_for_updates: false",  # keep these tests off the network
        "guards:",
        f"  lint: {str(lint).lower()}",
        "  tests: true",
        "  test_mode: full",
    ]
    if test_command:
        lines.append(f'  test_command: "{test_command}"')
    with open(os.path.join(config_dir, "config.yaml"), "w") as f:
        f.write("\n".join(lines) + "\n")


def _run_tier1(workdir) -> dict:
    """Run the default pipeline's tier1 stage on *workdir* (pre-commit only)."""
    config = load_pipeline_config(str(workdir))
    pipeline = Pipeline(config, str(workdir))
    result = pipeline.run({"id": "t", "title": "t", "criteria": []}, trigger="pre-commit")
    return result["stages"]["tier1"]


def _git_init(workdir) -> None:
    subprocess.run(["git", "init", "-q"], cwd=str(workdir), check=True, timeout=60)


# ── Detection: the tables are the single source of truth ────────────────


class TestDetectionTables:
    def test_every_detectable_language_has_commands(self):
        for _marker, language in lang_detect.SIGNATURE_FILES:
            assert lang_detect.lint_test_commands(language), f"no commands for {language}"
        for _ext, language in lang_detect.SOURCE_EXTENSIONS.items():
            assert lang_detect.lint_test_commands(language), f"no commands for {language}"

    def test_undetectable_language_has_no_commands(self):
        assert lang_detect.lint_test_commands(None) is None
        assert lang_detect.lint_test_commands("brainfuck") is None

    def test_python_packaging_markers_are_the_packaging_subset(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        assert lang_detect.python_packaging_present(repo_root), "the repo itself has pyproject.toml"


class TestSignatureDetection:
    def test_pyproject_detected_as_python(self, tmp_path):
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        assert lang_detect.detect_language(str(tmp_path)) == "python"
        assert lang_detect.signature_languages(str(tmp_path)) == ["python"]

    def test_signature_table_order_picks_primary(self, tmp_path):
        """go.mod outranks pyproject.toml — table order is part of the contract."""
        (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
        (tmp_path / "go.mod").write_text("module example.com/x\n")
        assert lang_detect.signature_languages(str(tmp_path)) == ["go", "python"]
        assert lang_detect.detect_language(str(tmp_path)) == "go"

    def test_wildcard_signature_matches(self, tmp_path):
        (tmp_path / "MyApp.csproj").write_text("<Project />\n")
        assert lang_detect.detect_language(str(tmp_path)) == "csharp"

    def test_gemspec_and_tsconfig_are_signatures(self, tmp_path):
        (tmp_path / "mygem.gemspec").write_text("Gem::Specification.new {}\n")
        assert lang_detect.detect_language(str(tmp_path)) == "ruby"
        other = tmp_path / "ts"
        other.mkdir()
        (other / "tsconfig.json").write_text("{}\n")
        assert lang_detect.detect_language(str(other)) == "js"


# ── Detection: source-extension fallback ────────────────────────────────


class TestExtensionFallback:
    def test_root_py_file_is_python_without_any_signature(self, tmp_path):
        """The DF-GITREINS-POC-16 repro: a plain .py tree is Python."""
        (tmp_path / "test_broken.py").write_text("def test_broken():\n    assert 1 == 2\n")
        assert lang_detect.detect_language(str(tmp_path)) == "python"

    def test_fallback_uses_git_ls_files(self, tmp_path):
        """Untracked (non-ignored) files inside a git repo still count."""
        if not shutil.which("git"):
            pytest.skip("git not available")
        _git_init(tmp_path)
        (tmp_path / "app.py").write_text("print('hi')\n")
        assert lang_detect.detect_language(str(tmp_path)) == "python"

    def test_gitignored_tree_is_not_a_language(self, tmp_path):
        if not shutil.which("git"):
            pytest.skip("git not available")
        _git_init(tmp_path)
        (tmp_path / ".gitignore").write_text("vendor/\n")
        os.makedirs(tmp_path / "vendor", exist_ok=True)
        (tmp_path / "vendor" / "lib.py").write_text("x = 1\n")
        assert lang_detect.detect_language(str(tmp_path)) is None

    def test_tests_dir_alone_is_not_detected(self, tmp_path):
        """A tree whose only sources are tests has no product code to grade."""
        os.makedirs(tmp_path / "tests", exist_ok=True)
        (tmp_path / "tests" / "test_x.py").write_text("def test_x():\n    pass\n")
        assert lang_detect.detect_language(str(tmp_path)) is None

    def test_tool_and_build_dirs_are_skipped(self, tmp_path):
        for rel in (".venv/lib/python3.10/site-packages/x.py", "node_modules/p/y.js", "build/z.rs"):
            path = tmp_path / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x\n")
        assert lang_detect.detect_language(str(tmp_path)) is None

    def test_foreign_ancestor_repo_is_not_used(self, tmp_path):
        """An enclosing repo's file list must never answer for a subdirectory."""
        if not shutil.which("git"):
            pytest.skip("git not available")
        outer = tmp_path / "outer"
        outer.mkdir()
        _git_init(outer)
        (outer / "main.go").write_text("package main\n")
        inner = outer / "inner"
        inner.mkdir()
        (inner / "app.py").write_text("x = 1\n")
        # The outer repo sees main.go (Go); the inner dir must answer from its
        # own tree — the walk, not the enclosing repo's index.
        assert lang_detect.detect_language(str(inner)) == "python"

    def test_extension_languages_ranked_by_count(self, tmp_path):
        (tmp_path / "a.py").write_text("x\n")
        (tmp_path / "b.py").write_text("x\n")
        (tmp_path / "c.ts").write_text("x\n")
        assert lang_detect.extension_languages(str(tmp_path)) == ["python", "js"]

    def test_readme_only_tree_detects_nothing(self, tmp_path):
        (tmp_path / "README.md").write_text("# nothing here\n")
        assert lang_detect.detect_languages(str(tmp_path)) == []
        assert lang_detect.detect_language(str(tmp_path)) is None

    def test_go_source_tree_detected_without_signature_file(self, tmp_path):
        os.makedirs(tmp_path / "cmd", exist_ok=True)
        (tmp_path / "cmd" / "main.go").write_text("package main\n")
        assert lang_detect.detect_language(str(tmp_path)) == "go"


class TestSqlDetection:
    def test_migrations_dir_counts_as_sql(self, tmp_path):
        os.makedirs(tmp_path / "migrations", exist_ok=True)
        assert lang_detect.has_sql_sources(str(tmp_path)) is True

    def test_no_sql_anywhere(self, tmp_path):
        (tmp_path / "main.py").write_text("x = 1\n")
        assert lang_detect.has_sql_sources(str(tmp_path)) is False


# ── Tier 1 step set (parity with the guard gate) ────────────────────────


class TestTier1StepSet:
    @pytest.mark.parametrize(
        "marker",
        ["pyproject.toml", "go.mod", "Cargo.toml", "package.json", "Makefile", "Gemfile"],
    )
    def test_packaging_file_repos_unchanged_step_ids(self, tmp_path, marker):
        """Criterion 2: a repo with a packaging file still gets secrets/lint/tests."""
        (tmp_path / marker).write_text("x\n")
        steps, marker_info = tier1_plan(str(tmp_path), {})
        assert [s["id"] for s in steps] == ["secrets", "lint", "tests"]
        assert marker_info["coverage"] == "secrets+lint+tests"
        assert marker_info["degraded"] is False

    def test_extension_fallback_repo_gets_the_same_step_set(self, tmp_path):
        """Criterion 1: a marker-less .py tree is no longer secrets-only."""
        (tmp_path / "app.py").write_text("x = 1\n")
        steps, marker_info = tier1_plan(str(tmp_path), {})
        assert [s["id"] for s in steps] == ["secrets", "lint", "tests"]
        assert marker_info["degraded"] is False

    def test_lint_disabled_is_a_config_choice_not_a_degradation(self, tmp_path):
        (tmp_path / "app.py").write_text("x = 1\n")
        steps, marker_info = tier1_plan(str(tmp_path), {"guards": {"lint": False}})
        assert [s["id"] for s in steps] == ["secrets", "tests"]
        assert marker_info["coverage"] == "secrets+tests"
        assert marker_info["degraded"] is False

    def test_default_tier1_steps_keeps_its_list_shape(self, tmp_path):
        (tmp_path / "app.py").write_text("x = 1\n")
        steps = _default_tier1_steps(str(tmp_path))
        assert isinstance(steps, list)
        assert [s["id"] for s in steps] == ["secrets", "lint", "tests"]

    def test_tests_step_reuses_the_guards_command_resolver(self, tmp_path, monkeypatch):
        """A configured `uv run pytest` on a uv-less machine resolves the same
        way the guard resolves it (never a 127 'command not found')."""
        (tmp_path / "app.py").write_text("x = 1\n")
        monkeypatch.setattr(shutil, "which", lambda _name: None)
        steps, _marker = tier1_plan(
            str(tmp_path), {"guards": {"test_command": "uv run pytest -x --tb=short"}}
        )
        tests_step = next(s for s in steps if s["id"] == "tests")
        assert tests_step["run"] == f"{sys.executable} -m pytest -x --tb=short"

    def test_tests_step_honors_configured_command_and_timeout(self, tmp_path):
        (tmp_path / "app.py").write_text("x = 1\n")
        steps, _marker = tier1_plan(
            str(tmp_path), {"guards": {"test_command": "my-runner check", "test_timeout": 42}}
        )
        tests_step = next(s for s in steps if s["id"] == "tests")
        assert tests_step["run"] == "my-runner check"
        assert tests_step["timeout"] == 42


class TestLintStepGuardSemantics:
    """A missing linter skips (guard parity); a lint finding still fails."""

    def _run(self, command: str, cwd) -> subprocess.CompletedProcess:
        return subprocess.run(
            _lint_step_run(command),
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_missing_linter_binary_is_a_skip(self, tmp_path):
        result = self._run("definitely-no-such-linter-xyz --check", tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "not found" in (result.stdout + result.stderr)

    def test_failing_linter_still_fails(self, tmp_path):
        result = self._run("false", tmp_path)
        assert result.returncode != 0


# ── Degraded marker (criterion 4) ───────────────────────────────────────


class TestDegradedMarker:
    def test_undetectable_tree_degrades_loudly(self, tmp_path):
        (tmp_path / "README.md").write_text("# docs only\n")
        steps, marker_info = tier1_plan(str(tmp_path), {})
        assert [s["id"] for s in steps] == ["secrets"]
        assert marker_info["coverage"] == "secrets-only"
        assert marker_info["degraded"] is True
        assert marker_info["skipped_steps"] == ["lint", "tests"]
        assert "no language detected" in marker_info["reason"]

        stage = _run_tier1(tmp_path)
        assert stage["passed"] is True, "the marker must not flip the verdict"
        assert stage["coverage"] == "secrets-only"
        assert stage["degraded"] is True
        assert stage["skipped_steps"] == ["lint", "tests"]
        warning = degradation_warning(stage)
        assert warning is not None
        assert "lint" in warning and "tests" in warning
        assert "gitreins guard" in warning

    def test_full_tier1_is_not_marked_degraded(self, tmp_path):
        (tmp_path / "app.py").write_text("x = 1\n")
        stage = _run_tier1(tmp_path)
        assert stage["coverage"] == "secrets+lint+tests"
        assert "degraded" not in stage
        assert degradation_warning(stage) is None

    def test_verdict_json_carries_the_marker(self, tmp_path):
        """The marker reaches the persisted verdict shape, not just the live dict."""
        import json

        (tmp_path / "README.md").write_text("# docs only\n")
        stage = _run_tier1(tmp_path)
        serialized = json.loads(json.dumps(stage))
        assert serialized["coverage"] == "secrets-only"
        assert serialized["degraded"] is True
        assert serialized["skipped_steps"] == ["lint", "tests"]
        assert "no language detected" in serialized["degradation_reason"]

        # ...and the stage definition the pipeline reads declares it.
        config = load_pipeline_config(str(tmp_path))
        stage_def = next(s for s in config["pipeline"]["stages"] if s["id"] == "tier1")
        assert stage_def["coverage"] == "secrets-only"
        assert stage_def["degraded"] is True


# ── Parity: judge tier1 FAILs exactly where the guard FAILs ─────────────


class TestJudgeGuardParity:
    def test_failing_test_fails_both_engines(self, tmp_path):
        """Criterion 1, hermetically: identical tree, both engines red on the
        same failing test, and the judge's tier1 evidence names it."""
        if not shutil.which("git"):
            pytest.skip("git not available")
        _git_init(tmp_path)
        (tmp_path / "test_broken.py").write_text("def test_broken():\n    assert 1 == 2\n")
        command = f"{sys.executable} -m pytest -x --tb=short -p no:cacheprovider"
        _write_config(tmp_path, test_command=command)
        # Stage the tree so the guard's tests step runs (it skips a clean index
        # unless guards.test_on_clean is set) — same shape as the E1 repro.
        subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True, timeout=60)

        stage = _run_tier1(tmp_path)
        assert {s["id"] for s in stage["steps"]} == {"secrets", "lint", "tests"}
        assert stage["passed"] is False, f"judge tier1 passed a failing tree: {stage}"

        tests_step = next(s for s in stage["steps"] if s["id"] == "tests")
        assert tests_step["passed"] is False
        assert "test_broken" in tests_step["output"]
        assert tests_step["data"]["exit_code"] != 0

        # The guard's own tests check on the same tree must agree.
        from engine.guard_manager import GuardManager

        guard = GuardManager(
            str(tmp_path), {"guards": {"test_command": command, "test_mode": "full"}}
        )
        guard_result = guard._check_tests()
        assert guard_result.passed is False
        assert "test_broken" in guard_result.output

    def test_passing_test_passes_both_engines(self, tmp_path):
        if not shutil.which("git"):
            pytest.skip("git not available")
        _git_init(tmp_path)
        (tmp_path / "test_ok.py").write_text(
            "def add(a, b):\n    return a + b\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
        )
        command = f"{sys.executable} -m pytest -x --tb=short -p no:cacheprovider"
        _write_config(tmp_path, test_command=command)

        stage = _run_tier1(tmp_path)
        assert {s["id"] for s in stage["steps"]} == {"secrets", "lint", "tests"}
        assert stage["passed"] is True, stage
        assert stage["coverage"] == "secrets+lint+tests"


# ── CLI: the verdict reaches the shell ──────────────────────────────────


class TestJudgeCliExitCode:
    """A FAIL verdict exits non-zero, like `gitreins guard` on the same tree."""

    def _cli(self, args, cwd):
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        env = os.environ.copy()
        env["PYTHONPATH"] = project_root + (
            os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
        )
        return subprocess.run(
            [sys.executable, os.path.join(project_root, "gitreins", "cli.py"), *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=PYTEST_RUN_TIMEOUT,
            env=env,
        )

    def _prepare(self, tmp_path, test_body: str) -> str:
        if not shutil.which("git"):
            pytest.skip("git not available")
        _git_init(tmp_path)
        (tmp_path / "test_case.py").write_text(test_body)
        command = f"{sys.executable} -m pytest -x --tb=short -p no:cacheprovider"
        _write_config(tmp_path, test_command=command)
        subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True, timeout=60)
        created = self._cli(["task", "create", "t1", "parity", "tree passes"], tmp_path)
        assert created.returncode == 0, created.stdout + created.stderr
        return command

    def test_failing_tree_exits_non_zero(self, tmp_path):
        self._prepare(tmp_path, "def test_broken():\n    assert 1 == 2\n")
        result = self._cli(["judge", "--skip-tier2", "t1"], tmp_path)
        assert result.returncode != 0, result.stdout + result.stderr
        assert "Stage tier1: FAIL" in result.stdout

    def test_passing_tree_exits_zero(self, tmp_path):
        self._prepare(
            tmp_path,
            "def add(a, b):\n    return a + b\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
        )
        result = self._cli(["judge", "--skip-tier2", "t1"], tmp_path)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Stage tier1: PASS" in result.stdout

    def test_undetectable_tree_warns_and_names_skipped_checks(self, tmp_path):
        """Criterion 4 end to end: the CLI warning names lint + tests."""
        if not shutil.which("git"):
            pytest.skip("git not available")
        _git_init(tmp_path)
        (tmp_path / "README.md").write_text("# docs only\n")
        command = f"{sys.executable} -m pytest -x --tb=short -p no:cacheprovider"
        _write_config(tmp_path, test_command=command)
        subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True, timeout=60)
        created = self._cli(["task", "create", "t1", "parity", "tree passes"], tmp_path)
        assert created.returncode == 0, created.stdout + created.stderr

        result = self._cli(["judge", "--skip-tier2", "t1"], tmp_path)
        combined = result.stdout + result.stderr
        assert "WARNING" in combined, combined
        assert "secrets-only" in combined, combined
        assert "lint" in combined and "tests" in combined, combined
        assert "gitreins guard" in combined, combined
        assert "Stage tier1: PASS" in combined, combined
