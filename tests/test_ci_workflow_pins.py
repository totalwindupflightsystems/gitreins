"""Text-level gates on the CI workflow's tool installs (INT-CI-8).

The workflow installs analysis tools on a fresh runner, so an unpinned
``@latest`` is resolved at run time: it can move the Go toolchain requirement
forward (staticcheck v0.8.1 needs go >= 1.26.0, so each job downloads a
toolchain first) and it lets one upstream release change the job. These tests
refuse to let the pin or the retry fall out again.
"""

import re
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml"


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def _install_step() -> str:
    """The 'Install system tools for LSP/static analysis' step, as text."""
    lines = _text().splitlines()
    start = next(i for i, ln in enumerate(lines) if "name: Install system tools" in ln)
    block = [lines[start]]
    for ln in lines[start + 1 :]:
        if ln.lstrip().startswith("- name:"):
            break
        block.append(ln)
    return "\n".join(block)


class TestWorkflowPins:
    def test_workflow_parses_as_yaml(self):
        doc = yaml.safe_load(_text())
        assert "test" in doc["jobs"]

    def test_no_floating_latest_install(self):
        """No install line may resolve its target at run time."""
        offenders = [
            line.strip()
            for line in _text().splitlines()
            if re.search(r"@latest\b", line) and not line.lstrip().startswith("#")
        ]
        assert offenders == []

    def test_staticcheck_version_is_pinned(self):
        match = re.search(r"STATICCHECK_VERSION:\s*(\S+)", _text())
        assert match is not None, "the CI tool step must pin staticcheck explicitly"
        assert re.fullmatch(r"v\d+\.\d+\.\d+", match.group(1))

    def test_install_consumes_the_pinned_version(self):
        install = re.search(
            r"go install\s+\"?honnef\.co/go/tools/cmd/staticcheck@([^\"\s]+)",
            _install_step(),
        )
        assert install is not None, "the tool step must install staticcheck"
        assert install.group(1) == "${STATICCHECK_VERSION}"


class TestInstallResilience:
    def test_install_retries(self):
        assert "for attempt in 1 2 3" in _install_step()

    def test_install_can_bypass_the_module_proxy(self):
        assert 'goproxy="direct"' in _install_step()

    def test_failed_install_fails_the_step(self):
        step = _install_step()
        assert 'if [ "$installed" -ne 1 ]' in step
        assert re.search(r"^\s+exit 1$", step, re.MULTILINE) is not None

    def test_installed_binary_is_verified(self):
        assert '"$HOME/go/bin/staticcheck" -version' in _install_step()


def _steps() -> list[dict]:
    """Every `- name: ...` step in the workflow's test job, in order."""
    job = yaml.safe_load(_text())["jobs"]["test"]
    return job["steps"]


# ── GR-GAP-063: formatting is gated in CI ─────────────────────────


class TestRuffFormatGate:
    """`ruff format` had no gate anywhere: this workflow ran no ruff at all,
    and the tree drifted from the formatter with every gate green. These tests
    pin the step that closes that hole — including the flag, because `--diff`
    and a bare `ruff format` are the false-green shapes that would make the
    step decorative."""

    def _format_step(self) -> dict:
        matches = [
            step
            for step in _steps()
            if isinstance(step.get("run"), str) and "ruff format" in step["run"]
        ]
        assert len(matches) == 1, f"expected exactly one ruff format step, found {len(matches)}"
        return matches[0]

    def test_workflow_has_a_ruff_format_step(self):
        assert self._format_step()["name"]

    def test_step_uses_check_not_diff(self):
        run = self._format_step()["run"]
        assert "ruff format --check" in run
        assert "--diff" not in run

    def test_step_is_not_a_bare_format(self):
        """A bare `ruff format .` rewrites the runner's checkout and exits 0 —
        it would pass forever and hide exactly the drift it is meant to catch."""
        assert re.search(r"ruff format\s+\.\s*$", _text(), re.MULTILINE) is None

    def test_step_covers_the_whole_tree(self):
        assert self._format_step()["run"].strip().endswith(".")

    def test_step_runs_before_the_guards(self):
        """Formatting drift reads as a clear, named failure in the workflow
        step list instead of only surfacing inside the guard's lint verdict."""
        names = [step.get("name", "") for step in _steps()]
        format_idx = next(i for i, n in enumerate(names) if "format" in n.lower())
        guard_idx = next(i for i, n in enumerate(names) if n == "Run guards")
        assert format_idx < guard_idx

    def test_ruff_is_installed_before_the_step(self):
        """The step must not be the first thing to need ruff: `Install
        dependencies` (`pip install -e ".[dev]"`, which pins ruff>=0.5) has to
        come first, or the step fails on a clean runner."""
        steps = _steps()
        format_idx = next(i for i, s in enumerate(steps) if "ruff format" in str(s.get("run", "")))
        install_idx = next(
            i for i, s in enumerate(steps) if s.get("name") == "Install dependencies"
        )
        assert install_idx < format_idx
        assert ".[dev]" in steps[install_idx]["run"]

    def test_step_runs_on_push_and_pull_request(self):
        """The gate is only a gate where it runs: the workflow's triggers must
        include both push and pull_request (the step inherits the job's
        triggers — there is no per-step `if` narrowing it)."""
        triggers = yaml.safe_load(_text())
        # PyYAML parses the bare key `on` as the boolean True.
        on = triggers.get("on", triggers.get(True))
        assert "push" in on and "pull_request" in on
        assert "if" not in self._format_step()
