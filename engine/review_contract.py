# ruff: noqa: E501
"""Version-one protected review policy contract (GR-219).

This module is deliberately deterministic. YAML is parsed with a duplicate-key
rejecting safe loader and all policy selection is performed locally; no review
model is involved in loading, validation, merging, or path matching.

ch:trace row=GR-219 spec=specs/SPEC-GR219-contract-schema.md#1-purpose wave=none:serial
 test=tests/test_review_contract.py::test_pr_self_weakening_falsifier doc=docs/review-contract-design.md
 evidence=none:worker-local witness=none:worker-local verdict=none:foreman-pending
 commit=none:worker-sha memory=none:unwritten
"""

# The trace marker is repeated in the test artifact as one complete line.
# ruff: noqa: E501

from __future__ import annotations

import dataclasses
import fnmatch
import hashlib
import json
import posixpath
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import yaml

LayerName = Literal["defaults", "organization", "repository", "path_overlays"]
MergeKind = Literal[
    "scalar", "maps", "instructions", "passes", "ignores", "include_paths", "exclude_paths"
]

_REQUIRED_LAYERS: tuple[LayerName, ...] = (
    "defaults",
    "organization",
    "repository",
    "path_overlays",
)
_ALLOWED_SIGNALS = frozenset(
    {
        "authentication_call",
        "authorization_check",
        "deserialization",
        "secret_access",
        "shell_execution",
        "unbounded_query",
        "public_signature_change",
        "new_loop_over_remote_data",
        "n_plus_one_candidate",
        "lock_scope_change",
        "schema_change",
    }
)


class PolicySource(Protocol):
    def read_base_file(self, base_ref: str, relative_path: str) -> str | None: ...

    def resolve_base_ref(self, requested_ref: str) -> str: ...

    def protected_ref(self, requested_ref: str) -> str: ...


@dataclass(frozen=True)
class ReviewLoadContext:
    workdir: Path
    base_ref: str
    head_ref: str
    protected_base_ref: str
    source: PolicySource


@dataclass(frozen=True)
class ReviewDiagnostic:
    code: str
    severity: Literal["warning", "error"]
    message: str
    path: str = ""
    layer: LayerName | None = None


@dataclass(frozen=True)
class ReviewContractError(Exception):
    code: str
    message: str
    path: str = ""
    layer: LayerName | None = None
    details: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        Exception.__init__(self, self.message)

    def diagnostic(self) -> ReviewDiagnostic:
        return ReviewDiagnostic(self.code, "error", self.message, self.path, self.layer)


@dataclass(frozen=True)
class PolicySourceConfig:
    ref: Literal["base"] = "base"
    require_protected_ref: bool = True
    allow_pr_override: bool = False


@dataclass(frozen=True)
class MergeConfig:
    scalar: Literal["override"] = "override"
    maps: Literal["deep_merge"] = "deep_merge"
    instructions: Literal["append"] = "append"
    passes: Literal["append"] = "append"
    ignores: Literal["union"] = "union"
    include_paths: Literal["intersect"] = "intersect"
    exclude_paths: Literal["union"] = "union"


@dataclass(frozen=True)
class InheritanceConfig:
    layers: tuple[LayerName, ...] = _REQUIRED_LAYERS
    merge: MergeConfig = field(default_factory=MergeConfig)
    conflict: Literal["deny_and_report"] = "deny_and_report"


@dataclass(frozen=True)
class ProfileConfig:
    model: str
    tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class IgnoreConfig:
    paths: tuple[str, ...] = ()
    findings: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class OrganizationConfig:
    extends: str | None = None
    locked: tuple[str, ...] = ()
    defaults: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RepositoryConfig:
    profiles: Mapping[str, ProfileConfig] = field(default_factory=dict)
    ignores: IgnoreConfig = field(default_factory=IgnoreConfig)


@dataclass(frozen=True)
class ExecutionConfig:
    model: str | None = None
    budget: int | float | None = None
    timeout_seconds: int | None = None
    tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class OutputConfig:
    finding_types: tuple[str, ...] = ()
    blocking_severities: tuple[str, ...] = ()
    labels: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TriggerConfig:
    any: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GateConfig:
    locked: bool | None = None
    minimum_severity: str | None = None
    minimum_confidence: float | None = None
    require: tuple[str, ...] = ()
    when: Mapping[str, Any] = field(default_factory=dict)
    action: str | None = None
    on_fail: str | None = None
    path_rules: tuple[Mapping[str, Any], ...] = ()
    finding_policy: Mapping[str, Any] = field(default_factory=dict)
    security: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LabelConfig:
    inputs: tuple[Mapping[str, Any], ...] = ()
    outputs: tuple[Mapping[str, Any], ...] = ()
    lifecycle: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedLabels:
    add: tuple[str, ...] = ()
    remove: tuple[str, ...] = ()
    inputs: tuple[Mapping[str, Any], ...] = ()
    outputs: tuple[Mapping[str, Any], ...] = ()
    lifecycle: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExplainabilityConfig:
    effective_config: Mapping[str, Any] = field(default_factory=dict)
    trace_each_finding: Mapping[str, Any] = field(default_factory=dict)
    dry_run: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class PathOverlayConfig:
    id: str
    match: Mapping[str, Any] = field(default_factory=dict)
    instructions: tuple[str, ...] = ()
    review: Mapping[str, Any] = field(default_factory=dict)
    gates: GateConfig = field(default_factory=GateConfig)
    labels: ResolvedLabels = field(default_factory=ResolvedLabels)
    action: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class PathSelectionConfig:
    include: tuple[str, ...] = ("**",)
    exclude: tuple[str, ...] = ()
    overlays: tuple[PathOverlayConfig, ...] = ()


