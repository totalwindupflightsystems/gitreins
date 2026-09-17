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
