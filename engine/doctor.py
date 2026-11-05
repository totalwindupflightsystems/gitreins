"""One health check for an installed repo (GR-162).

`gitreins doctor` used to look only at `.gitleaks.toml`. That answers "is my
allowlist syntactically valid" and nothing else, so a repo could pass doctor
while its hooks never ran — the exact fail-open shape the guard is supposed to
prevent. This module asks the questions an operator actually has:

* does the config say what this version reads? (delegates to config_schema)
* are the hooks present, ours, executable, and actually the ones git will run?
  (`core.hooksPath` silently displaces `.git/hooks`, which disarms every guard
  without touching a single hook file)
* are the external tools the guard shells out to on PATH?
* does the install manifest still match what is on disk?
* is the gitreins that runs at commit time the version in this checkout?

Statuses: ``ok``, ``warn``, ``fail``, ``skip``. Only ``fail`` changes the exit
code; ``warn`` is a degraded-but-working state (e.g. no gitleaks installed means
the secrets lane leans on the builtin scanner).
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
from typing import Any

from engine import install_manifest
from engine.config_schema import validate_config_file

OK, WARN, FAIL, SKIP = "ok", "warn", "fail", "skip"


@dataclasses.dataclass
class Check:
    name: str
    status: str
    detail: str
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": self.name, "status": self.status, "detail": self.detail}
        if self.hint:
            payload["hint"] = self.hint
        return payload

    def render(self) -> str:
        label = {"ok": "OK", "warn": "WARN", "fail": "FAIL", "skip": "SKIP"}[self.status]
        line = f"  [{label}] {self.name}: {self.detail}"
        return f"{line}\n         -> {self.hint}" if self.hint else line


def is_installed(workdir: str) -> bool:
    """True when GitReins has been INSTALLED here — not merely configured.

    ``gitreins init`` writes ``.gitreins/config.yaml`` without touching hooks, so
    a config alone is a legitimate state, not a broken install. Installation is
    proven by the manifest or by a managed hook actually being present.
    """
    if os.path.isfile(install_manifest.manifest_path(workdir)):
        return True
    for name in install_manifest.HOOK_NAMES:
        if install_manifest.is_managed_hook(install_manifest._hook_path(workdir, name)):
            return True
    return False


def _git_config(workdir: str, key: str) -> str:
    return subprocess.run(
        ["git", "config", "--get", key],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()


def check_config(workdir: str) -> Check:
    path = os.path.join(workdir, ".gitreins", "config.yaml")
    if not os.path.isfile(path):
        return Check("config", WARN, "no .gitreins/config.yaml — the guard uses built-in defaults")
    issues = validate_config_file(path)
    errors = [i for i in issues if i.severity == "error"]
    if errors:
        return Check("config", FAIL, f"{len(errors)} blocking problem(s)", errors[0].render())
    if issues:
        return Check(
            "config",
            WARN,
            f"{len(issues)} setting(s) this version does not read",
            issues[0].render() + " — run `gitreins config-validate`",
        )
    return Check("config", OK, "every key is one this version reads")


def check_hooks(workdir: str) -> Check:
    """Hooks present, ours, executable, and actually reached by git."""
    hooks_path = _git_config(workdir, "core.hooksPath")
    problems: list[str] = []
    for name in install_manifest.HOOK_NAMES:
        path = install_manifest._hook_path(workdir, name)
        if not os.path.isfile(path):
            problems.append(f"{name} missing")
        elif not install_manifest.is_managed_hook(path):
            problems.append(f"{name} is not a gitreins hook")
        elif not os.access(path, os.X_OK):
            problems.append(f"{name} not executable")

    if hooks_path:
        # A relative core.hooksPath is resolved against the work tree, so the
        # only way to be sure is to compare where the check resolved.
        resolved = install_manifest._hook_path(workdir, "pre-commit")
        if not os.path.isfile(resolved):
            return Check(
                "hooks",
                FAIL,
                f"core.hooksPath={hooks_path!r} points at a directory with no pre-commit hook",
                "git will run that directory instead of .git/hooks, so the guards never fire",
            )
    if problems:
        broken = [p for p in problems if "missing" in p or "not executable" in p]
        return Check(
            "hooks",
            FAIL if broken else WARN,
            "; ".join(problems),
            "run `gitreins install` to refresh the hooks, or `gitreins enable` after a disable",
        )
    return Check("hooks", OK, "pre-commit and pre-push present, managed, executable")


def check_tools(workdir: str) -> Check:
    """External binaries the guard shells out to."""
    required = {"git": "git"}
    optional = {"gitleaks": "secrets lane (falls back to the builtin scanner)"}
    missing_required = [label for binary, label in required.items() if not shutil.which(binary)]
    if missing_required:
        return Check("tools", FAIL, f"required tool not on PATH: {', '.join(missing_required)}")
    missing_optional = [
        f"{binary} -> {why}" for binary, why in optional.items() if not shutil.which(binary)
    ]
    if missing_optional:
        return Check("tools", WARN, "degraded: " + "; ".join(missing_optional))
    return Check("tools", OK, "git, gitleaks present")


def check_manifest(workdir: str) -> Check:
    manifest = install_manifest.read_manifest(workdir)
    artifacts = manifest.get("artifacts") or []
    if not artifacts:
        return Check(
            "manifest",
            WARN,
            "no install manifest — artifacts cannot be attributed to GitReins",
            "re-run `gitreins install` to record what this repo owns",
        )
    disabled = bool(manifest.get("disabled"))
    # A disabled install is SUPPOSED to have no hooks: reporting them as "gone"
    # would make `doctor` cry drift on every repo the operator deliberately
    # disarmed. Only config-like artifacts are expected to survive a disable.
    expected = [
        a for a in artifacts if a.get("path") and not (disabled and a.get("kind") == "hook")
    ]
    missing = [a["path"] for a in expected if not os.path.isfile(os.path.join(workdir, a["path"]))]
    if missing:
        return Check(
            "manifest",
            WARN,
            f"{len(missing)} recorded artifact(s) gone: {', '.join(missing)}",
            "re-run `gitreins install` to restore them",
        )
    state = "disabled (hooks intentionally removed)" if disabled else "armed"
    return Check("manifest", OK, f"{len(artifacts)} artifact(s) recorded; enforcement {state}")


def check_version(workdir: str) -> Check:
    """The gitreins that runs at commit time vs the version in this checkout."""
    try:
        from importlib.metadata import version as dist_version

        installed = dist_version("gitreins")
    except Exception:
        return Check("version", SKIP, "gitreins is not installed as a distribution here")
    checkout = os.path.join(workdir, "pyproject.toml")
    if not os.path.isfile(checkout):
        return Check("version", OK, f"installed {installed}")
    try:
        with open(checkout) as handle:
            text = handle.read()
    except OSError:
        return Check("version", OK, f"installed {installed}")
    if 'name = "gitreins"' not in text and "name = 'gitreins'" not in text:
        return Check("version", OK, f"installed {installed}")
    pinned = ""
    for line in text.splitlines():
        if line.strip().startswith("version ="):
            pinned = line.split("=", 1)[1].strip().strip('"').strip("'")
            break
    if pinned and pinned != installed:
        return Check(
            "version",
            FAIL,
            f"checkout is {pinned} but the installed distribution is {installed}",
            "the commit hook runs the INSTALLED version — reinstall before trusting a green run",
        )
    return Check("version", OK, f"installed {installed} matches this checkout")


def check_history(workdir: str) -> Check:
    path = os.path.join(workdir, ".gitreins")
    if not os.path.isdir(path):
        return Check("history", SKIP, "no .gitreins/ directory")
    if not os.access(path, os.W_OK):
        return Check("history", FAIL, f"{path} is not writable", "verdicts cannot be recorded")
    return Check("history", OK, ".gitreins/ present and writable")


ALL_CHECKS = (
    check_config,
    check_hooks,
    check_tools,
    check_manifest,
    check_version,
    check_history,
)


def run_checks(workdir: str) -> list[Check]:
    return [check(workdir) for check in ALL_CHECKS]


def has_failures(checks: list[Check]) -> bool:
    return any(check.status == FAIL for check in checks)


def format_checks(checks: list[Check]) -> str:
    lines = [check.render() for check in checks]
    failures = sum(1 for c in checks if c.status == FAIL)
    warnings = sum(1 for c in checks if c.status == WARN)
    if failures:
        lines.append(f"Summary: {failures} failing, {warnings} warning(s)")
    elif warnings:
        lines.append(f"Summary: healthy with {warnings} warning(s)")
    else:
        lines.append("Summary: all checks passed")
    return "\n".join(lines)