@dataclass(frozen=True)
class PassConfig:
    id: str
    enabled: bool = True
    scope: Mapping[str, Any] = field(default_factory=dict)
    instructions: tuple[str, ...] = ()
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    trigger: TriggerConfig = field(default_factory=TriggerConfig)
    gates: GateConfig = field(default_factory=GateConfig)
    output: OutputConfig = field(default_factory=OutputConfig)


@dataclass(frozen=True)
class ReviewContract:
    version: Literal[1]
    policy_source: PolicySourceConfig
    inheritance: InheritanceConfig
    organization: OrganizationConfig
    repository: RepositoryConfig
    paths: PathSelectionConfig
    passes: tuple[PassConfig, ...]
    triggers: TriggerConfig
    gates: GateConfig
    labels: LabelConfig
    explainability: ExplainabilityConfig


@dataclass(frozen=True)
class ResolvedPass:
    id: str
    enabled: bool
    scope: Mapping[str, Any]
    instructions: tuple[str, ...]
    execution: ExecutionConfig
    trigger: TriggerConfig
    gates: GateConfig
    output: OutputConfig


@dataclass(frozen=True)
class LoadedReviewContract:
    contract: ReviewContract
    source_ref: str
    source_commit: str
    config_hash: str
    used_defaults: bool
    diagnostics: tuple[ReviewDiagnostic, ...]


@dataclass(frozen=True)
class ResolvedReviewPolicy:
    layers: tuple[LayerName, ...]
    matched_overlay_ids: tuple[str, ...]
    matched_globs: tuple[str, ...]
    instructions: tuple[str, ...]
    passes: tuple[ResolvedPass, ...]
    include_paths: tuple[str, ...]
    exclude_paths: tuple[str, ...]
    effective_profile: str | None
    effective_tools: tuple[str, ...]
    gates: GateConfig
    labels: ResolvedLabels
    config_hash: str
    diagnostics: tuple[ReviewDiagnostic, ...]
    source_ref: str = ""
    source_commit: str = ""
    normalized_paths_digest: str = ""
    changed_files_digest: str = ""
    diff_digest: str = ""

    @property
    def passed(self) -> bool:
        return not any(d.severity == "error" for d in self.diagnostics)

    @property
    def disposition(self) -> str:
        return "allow" if self.passed else "deny_and_report"


