"""GR-160: .gitreins/config.yaml is validated against the keys GitReins reads.

The defect these tests pin: a misspelled key parses fine, is ignored, and the
guard runs at its built-in default — so the operator believes a guard is on when
it is off. Severity is WARN by default (one config file is commonly shared across
GitReins versions) and ``--strict`` promotes type/enum violations to errors.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

import pytest

from engine import config_schema
from engine.config_schema import validate_config, validate_config_file
from engine.config import GitReinsDefaults

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(rel: str) -> str:
    with open(os.path.join(REPO_ROOT, rel)) as handle:
        return handle.read()


def _accessor_keys(source: str, accessor: str) -> set[str]:
    """Keys read through a given dict accessor, e.g. guards_cfg.get("x")."""
    pattern = re.compile(rf"{accessor}\.get\(\"([a-z_]+)\"")
    return set(pattern.findall(source))


# ── the schema cannot drift from the code ────────────────────────────────────


def test_guards_schema_covers_every_key_guard_manager_reads() -> None:
    """Every guards_cfg.get("k") in guard_manager.py exists in the schema."""
    read = _accessor_keys(_read("engine/guard_manager.py"), "guards_cfg")
    assert read, "accessor scan matched nothing — the check itself is broken"
    missing = read - config_schema.known_keys("guards")
    assert not missing, f"guard_manager reads keys the schema does not know: {sorted(missing)}"


def test_evaluator_schema_covers_every_key_the_evaluator_reads() -> None:
    read: set[str] = set()
    for rel in ("engine/evaluator.py", "engine/eval_cap.py"):
        read |= _accessor_keys(_read(rel), "evaluator_cfg")
        read |= _accessor_keys(_read(rel), "ev")
    assert read, "accessor scan matched nothing — the check itself is broken"
    missing = read - config_schema.known_keys("evaluator")
    assert not missing, f"evaluator reads keys the schema does not know: {sorted(missing)}"


def test_defaults_schema_covers_every_key_load_defaults_reads() -> None:
    """`defaults.get("k")` in engine/config.py must all be known.

    `defaults` is the local name load_defaults binds to the `defaults:` block,
    so every access on it is a key this schema must list.
    """
    source = _read("engine/config.py")
    read = set(re.findall(r"defaults\.get\(\"([a-z_]+)\"", source))
    assert read, "accessor scan matched nothing — the check itself is broken"
    missing = read - config_schema.known_keys("defaults")
    assert not missing, f"load_defaults reads keys the schema does not know: {sorted(missing)}"


def test_flat_schema_covers_every_gitreins_defaults_field() -> None:
    """A new flat default must appear in the schema (or the typo check misses it)."""
    fields = {f.name for f in __import__("dataclasses").fields(GitReinsDefaults)}
    missing = {name for name in fields if name not in config_schema.known_top_level()}
    assert not missing, f"flat defaults missing from the schema: {sorted(missing)}"


def test_this_repos_own_config_is_clean() -> None:
    """The repo's own .gitreins/config.yaml must produce zero issues."""
    issues = validate_config_file(os.path.join(REPO_ROOT, ".gitreins", "config.yaml"))
    assert issues == [], "repo config warns: " + "; ".join(i.render() for i in issues)


def test_install_generated_config_is_clean() -> None:
    """GR-GAP-069: the config `gitreins install` writes must not trip the validator.

    DEFAULT_GITREINS_CONFIG emits `defaults.worktree_venv_source`/`_name` and a
    top-level `worktree_fleet`; the schema must know every key the product's own
    writers emit, or every hook run in every consumer repo prints warnings.
    """
    import yaml

    from gitreins.cli import DEFAULT_GITREINS_CONFIG

    config = yaml.safe_load(DEFAULT_GITREINS_CONFIG)
    issues = validate_config(config)
    assert issues == [], "install-generated config warns: " + "; ".join(i.render() for i in issues)


def test_worktree_fleet_section_keys_are_known() -> None:
    """Every shape _worktree_fleet_value reads out of `worktree_fleet:` validates."""
    config = {
        "worktree_fleet": {
            "max_concurrent_worktrees": 3,
            "venv_source": ".venv",
            "venv_name": ".venv",
            "disk_ceiling_mb": 4096,
            "venv": {"source": "shared-env", "name": ".fleet-venv"},
        }
    }
    assert validate_config(config) == []


def test_loader_reads_the_writers_venv_spellings() -> None:
    """The venv knobs the install writer emits must reach the loaded defaults."""
    config = {
        "defaults": {"worktree_venv_source": "shared-env", "worktree_venv_name": ".tree-venv"},
        "worktree_fleet": {"disk_ceiling_mb": 2048},
    }
    defaults = GitReinsDefaults().overlay(config)
    assert defaults.worktree_venv_source == "shared-env"
    assert defaults.worktree_venv_name == ".tree-venv"
    assert defaults.worktree_disk_ceiling_mb == 2048


