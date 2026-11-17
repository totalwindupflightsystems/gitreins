"""GR-227: `gitreins doctor --json` is the machine surface it claims to be.

The failure mode this pins: ``--json`` was honored only when
``doctor.is_installed()`` returned true. Every other repo — including a repo
that has hooks git never reaches, or one that was never installed at all — fell
through to the human ``.gitleaks.toml`` table, so stdout was prose, the command
still exited 0, and a caller's only signal was a downstream parse error
(``jq: parse error: Invalid numeric literal at line 1, column 9``).

Two things are therefore asserted here, in this order:

1. ``--json`` PARSES on every repo state — one JSON object, never prose.
2. The ids it reports are the SAME ids the human table prints. The human table
   is the source of truth for what a check is called; the JSON key ``id`` is
   sourced from the same ``Check.name``, and this test proves they agree by
   reading the ids back out of both surfaces.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

from engine import install_manifest
from engine.install_manifest import Artifact

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK_BODY = "#!/bin/sh\n# gitreins-managed — test hook\nexit 0\n"

# `  [OK] config: every key is one this version reads`
_TABLE_ROW = re.compile(r"^\s*\[(?:OK|WARN|FAIL|SKIP)\]\s+(\S+?):")

VALID_GITLEAKS = """title = "test"
[allowlist]
paths = ['''tests/fixtures/.*''']
"""


def _repo(tmp_path) -> str:
    workdir = tmp_path / "repo"
    (workdir / ".git" / "hooks").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=str(workdir), check=True)
    return str(workdir)


def _install(workdir: str) -> None:
    """A minimal healthy install: managed hooks, config, manifest."""
    artifacts = []
    for name in install_manifest.HOOK_NAMES:
        path = install_manifest._hook_path(workdir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write(HOOK_BODY)
        os.chmod(path, 0o755)
        artifacts.append(
            Artifact(os.path.relpath(path, workdir), "hook", install_manifest.sha256_file(path))
        )
    config = os.path.join(workdir, ".gitreins", "config.yaml")
    os.makedirs(os.path.dirname(config), exist_ok=True)
    with open(config, "w") as handle:
        handle.write("guards:\n  secrets: true\n")
    artifacts.append(
        Artifact(".gitreins/config.yaml", "config", install_manifest.sha256_file(config))
    )
    install_manifest.record_install(workdir, artifacts)


def _run(workdir: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "gitreins", "doctor", *args],
        cwd=workdir,
        capture_output=True,
        text=True,
        env=dict(os.environ, PYTHONPATH=REPO_ROOT),
    )


def _table_ids(stdout: str) -> list[str]:
    return [match.group(1) for match in map(_TABLE_ROW.match, stdout.splitlines()) if match]


def test_json_parses_on_a_repo_that_was_never_installed(tmp_path) -> None:
    """The dogfood case: no manifest, no managed hooks — and a .gitleaks.toml.

    The allowlist table is what used to reach stdout here. It must be gone: JSON
    mode answers with checks and never runs the allowlist section.
    """
    workdir = _repo(tmp_path)
    with open(os.path.join(workdir, ".gitleaks.toml"), "w") as handle:
        handle.write(VALID_GITLEAKS)

    result = _run(workdir, "--json")

    payload = json.loads(result.stdout)
    assert isinstance(payload, dict), f"one JSON object expected, got {type(payload).__name__}"
    assert "gitleaks config doctor" not in result.stdout
    assert isinstance(payload["checks"], list) and payload["checks"]
    for entry in payload["checks"]:
        assert entry["id"] and entry["status"] in {"ok", "warn", "fail", "skip"}
    # An uninstalled repo is not healthy, and JSON mode says so instead of
    # exiting 0 on prose.
    assert payload["ok"] is False
    assert result.returncode == 1, result.stdout + result.stderr


def test_json_ids_equal_the_human_table_ids_on_a_healthy_install(tmp_path) -> None:
    workdir = _repo(tmp_path)
    _install(workdir)

    human = _run(workdir)
    as_json = _run(workdir, "--json")

    assert human.returncode == 0, human.stdout + human.stderr
    assert as_json.returncode == 0, as_json.stdout + as_json.stderr
    payload = json.loads(as_json.stdout)
    assert payload["ok"] is True
    human_ids = _table_ids(human.stdout)
    assert human_ids, human.stdout
    assert [entry["id"] for entry in payload["checks"]] == human_ids


def test_json_reports_an_unhealthy_install_as_a_check(tmp_path) -> None:
    """A broken install exits 1 in JSON mode too — the human exit contract."""
    workdir = _repo(tmp_path)
    _install(workdir)
    os.remove(os.path.join(workdir, ".git", "hooks", "pre-commit"))

    result = _run(workdir, "--json")

    payload = json.loads(result.stdout)
    hooks = next(entry for entry in payload["checks"] if entry["id"] == "hooks")
    assert hooks["status"] == "fail"
    assert payload["ok"] is False
    assert result.returncode == 1


def test_json_never_rewrites_the_allowlist_under_fix(tmp_path) -> None:
    """`--fix` belongs to the allowlist section, which JSON mode does not run.

    The mutation must not happen behind a machine-readable stdout, and the
    caller must be told rather than left guessing.
    """
    workdir = _repo(tmp_path)
    config = os.path.join(workdir, ".gitleaks.toml")
    legacy = "title = \"test\"\n[allowlist]\npaths = ['''*.log''']\n"
    with open(config, "w") as handle:
        handle.write(legacy)

    result = _run(workdir, "--fix", "--json")

    json.loads(result.stdout)
    with open(config) as handle:
        assert handle.read() == legacy
    assert "--fix was not applied" in result.stderr
