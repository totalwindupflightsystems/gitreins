"""Schema validation for ``.gitreins/config.yaml`` (GR-160).

The loader returns a raw ``yaml.safe_load`` dict, so a misspelled key —
``security_scna:``, ``min_confidance:`` — parses fine, is silently ignored, and
the guard runs at its built-in default. The operator believes a guard is ON when
it is OFF, which is the same class of lie as a gate that never ran.

This module names the keys GitReins actually reads and reports everything else.

Severity policy is WARN by default, deliberately: one config file is frequently
shared across several GitReins versions (a repo checked out at 0.17 and at 0.18
reads the same ``.gitreins/config.yaml``), so a key this build does not know may
be a key a sibling build legitimately uses. Failing on it would break a repo the
operator did not change. ``strict=True`` promotes type, enum and shape
violations to errors for CI; unknown keys stay warnings in both modes for the
same cross-version reason.

The schema cannot drift silently: ``tests/test_config_schema.py`` scans the
source for the accessor literals (``guards_cfg.get("...")``,
``evaluator_cfg.get("...")``) and fails when a key GitReins reads is missing
from the schema, or when the schema claims a key nothing reads.
"""

from __future__ import annotations

import dataclasses
import difflib
from typing import Any

import yaml

from engine.config import (
    DataProtectionConfig,
    GitReinsDefaults,
    QualityConfig,
    SecurityReviewConfig,
    SpecAlignmentConfig,
)

WARN = "warn"
ERROR = "error"

_MAX_SUGGESTIONS = 3


@dataclasses.dataclass(frozen=True)
class ConfigIssue:
    """One problem found in a config file."""

    severity: str
    path: str
    message: str
    hint: str = ""

    def to_dict(self) -> dict[str, str]:
        payload = {"severity": self.severity, "path": self.path, "message": self.message}
        if self.hint:
            payload["hint"] = self.hint
        return payload

    def render(self) -> str:
        line = f"{self.severity.upper()}: {self.path}: {self.message}"
        return f"{line} — {self.hint}" if self.hint else line


@dataclasses.dataclass(frozen=True)
class KeySpec:
    """What a single key is allowed to hold.

    ``kind`` is one of ``bool``, ``int``, ``float``, ``str``, ``dict``, ``list``
    or ``any`` (``any`` disables type checking for keys whose shape this module
    does not model — typos are still caught).
    """

    kind: str
    enum: tuple[str, ...] = ()
    nested: "frozenset[str] | None" = None


def _kind_of(value: Any) -> str:
    # bool first: bool is a subclass of int and would otherwise read as int.
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, dict):
        return "dict"
    if isinstance(value, list):
        return "list"
    return "any"


def _specs_from_dataclass(
    cls: type, enums: dict[str, tuple[str, ...]] | None = None
) -> dict[str, KeySpec]:
    """Build key specs from a dataclass whose defaults are the real defaults."""
    instance = cls()
    enums = enums or {}
    return {
        f.name: KeySpec(_kind_of(getattr(instance, f.name)), enums.get(f.name, ()))
        for f in dataclasses.fields(cls)
    }


# ── guards: ──────────────────────────────────────────────────────────────────
# Every key GuardManager reads out of the `guards:` block. The drift test pins
# this list to the accessor literals in engine/guard_manager.py.
_GUARDS: dict[str, KeySpec] = {
    "secrets": KeySpec("bool"),
    "lint": KeySpec("bool"),
    "tests": KeySpec("bool"),
    "test_mode": KeySpec("str", ("diff", "full")),
    "test_on_clean": KeySpec("bool"),
    "test_command": KeySpec("str"),
    "test_timeout": KeySpec("int"),
    "hook_timeout": KeySpec("int"),
    "allow_skips": KeySpec("bool"),
    "dead_code": KeySpec("bool"),
    "skylos": KeySpec("bool"),
    "static_analysis": KeySpec("bool"),
    "static_analysis_tools": KeySpec("dict"),
    "lsp": KeySpec("bool"),
    "lsp_tools": KeySpec("list"),
    "lsp_timeouts": KeySpec("dict", nested=frozenset({"init", "per_file"})),
    "security_scan": KeySpec("dict", nested=frozenset({"enabled"})),
    "go": KeySpec("dict"),
    "scanner_nice": KeySpec("int"),
    "sensitive_paths": KeySpec("dict"),
}