# ── the defect: a misspelled key was silently ignored ────────────────────────


def test_misspelled_guards_key_warns_and_suggests_the_real_key() -> None:
    issues = validate_config({"guards": {"security_scna": {"enabled": True}}})
    assert len(issues) == 1
    issue = issues[0]
    assert issue.severity == "warn"
    assert issue.path == "guards.security_scna"
    assert "security_scan" in issue.hint, issue.hint


def test_misspelled_value_key_warns() -> None:
    issues = validate_config(
        {"guards": {"security_scan": {"enabled": True, "min_confidance": 0.9}}}
    )
    assert [i.path for i in issues] == ["guards.security_scan.min_confidance"]


def test_unknown_top_level_key_warns() -> None:
    issues = validate_config({"gurds": {"secrets": True}})
    assert len(issues) == 1
    assert issues[0].path == "gurds"
    assert "guards" in issues[0].hint


def test_valid_config_produces_no_issues() -> None:
    config = {
        "guards": {
            "secrets": True,
            "lint": True,
            "tests": True,
            "test_mode": "diff",
            "test_timeout": 900,
            "allow_skips": True,
            "security_scan": {"enabled": False},
            "lsp_timeouts": {"init": 300, "per_file": 120},
        },
        "defaults": {"model": "deepseek-v4-flash", "max_time": "90m"},
        "history": {"enabled": True, "storage": "git"},
        "resolution": {"enabled": {"cli": True}, "egress_exclude": ["*.local"]},
    }
    assert validate_config(config) == []


# ── severity policy: warn by default, strict on demand ───────────────────────


@pytest.mark.parametrize(
    "config,path",
    [
        ({"guards": {"test_timeout": "900"}}, "guards.test_timeout"),
        ({"guards": {"secrets": "yes"}}, "guards.secrets"),
        ({"guards": {"test_mode": "sometimes"}}, "guards.test_mode"),
        ({"history": []}, "history"),
    ],
)
def test_type_and_enum_violations_warn_by_default(config: dict, path: str) -> None:
    issues = validate_config(config)
    assert [i.severity for i in issues] == ["warn"], issues
    assert issues[0].path == path


def test_strict_promotes_type_violations_to_errors_but_keeps_unknown_keys_warn() -> None:
    issues = validate_config({"guards": {"test_timeout": "900", "security_scna": {}}}, strict=True)
    by_path = {i.path: i.severity for i in issues}
    assert by_path == {"guards.test_timeout": "error", "guards.security_scna": "warn"}


# ── loader / file level ──────────────────────────────────────────────────────


def test_absent_or_empty_config_is_not_an_issue() -> None:
    assert validate_config(None) == []
    assert validate_config({}) == []


def test_non_mapping_root_is_reported() -> None:
    issues = validate_config(["guards"])
    assert issues and issues[0].path == "<root>"


def test_broken_yaml_is_an_error_naming_the_file(tmp_path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("guards: [unclosed\n")
    issues = validate_config_file(str(path))
    assert issues and issues[0].severity == "error"
    assert "does not parse" in issues[0].message


def test_loader_warns_once_per_file(tmp_path, capsys) -> None:
    """Repeated load_config calls must not repeat the same warning."""
    from gitreins import cli as cli_module

    workdir = tmp_path / "repo"
    (workdir / ".gitreins").mkdir(parents=True)
    (workdir / ".gitreins" / "config.yaml").write_text("guards:\n  security_scna: true\n")
    cli_module._REPORTED_CONFIG_ISSUES.clear()
    for _ in range(3):
        assert cli_module.load_config(str(workdir)) == {"guards": {"security_scna": True}}
    assert len(cli_module._REPORTED_CONFIG_ISSUES) == 1


def test_cli_reports_ok_on_the_repo_config() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "gitreins", "config-validate", "--json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout) == []


def test_cli_strict_exits_one_on_a_type_violation(tmp_path) -> None:
    workdir = tmp_path / "repo"
    (workdir / ".gitreins").mkdir(parents=True)
    (workdir / ".gitreins" / "config.yaml").write_text("guards:\n  test_timeout: oops\n")
    # PYTHONPATH pins the subprocess to THIS checkout: the repo venv's editable
    # install points at the main worktree, which would prove nothing about the
    # code under test.
    env = dict(os.environ, PYTHONPATH=REPO_ROOT)
    result = subprocess.run(
        [sys.executable, "-m", "gitreins", "config-validate", "--strict"],
        cwd=str(workdir),
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "guards.test_timeout" in result.stdout