class _UniqueSafeLoader(yaml.SafeLoader):
    """SafeLoader variant that refuses duplicate mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueSafeLoader, node: yaml.nodes.MappingNode, deep: bool = False
) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            mark = key_node.start_mark
            raise ReviewContractError(
                "REVIEW_YAML_DUPLICATE_KEY",
                f"duplicate YAML key {key!r} at line {mark.line + 1}, column {mark.column + 1}",
                str(key),
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueSafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _kind(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "list"
    if isinstance(value, Mapping):
        return "mapping"
    return type(value).__name__


def _require_mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReviewContractError(
            "REVIEW_YAML_TYPE", f"{path} must be a mapping, found {_kind(value)}", path
        )
    return value


def _unknown(mapping: Mapping[str, Any], allowed: set[str], path: str) -> None:
    for key in mapping:
        if not isinstance(key, str) or key not in allowed:
            key_path = f"{path}.{key}" if path else str(key)
            raise ReviewContractError(
                "REVIEW_UNKNOWN_KEY", f"unknown contract key at {key_path}", key_path
            )


def _tuple_strings(value: Any, path: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ReviewContractError("REVIEW_YAML_TYPE", f"{path} must be a list", path)
    if any(not isinstance(item, str) for item in value):
        raise ReviewContractError("REVIEW_YAML_TYPE", f"{path} must contain strings", path)
    return tuple(value)


def _mapping_list(value: Any, path: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list):
        raise ReviewContractError("REVIEW_YAML_TYPE", f"{path} must be a list", path)
    result: list[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        result.append(_require_mapping(item, f"{path}[{index}]"))
    return tuple(dict(item) for item in result)


def _parse_gate(value: Any, path: str) -> GateConfig:
    data = _require_mapping(value, path)
    allowed = {
        "locked",
        "minimum_severity",
        "minimum_confidence",
        "require",
        "when",
        "action",
        "on_fail",
        "path_rules",
        "finding_policy",
        "security",
    }
    _unknown(data, allowed, path)
    confidence = data.get("minimum_confidence")
    if confidence is not None and (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not 0 <= confidence <= 1
    ):
        raise ReviewContractError(
            "REVIEW_YAML_TYPE",
            f"{path}.minimum_confidence must be between 0 and 1",
            f"{path}.minimum_confidence",
        )
    required = _tuple_strings(data["require"], f"{path}.require") if "require" in data else ()
    rules = _mapping_list(data["path_rules"], f"{path}.path_rules") if "path_rules" in data else ()
    return GateConfig(
        locked=data.get("locked") if isinstance(data.get("locked"), bool) else None,
        minimum_severity=data.get("minimum_severity")
        if isinstance(data.get("minimum_severity"), str)
        else None,
        minimum_confidence=float(confidence) if confidence is not None else None,
        require=required,
        when=dict(data.get("when", {})) if isinstance(data.get("when", {}), Mapping) else {},
        action=data.get("action") if isinstance(data.get("action"), str) else None,
        on_fail=data.get("on_fail") if isinstance(data.get("on_fail"), str) else None,
        path_rules=rules,
        finding_policy=dict(data.get("finding_policy", {}))
        if isinstance(data.get("finding_policy", {}), Mapping)
        else {},
        security=dict(data.get("security", {}))
        if isinstance(data.get("security", {}), Mapping)
        else {},
    )


def _validate_nested_signals(value: Any, path: str) -> None:
    """Reject unsupported signal names anywhere in a trigger tree."""
    if isinstance(value, Mapping):
        for key, nested in value.items():
            nested_path = f"{path}.{key}"
            if key == "diff_signals":
                if not isinstance(nested, list):
                    raise ReviewContractError(
                        "REVIEW_YAML_TYPE", f"{nested_path} must be a list", nested_path
                    )
                for index, signal in enumerate(nested):
                    if signal not in _ALLOWED_SIGNALS:
                        raise ReviewContractError(
                            "REVIEW_UNSUPPORTED_SIGNAL",
                            f"unsupported diff signal {signal!r}",
                            f"{nested_path}[{index}]",
                        )
            else:
                _validate_nested_signals(nested, nested_path)
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _validate_nested_signals(nested, f"{path}[{index}]")


def _parse_trigger(value: Any, path: str) -> TriggerConfig:
    data = _require_mapping(value, path)
    _unknown(
        data,
        {
            "any",
            "paths",
            "diff_signals",
            "events",
            "review_new_pushes",
            "drafts",
            "labels",
            "title_patterns",
            "files_changed_gt",
            "changed_lines_gt",
            "target_branch",
        },
        path,
    )
    _validate_nested_signals(data, path)
    return TriggerConfig(any=dict(data))


def _parse_execution(value: Any, path: str) -> ExecutionConfig:
    data = _require_mapping(value, path)
    _unknown(data, {"model", "budget", "timeout_seconds", "tools"}, path)
    tools = _tuple_strings(data["tools"], f"{path}.tools") if "tools" in data else ()
    return ExecutionConfig(
        model=data.get("model") if isinstance(data.get("model"), str) else None,
        budget=data.get("budget")
        if isinstance(data.get("budget"), (int, float)) and not isinstance(data.get("budget"), bool)
        else None,
        timeout_seconds=data.get("timeout_seconds")
        if isinstance(data.get("timeout_seconds"), int)
        and not isinstance(data.get("timeout_seconds"), bool)
        else None,
        tools=tools,
    )


def _parse_output(value: Any, path: str) -> OutputConfig:
    data = _require_mapping(value, path)
    _unknown(data, {"finding_types", "blocking_severities", "labels"}, path)
    return OutputConfig(
        finding_types=_tuple_strings(data["finding_types"], f"{path}.finding_types")
        if "finding_types" in data
        else (),
        blocking_severities=_tuple_strings(
            data["blocking_severities"], f"{path}.blocking_severities"
        )
        if "blocking_severities" in data
        else (),
        labels=dict(data.get("labels", {})) if isinstance(data.get("labels", {}), Mapping) else {},
    )


def _parse_pass(value: Any, index: int) -> PassConfig:
    path = f"passes[{index}]"
    data = _require_mapping(value, path)
    _unknown(
        data,
        {"id", "enabled", "scope", "instructions", "execution", "trigger", "gates", "output"},
        path,
    )
    if not isinstance(data.get("id"), str) or not data["id"]:
        raise ReviewContractError(
            "REVIEW_YAML_TYPE", f"{path}.id must be a non-empty string", f"{path}.id"
        )
    instructions: tuple[str, ...] = ()
    if "instructions" in data:
        inst = _require_mapping(data["instructions"], f"{path}.instructions")
        _unknown(inst, {"append"}, f"{path}.instructions")
        raw = inst.get("append", "")
        if not isinstance(raw, str):
            raise ReviewContractError(
                "REVIEW_YAML_TYPE",
                f"{path}.instructions.append must be a string",
                f"{path}.instructions.append",
            )
        instructions = (raw,) if raw else ()
    return PassConfig(
        id=data["id"],
        enabled=data.get("enabled", True) if isinstance(data.get("enabled", True), bool) else True,
        scope=dict(data.get("scope", {})) if isinstance(data.get("scope", {}), Mapping) else {},
        instructions=instructions,
        execution=_parse_execution(data.get("execution", {}), f"{path}.execution"),
        trigger=_parse_trigger(data["trigger"], f"{path}.trigger")
        if "trigger" in data
        else TriggerConfig(),
        gates=_parse_gate(data["gates"], f"{path}.gates") if "gates" in data else GateConfig(),
        output=_parse_output(data["output"], f"{path}.output")
        if "output" in data
        else OutputConfig(),
    )


def _parse_labels(value: Any, path: str) -> LabelConfig:
    data = _require_mapping(value, path)
    _unknown(data, {"inputs", "outputs", "lifecycle"}, path)
    return LabelConfig(
        inputs=_mapping_list(data["inputs"], f"{path}.inputs") if "inputs" in data else (),
        outputs=_mapping_list(data["outputs"], f"{path}.outputs") if "outputs" in data else (),
        lifecycle=dict(data.get("lifecycle", {}))
        if isinstance(data.get("lifecycle", {}), Mapping)
        else {},
    )


def _parse_overlay(value: Any, index: int) -> PathOverlayConfig:
    path = f"paths.overlays[{index}]"
    data = _require_mapping(value, path)
    _unknown(
        data, {"id", "match", "instructions", "review", "gates", "labels", "action", "reason"}, path
    )
    if not isinstance(data.get("id"), str) or not data["id"]:
        raise ReviewContractError(
            "REVIEW_YAML_TYPE", f"{path}.id must be a non-empty string", f"{path}.id"
        )
    match = _require_mapping(data.get("match", {}), f"{path}.match")
    _unknown(match, {"any", "all"}, f"{path}.match")
    patterns: list[str] = []
    for key in ("any", "all"):
        if key in match:
            patterns.extend(_tuple_strings(match[key], f"{path}.match.{key}"))
    for pattern in patterns:
        _validate_glob(pattern, f"{path}.match")
    instructions: tuple[str, ...] = ()
    if "instructions" in data:
        inst = _require_mapping(data["instructions"], f"{path}.instructions")
        _unknown(inst, {"append"}, f"{path}.instructions")
        raw = inst.get("append", "")
        if not isinstance(raw, str):
            raise ReviewContractError(
                "REVIEW_YAML_TYPE",
                f"{path}.instructions.append must be a string",
                f"{path}.instructions.append",
            )
        instructions = (raw,) if raw else ()
    labels = data.get("labels", {})
    label_data = _require_mapping(labels, f"{path}.labels")
    _unknown(label_data, {"add", "remove"}, f"{path}.labels")
    return PathOverlayConfig(
        id=data["id"],
        match=dict(match),
        instructions=instructions,
        review=dict(data.get("review", {})) if isinstance(data.get("review", {}), Mapping) else {},
        gates=_parse_gate(data["gates"], f"{path}.gates") if "gates" in data else GateConfig(),
        labels=ResolvedLabels(
            add=_tuple_strings(label_data["add"], f"{path}.labels.add")
            if "add" in label_data
            else (),
            remove=_tuple_strings(label_data["remove"], f"{path}.labels.remove")
            if "remove" in label_data
            else (),
        ),
        action=data.get("action") if isinstance(data.get("action"), str) else None,
        reason=data.get("reason") if isinstance(data.get("reason"), str) else None,
    )


def _validate_glob(pattern: str, path: str) -> None:
    if (
        not pattern
        or "\x00" in pattern
        or pattern.startswith("/")
        or pattern.startswith("../")
        or "/../" in pattern
    ):
        raise ReviewContractError("REVIEW_GLOB_PARSE_FAILURE", f"invalid glob {pattern!r}", path)
    if pattern.count("[") != pattern.count("]"):
        raise ReviewContractError("REVIEW_GLOB_PARSE_FAILURE", f"invalid glob {pattern!r}", path)


def parse_review_contract(yaml_text: str, *, source_ref: str, source_commit: str) -> ReviewContract:
    """Parse and validate one v1 contract using a safe, duplicate-rejecting loader."""
    try:
        raw = yaml.load(yaml_text, Loader=_UniqueSafeLoader)
    except ReviewContractError:
        raise
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        raise ReviewContractError(
            "REVIEW_YAML_UNPARSEABLE", f"YAML does not parse{location}: {exc}", "<root>"
        ) from exc
    if raw is None:
        raise ReviewContractError(
            "REVIEW_YAML_NOT_MAPPING", "review contract root is null", "<root>"
        )
    root = _require_mapping(raw, "<root>")
    allowed = {
        "version",
        "policy_source",
        "inheritance",
        "organization",
        "repository",
        "paths",
        "passes",
        "triggers",
        "gates",
        "labels",
        "explainability",
    }
    _unknown(root, allowed, "")
    if "version" not in root:
        raise ReviewContractError(
            "REVIEW_VERSION_MISSING", "review contract must declare version: 1", "version"
        )
    if (
        isinstance(root["version"], bool)
        or not isinstance(root["version"], int)
        or root["version"] != 1
    ):
        raise ReviewContractError(
            "REVIEW_VERSION_UNSUPPORTED", "review contract version must be integer 1", "version"
        )

    source_data = (
        _require_mapping(root.get("policy_source"), "policy_source")
        if "policy_source" in root
        else None
    )
    if source_data is None:
        raise ReviewContractError(
            "REVIEW_POLICY_SOURCE_MISSING", "policy_source.ref is required", "policy_source.ref"
        )
    _unknown(source_data, {"ref", "require_protected_ref", "allow_pr_override"}, "policy_source")
    if "ref" not in source_data or source_data.get("ref") is None:
        raise ReviewContractError(
            "REVIEW_POLICY_SOURCE_MISSING", "policy_source.ref is required", "policy_source.ref"
        )
    if source_data["ref"] != "base":
        raise ReviewContractError(
            "REVIEW_POLICY_SOURCE_NOT_BASE",
            "policy_source.ref must be exactly 'base'",
            "policy_source.ref",
        )
    policy_source = PolicySourceConfig(
        require_protected_ref=source_data.get("require_protected_ref", True) is True,
        allow_pr_override=source_data.get("allow_pr_override", False) is True,
    )
    if source_data.get("require_protected_ref", True) is not True:
        raise ReviewContractError(
            "REVIEW_PROTECTED_REF_REQUIRED",
            "protected-base loading is mandatory",
            "policy_source.require_protected_ref",
        )

    inheritance_data = _require_mapping(root.get("inheritance", {}), "inheritance")
    _unknown(inheritance_data, {"layers", "merge", "conflict"}, "inheritance")
    layers_raw = _tuple_strings(
        inheritance_data.get("layers", list(_REQUIRED_LAYERS)), "inheritance.layers"
    )
    layers: tuple[LayerName, ...] = _REQUIRED_LAYERS
    if layers_raw != _REQUIRED_LAYERS:
        bad = next(
            (
                i
                for i, layer in enumerate(layers_raw)
                if layer not in _REQUIRED_LAYERS or layers_raw.count(layer) > 1
            ),
            0,
        )
        raise ReviewContractError(
            "REVIEW_UNKNOWN_LAYER",
            f"inheritance.layers must be {_REQUIRED_LAYERS}; offending index {bad}",
            f"inheritance.layers[{bad}]",
        )
    merge_data = _require_mapping(inheritance_data.get("merge", {}), "inheritance.merge")
    _unknown(merge_data, {"scalar", "maps", "lists", "conflict"}, "inheritance.merge")
    lists = _require_mapping(merge_data.get("lists", {}), "inheritance.merge.lists")
    _unknown(
        lists,
        {"instructions", "passes", "ignores", "include_paths", "exclude_paths"},
        "inheritance.merge.lists",
    )
    merge = MergeConfig(
        scalar=merge_data.get("scalar", "override"),
        maps=merge_data.get("maps", "deep_merge"),
        instructions=lists.get("instructions", "append"),
        passes=lists.get("passes", "append"),
        ignores=lists.get("ignores", "union"),
        include_paths=lists.get("include_paths", "intersect"),
        exclude_paths=lists.get("exclude_paths", "union"),
    )
    expected_ops = dataclasses.asdict(MergeConfig())
    if dataclasses.asdict(merge) != expected_ops:
        raise ReviewContractError(
            "REVIEW_CONFLICT", "v1 merge operators cannot be changed", "inheritance.merge"
        )
    inheritance = InheritanceConfig(
        layers=layers, merge=merge, conflict=inheritance_data.get("conflict", "deny_and_report")
    )
    if inheritance.conflict != "deny_and_report":
        raise ReviewContractError(
            "REVIEW_CONFLICT", "v1 conflict policy must be deny_and_report", "inheritance.conflict"
        )

    org_data = _require_mapping(root.get("organization", {}), "organization")
    _unknown(org_data, {"extends", "locked", "defaults"}, "organization")
    organization = OrganizationConfig(
        extends=org_data.get("extends") if isinstance(org_data.get("extends"), str) else None,
        locked=_tuple_strings(org_data["locked"], "organization.locked")
        if "locked" in org_data
        else (),
        defaults=dict(org_data.get("defaults", {}))
        if isinstance(org_data.get("defaults", {}), Mapping)
        else {},
    )

    repo_data = _require_mapping(root.get("repository", {}), "repository")
    _unknown(repo_data, {"profiles", "ignores"}, "repository")
    profiles: dict[str, ProfileConfig] = {}
    profiles_data = _require_mapping(repo_data.get("profiles", {}), "repository.profiles")
    for name, value in profiles_data.items():
        if not isinstance(name, str):
            raise ReviewContractError(
                "REVIEW_YAML_TYPE", "profile names must be strings", "repository.profiles"
            )
        profile = _require_mapping(value, f"repository.profiles.{name}")
        _unknown(profile, {"model", "tools"}, f"repository.profiles.{name}")
        if not isinstance(profile.get("model"), str):
            raise ReviewContractError(
                "REVIEW_YAML_TYPE",
                f"repository.profiles.{name}.model is required",
                f"repository.profiles.{name}.model",
            )
        profiles[name] = ProfileConfig(
            profile["model"],
            _tuple_strings(profile["tools"], f"repository.profiles.{name}.tools")
            if "tools" in profile
            else (),
        )
    ignores_data = _require_mapping(repo_data.get("ignores", {}), "repository.ignores")
    _unknown(ignores_data, {"paths", "findings"}, "repository.ignores")
    ignores = IgnoreConfig(
        paths=_tuple_strings(ignores_data["paths"], "repository.ignores.paths")
        if "paths" in ignores_data
        else (),
        findings=_mapping_list(ignores_data["findings"], "repository.ignores.findings")
        if "findings" in ignores_data
        else (),
    )
    for pattern in ignores.paths:
        _validate_glob(pattern, "repository.ignores.paths")
    repository = RepositoryConfig(profiles=profiles, ignores=ignores)

    paths_data = _require_mapping(root.get("paths", {}), "paths")
    _unknown(paths_data, {"include", "exclude", "overlays"}, "paths")
    includes = (
        _tuple_strings(paths_data["include"], "paths.include")
        if "include" in paths_data
        else ("**",)
    )
    excludes = (
        _tuple_strings(paths_data["exclude"], "paths.exclude") if "exclude" in paths_data else ()
    )
    for path, patterns in (("paths.include", includes), ("paths.exclude", excludes)):
        for pattern in patterns:
            _validate_glob(pattern, path)
    overlays_raw = paths_data.get("overlays", [])
    if not isinstance(overlays_raw, list):
        raise ReviewContractError(
            "REVIEW_YAML_TYPE", "paths.overlays must be a list", "paths.overlays"
        )
    overlays = tuple(_parse_overlay(item, i) for i, item in enumerate(overlays_raw))
    overlay_ids = [overlay.id for overlay in overlays]
    if len(overlay_ids) != len(set(overlay_ids)):
        raise ReviewContractError(
            "REVIEW_CONFLICT",
            "duplicate path overlay IDs are not merge-compatible",
            "paths.overlays",
        )
    paths = PathSelectionConfig(includes, excludes, overlays)

    passes_raw = root.get("passes", [])
    if not isinstance(passes_raw, list):
        raise ReviewContractError("REVIEW_YAML_TYPE", "passes must be a list", "passes")
    passes = tuple(_parse_pass(item, i) for i, item in enumerate(passes_raw))
    pass_ids = [item.id for item in passes]
    if len(pass_ids) != len(set(pass_ids)):
        raise ReviewContractError(
            "REVIEW_CONFLICT", "duplicate pass IDs are not merge-compatible", "passes"
        )
    triggers_data = _require_mapping(root.get("triggers", {}), "triggers")
    _unknown(triggers_data, {"automatic", "skip", "dynamic"}, "triggers")
    triggers = TriggerConfig(any=dict(triggers_data))
    gates = _parse_gate(root.get("gates", {}), "gates")
    labels = _parse_labels(root.get("labels", {}), "labels")
    explain_data = _require_mapping(root.get("explainability", {}), "explainability")
    _unknown(explain_data, {"effective_config", "trace_each_finding", "dry_run"}, "explainability")
    explainability = ExplainabilityConfig(
        effective_config=dict(explain_data.get("effective_config", {}))
        if isinstance(explain_data.get("effective_config", {}), Mapping)
        else {},
        trace_each_finding=dict(explain_data.get("trace_each_finding", {}))
        if isinstance(explain_data.get("trace_each_finding", {}), Mapping)
        else {},
        dry_run=dict(explain_data.get("dry_run", {}))
        if isinstance(explain_data.get("dry_run", {}), Mapping)
        else {},
    )
    return ReviewContract(
        1,
        policy_source,
        inheritance,
        organization,
        repository,
        paths,
        passes,
        triggers,
        gates,
        labels,
        explainability,
    )


def _canonical(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {
            field.name: _canonical(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _canonical(value[key]) for key in sorted(value, key=str)}
    if isinstance(value, (tuple, list)):
        return [_canonical(item) for item in value]
    return value


def contract_hash(contract: ReviewContract) -> str:
    payload = json.dumps(
        _canonical(contract), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _default_contract() -> ReviewContract:
    return ReviewContract(
        1,
        PolicySourceConfig(),
        InheritanceConfig(),
        OrganizationConfig(),
        RepositoryConfig(),
        PathSelectionConfig(),
        (),
        TriggerConfig(),
        GateConfig(),
        LabelConfig(),
        ExplainabilityConfig(),
    )


def _loaded_denial(
    source_ref: str, source_commit: str, diagnostic: ReviewDiagnostic
) -> LoadedReviewContract:
    contract = _default_contract()
    return LoadedReviewContract(
        contract, source_ref, source_commit, contract_hash(contract), False, (diagnostic,)
    )


def load_review_contract(context: ReviewLoadContext) -> LoadedReviewContract:
    """Load only the protected base copy; absent and unreadable are distinct."""
    requested = context.protected_base_ref or context.base_ref
    try:
        protected = context.source.protected_ref(requested)
    except Exception as exc:
        return _loaded_denial(
            requested,
            "",
            ReviewDiagnostic(
                "REVIEW_PROTECTED_REF_REQUIRED",
                "error",
                f"cannot establish protected ref {requested!r}: {exc}",
                "policy_source",
            ),
        )
    if not isinstance(protected, str) or not protected:
        return _loaded_denial(
            requested,
            "",
            ReviewDiagnostic(
                "REVIEW_PROTECTED_REF_REQUIRED", "error", "protected ref was empty", "policy_source"
            ),
        )
    try:
        text = context.source.read_base_file(protected, ".gitreins/review.yaml")
    except Exception as exc:
        return _loaded_denial(
            context.base_ref,
            protected,
            ReviewDiagnostic(
                "REVIEW_BASE_REF_UNAVAILABLE",
                "error",
                f"cannot read policy from {protected}: {exc}",
                ".gitreins/review.yaml",
            ),
        )
    if text is None:
        contract = _default_contract()
        return LoadedReviewContract(
            contract, context.base_ref, protected, contract_hash(contract), True, ()
        )
    try:
        contract = parse_review_contract(text, source_ref=context.base_ref, source_commit=protected)
    except ReviewContractError as exc:
        return _loaded_denial(context.base_ref, protected, exc.diagnostic())
    return LoadedReviewContract(
        contract, context.base_ref, protected, contract_hash(contract), False, ()
    )


def _normalize_changed_paths(changed_files: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: dict[str, str] = {}
    for raw in changed_files:
        if (
            not isinstance(raw, str)
            or not raw
            or "\x00" in raw
            or raw.startswith("/")
            or "\\" in raw
        ):
            raise ReviewContractError(
                "REVIEW_PATH_UNSAFE", f"unsafe changed path {raw!r}", "changed_files"
            )
        if raw != posixpath.normpath(raw) or raw.startswith("./") or raw.endswith("/"):
            raise ReviewContractError(
                "REVIEW_PATH_UNSAFE", f"changed path is not normalized: {raw!r}", "changed_files"
            )
        if raw == ".." or raw.startswith("../") or "/../" in raw:
            raise ReviewContractError(
                "REVIEW_PATH_UNSAFE", f"path traversal in changed path {raw!r}", "changed_files"
            )
        nfc = unicodedata.normalize("NFC", raw)
        if nfc in seen and seen[nfc] != raw:
            raise ReviewContractError(
                "REVIEW_PATH_COLLISION",
                f"NFC path collision between {seen[nfc]!r} and {raw!r}",
                "changed_files",
            )
        seen[nfc] = raw
        normalized.append(nfc)
    return tuple(dict.fromkeys(normalized))


def _matches(pattern: str, path: str) -> bool:
    return fnmatch.fnmatchcase(path, pattern)


def _overlay_matches(overlay: PathOverlayConfig, path: str) -> tuple[bool, tuple[str, ...]]:
    patterns: list[str] = []
    for key in ("any", "all"):
        raw = overlay.match.get(key, ())
        if isinstance(raw, (list, tuple)):
            patterns.extend(str(item) for item in raw)
    if not patterns:
        return False, ()
    any_patterns = tuple(str(item) for item in overlay.match.get("any", ()) or ())
    all_patterns = tuple(str(item) for item in overlay.match.get("all", ()) or ())
    matches = tuple(
        pattern for pattern in (*any_patterns, *all_patterns) if _matches(pattern, path)
    )
    return (
        bool(any_patterns and any(_matches(pattern, path) for pattern in any_patterns))
        or bool(all_patterns and all(_matches(pattern, path) for pattern in all_patterns))
    ), matches


def _append_unique(items: list[str], values: Sequence[str]) -> None:
    for value in values:
        if value not in items:
            items.append(value)


def _merge_gate(base: GateConfig, overlay: GateConfig) -> GateConfig:
    return GateConfig(
        locked=overlay.locked if overlay.locked is not None else base.locked,
        minimum_severity=overlay.minimum_severity or base.minimum_severity,
        minimum_confidence=overlay.minimum_confidence
        if overlay.minimum_confidence is not None
        else base.minimum_confidence,
        require=tuple(dict.fromkeys((*base.require, *overlay.require))),
        when={**dict(base.when), **dict(overlay.when)},
        action=overlay.action or base.action,
        on_fail=overlay.on_fail or base.on_fail,
        path_rules=(*base.path_rules, *overlay.path_rules),
        finding_policy={**dict(base.finding_policy), **dict(overlay.finding_policy)},
        security={**dict(base.security), **dict(overlay.security)},
    )


def _field_value(contract: ReviewContract, path: str) -> Any:
    current: Any = contract
    for part in path.split("."):
        if isinstance(current, Mapping):
            current = current.get(part)
        else:
            current = getattr(current, part, None)
    return current


def resolve_review_policy(
    loaded: LoadedReviewContract,
    *,
    changed_files: Sequence[str],
    labels: Sequence[str] = (),
    target_branch: str | None = None,
    is_draft: bool = False,
    title: str = "",
    changed_lines: Mapping[str, set[int]] | None = None,
) -> ResolvedReviewPolicy:
    """Resolve one immutable complete policy for a normalized changed-file set."""
    diagnostics = list(loaded.diagnostics)
    try:
        normalized = _normalize_changed_paths(changed_files)
    except ReviewContractError as exc:
        diagnostics.append(exc.diagnostic())
        normalized = ()
    contract = loaded.contract
    include = list(contract.paths.include)
    exclude: list[str] = []
    _append_unique(exclude, contract.paths.exclude)
    _append_unique(exclude, contract.repository.ignores.paths)
    for patterns in (include, exclude):
        for pattern in patterns:
            try:
                _validate_glob(pattern, "paths")
            except ReviewContractError as exc:
                diagnostics.append(exc.diagnostic())

    eligible = [
        path
        for path in normalized
        if any(_matches(pattern, path) for pattern in include)
        and not any(_matches(pattern, path) for pattern in exclude)
    ]
    matched_ids: list[str] = []
    matched_globs: list[str] = []
    instructions: list[str] = []
    resolved_passes: list[ResolvedPass] = []
    for item in contract.passes:
        resolved_passes.append(
            ResolvedPass(
                item.id,
                item.enabled,
                item.scope,
                item.instructions,
                item.execution,
                item.trigger,
                item.gates,
                item.output,
            )
        )
        _append_unique(instructions, item.instructions)

    effective_profile: str | None = None
    effective_tools: list[str] = []
    if contract.repository.profiles:
        effective_profile = next(iter(contract.repository.profiles))
        _append_unique(effective_tools, contract.repository.profiles[effective_profile].tools)
    gates = contract.gates
    resolved_labels = ResolvedLabels(
        inputs=contract.labels.inputs,
        outputs=contract.labels.outputs,
        lifecycle=contract.labels.lifecycle,
    )
    for overlay in contract.paths.overlays:
        overlay_matches: list[str] = []
        for path in eligible:
            matched, globs = _overlay_matches(overlay, path)
            if matched:
                overlay_matches.extend(globs)
        if not overlay_matches:
            continue
        if overlay.id in matched_ids:
            diagnostics.append(
                ReviewDiagnostic(
                    "REVIEW_CONFLICT",
                    "error",
                    f"overlay {overlay.id!r} matched more than once",
                    "paths.overlays",
                )
            )
        matched_ids.append(overlay.id)
        _append_unique(matched_globs, overlay_matches)
        _append_unique(instructions, overlay.instructions)
        review_profile = overlay.review.get("profile")
        if isinstance(review_profile, str):
            if review_profile not in contract.repository.profiles:
                diagnostics.append(
                    ReviewDiagnostic(
                        "REVIEW_UNKNOWN_PROFILE",
                        "error",
                        f"unknown profile {review_profile!r}",
                        "paths.overlays",
                    )
                )
            else:
                effective_profile = review_profile
                effective_tools = list(contract.repository.profiles[review_profile].tools)
        tools_add = overlay.review.get("tools_add", ())
        if isinstance(tools_add, (list, tuple)):
            _append_unique(effective_tools, tuple(str(tool) for tool in tools_add))
        gates = _merge_gate(gates, overlay.gates)
        _append_unique(list(resolved_labels.add), overlay.labels.add)
        resolved_labels = ResolvedLabels(
            add=tuple(dict.fromkeys((*resolved_labels.add, *overlay.labels.add))),
            remove=tuple(dict.fromkeys((*resolved_labels.remove, *overlay.labels.remove))),
            inputs=resolved_labels.inputs,
            outputs=resolved_labels.outputs,
            lifecycle=resolved_labels.lifecycle,
        )

    # Organization locks are evaluated against every lower-level value that is
    # represented by the contract.  This is intentionally fail-closed.
    for locked in contract.organization.locked:
        baseline = _field_value(contract, locked)
        if locked == "policy_source" and contract.policy_source.allow_pr_override:
            diagnostics.append(
                ReviewDiagnostic(
                    "REVIEW_LOCKED_FIELD_VIOLATION",
                    "error",
                    "locked policy_source cannot allow PR override",
                    locked,
                    "organization",
                )
            )
        if locked == "gates.security" and not contract.gates.security:
            diagnostics.append(
                ReviewDiagnostic(
                    "REVIEW_LOCKED_FIELD_VIOLATION",
                    "error",
                    "locked gates.security was removed",
                    locked,
                    "repository",
                )
            )
        if locked == "passes.security" and not any(
            item.id == "security" and item.enabled for item in contract.passes
        ):
            diagnostics.append(
                ReviewDiagnostic(
                    "REVIEW_LOCKED_FIELD_VIOLATION",
                    "error",
                    "locked security pass was removed or disabled",
                    locked,
                    "repository",
                )
            )
        if baseline is None:
            diagnostics.append(
                ReviewDiagnostic(
                    "REVIEW_LOCKED_FIELD_VIOLATION",
                    "error",
                    f"locked field {locked} is absent",
                    locked,
                    "organization",
                )
            )

    if is_draft and contract.triggers.any.get("skip", {}).get("drafts") is True:
        diagnostics.append(
            ReviewDiagnostic(
                "REVIEW_SKIPPED", "warning", "draft review is skipped", "triggers.skip.drafts"
            )
        )
    skip = contract.triggers.any.get("skip", {})
    if isinstance(skip, Mapping) and set(labels) & set(skip.get("labels", ()) or ()):
        diagnostics.append(
            ReviewDiagnostic(
                "REVIEW_SKIPPED", "warning", "review skipped by label", "triggers.skip.labels"
            )
        )
    for pattern in skip.get("title_patterns", ()) if isinstance(skip, Mapping) else ():
        if isinstance(pattern, str) and re.search(pattern, title):
            diagnostics.append(
                ReviewDiagnostic(
                    "REVIEW_SKIPPED",
                    "warning",
                    "review skipped by title pattern",
                    "triggers.skip.title_patterns",
                )
            )
    _ = target_branch

    normalized_payload = json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    changed_payload = json.dumps(
        sorted(normalized), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    diff_payload = json.dumps(
        {key: sorted(value) for key, value in sorted((changed_lines or {}).items())},
        separators=(",", ":"),
    ).encode("utf-8")

    def digest(payload: bytes) -> str:
        return hashlib.sha256(payload).hexdigest()

    return ResolvedReviewPolicy(
        layers=contract.inheritance.layers,
        matched_overlay_ids=tuple(matched_ids),
        matched_globs=tuple(matched_globs),
        instructions=tuple(instructions),
        passes=tuple(resolved_passes),
        include_paths=tuple(include),
        exclude_paths=tuple(exclude),
        effective_profile=effective_profile,
        effective_tools=tuple(effective_tools),
        gates=gates,
        labels=resolved_labels,
        config_hash=loaded.config_hash,
        diagnostics=tuple(diagnostics),
        source_ref=loaded.source_ref,
        source_commit=loaded.source_commit,
        normalized_paths_digest=digest(normalized_payload),
        changed_files_digest=digest(changed_payload),
        diff_digest=digest(diff_payload),
    )
