"""Hermetic contract and execution checks for the consumer Action."""

import json
import os
from pathlib import Path
import re
import subprocess
from uuid import uuid4

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "action.yml"
REFERENCE = re.compile(r"\$\{\{\s*inputs\.([\w-]+)\s*\}\}")


def resolve(manifest, supplied):
    declared = manifest["inputs"]
    referenced = set(REFERENCE.findall(yaml.safe_dump(manifest["runs"])))
    assert referenced <= declared.keys(), f"Undeclared inputs: {referenced - declared.keys()}"
    values = {}
    for name, spec in declared.items():
        assert "description" in spec and "required" in spec
        value = supplied.get(name, spec.get("default"))
        assert not (spec["required"] and not value), f"Missing required input: {name}"
        values[name] = "" if value is None else str(value)
    return values


def execute(tmp_path, supplied, guard_exit=0, judge_exit=0, outcome="pass"):
    manifest = yaml.safe_load(MANIFEST.read_text())
    assert manifest["runs"]["using"] in ("composite", "docker")
    values = resolve(manifest, supplied)
    steps = manifest["runs"]["steps"]
    commands = "\n".join(step.get("run", "") for step in steps)
    assert re.search(r"\bgitreins\s+guard\b", commands)
    assert re.search(r"\bgitreins\s+(?:judge|review)\b", commands)
    step = next(step for step in steps if step.get("id") == "evaluate")
    tmp_path = tmp_path / uuid4().hex
    tmp_path.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "gitreins"
    stub.write_text(
        '#!/bin/bash\nprintf "%s\\n" "$*" >> "$CALLS"\n'
        'if [[ "$1" == guard ]]; then exit "$GUARD_EXIT"; fi\n'
        'printf \'{"outcome":"%s","passed":%s}\\n\' "$OUTCOME" "$PASSED"\n'
        'exit "$JUDGE_EXIT"\n'
    )
    stub.chmod(0o755)
    output = tmp_path / "output"
    calls = tmp_path / "calls"
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        GITHUB_WORKSPACE=str(tmp_path),
        RUNNER_TEMP=str(tmp_path),
        GITHUB_RUN_ID="1",
        GITHUB_RUN_ATTEMPT="1",
        GITHUB_OUTPUT=str(output),
        CALLS=str(calls),
        GUARD_EXIT=str(guard_exit),
        JUDGE_EXIT=str(judge_exit),
        OUTCOME=outcome,
        PASSED=str(outcome == "pass").lower(),
        TEST_KEY="test-value",
    )
    for name, expression in step["env"].items():
        match = REFERENCE.fullmatch(expression)
        assert match, expression
        env[name] = values[match.group(1)]
    result = subprocess.run(["bash", "-c", step["run"]], env=env, text=True, capture_output=True)
    return result, calls.read_text() if calls.exists() else "", output.read_text()


def test_action_full_input_set(tmp_path):
    result, calls, output = execute(
        tmp_path,
        {
            "path": ".",
            "scope": "staged",
            "task-id": "T-1",
            "title": "",
            "criterion": "",
            "tier1-only": "false",
            "api-key-env": "TEST_KEY",
        },
    )
    assert result.returncode == 0, result.stderr
    assert "guard --scope tree --full" in calls and "judge --scope staged --json -- T-1" in calls
    assert "summary=pass" in output


def test_action_minimal_input_set(tmp_path):
    result, calls, output = execute(
        tmp_path, {"title": "Review", "criterion": "Works", "tier1-only": "true"}
    )
    assert result.returncode == 0, result.stderr
    assert (
        "guard " in calls and "judge --scope working-tree --json --skip-tier2 --ephemeral" in calls
    )
    assert "summary=pass" in output


def test_action_rejects_manifest_drift():
    manifest = yaml.safe_load(MANIFEST.read_text())
    manifest["inputs"].pop("scope")
    with pytest.raises(AssertionError, match="Undeclared inputs"):
        resolve(manifest, {})
    manifest = yaml.safe_load(MANIFEST.read_text())
    manifest["inputs"]["scope"]["required"] = True
    manifest["inputs"]["scope"].pop("default")
    with pytest.raises(AssertionError, match="Missing required input"):
        resolve(manifest, {})


@pytest.mark.parametrize(
    "guard_exit,judge_exit,outcome", [(1, 0, "pass"), (0, 1, "fail"), (0, 0, "error")]
)
def test_action_nonpass_fails_closed(tmp_path, guard_exit, judge_exit, outcome):
    result, calls, output = execute(
        tmp_path,
        {"task-id": "T-1", "tier1-only": "true"},
        guard_exit,
        judge_exit,
        outcome,
    )
    assert result.returncode != 0
    assert "guard " in calls and "judge " in calls, (result.stdout, result.stderr, calls)
    assert "summary=fail" in output and "summary=pass" not in output
    verdict = next(
        line.split("=", 1)[1] for line in output.splitlines() if line.startswith("verdict-path=")
    )
    assert json.loads(Path(verdict).read_text())["outcome"] == outcome
