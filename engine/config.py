"""
GitReins Unified Configuration — single source of truth for all defaults.

Every other module (LLM, evaluator, pipeline, guards, task manager)
reads its defaults from here rather than hardcoding them.

Load order (later overrides earlier):
    1. Hardcoded defaults (this file)
    2. .gitreins/config.yaml in the target repo
    3. Explicit constructor parameters
"""

import json
import logging
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from engine import scanner_nice
from engine.data_protection import DataProtectionPolicy

logger = logging.getLogger("gitreins.config")


# ── Constants ─────────────────────────────────────────────────

UPDATE_CACHE_DIR = os.path.join(
    os.environ.get("HOME", os.path.expanduser("~")), ".cache", "gitreins"
)
UPDATE_CACHE_FILE = os.path.join(UPDATE_CACHE_DIR, "update-check.json")


# ── Defaults dataclass ────────────────────────────────────────


@dataclass
class QualityConfig:
    """Repo-owned quality artifact command and per-metric policy."""

    enabled: bool = False
    command: str = ""
    artifact_path: str = ""
    targets_source: str = ""
    per_metric_mode: dict[str, str] = field(default_factory=dict)
    timeout: int = 300

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "QualityConfig":
        value = value if isinstance(value, dict) else {}
        modes = value.get("per_metric_mode", {})
        modes = modes if isinstance(modes, dict) else {}
        return cls(
            enabled=value.get("enabled") is True,
            command=str(value.get("command", "")),
            artifact_path=str(value.get("artifact_path", "")),
            targets_source=str(value.get("targets_source", "")),
            per_metric_mode={str(k): v for k, v in modes.items() if v in ("warn", "block")},
            timeout=_coerce_positive_int(value.get("timeout", 300)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "command": self.command,
            "artifact_path": self.artifact_path,
            "targets_source": self.targets_source,
            "per_metric_mode": dict(self.per_metric_mode),
            "timeout": self.timeout,
        }


@dataclass
class SpecAlignmentConfig:
    """Optional Auger-backed staged-diff specification alignment (GR-154)."""

    enabled: bool = False
    command: str = ""
    endpoint: str = ""
    spec_root: str = "specs"
    mode: str = "warn"
    missing_checker: str = "warn"
    timeout: int = 30
    protocol_version: str = "1"
    min_checker_version: str = "1.0.0"

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "SpecAlignmentConfig":
        block = value if isinstance(value, dict) else {}
        mode = str(block.get("mode", "warn")).lower()
        missing = str(block.get("missing_checker", "warn")).lower()
        return cls(
            enabled=block.get("enabled") is True,
            command=str(block.get("command", "")),
            endpoint=str(block.get("endpoint", "")),
            spec_root=str(block.get("spec_root", "specs")),
            mode=mode if mode in {"warn", "block"} else "warn",
            missing_checker=missing if missing in {"warn", "block"} else "warn",
            timeout=_coerce_positive_int(block.get("timeout", 30)),
            protocol_version=str(block.get("protocol_version", "1")),
            min_checker_version=str(block.get("min_checker_version", "1.0.0")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "command": self.command,
            "endpoint": self.endpoint,
            "spec_root": self.spec_root,
            "mode": self.mode,
            "missing_checker": self.missing_checker,
            "timeout": self.timeout,
            "protocol_version": self.protocol_version,
            "min_checker_version": self.min_checker_version,
        }


@dataclass
class DataProtectionConfig:
    """The top-level ``data_protection:`` policy block (GR-146).

    A typed holder so the unified config loader validates the block the same
    way it validates every other section, and so ``to_config_dict`` can
    round-trip it.  All detection/handling authority lives in
    :mod:`engine.data_protection`; validation is delegated there so the guard
    and the judge can never disagree about what a policy means (AC1/AC3).
    """

    policy: DataProtectionPolicy = field(default_factory=DataProtectionPolicy.default)

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "DataProtectionConfig":
        block = value if isinstance(value, dict) else {}
        return cls(policy=DataProtectionPolicy.from_dict(block))

    @property
    def enabled(self) -> bool:
        return self.policy.enabled

    def to_dict(self) -> dict[str, Any]:
        return self.policy.to_dict()


@dataclass
class SecurityReviewConfig:
    """Staged security review pipeline (GR-156).

    ``mode`` gates the guard's ``security_review`` lane:

      * ``off``    — no stage runs; guard output is unchanged (default).
      * ``staged`` — deterministic rules (semgrep/ast-grep) + Antares
        localization over the staged files.
      * ``full``   — staged plus the LLM judge over surviving hypotheses.

    ``missing_tool_policy`` decides how a stage degrades when its tool is
    absent (semgrep/ast-grep/Antares/judge): ``fail`` fails the lane loudly;
    ``skip`` records a named skip — never a silent pass.
    """

    mode: str = "off"  # "off" | "staged" | "full"
    min_confidence: float = 0.7
    per_stage_timeout_seconds: int = 60
    missing_tool_policy: str = "fail"  # "skip" | "fail"

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "SecurityReviewConfig":
        block = value if isinstance(value, dict) else {}
        mode = str(block.get("mode", "off")).strip().lower()
        policy = str(block.get("missing_tool_policy", "fail")).strip().lower()
        confidence = block.get("min_confidence", 0.7)
        try:
            min_confidence = float(confidence)
        except (TypeError, ValueError):
            raise ValueError(
                f"security_review.min_confidence must be a number, got {confidence!r}"
            ) from None
        if not 0.0 <= min_confidence <= 1.0:
            raise ValueError(
                f"security_review.min_confidence must be between 0.0 and 1.0, got {confidence!r}"
            )
        timeout = block.get("per_stage_timeout_seconds", 60)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
            raise ValueError(
                "security_review.per_stage_timeout_seconds must be a positive "
                f"integer, got {timeout!r}"
            )
        return cls(
            mode=mode if mode in ("off", "staged", "full") else "off",
            min_confidence=min_confidence,
            per_stage_timeout_seconds=timeout,
            missing_tool_policy=policy if policy in ("skip", "fail") else "fail",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "min_confidence": self.min_confidence,
            "per_stage_timeout_seconds": self.per_stage_timeout_seconds,
            "missing_tool_policy": self.missing_tool_policy,
        }


@dataclass
class GitReinsDefaults:
    """Every default GitReins needs, in one place.

    Create with no args to get production defaults, or use
    load_defaults() to layer in .gitreins/config.yaml.
    """

    # ── LLM ──
    # REVIEW-GITREINS-031: this model and engine/llm.py's default base_url
    # (https://api.deepseek.com/v1) are a coherent PAIR — if either side
    # changes, change the other (or add a `gitreins doctor` warning).
    model: str = "deepseek-v4-flash"
    llm_reasoning: str = "disabled"  # "disabled" | "enabled" — DeepSeek thinking mode

    # ── Evaluation caps ──
    max_iterations: float = 100.0  # -1 = unlimited
    max_seconds: float = -1.0  # -1 = unlimited
    max_input_tokens: int = 10_000_000  # 10M
    max_output_tokens: int = 131_072  # 128K — safe floor below most provider caps
    max_tokens_per_call: int = 16384  # Per-LLM-call cap (session budget is separate)
    tool_call_weight: float = 0.1
    # Compact once cumulative input tokens consumed since the last compaction
    # exceed this share of max_input_tokens — the same quantity the cap meters.
    compaction_threshold: float = 0.90
    code_context_budget: float = 0.70  # cap pre-loaded code context to 70% of input budget
    # GR-170: repo-wide review is the default — the reviewer must be able to
    # follow a changed symbol to its callers and entrypoint. 'changed' (only
    # changed files + mapped tests + always-allowed config) is the explicit
    # opt-out and stays available for large trees with tight budgets.
    file_scope: str = "full"
    fast_track: str = "auto"  # skip full call-graph on large repos (GR-064a)
    max_file_bytes: int = (
        131_072  # 128KB — cap read_file results to prevent context explosion (GR-064d)
    )
    # GR-170: a whole-file read above this many bytes is REFUSED (not truncated)
    # so the judge never grades against a partial file; range reads still work.
    max_read_bytes: int = 524_288
    pass_on_error: bool = False  # skip Tier 2 when LLM is unavailable (advisory-only mode)

    # ── Guard defaults ──
    hook_timeout: int = 300  # overall pre-commit hook timeout (GR-064e, raised from 120)
    # DF-GITREINS-POC-55: nice(1) level for every external scanner the guard and
    # the judge's Tier 1 spawn (gitleaks, golangci-lint, go build/test, ruff,
    # pytest, LSP servers). Scans are background verification, so they run as
    # the least urgent CPU consumer by default; 0 disables the prefix entirely.
    # Overridden by guards.scanner_nice in config.yaml, and by
    # GITREINS_SCANNER_NICE in the environment (precedence env > config > this).
    scanner_nice: int = 10

    # ── Parallel worktree fleet ──
    max_concurrent_worktrees: int = 2
    worktree_venv_source: str = ".venv"
    worktree_venv_name: str = ".venv"
    worktree_disk_ceiling_mb: int = 4096

    # ── Security scan (Antares) ──
    security_scan_enabled: bool = False
    security_scan_model: str = "antares-1b"  # "antares-1b" | "antares-350m"
    security_scan_min_confidence: float = 0.7
    security_scan_cve_source: str = "nvd"  # "nvd" | "github" | "both"

    # ── Commit audit ──
    commit_audit_enabled: bool = True  # validate commit messages against staged diff
    commit_audit_mode: str = "warn"  # "warn" | "block" | "suggest"
    commit_audit_strictness: str = "standard"  # "lenient" | "standard" | "strict"
    commit_audit_max_iterations: int = 3  # LLM exploration rounds (0 = no tools, single call)
    commit_audit_suggest_message: bool = True  # suggest better message on rejection

    # ── Commit review (GR-065: CodeRabbit-style) ──
    commit_audit_review_mode: str = "message"  # "message" | "review" | "agent"
    commit_audit_review_checks_bugs: bool = True
    commit_audit_review_checks_security: bool = True
    commit_audit_review_checks_style: bool = False
    commit_audit_review_checks_performance: bool = False
    commit_audit_review_checks_anti_patterns: bool = True
    commit_audit_review_severity: str = "standard"  # "critical-only" | "standard" | "all"
    commit_audit_review_suggest_fix: bool = True
    commit_audit_review_max_tokens: int = 2048

    # ── Commit review scoring (GR-066: CVE-style) ──
    commit_audit_review_score_threshold: float = 8.0
    commit_audit_review_score_offset: float = 1.0

    # ── Commit review profiles + effort (GR-148) ──
    commit_audit_review_profile: str = "standard"  # quick | standard | deep
    commit_audit_review_effort: dict[str, int] = field(default_factory=dict)
    """Repo-default effort budget (commit_audit.review_effort in .gitreins
    config). Keys: max_passes, max_llm_calls, max_tokens, time_budget_s,
    max_tool_calls. Per-invocation override (CLI) takes precedence over this;
    the profile default is the base."""

    # ── Update checking ──
    check_for_updates: bool = True
    update_check_ttl_hours: float = 24.0  # re-check after this many hours

    # ── History / verdict persistence ──
    history_enabled: bool = True
    history_path: str = ".gitreins/history"
    history_storage: str = "git"  # "git" or "filesystem"
    history_max_verdicts: int = 1000

    # ── Jev resolution gate (JEVRES-006) ──
    # One `resolution:` block in .gitreins/config.yaml drives every surface.
    # Each surface defaults to DISABLED: the gate is a real egress (the bundle
    # leaves the host for OpenRouter) and the judge-adjacent surfaces have no
    # calibration numbers until JEVRES-005 lands — so nothing ships enabled
    # until an operator opts in. The defaults that are measured facts (model
    # build, token ceiling, band thresholds) live in docs/jev-resolution-gate.md
    # §2/§9 — the single design authority; they are mirrored here only as code
    # defaults, never re-stated in prose docs.
    resolution_enabled_cli: bool = False
    resolution_enabled_mcp: bool = False
    resolution_enabled_predispatch: bool = False
    resolution_enabled_judge_prescreen: bool = False
    resolution_model: str = "typesafe/jev-1.13"
    resolution_tokens_max: int = 28_000
    resolution_resolved_at: float = 0.85
    resolution_review_at: float = 0.50
    resolution_egress_exclude: tuple[str, ...] = ()

    # ── Repo-produced quality snapshot (GR-142) ──
    quality: QualityConfig = field(default_factory=QualityConfig)

    # ── Optional Auger spec-alignment guard (GR-154) ──
    # Disabled and side-effect free unless a repository opts in explicitly.
    spec_alignment: SpecAlignmentConfig = field(default_factory=SpecAlignmentConfig)

    # ── Tier-1 data protection (GR-146) ──
    # Ships DISABLED: an absent block is the backwards-compatible no-op, so an
    # existing repository's guard/judge behaviour is byte-for-byte unchanged
    # until an operator opts in (AC1). Validation is delegated to
    # engine.data_protection so a malformed policy fails loudly (AC3).
    data_protection: DataProtectionConfig = field(default_factory=DataProtectionConfig)

    # ── Staged security review (GR-156) ──
    # Ships OFF: mode "off" runs no stage, so an existing repository's guard
    # output is byte-for-byte unchanged until an operator opts in.
    security_review: SecurityReviewConfig = field(default_factory=SecurityReviewConfig)

    # Metadata
    _source: str = field(default="(built-in defaults)", repr=False)

    def overlay(self, config_dict: dict[str, Any] | None) -> "GitReinsDefaults":
        """Return a new defaults object with config.yaml values overlaid.

        Only keys explicitly present in config replace the defaults.
        """
        if not config_dict:
            return self

        defaults = config_dict.get("defaults", {})

        # Deep copy numeric/string fields
        result = GitReinsDefaults(
            model=defaults.get("model", self.model),
            llm_reasoning=str(defaults.get("llm_reasoning", self.llm_reasoning)),
            max_iterations=_coerce_float(defaults.get("max_iterations", self.max_iterations)),
            max_seconds=_coerce_seconds(defaults.get("max_time", self.max_seconds)),
            max_input_tokens=_coerce_tokens(
                defaults.get("max_input_tokens", self.max_input_tokens)
            ),
            max_output_tokens=_coerce_tokens(
                defaults.get("max_output_tokens", self.max_output_tokens)
            ),
            max_tokens_per_call=int(defaults.get("max_tokens_per_call", self.max_tokens_per_call)),
            tool_call_weight=float(defaults.get("tool_call_weight", self.tool_call_weight)),
            compaction_threshold=float(
                defaults.get("compaction_threshold", self.compaction_threshold)
            ),
            code_context_budget=float(
                defaults.get("code_context_budget", self.code_context_budget)
            ),
            file_scope=str(defaults.get("file_scope", self.file_scope)),
            fast_track=str(defaults.get("fast_track", self.fast_track)),
            max_file_bytes=int(defaults.get("max_file_bytes", self.max_file_bytes)),
            max_read_bytes=int(defaults.get("max_read_bytes", self.max_read_bytes)),
            pass_on_error=bool(defaults.get("pass_on_error", self.pass_on_error)),
            hook_timeout=int(defaults.get("hook_timeout", self.hook_timeout)),
            # DF-GITREINS-POC-55: the scanner nice level is read from the
            # `guards:` block (the block the guard itself consumes), not from
            # `defaults:`. An unusable value is ignored with a warning — a
            # scheduling nicety must never be able to fail a scan.
            scanner_nice=scanner_nice.coerce_level(
                _guards_value(config_dict, scanner_nice.CONFIG_KEY),
                fallback=self.scanner_nice,
                what=f"{scanner_nice.CONFIG_SECTION}.{scanner_nice.CONFIG_KEY}",
            ),
            max_concurrent_worktrees=_coerce_positive_int(
                _worktree_fleet_value(
                    config_dict, defaults, "max_concurrent_worktrees", self.max_concurrent_worktrees
                )
            ),
            worktree_venv_source=str(
                _worktree_fleet_value(
                    config_dict, defaults, "venv_source", self.worktree_venv_source
                )
            ),
            worktree_venv_name=str(
                _worktree_fleet_value(config_dict, defaults, "venv_name", self.worktree_venv_name)
            ),
            worktree_disk_ceiling_mb=_coerce_disk_ceiling(
                _worktree_fleet_value(
                    config_dict, defaults, "disk_ceiling_mb", self.worktree_disk_ceiling_mb
                )
            ),
            security_scan_enabled=bool(
                defaults.get("security_scan", {}).get("enabled", self.security_scan_enabled)
            ),
            security_scan_model=str(
                defaults.get("security_scan", {}).get("model", self.security_scan_model)
            ),
            security_scan_min_confidence=float(
                defaults.get("security_scan", {}).get(
                    "min_confidence", self.security_scan_min_confidence
                )
            ),
            security_scan_cve_source=str(
                defaults.get("security_scan", {}).get("cve_source", self.security_scan_cve_source)
            ),
            commit_audit_enabled=bool(
                defaults.get("commit_audit", {}).get("enabled", self.commit_audit_enabled)
            ),
            commit_audit_mode=str(
                defaults.get("commit_audit", {}).get("mode", self.commit_audit_mode)
            ),
            commit_audit_strictness=str(
                defaults.get("commit_audit", {}).get("strictness", self.commit_audit_strictness)
            ),
            commit_audit_max_iterations=int(
                defaults.get("commit_audit", {}).get(
                    "max_iterations", self.commit_audit_max_iterations
                )
            ),
            commit_audit_suggest_message=bool(
                defaults.get("commit_audit", {}).get(
                    "suggest_message", self.commit_audit_suggest_message
                )
            ),
            # Review fields
            commit_audit_review_mode=str(
                defaults.get("commit_audit", {}).get("review_mode", self.commit_audit_review_mode)
            ),
            commit_audit_review_checks_bugs=bool(
                defaults.get("commit_audit", {})
                .get("review_checks", {})
                .get("bugs", self.commit_audit_review_checks_bugs)
            ),
            commit_audit_review_checks_security=bool(
                defaults.get("commit_audit", {})
                .get("review_checks", {})
                .get("security", self.commit_audit_review_checks_security)
            ),
            commit_audit_review_checks_style=bool(
                defaults.get("commit_audit", {})
                .get("review_checks", {})
                .get("style", self.commit_audit_review_checks_style)
            ),
            commit_audit_review_checks_performance=bool(
                defaults.get("commit_audit", {})
                .get("review_checks", {})
                .get("performance", self.commit_audit_review_checks_performance)
            ),
            commit_audit_review_checks_anti_patterns=bool(
                defaults.get("commit_audit", {})
                .get("review_checks", {})
                .get("anti_patterns", self.commit_audit_review_checks_anti_patterns)
            ),
            commit_audit_review_severity=str(
                defaults.get("commit_audit", {}).get(
                    "review_severity", self.commit_audit_review_severity
                )
            ),
            commit_audit_review_suggest_fix=bool(
                defaults.get("commit_audit", {}).get(
                    "review_suggest_fix", self.commit_audit_review_suggest_fix
                )
            ),
            commit_audit_review_profile=str(
                defaults.get("commit_audit", {}).get(
                    "review_profile", self.commit_audit_review_profile
                )
            ),
            commit_audit_review_effort=dict(
                defaults.get("commit_audit", {}).get(
                    "review_effort", self.commit_audit_review_effort
                )
                or {}
            ),
            commit_audit_review_max_tokens=int(
                defaults.get("commit_audit", {}).get(
                    "review_max_tokens", self.commit_audit_review_max_tokens
                )
            ),
            # Scoring (GR-066)
            commit_audit_review_score_threshold=float(
                defaults.get("commit_audit", {}).get(
                    "review_score_threshold", self.commit_audit_review_score_threshold
                )
            ),
            commit_audit_review_score_offset=float(
                defaults.get("commit_audit", {}).get(
                    "review_score_offset", self.commit_audit_review_score_offset
                )
            ),
            check_for_updates=bool(defaults.get("check_for_updates", self.check_for_updates)),
            update_check_ttl_hours=_coerce_float(
                defaults.get("update_check_ttl", self.update_check_ttl_hours)
            ),
            history_enabled=bool(defaults.get("history_enabled", self.history_enabled)),
            history_path=str(defaults.get("history_path", self.history_path)),
            history_storage=str(defaults.get("history_storage", self.history_storage)),
            history_max_verdicts=int(
                defaults.get("history_max_verdicts", self.history_max_verdicts)
            ),
            # ── Jev resolution gate (JEVRES-006) ──
            # The block lives at the TOP LEVEL of config.yaml (same shape
            # cmd_init writes and the same precedent as worktree_fleet), not
            # nested under `defaults:`.
            resolution_enabled_cli=bool(_resolution_enabled(config_dict, "cli")),
            resolution_enabled_mcp=bool(_resolution_enabled(config_dict, "mcp")),
            resolution_enabled_predispatch=bool(_resolution_enabled(config_dict, "predispatch")),
            resolution_enabled_judge_prescreen=bool(
                _resolution_enabled(config_dict, "judge_prescreen")
            ),
            resolution_model=str(resolution_cfg(config_dict).get("model", self.resolution_model)),
            resolution_tokens_max=_resolution_tokens_max(config_dict, self.resolution_tokens_max),
            resolution_resolved_at=float(
                resolution_bands(config_dict).get("resolved_at", self.resolution_resolved_at)
            ),
            resolution_review_at=float(
                resolution_bands(config_dict).get("review_at", self.resolution_review_at)
            ),
            resolution_egress_exclude=_resolution_excludes(config_dict),
            quality=QualityConfig.from_dict(config_dict.get("quality")),
            spec_alignment=SpecAlignmentConfig.from_dict(config_dict.get("spec_alignment")),
            data_protection=DataProtectionConfig.from_dict(config_dict.get("data_protection")),
            security_review=SecurityReviewConfig.from_dict(config_dict.get("security_review")),
            _source=".gitreins/config.yaml" if config_dict else self._source,
        )

        return result

    def to_config_dict(self) -> dict[str, Any]:
        """Produce a dict suitable for writing to .gitreins/config.yaml."""
        return {
            "model": self.model,
            "llm_reasoning": self.llm_reasoning,
            "max_iterations": (
                int(self.max_iterations)
                if self.max_iterations == int(self.max_iterations)
                else self.max_iterations
            ),
            "max_time": (_fmt_seconds(self.max_seconds) if self.max_seconds > 0 else None),
            "max_input_tokens": (
                _fmt_tokens(self.max_input_tokens) if self.max_input_tokens > 0 else None
            ),
            "max_output_tokens": (
                _fmt_tokens(self.max_output_tokens) if self.max_output_tokens > 0 else None
            ),
            "max_tokens_per_call": (
                _fmt_tokens(self.max_tokens_per_call) if self.max_tokens_per_call > 0 else None
            ),
            "tool_call_weight": self.tool_call_weight,
            "compaction_threshold": self.compaction_threshold,
            "code_context_budget": self.code_context_budget,
            "file_scope": self.file_scope,
            "fast_track": self.fast_track,
            "max_file_bytes": self.max_file_bytes,
            "max_read_bytes": self.max_read_bytes,
            "pass_on_error": self.pass_on_error,
            "hook_timeout": self.hook_timeout,
            "max_concurrent_worktrees": self.max_concurrent_worktrees,
            "worktree_venv_source": self.worktree_venv_source,
            "worktree_venv_name": self.worktree_venv_name,
            "disk_ceiling_mb": self.worktree_disk_ceiling_mb,
            "commit_audit": {
                "enabled": self.commit_audit_enabled,
                "mode": self.commit_audit_mode,
                "strictness": self.commit_audit_strictness,
                "max_iterations": self.commit_audit_max_iterations,
                "suggest_message": self.commit_audit_suggest_message,
                "review_mode": self.commit_audit_review_mode,
                "review_checks": {
                    "bugs": self.commit_audit_review_checks_bugs,
                    "security": self.commit_audit_review_checks_security,
                    "style": self.commit_audit_review_checks_style,
                    "performance": self.commit_audit_review_checks_performance,
                    "anti_patterns": self.commit_audit_review_checks_anti_patterns,
                },
                "review_severity": self.commit_audit_review_severity,
                "review_suggest_fix": self.commit_audit_review_suggest_fix,
                "review_max_tokens": self.commit_audit_review_max_tokens,
                "review_score_threshold": self.commit_audit_review_score_threshold,
                "review_score_offset": self.commit_audit_review_score_offset,
                "review_profile": self.commit_audit_review_profile,
                "review_effort": self.commit_audit_review_effort,
            },
            "check_for_updates": self.check_for_updates,
            "update_check_ttl": f"{int(self.update_check_ttl_hours)}h",
            # Jev resolution gate (JEVRES-006): every surface ships disabled;
            # measured defaults are docs/jev-resolution-gate.md §2's, not
            # re-derived here.
            "resolution": {
                "enabled": {
                    "cli": self.resolution_enabled_cli,
                    "mcp": self.resolution_enabled_mcp,
                    "predispatch": self.resolution_enabled_predispatch,
                    "judge_prescreen": self.resolution_enabled_judge_prescreen,
                },
                "model": self.resolution_model,
                "tokens_max": self.resolution_tokens_max,
                "bands": {
                    "resolved_at": self.resolution_resolved_at,
                    "review_at": self.resolution_review_at,
                },
                "egress_exclude": list(self.resolution_egress_exclude),
            },
            "quality": self.quality.to_dict(),
            "spec_alignment": self.spec_alignment.to_dict(),
            "data_protection": self.data_protection.to_dict(),
            "security_review": self.security_review.to_dict(),
        }


# ── Resolution-block accessors (JEVRES-006) ──────────────────
# Config from other users is data, not schema: a scalar where a mapping is
# expected (`resolution: true`) or a non-list exclude pattern must degrade to
# the default, never crash config loading (QA-GITREINS-POC-6 posture).


def resolution_cfg(defaults: dict[str, Any]) -> dict[str, Any]:
    """The `resolution:` block of a raw config dict, tolerating wrong types."""
    block = defaults.get("resolution", {})
    return block if isinstance(block, dict) else {}


def resolution_bands(defaults: dict[str, Any]) -> dict[str, Any]:
    """The `resolution.bands:` mapping, tolerating a scalar/absent parent."""
    bands = resolution_cfg(defaults).get("bands", {})
    return bands if isinstance(bands, dict) else {}


def _resolution_enabled(defaults: dict[str, Any], surface: str) -> bool:
    """Per-surface enable flag; only an explicit `true` turns a surface on."""
    enabled = resolution_cfg(defaults).get("enabled", {})
    if not isinstance(enabled, dict):
        return False
    return enabled.get(surface) is True


def _resolution_excludes(defaults: dict[str, Any]) -> tuple[str, ...]:
    """The egress exclusion patterns, as a clean tuple of non-empty strings."""
    patterns = resolution_cfg(defaults).get("egress_exclude", [])
    if not isinstance(patterns, list):
        return ()
    return tuple(str(p) for p in patterns if str(p).strip())


def _resolution_tokens_max(defaults: dict[str, Any], fallback: int) -> int:
    """``tokens_max`` accepts bare ints or '8k'-style strings (like max_input_tokens)."""
    value = resolution_cfg(defaults).get("tokens_max")
    coerced = _coerce_tokens(value) if value is not None else fallback
    return coerced if coerced > 0 else fallback


def _guards_value(config_dict: dict[str, Any], key: str) -> object:
    """One value out of the ``guards:`` block, tolerating a scalar/absent block.

    Config from other users is data, not schema (QA-GITREINS-POC-6 posture):
    ``guards: true`` must degrade to "key absent", never raise.
    """
    guards = config_dict.get("guards", {})
    if not isinstance(guards, dict):
        return None
    return guards.get(key)


def _worktree_fleet_value(
    config_dict: dict[str, Any], defaults: dict[str, Any], key: str, fallback: object
) -> object:
    """Resolve a fleet option from the documented and legacy config shapes."""
    fleet = config_dict.get("worktree_fleet", {})
    if not isinstance(fleet, dict):
        fleet = {}
    venv = fleet.get("venv", {})
    if not isinstance(venv, dict):
        venv = {}
    if key in fleet:
        return fleet[key]
    nested_key = {"venv_source": "source", "venv_name": "name"}.get(key, key)
    if nested_key in venv:
        return venv[nested_key]
    aliases = {
        "venv_source": "worktree_venv_source",
        "venv_name": "worktree_venv_name",
    }
    default_key = aliases.get(key, key)
    if default_key in defaults:
        return defaults[default_key]
    default_venv = defaults.get("worktree_venv", {})
    if isinstance(default_venv, dict) and nested_key in default_venv:
        return default_venv[nested_key]
    return fallback


def _coerce_positive_int(value: object) -> int:
    """Accept an integer cap, rejecting booleans, zero, and fractions."""
    if isinstance(value, bool):
        raise ValueError("max_concurrent_worktrees must be a positive integer, not boolean")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.strip().isdigit():
        result = int(value.strip())
    else:
        raise ValueError(f"max_concurrent_worktrees must be a positive integer, got {value!r}")
    if result < 1:
        raise ValueError(f"max_concurrent_worktrees must be greater than zero, got {result}")
    return result


def _coerce_disk_ceiling(value: object) -> int:
    """Accept zero/negative unlimited disk ceilings, rejecting booleans."""
    if isinstance(value, bool):
        raise ValueError("disk_ceiling_mb must be an integer, not boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    raise ValueError(f"disk_ceiling_mb must be an integer, got {value!r}")


# ── Factory ───────────────────────────────────────────────────


def load_defaults(workdir: str | None = None) -> GitReinsDefaults:
    """Load defaults, overlaid with .gitreins/config.yaml if present.

    Args:
        workdir: Repo root. If None, returns built-in defaults only.

    Returns:
        GitReinsDefaults with config.yaml values overlaid.
    """
    base = GitReinsDefaults()

    if not workdir:
        return base

    import yaml

    config_path = os.path.join(workdir, ".gitreins", "config.yaml")
    if os.path.isfile(config_path):
        try:
            with open(config_path, encoding="utf-8") as f:
                config = yaml.safe_load(f) or {}
        except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
            # QA-GITREINS-POC-6: an unreadable config must degrade to the
            # built-in defaults with a NAMED diagnostic. It used to escape:
            # UnicodeDecodeError is a ValueError subclass, so `except ValueError:
            # raise` below re-raised it and a binary-corrupted config.yaml made
            # `load_defaults()` raise instead of falling back.
            logger.warning("Unreadable %s — using built-in defaults: %s", config_path, exc)
            return base
        try:
            # Values still validate loudly: an out-of-range setting is a config
            # mistake to fix, not corruption to paper over.
            return base.overlay(config)
        except ValueError:
            raise
        except Exception as exc:
            logger.warning("Failed to load %s, using built-in defaults: %s", config_path, exc)

    return base


def load_raw_config(workdir: str | None = None) -> dict[str, Any]:
    """Load .gitreins/config.yaml as a raw dict. Returns {} if not found."""
    if not workdir:
        return {}
    import yaml

    config_path = os.path.join(workdir, ".gitreins", "config.yaml")
    if os.path.isfile(config_path):
        try:
            with open(config_path) as f:
                return yaml.safe_load(f) or {}
        except Exception:
            pass
    return {}


# ── Comment-preserving config rewrite (GR-194) ────────────────
# A config file that loses its rationale comments is not reviewable, so a
# rewrite of an existing .gitreins/config.yaml is a TEXT-level merge: untouched
# lines (and every comment on them) stay byte-identical, and any structure the
# line-level machinery cannot merge safely is REFUSED loudly instead of being
# silently re-dumped without comments.


class ConfigMergeError(Exception):
    """A config rewrite was refused because it could not be merged safely.

    The caller must leave the existing file untouched and report the reason —
    never fall back to a wholesale re-dump that would strip the operator's
    rationale comments.
    """


_TOP_LEVEL_KEY_RE = re.compile(r"^([^\s:#][^:\s]*):(?:\s|$)")


def _yaml_scalar(value: Any) -> str:
    """Render one scalar as a single-line YAML value (no wrapping)."""
    import yaml

    rendered = yaml.dump(value, default_flow_style=False, sort_keys=False, width=4096)
    lines = rendered.rstrip("\n").split("\n")
    if lines and lines[-1] == "...":
        # yaml.dump terminates a scalar-only document with an explicit
        # "..." document-end marker; a mapping document does not carry one.
        lines = lines[:-1]
    if len(lines) != 1:
        raise ValueError(f"value does not render as a single line: {value!r}")
    return lines[0]


def _render_fragment(key: str, value: Any, indent: int) -> list[str]:
    """Render ``key: <value>`` as block YAML lines, indented by *indent*."""
    import yaml

    dumped = yaml.dump({key: value}, default_flow_style=False, sort_keys=False, width=4096)
    pad = " " * indent
    return [pad + line for line in dumped.rstrip("\n").split("\n")]


def _split_trailing_comment(text: str) -> tuple[str, str]:
    """Split ``value  # comment`` into (value, value_gap + comment).

    The gap whitespace between the value and the ``#`` is preserved verbatim
    so a patched line keeps the comment exactly where the operator put it.
    Quotes are honoured: a ``#`` inside quotes is not a comment.
    """
    quote: str | None = None
    for i, ch in enumerate(text):
        if quote is not None:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            value = text[:i].rstrip()
            gap = text[len(value) : i]
            comment = text[i:].rstrip()
            return value, f"{gap}{comment}" if comment else ""
    return text.rstrip(), ""


def _find_key_line(lines: list[str], key: str, indent: int) -> int | None:
    """Last index whose line defines *key* at exactly *indent* columns, or None."""
    prefix = " " * indent
    found: int | None = None
    for i, line in enumerate(lines):
        if not line.startswith(prefix):
            continue
        rest = line[indent:]
        if not rest or rest.startswith((" ", "\t")) or rest.lstrip().startswith("#"):
            continue
        if rest == f"{key}:" or rest.startswith(f"{key}: "):
            found = i
    return found


def _parent_insertion_index(lines: list[str], parent: tuple[str, ...]) -> int | None:
    """Index just past the parent mapping's subtree (added children go there)."""
    depth = len(parent)
    if depth == 0:
        return len(lines)
    idx = _find_key_line(lines, parent[-1], 2 * (depth - 1))
    if idx is None:
        return None
    j = idx + 1
    while j < len(lines):
        line = lines[j]
        if not line.strip() or line.lstrip().startswith("#"):
            j += 1
            continue
        if len(line) - len(line.lstrip()) <= 2 * (depth - 1):
            break
        j += 1
    return j


def _dict_leaf_changes(
    old: dict[str, Any], new: dict[str, Any]
) -> tuple[
    list[tuple[tuple[str, ...], Any]],
    list[tuple[tuple[str, ...], Any, Any]],
    list[tuple[tuple[str, ...], Any]],
]:
    """Diff two nested dicts into added / replaced / removed leaf paths."""
    added: list[tuple[tuple[str, ...], Any]] = []
    replaced: list[tuple[tuple[str, ...], Any, Any]] = []
    removed: list[tuple[tuple[str, ...], Any]] = []

    def walk(o: dict[str, Any], n: dict[str, Any], path: tuple[str, ...]) -> None:
        for key, value in n.items():
            child = path + (key,)
            if key not in o:
                added.append((child, value))
            elif isinstance(o[key], dict) and isinstance(value, dict):
                walk(o[key], value, child)
            elif o[key] != value:
                replaced.append((child, o[key], value))
        for key in o:
            if key not in n:
                removed.append((path + (key,), o[key]))

    walk(old, new, ())
    return added, replaced, removed


def _merge_block_lines(
    lines: list[str], old: dict[str, Any], new: dict[str, Any]
) -> list[str] | None:
    """Rewrite a block's lines for the old→new dict change, or None to re-render.

    Only two shapes are merged in place: a replaced scalar leaf (the value is
    patched on its own line, trailing comment kept) and added keys/subtrees
    (rendered and inserted after the parent's existing subtree). Anything else
    — key removal, block scalars, flow-style subtrees — returns None and the
    caller re-renders the whole block.
    """
    added, replaced, removed = _dict_leaf_changes(old, new)
    if removed:
        return None
    out = list(lines)

    # 1. Patch replaced scalar leaves in place (no line count change, so the
    # indexes computed here stay valid for step 2).
    for path, _old_value, new_value in replaced:
        idx = _find_key_line(out, path[-1], 2 * len(path))
        if idx is None:
            return None
        try:
            rendered = _yaml_scalar(new_value)
        except ValueError:
            return None
        rest = out[idx][2 * len(path) + len(path[-1]) + 1 :]
        value_part, comment = _split_trailing_comment(rest)
        if not value_part or value_part.lstrip()[:1] in ("|", ">"):
            return None  # block scalar (or empty value) is not a single-line leaf
        out[idx] = f"{' ' * (2 * len(path))}{path[-1]}: {rendered}{comment}"

    # 2. Insert added keys/subtrees after their parent's existing subtree,
    # deepest insertion index first so the not-yet-applied indexes stay valid.
    insertions: dict[tuple[str, ...], list[str]] = {}
    for path, value in added:
        parent = path[:-1]
        chunk = _render_fragment(path[-1], value, 2 * len(parent) + 2)
        insertions.setdefault(parent, []).extend(chunk)
    points = []
    for parent, chunk in insertions.items():
        idx = _parent_insertion_index(out, parent)
        if idx is None:
            return None
        points.append((idx, chunk))
    for idx, chunk in sorted(points, key=lambda point: point[0], reverse=True):
        out[idx:idx] = chunk
    return out


def _block_lines_carry_anchors(lines: list[str]) -> str | None:
    """A sample line if the block uses anchors/aliases/merge keys, else None."""
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.match(r"^<<\s*:", stripped) or stripped.startswith("? "):
            return stripped
        quote: str | None = None
        for i, ch in enumerate(stripped):
            if quote is not None:
                if ch == quote:
                    quote = None
            elif ch in "'\"":
                quote = ch
            elif ch == ":" and (i + 1 == len(stripped) or stripped[i + 1] in " \t"):
                if stripped[i + 1 :].lstrip()[:1] in ("&", "*"):
                    return stripped
                break
            elif ch == "#":
                break
    return None


def _segment_config_lines(
    lines: list[str],
) -> tuple[list[str], list[tuple[str, int, int]]]:
    """Split *lines* into (preamble, blocks); each block is (key, start, stop).

    ``stop`` includes the block's trailing blank/comment tail, which stays
    attached to the block and is kept verbatim on every merge path.
    """
    keys: list[tuple[str, int]] = []
    for i, line in enumerate(lines):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t":
            continue
        if line.startswith(("---", "...")):
            raise ConfigMergeError(
                f"line {i + 1}: YAML document markers are not supported by the "
                "comment-preserving merge — remove them, then re-run"
            )
        match = _TOP_LEVEL_KEY_RE.match(line)
        if match is None:
            raise ConfigMergeError(
                f"line {i + 1} is not a top-level key, comment or blank line "
                f"({line!r}) — structure not understood, refusing to merge"
            )
        keys.append((match.group(1), i))
    seen: set[str] = set()
    for key, i in keys:
        if key in seen:
            raise ConfigMergeError(
                f"line {i + 1}: duplicate top-level key {key!r} — refusing to merge"
            )
        seen.add(key)
    preamble_end = keys[0][1] if keys else len(lines)
    blocks = []
    for j, (key, start) in enumerate(keys):
        stop = keys[j + 1][1] if j + 1 < len(keys) else len(lines)
        blocks.append((key, start, stop))
    return lines[:preamble_end], blocks


def _block_content_stop(lines: list[str], start: int, stop: int) -> int:
    """End of a block's KEY CONTENT (trailing blank/comment tail excluded)."""
    end = stop
    while end > start and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
        end -= 1
    return end


def merge_config_text(
    original_text: str,
    merged: dict[str, Any],
    on_full_render: Callable[[str], None] | None = None,
    allow_section_rebuild: bool = False,
) -> str:
    """Apply the merged mapping to the existing file's TEXT, keeping comments.

    Untouched lines (whole unchanged blocks, comments, blank separators) come
    back byte-identical; a changed dict block is merged leaf-by-leaf so the
    rationale comments around the changed values survive; a block whose
    structure cannot be merged in place is re-rendered and reported through
    ``on_full_render`` (values are kept — only that section's comments are
    not). The rebuilt text is always verified to parse back to exactly
    *merged* before it is returned.

    Fail-closed refusals (:class:`ConfigMergeError`; caller must leave the
    file untouched): the text cannot be parsed as a mapping, uses document
    markers or CR line endings, removes a key the file carries, or would not
    round-trip. Removing a key INSIDE a block is also a refusal unless
    ``allow_section_rebuild`` is set — an additive upgrade must never strip a
    user's setting; with the flag (``gitreins init --reset``) the rebuilt
    section is re-rendered through ``on_full_render`` instead, which is the
    semantics the operator explicitly asked for.
    """
    import yaml

    if not isinstance(merged, dict):
        raise ConfigMergeError(f"merged config is a {type(merged).__name__}, not a mapping")
    if not original_text.strip():
        raise ConfigMergeError("existing config is empty — nothing to merge into")
    if "\r" in original_text:
        raise ConfigMergeError("CR line endings are not supported by the comment-preserving merge")
    try:
        original = yaml.safe_load(original_text)
    except yaml.YAMLError as exc:
        raise ConfigMergeError(f"existing config cannot be parsed: {exc}") from exc
    if original is None:
        original = {}
    if not isinstance(original, dict):
        raise ConfigMergeError(
            f"existing config is a {type(original).__name__}, not a mapping — refusing"
        )
    for key in original:
        if key not in merged:
            raise ConfigMergeError(
                f"key {key!r} exists in the file but not in the merged config — "
                "key removal is not a merge, refusing"
            )

    original_lines = original_text.split("\n")
    preamble, blocks = _segment_config_lines(original_lines)

    out: list[str] = list(preamble)
    appended_new_block = False
    for key, start, stop in blocks:
        content_stop = _block_content_stop(original_lines, start, stop)
        tail = original_lines[content_stop:stop]
        old_value = original[key]
        new_value = merged[key]
        if old_value == new_value:
            out.extend(original_lines[start:stop])
            continue
        anchor = _block_lines_carry_anchors(original_lines[start:content_stop])
        if anchor is not None:
            raise ConfigMergeError(
                f"section {key!r} changes value but uses YAML anchors/aliases/merge "
                f"keys ({anchor!r}) — cannot merge textually, refusing"
            )
        new_lines: list[str] | None = None
        if isinstance(old_value, dict) and isinstance(new_value, dict):
            removed = _dict_leaf_changes(old_value, new_value)[2]
            if removed and not allow_section_rebuild:
                dotted = ".".join((key, *removed[0][0]))
                raise ConfigMergeError(
                    f"key {dotted!r} exists in the file but was removed from the "
                    "merged config — an additive upgrade never strips settings, "
                    "refusing"
                )
            new_lines = _merge_block_lines(original_lines[start:content_stop], old_value, new_value)
        if new_lines is None:
            if on_full_render is not None:
                on_full_render(key)
            new_lines = _render_fragment(key, new_value, 0)
        out.extend(new_lines)
        out.extend(tail)
    for key in [k for k in merged if k not in original]:
        out.extend(_render_fragment(key, merged[key], 0))
        appended_new_block = True

    text = "\n".join(out)
    if appended_new_block and not text.endswith("\n"):
        text += "\n"
    try:
        final = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigMergeError(f"merged text does not parse: {exc}") from exc
    if final != merged:
        raise ConfigMergeError(
            "merged text does not round-trip to the merged config — refusing to write"
        )
    return text


# ── Update checker ────────────────────────────────────────────


def check_for_update(workdir: str | None = None, force: bool = False) -> str | None:
    """Check PyPI for a newer version of GitReins.

    Respects update_check_ttl — only re-checks after the TTL expires
    unless force=True.

    Args:
        workdir: Repo root for loading config defaults.
        force: Bypass the TTL cache and check immediately.

    Returns:
        A message string if an update is available, or None if current.
    """
    from engine.version import __version__ as current_version

    defaults = load_defaults(workdir)

    if not defaults.check_for_updates and not force:
        return None

    # Check cache
    os.makedirs(UPDATE_CACHE_DIR, exist_ok=True)
    cache = _read_cache()
    last_checked = cache.get("last_checked", 0)
    ttl_seconds = defaults.update_check_ttl_hours * 3600

    if not force and time.time() - last_checked < ttl_seconds:
        cached_version = cache.get("latest_version", "")
        if cached_version and _version_greater(cached_version, current_version):
            return f"Update available: v{cached_version} → {_pypi_url()}"
        return None

    # Fetch from PyPI
    latest = _fetch_latest_version()
    if not latest:
        _write_cache({"last_checked": time.time(), "latest_version": current_version})
        return None

    _write_cache({"last_checked": time.time(), "latest_version": latest})

    if _version_greater(latest, current_version):
        return f"Update available: {current_version} → {latest} — {_pypi_url()}"

    return None


# ── Internal helpers ──────────────────────────────────────────


def _read_cache() -> dict[str, Any]:
    try:
        with open(UPDATE_CACHE_FILE) as f:
            data: dict[str, Any] = json.load(f)
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _write_cache(data: dict[str, Any]) -> None:
    try:
        os.makedirs(UPDATE_CACHE_DIR, exist_ok=True)
        with open(UPDATE_CACHE_FILE, "w") as f:
            json.dump(data, f)
    except OSError:
        pass


def _fetch_latest_version() -> str | None:
    """Fetch the latest version string from PyPI JSON API."""
    import requests

    try:
        resp = requests.get("https://pypi.org/pypi/gitreins/json", timeout=15)
        resp.raise_for_status()
        version: str = resp.json()["info"]["version"]
        return version
    except Exception:
        logger.debug("Failed to fetch latest version from PyPI", exc_info=True)
        return None


def _version_greater(newer: str, current: str) -> bool:
    """Compare two semantic version strings. True if newer > current."""
    from packaging.version import parse as parse_version

    try:
        return parse_version(newer) > parse_version(current)
    except Exception:
        # Fallback: string comparison
        return newer != current


def _pypi_url() -> str:
    return "https://pypi.org/project/gitreins/"


# ── Type coercion helpers ─────────────────────────────────────


def _coerce_float(val: object) -> float:
    """Coerce a config value to float, handling strings like '100'."""
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        try:
            return float(val.strip())
        except ValueError:
            pass
    return float(val) if isinstance(val, (int, float)) else -1.0


def _coerce_seconds(val: object) -> float:
    """Coerce a time string like '30m' or '2h' to seconds."""
    import re

    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, str):
        val = val.strip()
        m = re.match(r"^(\d+\.?\d*)\s*(s|sec|secs|m|min|mins|h|hr|hrs)$", val.lower())
        if m:
            num = float(m.group(1))
            unit = m.group(2)
            if unit in ("s", "sec", "secs"):
                return num
            elif unit in ("m", "min", "mins"):
                return num * 60
            elif unit in ("h", "hr", "hrs"):
                return num * 3600
        # Try plain number
        try:
            return float(val)
        except ValueError:
            pass
    return -1.0


def _coerce_tokens(val: object) -> int:
    """Coerce a token string like '10M', '200k', or '500' to int."""
    import re

    if isinstance(val, int):
        return val
    if isinstance(val, str):
        val = val.strip()
        m = re.match(r"^(\d+\.?\d*)(k|m|K|M)?$", val)
        if m:
            num = float(m.group(1))
            suffix = (m.group(2) or "").lower()
            if suffix == "k":
                num *= 1_000
            elif suffix == "m":
                num *= 1_000_000
            return int(num)
    return int(val) if isinstance(val, (int, float)) else -1


def _fmt_seconds(secs: float) -> str:
    s = int(secs)
    if s < 60:
        return f"{s}s"
    elif s < 3600:
        return f"{s // 60}m" if s % 60 == 0 else f"{s // 60}m{s % 60}s"
    else:
        h = s // 3600
        m = (s % 3600) // 60
        return f"{h}h" if m == 0 else f"{h}h{m}m"


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    elif n >= 1_000:
        return f"{n // 1_000}k"
    return str(n)