# ── evaluator: ───────────────────────────────────────────────────────────────
# Read as `evaluator_cfg.get(...)` by engine/evaluator.py and engine/eval_cap.py.
# Kinds are "any": this module does not model every evaluator knob, and a wrong
# guess would produce a false type warning on a valid file.
_EVALUATOR_KEYS = (
    "cap",
    "code_context_budget",
    "compaction_threshold",
    "fast_track",
    "file_scope",
    "max_file_bytes",
    "max_read_bytes",
    "max_input_tokens",
    "max_iterations",
    "max_output_tokens",
    "max_time",
    "max_tokens_per_call",
    "pass_on_error",
    "prescreen",
    "static_analysis_diagnostics",
    "tool_call_weight",
)
_EVALUATOR: dict[str, KeySpec] = {key: KeySpec("any") for key in _EVALUATOR_KEYS}

# ── defaults: ────────────────────────────────────────────────────────────────
# `defaults:` mirrors GitReinsDefaults with its own key names (max_time maps to
# max_seconds). The worktree fleet knobs are resolved through
# _worktree_fleet_value, which reads the spellings the writers emit
# (worktree_venv_source/_name, as in DEFAULT_GITREINS_CONFIG and to_dict) and
# tolerates the legacy flat venv_source/venv_name and nested worktree_venv
# shapes. Read by GitReinsDefaults.load_defaults (engine/config.py:278+).
_DEFAULTS: dict[str, KeySpec] = {
    "model": KeySpec("str"),
    "llm_reasoning": KeySpec("str", ("disabled", "enabled")),
    "max_iterations": KeySpec("any"),
    "max_time": KeySpec("any"),
    "max_input_tokens": KeySpec("any"),
    "max_output_tokens": KeySpec("any"),
    "max_tokens_per_call": KeySpec("any"),
    "tool_call_weight": KeySpec("any"),
    "compaction_threshold": KeySpec("any"),
    "code_context_budget": KeySpec("any"),
    "file_scope": KeySpec("str", ("changed", "full")),
    "fast_track": KeySpec("str"),
    "max_file_bytes": KeySpec("any"),
    "max_read_bytes": KeySpec("any"),
    "pass_on_error": KeySpec("bool"),
    "hook_timeout": KeySpec("any"),
    "max_concurrent_worktrees": KeySpec("any"),
    "worktree_venv_source": KeySpec("str"),
    "worktree_venv_name": KeySpec("str"),
    "venv_source": KeySpec("str"),
    "venv_name": KeySpec("str"),
    "disk_ceiling_mb": KeySpec("any"),
    "security_scan": KeySpec(
        "dict", nested=frozenset({"enabled", "model", "min_confidence", "cve_source"})
    ),
    "commit_audit": KeySpec("dict"),
    "check_for_updates": KeySpec("bool"),
    "update_check_ttl": KeySpec("any"),
    "history_enabled": KeySpec("bool"),
    "history_path": KeySpec("str"),
    "history_storage": KeySpec("str"),
    "history_max_verdicts": KeySpec("any"),
    "resolution": KeySpec("dict"),
    "worktree_venv": KeySpec("dict"),
}

# ── worktree_fleet: ──────────────────────────────────────────────────────────
# Read through engine/config.py _worktree_fleet_value: a top-level fleet block
# may carry any of the fleet knobs directly, or nest the venv options under
# `venv:` (source/name). GR-GAP-069: this block is written by DEFAULT_GITREINS_CONFIG,
# so it must be known, not warned about, on every run.
_WORKTREE_FLEET: dict[str, KeySpec] = {
    "max_concurrent_worktrees": KeySpec("any"),
    "venv_source": KeySpec("str"),
    "venv_name": KeySpec("str"),
    "disk_ceiling_mb": KeySpec("any"),
    "venv": KeySpec("dict", nested=frozenset({"source", "name"})),
}

# ── history: ─────────────────────────────────────────────────────────────────
# DEFAULT_HISTORY_CONFIG (engine/persist.py:100) is the authoritative key set.
_HISTORY: dict[str, KeySpec] = {
    "enabled": KeySpec("bool"),
    "path": KeySpec("str"),
    "storage": KeySpec("str"),
    "max_verdicts": KeySpec("any"),
}

# ── resolution: ──────────────────────────────────────────────────────────────
# Read through engine/config.py resolution_cfg/resolution_bands/_resolution_*.
_RESOLUTION: dict[str, KeySpec] = {
    "enabled": KeySpec("dict", nested=frozenset({"cli", "mcp", "predispatch", "judge_prescreen"})),
    "model": KeySpec("str"),
    "tokens_max": KeySpec("any"),
    "bands": KeySpec("dict"),
    "egress_exclude": KeySpec("list"),
}

# ── criteria: ────────────────────────────────────────────────────────────────
# Read through engine/task_manager.py resolve_criteria_enforce (GR-172).
_CRITERIA: dict[str, KeySpec] = {
    "enforce": KeySpec("str", ("refuse", "warn", "off")),
}

# ── pipeline / commit_audit: ─────────────────────────────────────────────────
_PIPELINE: dict[str, KeySpec] = {"stages": KeySpec("list")}
_COMMIT_AUDIT: dict[str, KeySpec] = {
    "enabled": KeySpec("bool"),
    "mode": KeySpec("str", ("warn", "block", "suggest")),
    "strictness": KeySpec("str", ("lenient", "standard", "strict")),
    "suggest_message": KeySpec("bool"),
    "review_mode": KeySpec("str", ("message", "review", "agent")),
    "review_severity": KeySpec("str", ("critical-only", "standard", "all")),
    "review_suggest_fix": KeySpec("bool"),
    "review_max_tokens": KeySpec("any"),
    "review_checks": KeySpec("dict"),
    "review_effort": KeySpec("dict"),
    "review_score_threshold": KeySpec("any"),
    "review_score_offset": KeySpec("any"),
}

# ── top-level sections ───────────────────────────────────────────────────────
_SECTIONS: dict[str, dict[str, KeySpec]] = {
    "guards": _GUARDS,
    "evaluator": _EVALUATOR,
    "defaults": _DEFAULTS,
    "worktree_fleet": _WORKTREE_FLEET,
    "history": _HISTORY,
    "resolution": _RESOLUTION,
    "criteria": _CRITERIA,
    "pipeline": _PIPELINE,
    "commit_audit": _COMMIT_AUDIT,
    "quality": _specs_from_dataclass(QualityConfig),
    "spec_alignment": _specs_from_dataclass(
        SpecAlignmentConfig, {"mode": ("warn", "block"), "missing_checker": ("warn", "block")}
    ),
    "data_protection": {
        key: KeySpec(_kind_of(value)) for key, value in DataProtectionConfig().to_dict().items()
    },
    "security_review": _specs_from_dataclass(
        SecurityReviewConfig,
        {"mode": ("off", "staged", "full"), "missing_tool_policy": ("skip", "fail")},
    ),
}

# Flat top-level keys carrying GitReinsDefaults fields (model, max_*, ...).
_FLAT: dict[str, KeySpec] = _specs_from_dataclass(
    GitReinsDefaults,
    {
        "security_scan_model": ("antares-1b", "antares-350m"),
        "security_scan_cve_source": ("nvd", "github", "both"),
        "commit_audit_mode": ("warn", "block", "suggest"),
        "commit_audit_strictness": ("lenient", "standard", "strict"),
        "commit_audit_review_mode": ("message", "review", "agent"),
        "commit_audit_review_severity": ("critical-only", "standard", "all"),
        "file_scope": ("changed", "full"),
        "llm_reasoning": ("disabled", "enabled"),
    },
)

# Top-level keys that are not GitReinsDefaults fields but are read by the CLI.
_EXTRA_FLAT: dict[str, KeySpec] = {
    "review": KeySpec("dict"),
    "review_profiles": KeySpec("dict"),
    "review_effort": KeySpec("dict"),
    "preflight": KeySpec("dict"),
    "resolve": KeySpec("dict"),
}


def known_top_level() -> set[str]:
    """Every top-level key the schema recognises."""
    return set(_SECTIONS) | set(_FLAT) | set(_EXTRA_FLAT)


def known_keys(section: str) -> set[str]:
    """Keys recognised inside ``section`` (empty set for flat/unknown sections)."""
    if section in _SECTIONS:
        return set(_SECTIONS[section])
    return set()


def _suggest(key: str, candidates: set[str]) -> str:
    matches = difflib.get_close_matches(key, sorted(candidates), n=_MAX_SUGGESTIONS, cutoff=0.6)
    if not matches:
        return ""
    if len(matches) == 1:
        return f"did you mean '{matches[0]}'?"
    return "did you mean one of: " + ", ".join(f"'{m}'" for m in matches) + "?"


def _type_ok(value: Any, spec: KeySpec) -> bool:
    if spec.kind == "any":
        return True
    actual = _kind_of(value)
    if spec.kind == "float":
        return actual in ("int", "float")
    if spec.kind == "int":
        return actual == "int"
    return actual == spec.kind


def _check_value(path: str, value: Any, spec: KeySpec, *, strict: bool) -> list[ConfigIssue]:
    issues: list[ConfigIssue] = []
    severity = ERROR if strict else WARN
    if not _type_ok(value, spec):
        issues.append(
            ConfigIssue(
                severity,
                path,
                f"expected {spec.kind}, found {_kind_of(value)} ({value!r})",
                "the value is ignored and the built-in default is used",
            )
        )
        return issues
    if spec.enum and isinstance(value, str) and value.lower() not in spec.enum:
        allowed = ", ".join(spec.enum)
        issues.append(
            ConfigIssue(
                severity,
                path,
                f"{value!r} is not one of: {allowed}",
                "an unsupported value falls back to the default",
            )
        )
    if spec.nested and isinstance(value, dict):
        for key in value:
            if key not in spec.nested:
                issues.append(
                    ConfigIssue(
                        WARN,
                        f"{path}.{key}",
                        "unknown key",
                        _suggest(str(key), set(spec.nested)) or "this key is ignored",
                    )
                )
    return issues


def validate_config(config: Any, *, strict: bool = False) -> list[ConfigIssue]:
    """Validate a parsed config mapping.

    Returns [] for an empty/absent config. Never raises: a config that is not a
    mapping at all is reported as an issue, because the loader hands callers a
    dict and a non-mapping document would otherwise crash them later.
    """
    issues: list[ConfigIssue] = []
    if config is None:
        return issues
    if not isinstance(config, dict):
        return [
            ConfigIssue(
                ERROR if strict else WARN,
                "<root>",
                f"config must be a mapping, found {_kind_of(config)}",
                "fix the YAML so the top level is a set of `section:` blocks",
            )
        ]

    for key, value in config.items():
        key = str(key)
        if key in _SECTIONS:
            spec_section = _SECTIONS[key]
            if not isinstance(value, dict):
                issues.append(
                    ConfigIssue(
                        ERROR if strict else WARN,
                        key,
                        f"expected a mapping of settings, found {_kind_of(value)}",
                        "this whole section is ignored",
                    )
                )
                continue
            for sub_key, sub_value in value.items():
                sub_key_str = str(sub_key)
                path = f"{key}.{sub_key_str}"
                if sub_key_str not in spec_section:
                    issues.append(
                        ConfigIssue(
                            WARN,
                            path,
                            "unknown key",
                            _suggest(sub_key_str, set(spec_section)) or "this key is ignored",
                        )
                    )
                    continue
                issues.extend(
                    _check_value(path, sub_value, spec_section[sub_key_str], strict=strict)
                )
        elif key in _FLAT or key in _EXTRA_FLAT:
            spec = _FLAT.get(key) or _EXTRA_FLAT[key]
            issues.extend(_check_value(key, value, spec, strict=strict))
        else:
            issues.append(
                ConfigIssue(
                    WARN,
                    key,
                    "unknown top-level key",
                    _suggest(key, known_top_level()) or "this key is ignored by this version",
                )
            )
    return issues


def validate_config_file(path: str, *, strict: bool = False) -> list[ConfigIssue]:
    """Validate a config file, including whether it parses at all."""
    try:
        with open(path) as handle:
            data = yaml.safe_load(handle)
    except yaml.YAMLError as exc:
        return [
            ConfigIssue(
                ERROR,
                path,
                f"YAML does not parse: {exc}",
                "every setting in this file is ignored until the syntax is fixed",
            )
        ]
    except OSError as exc:
        return [ConfigIssue(ERROR, path, f"cannot read: {exc}")]
    return validate_config(data, strict=strict)


def format_issues(issues: list[ConfigIssue], *, source: str = "config") -> str:
    """One line per issue, prefixed by the source path."""
    return "\n".join(f"{source}: {issue.render()}" for issue in issues)
