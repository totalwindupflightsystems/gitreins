"""
Pipeline Engine — Configurable evaluation pipelines.

Pipelines are defined in .gitreins/config.yaml as a list of stages.
Each stage can be sequential or parallel. Results pipe between stages.

Key features:
    - Nested lists = parallel groups (items in a parallel list run concurrently)
    - Flat lists = sequential stages
    - Conditional execution (skip AI if scripts pass)
    - Result piping (failures from Tier 1 feed into Tier 2 AI context)
    - Script stages, AI evaluation stages, output stages

YAML schema:

pipeline:
  stages:
    - id: tier1
      parallel: true
      on: [pre-commit, pre-eval]   # When to run
      steps:
        - id: secrets
          type: script
          # DF-012: gitleaks alone is not trustworthy (its default rules
          # miss sk-/ghp_ patterns) — cross-check with the built-in scanner.
          run: "gitleaks detect --source . --no-git --no-banner && built-in cross-check"
          on_fail: continue          # continue | block | skip_remaining

        - id: lint
          type: script
          run: "ruff check ."

    - id: tier2
      type: ai_eval
      condition: "stage.tier1.any_failed"
      max_iterations: -1  # Defer to evaluator config
      tools: [read_file, run_command, search_pattern, read_diff, sandbox]
      prompt_template: |
        Evaluate task completeness.
        Tier 1 results: {{ stage.tier1 }}
        Criteria: {{ task.criteria }}

    - id: verdict
      type: output
"""

import concurrent.futures
import json
import logging
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

import yaml

from engine import lang_detect
from engine.guard_manager import (
    HARNESS_STATE_DIRS,
    _resolve_test_command,
    harness_state_allowlist_paths,
)
from engine.types import (
    _FAILED_TEST_LINE,
    _first_nonblank_line,
    parse_first_failing_test,
    pytest_outcome,
    strip_ansi,
)

logger = logging.getLogger("gitreins.pipeline")

# DF-GITREINS-POC-8: step evidence budget. Matches MAX_EVIDENCE_CHARS (4000)
# in engine/worktree_fleet.py and engine/worktree_disposable.py so every
# surface that persists command output uses the same bound. The old behavior
# (output[:500] in StepResult.to_dict) kept only the pytest banner and threw
# away the short test summary at the END of the output — the part that names
# the failing test.
MAX_STEP_EVIDENCE_CHARS = 4000

# pytest short-summary ERROR lines ("ERROR tests/test_x.py::test_setup - ...")
# mirror engine.types._FAILED_TEST_LINE for collection/setup errors.
_ERROR_TEST_LINE = re.compile(r"^ERROR \S+::")

# INT-FLAKE-2: script steps whose command is a pytest invocation get their
# outcome classified (engine.types.pytest_outcome) instead of leaving a bare
# exit code in the verdict. Matches `pytest`, `python -m pytest`, `uv run
# pytest`, and a venv console script — the whole `\bpytest\b` word.
_PYTEST_INVOCATION = re.compile(r"\bpytest\b")

# TRUST-001: a step that SKIPS (e.g. the linter is not on PATH) prints this
# marker and exits 0, so the stage can record the gate it never graded instead
# of reading as a graded pass. Format on the line:
#   <marker> <step-id>=<short reason>
SKIP_SENTINEL = "GITREINS_SKIP:"


def parse_skip_sentinels(output: str) -> list[tuple[str, str]]:
    """Extract ``(step_id, reason)`` pairs from a step's output.

    Only lines carrying :data:`SKIP_SENTINEL` are read, so an ordinary lint or
    test output can never be mistaken for a skip.
    """
    found: list[tuple[str, str]] = []
    for line in (output or "").split("\n"):
        idx = line.find(SKIP_SENTINEL)
        if idx == -1:
            continue
        payload = line[idx + len(SKIP_SENTINEL) :].strip()
        step, sep, reason = payload.partition("=")
        step = step.strip()
        if not step:
            continue
        found.append((step, reason.strip() if sep else "reason not recorded"))
    return found


_SECRETS_SCANNERS_LINE = re.compile(r"^secrets:\s*scanners=(?P<ids>[^\n]+)$", re.M)


def parse_secrets_scanners(output: str) -> list[str]:
    """Scanner ids the Tier 1 ``secrets`` step reported running.

    DF-GITREINS-POC-15: the step echoes ``secrets: scanners=...`` (TRUST-003)
    so the console and the run log name the active scanner, but the verdict's
    step ``data`` carried no machine-readable scanner id — a consumer had to
    parse prose to learn whether gitleaks or only the built-in cross-check
    graded the tree. Returns ``["gitleaks", "builtin"]`` in the order the step
    named them, ``[]`` when the line is absent or unparseable (never a guess:
    the fallback is "no attribution recorded", not a default scanner).
    """
    match = _SECRETS_SCANNERS_LINE.search(output or "")
    if not match:
        return []
    raw = match.group("ids").strip()
    # "gitleaks+builtin cross-check" | "builtin cross-check only (gitleaks not on PATH)"
    raw = raw.split(" only")[0]
    ids: list[str] = []
    for token in raw.split("+"):
        token = token.strip()
        if not token:
            continue
        scanner = "gitleaks" if token.startswith("gitleaks") else "builtin"
        if scanner not in ids:
            ids.append(scanner)
    return ids


def _bound_step_evidence(output: str, cap: int = MAX_STEP_EVIDENCE_CHARS) -> str:
    """Bound step evidence to *cap* chars, keeping BOTH ends of the output.

    Output at or under the cap is returned byte-identical. Longer output is
    kept as head (~60% of the budget) + an omission marker + tail (~40%) —
    the tail carries pytest's short test summary, so it is never dropped.
    Any FAILED/ERROR short-summary line inside the omitted middle is hoisted
    into the marker region (deduped, order preserved, max 20 lines) so a
    failing test id survives even when the suite was interrupted mid-run and
    the tail holds no summary.
    """
    if len(output) <= cap:
        return output
    head_len = (cap * 6) // 10
    tail_len = cap - head_len
    head = output[:head_len]
    tail = output[-tail_len:]
    omitted_len = len(output) - head_len - tail_len
    hoisted: list[str] = []
    seen: set[str] = set()
    for line in output[head_len : len(output) - tail_len].split("\n"):
        stripped = line.strip()
        if not (_FAILED_TEST_LINE.match(stripped) or _ERROR_TEST_LINE.match(stripped)):
            continue
        if stripped in seen:
            continue
        seen.add(stripped)
        hoisted.append(stripped)
        if len(hoisted) >= 20:
            break
    marker = f"\n… [{omitted_len} chars omitted] …\n"
    if hoisted:
        marker += "\n".join(hoisted) + "\n…\n"
    return head + marker + tail


@dataclass
class StepResult:
    id: str
    type: str  # "script" | "ai_eval" | "output"
    passed: bool = True
    output: str = ""
    error: str = ""
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "passed": self.passed,
            # DF-GITREINS-POC-8: head+tail bounding instead of a head-only
            # [:500] slice that discarded pytest's short test summary.
            "output": _bound_step_evidence(strip_ansi(self.output)),
            "error": self.error,
            "data": self.data,
        }


@dataclass
class StageResult:
    id: str
    passed: bool = True
    steps: list[StepResult] = field(default_factory=list)
    any_failed: bool = False
    summary: str = ""
    # DF-GITREINS-POC-16: coverage marker. `coverage` names the checks this
    # stage actually graded; `degraded` is True when the stage ran a SUBSET of
    # the gate for the detected language (e.g. secrets-only because nothing
    # was detectable). A degraded stage may still be `passed` — the marker
    # exists so a narrow pass cannot be mistaken for a full one.
    coverage: str = ""
    degraded: bool = False
    skipped_steps: list[str] = field(default_factory=list)
    degradation_reason: str = ""

    def to_dict(self) -> dict:
        d = {
            "id": self.id,
            "passed": self.passed,
            "any_failed": self.any_failed,
            "summary": self.summary,
            "steps": [s.to_dict() for s in self.steps],
        }
        if self.coverage:
            d["coverage"] = self.coverage
        if self.degraded:
            d["degraded"] = True
            d["skipped_steps"] = list(self.skipped_steps)
            d["degradation_reason"] = self.degradation_reason
        return d


def degradation_warning(stage: dict) -> str | None:
    """One-line CLI warning for a stage narrower than the guard gate.

    Returns None for a stage with no degradation marker, so callers can call
    this unconditionally while printing a stage summary.
    """
    if not stage.get("degraded"):
        return None
    skipped = ", ".join(stage.get("skipped_steps") or []) or "unknown checks"
    reason = stage.get("degradation_reason") or "unknown reason"
    return (
        f"WARNING: coverage is {stage.get('coverage') or 'unknown'} — {skipped} did not run "
        f"({reason}); run `gitreins guard` for the full gate"
    )


def _record_runtime_skips(stage: StageResult) -> None:
    """Fold runtime skip sentinels into *stage*'s degradation marker.

    TRUST-001: :func:`tier1_plan` declares the skips it knows statically (an
    undetectable tree). A step can also skip on the machine it actually runs
    on — ``_lint_step_run`` echoes :data:`SKIP_SENTINEL` and exits 0 when the
    linter is not on PATH. Without this pass, that run reads as a graded lint
    and the verdict carries no ``skipped_steps`` for a merge-back to refuse.
    Idempotent: ids are deduplicated and an existing reason is preserved.
    """
    found: list[tuple[str, str]] = []
    for step in stage.steps:
        for step_id, reason in parse_skip_sentinels(f"{step.output}\n{step.error}"):
            if step_id in stage.skipped_steps or step_id in [sid for sid, _ in found]:
                continue
            found.append((step_id, reason))
    if not found:
        return
    stage.degraded = True
    stage.skipped_steps.extend(step_id for step_id, _ in found)
    detail = "skipped at runtime — " + ", ".join(f"{sid}: {reason}" for sid, reason in found)
    stage.degradation_reason = (
        f"{stage.degradation_reason}; {detail}" if stage.degradation_reason else detail
    )


class Pipeline:
    """Execute a pipeline of stages against a task."""

    def __init__(self, config: dict, workdir: str = ".", llm=None):
        self.workdir = os.path.abspath(workdir)
        self.config = config
        self.stages: list[dict] = config.get("pipeline", {}).get("stages", [])
        self._stage_results: dict[str, StageResult] = {}
        self._llm = llm  # Can be injected by Judge

    def run(self, task: dict, trigger: str = "pre-eval") -> dict:
        """Run all stages that match the trigger.

        Args:
            task: Task dict with id, title, criteria, status.
            trigger: "pre-commit" or "pre-eval" — filters which stages run.

        Returns:
            Dict with overall verdict and per-stage results.
        """
        self._stage_results = {}

        for stage_def in self.stages:
            # Check if this stage should run for this trigger
            stage_on = stage_def.get("on", ["pre-eval", "pre-commit"])
            if trigger not in stage_on:
                logger.debug(
                    "Skipping stage %s (trigger mismatch: %s)", stage_def.get("id"), trigger
                )
                continue

            # Check condition
            if not self._check_condition(stage_def.get("condition"), task):
                logger.debug("Skipping stage %s (condition not met)", stage_def.get("id"))
                continue

            stage_id = stage_def.get("id", f"stage_{len(self._stage_results)}")
            logger.info("Running stage: %s", stage_id)

            if stage_def.get("parallel"):
                result = self._run_parallel_stage(stage_id, stage_def, task)
            else:
                result = self._run_sequential_stage(stage_id, stage_def, task)

            # DF-GITREINS-POC-16: carry the stage's coverage marker into the
            # verdict (see StageResult). Declared by the stage definition, so
            # a custom pipeline simply has none.
            result.coverage = stage_def.get("coverage", "")
            result.degraded = bool(stage_def.get("degraded", False))
            result.skipped_steps = list(stage_def.get("skipped_steps") or [])
            result.degradation_reason = stage_def.get("degradation_reason", "")
            # TRUST-001: a step that skipped AT RUNTIME (linter missing on this
            # machine) is a degradation the stage definition cannot declare in
            # advance. Fold it in so verdict.json carries skipped_steps and a
            # judge-gated merge-back can refuse a pass whose gates never ran.
            _record_runtime_skips(result)

            self._stage_results[stage_id] = result

        return self._compile_results()

    def _check_condition(self, condition: str | None, task: dict) -> bool:
        """Evaluate a condition expression.

        Supported:
            - None/empty → always true
            - "stage.X.any_failed" → true if stage X had failures
            - "stage.X.passed" → true if stage X passed
            - "task.has_criteria" → true if task has criteria
            - "task.skip_tier2" → true if task has skip_tier2 flag set
            - "not task.skip_tier2" → true if task does NOT have skip_tier2 flag
            - "true" / "always" → always true
            - "false" → always false
            - Expressions with AND/OR: "stage.tier1.any_failed or task.has_criteria"
        """
        if not condition:
            return True
        if condition in ("true", "always"):
            return True
        if condition == "false":
            return False

        # Parse simple expressions
        condition = condition.strip()

        # Handle OR
        if " or " in condition:
            parts = condition.split(" or ")
            return any(self._check_condition(p.strip(), task) for p in parts)

        # Handle AND
        if " and " in condition:
            parts = condition.split(" and ")
            return all(self._check_condition(p.strip(), task) for p in parts)

        # Handle individual predicates
        if condition == "task.has_criteria":
            return bool(task.get("criteria"))
        if condition == "task.skip_tier2":
            return bool(task.get("skip_tier2", False))
        if condition == "not task.skip_tier2":
            return not bool(task.get("skip_tier2", False))
        if condition.startswith("stage."):
            # stage.tier1.any_failed
            parts = condition.split(".")
            if len(parts) == 3:
                stage_id = parts[1]
                prop = parts[2]
                stage = self._stage_results.get(stage_id)
                if stage:
                    if prop == "any_failed":
                        return stage.any_failed
                    elif prop == "passed":
                        return stage.passed
            return False

        return True

    def _run_parallel_stage(self, stage_id: str, stage_def: dict, task: dict) -> StageResult:
        """Run all steps in parallel."""
        steps = stage_def.get("steps", [])
        result = StageResult(id=stage_id)

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(steps)) as executor:
            futures = {
                executor.submit(self._run_step, step, task, stage_id): step for step in steps
            }
            for future in concurrent.futures.as_completed(futures):
                step_result = future.result()
                result.steps.append(step_result)

        # Check results
        result.any_failed = any(not s.passed for s in result.steps)
        result.passed = not result.any_failed
        result.summary = self._summarize_stage(result)
        return result

    def _run_sequential_stage(self, stage_id: str, stage_def: dict, task: dict) -> StageResult:
        """Run steps sequentially — in order, no concurrency."""
        result = StageResult(id=stage_id)

        steps = stage_def.get("steps", [])
        if steps:
            # Multi-step sequential stage (e.g. tier1 with secrets→lint→tests)
            for step_def in steps:
                step_result = self._run_step(step_def, task, stage_id)
                result.steps.append(step_result)
                if not step_result.passed and step_def.get("on_fail") != "continue":
                    # Stop at first hard failure — later steps won't change the verdict
                    break
            result.any_failed = any(not s.passed for s in result.steps)
            result.passed = not result.any_failed
            result.summary = self._summarize_stage(result)
            return result

        if stage_def.get("type") == "ai_eval":
            step_result = self._run_ai_eval(stage_def, task)
        elif stage_def.get("type") == "commit_audit":
            step_result = self._run_commit_audit(stage_def, task)
        elif stage_def.get("type") == "output":
            step_result = self._run_output(stage_def, task)
        else:
            # Treat as a single script step
            step_result = self._run_script_step(stage_def, task, stage_id)

        result.steps.append(step_result)
        result.passed = step_result.passed
        result.any_failed = not step_result.passed
        result.summary = step_result.output or step_result.error
        return result

    def _run_step(self, step_def: dict, task: dict, stage_id: str | None = None) -> StepResult:
        """Run a single step (used by parallel stages)."""
        step_type = step_def.get("type", "script")
        step_id = step_def.get("id", "unnamed")

        if step_type == "script":
            return self._run_script_step(step_def, task, stage_id)
        elif step_type == "ai_eval":
            return self._run_ai_eval(step_def, task)
        elif step_type == "commit_audit":
            return self._run_commit_audit(step_def, task)
        elif step_type == "output":
            return self._run_output(step_def, task)
        else:
            return StepResult(
                id=step_id, type=step_type, passed=False, error=f"Unknown step type: {step_type}"
            )

    def _guard_log_ref(self, stage_id: str | None) -> str | None:
        """Newest persisted guard-run log, for tier-1 step evidence (DF-018).

        The path derivation lives in ONE place (engine.guard_manager's
        accessor) — the tier-1 stage points a written verdict at the raw,
        untruncated guard output instead of duplicating the path logic.
        Returns None when the stage is not tier1, or when no guard run has
        persisted a log for this workdir yet.
        """
        if stage_id != "tier1":
            return None
        try:
            from engine.guard_manager import newest_guard_log

            return newest_guard_log(self.workdir)
        except Exception:  # evidence plumbing must never fail a pipeline
            return None

    def _run_script_step(
        self, step_def: dict, task: dict, stage_id: str | None = None
    ) -> StepResult:
        """Execute a shell command."""
        step_id = step_def.get("id", "unnamed")
        cmd = step_def.get("run", "")

        if not cmd:
            return StepResult(id=step_id, type="script", passed=False, error="No command specified")

        # Template substitution
        cmd = self._template(cmd, task)

        logger.debug("Running script: %s", cmd)
        try:
            # Strip GIT_* env vars (GIT_INDEX_FILE etc.) leaked by the
            # pre-commit hook — they poison nested git commands in tests
            # (same class as DF-008; guards.py got this in 3cad082).
            sanitized_env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
            result = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=step_def.get("timeout", 120),
                cwd=self.workdir,
                env=sanitized_env,
            )
            # INT-FLAKE-2: keep the WHOLE captured output. The previous
            # head-only [:2000] slice landed exactly where pytest's short test
            # summary begins on a long run, so the verdict evidence had no tail
            # for _bound_step_evidence to preserve (DF-GITREINS-POC-8's
            # head+tail bound cannot recover what was already discarded) and
            # the failing test id vanished from the record. Bounding now
            # happens once, at serialization (StepResult.to_dict).
            output = result.stdout + result.stderr
            # A non-zero exit is a hard failure regardless of on_fail. on_fail
            # only controls whether later steps still run; it must never turn a
            # failed lint/test into a pass (previously `on_fail: continue` and
            # generated `cmd || true` both zeroed the failure). 2026-08-08.
            passed = result.returncode == 0

            data: dict = {"exit_code": result.returncode}
            # INT-FLAKE-2: an exit code cannot say WHY pytest ended — under
            # `-x` + xdist a REAL failing test exits 2 (INTERRUPTED), exactly
            # like a signalled run. Classify from the output so the verdict
            # names the cause instead of the reader's guess.
            if _PYTEST_INVOCATION.search(cmd):
                data["pytest_outcome"] = pytest_outcome(result.returncode, output)
            # DF-018: the tier-1 stage points the written verdict at the raw
            # guard evidence (complete, untruncated run log) when one exists.
            guard_log = self._guard_log_ref(stage_id)
            if guard_log:
                data["guard_log"] = guard_log
            # DF-GITREINS-POC-15: stamp the secrets scanners this run actually
            # used, so a consumer reads the attribution instead of parsing the
            # step output's prose. Absent when the step reported none.
            if step_id == "secrets":
                scanners = parse_secrets_scanners(output)
                if scanners:
                    data["secrets_scanners"] = scanners

            return StepResult(
                id=step_id,
                type="script",
                passed=passed,
                output=output,
                data=data,
            )
        except subprocess.TimeoutExpired:
            return StepResult(id=step_id, type="script", passed=False, error="Command timed out")
        except Exception as e:
            return StepResult(id=step_id, type="script", passed=False, error=str(e))

    def _run_ai_eval(self, step_def: dict, task: dict) -> StepResult:
        """Run the AI evaluator as a pipeline step."""
        step_id = step_def.get("id", "ai_eval")
        model = step_def.get("model")
        max_iterations = step_def.get("max_iterations", -1)

        # Lazy init LLM client
        if self._llm is None:
            from engine.llm import LLMClient

            if model:
                self._llm = LLMClient(model=model)
            else:
                self._llm = LLMClient()

        from engine.evaluator import AgenticEvaluator
        from engine.eval_cap import (
            EvalCap,
            _parse_time,
            _parse_tokens,
            eval_cap_from_config,
        )

        # Cap resolution: config.yaml evaluator: section is the base;
        # caps EXPLICITLY set in the step config override it. A step that
        # sets nothing (or only max_iterations: -1 = "defer to evaluator
        # config", the default) passes eval_cap=None so AgenticEvaluator
        # reads the config itself — the documented working path (helix
        # tick 60: -1 → full caps from config, zero compaction cycles).
        #
        # Do NOT build an explicit EvalCap with -1 defaults for token
        # caps: compaction threshold int(-1*0.9)=0 → the evaluator
        # compacts on every turn and never produces a verdict (fleet-wide
        # tier2 INCOMPLETE 'Context near limit (N/-1 tokens)', 2026-08).
        explicit_caps: dict[str, float | int] = {}
        if max_iterations not in (None, -1):
            explicit_caps["max_iterations"] = max_iterations
        max_time = step_def.get("max_time")
        if max_time:
            max_seconds = _parse_time(str(max_time))
            if max_seconds is not None:
                explicit_caps["max_seconds"] = float(max_seconds)
        for key in ("max_input_tokens", "max_output_tokens"):
            raw = step_def.get(key)
            if raw in (None, -1):
                continue
            if isinstance(raw, str):
                parsed = _parse_tokens(raw)
                if parsed is None:
                    logger.warning("Unparseable %s=%r in step %s — ignoring", key, raw, step_id)
                    continue
                explicit_caps[key] = parsed
            else:
                explicit_caps[key] = int(raw)
        if step_def.get("tool_call_weight") is not None:
            explicit_caps["tool_call_weight"] = float(step_def["tool_call_weight"])

        if explicit_caps:
            # Merge step overrides over the config base so unset caps
            # never fall back to unlimited.
            base = eval_cap_from_config(self.config)
            eval_cap = EvalCap(
                max_iterations=float(explicit_caps.get("max_iterations", base.max_iterations)),
                max_seconds=float(explicit_caps.get("max_seconds", base.max_seconds)),
                max_input_tokens=int(explicit_caps.get("max_input_tokens", base.max_input_tokens)),
                max_output_tokens=int(
                    explicit_caps.get("max_output_tokens", base.max_output_tokens)
                ),
                tool_call_weight=float(
                    explicit_caps.get("tool_call_weight", base.tool_call_weight)
                ),
            )
            evaluator = AgenticEvaluator(self._llm, self.workdir, eval_cap=eval_cap)
        else:
            # Nothing set in the step — defer to .gitreins/config.yaml
            evaluator = AgenticEvaluator(self._llm, self.workdir)

        # Build prompt with template substitution — the custom prompt_template
        # (if any) is passed to the evaluator as its system-prompt override so
        # it actually becomes the evaluation prompt. Previously this branch set
        # _pipeline_context then did nothing with the template (2026-08-08).
        prompt_template = step_def.get("prompt_template", "")
        if prompt_template:
            pipeline_context = self._get_pipeline_context()
            import json as _json

            ctx_str = _json.dumps(pipeline_context.get("stages", {}), default=str)[:4000]
            rendered = prompt_template.replace(
                "{{ pipeline_context }}",
                ctx_str,
            )
            task["_system_prompt_override"] = rendered
            task["_pipeline_context"] = pipeline_context

        try:
            verdict = evaluator.evaluate(task)
            passed = verdict.verdict == "COMPLETE"

            items_output = "\n".join(
                f"  {'✓' if i.status == 'PASS' else '✗'} {i.criterion}: {i.detail}"
                for i in verdict.items
            )

            # Persist the judge's real token usage so external tools (e.g. the
            # coding-hermes scheduler dashboard) can sum GitReins judge cost
            # alongside foreman/worker cost. GitReins uses its own LLM client,
            # so its usage never appears in Hermes' state.db telemetry; without
            # this, judge cost was invisible. Append to .gitreins/usage.jsonl,
            # timestamped, one JSON line per judge run. Best-effort — never
            # blocks or fails the eval on a write error. (2026-08-08)
            try:
                import json as _uj
                import os as _uos
                import time as _time

                _cap = getattr(evaluator, "eval_cap", None)
                if _cap is not None:
                    usage_line = {
                        "ts": _time.time(),
                        "tokens_in": getattr(_cap, "cumulative_input_tokens", 0),
                        "tokens_out": getattr(_cap, "cumulative_output_tokens", 0),
                        "cache_read": getattr(_cap, "cumulative_cache_read", 0),
                        "cache_write": getattr(_cap, "cumulative_cache_write", 0),
                        "step": step_id,
                    }
                    usage_path = _uos.path.join(self.workdir, ".gitreins", "usage.jsonl")
                    _uos.makedirs(_uos.path.dirname(usage_path), exist_ok=True)
                    with open(usage_path, "a") as _f:
                        _f.write(_uj.dumps(usage_line) + "\n")
            except Exception:
                pass  # non-fatal

            return StepResult(
                id=step_id,
                type="ai_eval",
                passed=passed,
                output=f"{verdict.verdict}\n{items_output}\n{verdict.summary}",
                data={
                    "verdict": verdict.verdict,
                    "items": [
                        {"criterion": i.criterion, "status": i.status, "detail": i.detail}
                        for i in verdict.items
                    ],
                    "summary": verdict.summary,
                },
            )
        except Exception as e:
            logger.exception("AI eval failed")
            return StepResult(id=step_id, type="ai_eval", passed=False, error=str(e))

    def _run_commit_audit(self, step_def: dict, task: dict) -> StepResult:
        """Run the commit message auditor as a pipeline step.

        Reads the commit message from ``task["commit_message"]`` and the
        staged diff from git.  Uses the CommitAuditor to validate the
        message against the diff, with optional LLM exploration
        (configured via ``max_iterations`` in the step or config).

        Config keys (from .gitreins/config.yaml):
          ``commit_audit.mode`` — "warn" (default) | "block" | "suggest"
          ``commit_audit.strictness`` — "lenient" | "standard" (default) | "strict"
          ``commit_audit.max_iterations`` — int, default 3
          ``commit_audit.suggest_message`` — bool, default True
          ``commit_audit.review_score_threshold`` — float, default 8.0 (GR-066)
          ``commit_audit.review_score_offset`` — float, default 1.0 (GR-066)
        """
        step_id = step_def.get("id", "commit_audit")

        # Lazy init LLM client
        if self._llm is None:
            from engine.llm import LLMClient

            self._llm = LLMClient()

        from engine.commit_audit import CommitAuditor

        # Read config for commit_audit settings
        config = self._load_commit_audit_config()

        score_threshold = float(config.get("review_score_threshold", 8.0))
        score_offset = float(config.get("review_score_offset", 1.0))

        auditor = CommitAuditor(
            self._llm,
            self.workdir,
            strictness=config.get("strictness", "standard"),
            max_iterations=config.get("max_iterations", 3),
            suggest_message=config.get("suggest_message", True),
            review_mode=config.get("review_mode", "message"),
            review_checks=config.get("review_checks", None),
            review_severity=config.get("review_severity", "standard"),
            review_suggest_fix=config.get("review_suggest_fix", True),
            review_score_threshold=score_threshold,
            review_score_offset=score_offset,
        )

        message = task.get("commit_message", "")
        if not message:
            # Try reading from git commit message file
            msg_path = os.path.join(self.workdir, ".git", "COMMIT_EDITMSG")
            if os.path.exists(msg_path):
                try:
                    with open(msg_path, "r") as f:
                        raw = f.read().strip()
                    # Strip comment lines
                    message = "\n".join(
                        line for line in raw.split("\n") if not line.startswith("#")
                    ).strip()
                except Exception:
                    pass

        if not message:
            return StepResult(
                id=step_id,
                type="commit_audit",
                passed=True,
                output="No commit message to audit.",
            )

        try:
            result = auditor.audit(message)
        except Exception as e:
            logger.warning("Commit audit failed: %s", e)
            return StepResult(
                id=step_id,
                type="commit_audit",
                passed=True,
                output=f"Audit error (passing): {e}",
            )

        mode = config.get("mode", "warn")
        passed = result.valid or mode != "block"

        output_lines: list[str] = []
        # ── CVE-style scoring (GR-066) ──
        all_review_issues = getattr(result, "review_issues", [])
        if all_review_issues:
            # Determine highest effective score
            max_effective = 0.0
            for ri in all_review_issues:
                raw_score = ri.get("score", 0.0)
                effective = raw_score * score_offset
                ri["effective_score"] = effective
                if effective > max_effective:
                    max_effective = effective

            sev_marker = {
                "critical": "🔴 CRITICAL",
                "high": "🟠 HIGH",
                "medium": "🟡 MEDIUM",
                "low": "🟢 LOW",
                "info": "ℹ️ INFO",
            }
            output_lines.append(
                f"⚠ Commit review — {len(all_review_issues)} issue(s) found (overall: {max_effective:.1f}/{score_threshold:.1f})"
            )
            review_summary = getattr(result, "review_summary", "")
            if review_summary:
                output_lines.append(f"   {review_summary}")
            output_lines.append("")

            blocked = False
            warn_issues = False
            for ri in all_review_issues:
                sev = sev_marker.get(ri.get("severity", "info"), "ℹ️ INFO")
                cat = ri.get("category", "unknown")
                file_ref = f"{ri.get('file', '')}:{ri.get('line', 0)}"
                title = ri.get("title", "")
                desc = ri.get("description", "")
                sugg = ri.get("suggestion", "")
                effective = ri.get("effective_score", 0.0)

                # Score-based action marker (GR-066)
                if effective >= score_threshold:
                    action_mark = "🚫 BLOCK"
                    blocked = True
                elif effective >= score_threshold * 0.75:
                    action_mark = "⚠️ WARN"
                    warn_issues = True
                else:
                    action_mark = "ℹ️ INFO"

                output_lines.append(
                    f"  {file_ref} [{cat}] [{sev}] {action_mark} (score: {effective:.1f}) — {title}"
                )
                if desc:
                    output_lines.append(f"    {desc}")
                if sugg:
                    output_lines.append(f"    Fix: {sugg}")
                output_lines.append("")

            # Apply scoring to pass/fail
            if blocked and mode == "block":
                passed = False
            elif blocked and mode == "warn":
                output_lines.append("(Warning: issues above threshold — review recommended)")
            elif warn_issues:
                output_lines.append("(Warning: issues in warning range — review recommended)")

        # ── Message audit ──
        if result.valid:
            output_lines.append("✓ Commit message looks good.")
        else:
            output_lines.append("⚠ Commit message issues:")
            for issue in result.issues:
                output_lines.append(f"  - {issue}")
            if result.suggested_message:
                output_lines.append(f"\nSuggested message: {result.suggested_message}")
            if mode == "block":
                output_lines.append(
                    "\n(Commit BLOCKED — fix message or set commit_audit.mode=warn)"
                )
            elif mode == "warn":
                output_lines.append("\n(Warning only — commit will proceed)")

        return StepResult(
            id=step_id,
            type="commit_audit",
            passed=passed,
            output="\n".join(output_lines),
            data={
                "valid": result.valid,
                "issues": result.issues,
                "suggested_message": result.suggested_message,
                "mode": mode,
                "iterations_used": result.iterations_used,
                "review_issues": getattr(result, "review_issues", []),
                "review_summary": getattr(result, "review_summary", ""),
            },
        )

    def _load_commit_audit_config(self) -> dict:
        """Read commit_audit section from .gitreins/config.yaml."""
        import yaml

        config_path = os.path.join(self.workdir, ".gitreins", "config.yaml")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r") as f:
                    cfg = yaml.safe_load(f) or {}
                return cfg.get("commit_audit", {})
            except Exception:
                pass
        return {}

    def _run_output(self, step_def: dict, task: dict) -> StepResult:
        """Compile output from all stages."""
        step_id = step_def.get("id", "output")
        fmt = step_def.get("format", "{{ stages }}")

        output = self._template(fmt, task)
        return StepResult(id=step_id, type="output", passed=True, output=output)

    def _template(self, text: str, task: dict) -> str:
        """Simple template substitution with {{ var }} syntax.

        Available vars:
            {{ task.id }}, {{ task.title }}, {{ task.criteria }}
            {{ stage.<id>.passed }}, {{ stage.<id>.any_failed }}
            {{ stage.<id>.summary }}
            {{ stages }} — full stage results as JSON
        """
        # Task vars
        text = text.replace("{{ task.id }}", str(task.get("id", "")))
        text = text.replace("{{ task.title }}", str(task.get("title", "")))
        text = text.replace("{{ task.criteria }}", json.dumps(task.get("criteria", []), indent=2))

        # Stage vars
        for stage_id, stage in self._stage_results.items():
            prefix = f"{{{{ stage.{stage_id}"
            text = text.replace(f"{prefix}.passed }}}}", str(stage.passed))
            text = text.replace(f"{prefix}.any_failed }}}}", str(stage.any_failed))
            text = text.replace(f"{prefix}.summary }}}}", str(stage.summary))
            text = text.replace(f"{prefix} }}}}", json.dumps(stage.to_dict(), indent=2))

        # All stages
        stages_json = json.dumps(
            {sid: s.to_dict() for sid, s in self._stage_results.items()},
            indent=2,
        )
        text = text.replace("{{ stages }}", stages_json)

        return text

    def _get_pipeline_context(self) -> dict:
        """Get context from previous stages to inject into AI evaluation."""
        return {
            "stages": {sid: s.to_dict() for sid, s in self._stage_results.items()},
        }

    def _summarize_stage(self, stage: StageResult) -> str:
        """Create a summary string for a stage — ONE line per step.

        DF-GITREINS-POC-14: this used to render ``output[:100]`` verbatim, so
        a step whose capture carried a newline printed as several lines and a
        step with raw escape codes (gitleaks' ``\\x1b[32mINF`` log lines)
        printed them; a step that produced NO output printed a dangling
        ``✓ lint: ``. Now every detail is ANSI-stripped, single-line, and
        named when empty.
        """
        lines = []
        for step in stage.steps:
            status = "✓" if step.passed else "✗"
            source = step.output or step.error
            detail = _first_nonblank_line(source)
            if not step.passed and step.output:
                # A failing step's first 100 chars are the pytest banner;
                # surface the first FAILED/ERROR short-summary line instead
                # so the failing test id is visible (DF-021 kin /
                # DF-GITREINS-POC-8). TRUST-003 (AC1): the parsed id is named
                # with the same '[first failing id]' marker the guard console
                # uses. Fall back to the sanitized head when the output
                # carries no recognizable failure line.
                clean = strip_ansi(step.output)
                first_id = parse_first_failing_test(clean)
                if first_id:
                    detail = f"FAIL ({first_id} [first failing id])"[:100]
                else:
                    for line in clean.split("\n"):
                        stripped = line.strip()
                        if _FAILED_TEST_LINE.match(stripped) or _ERROR_TEST_LINE.match(stripped):
                            detail = stripped[:100]
                            break
            if not detail:
                # Never print a dangling colon: name the empty case instead.
                detail = "ok (no output)" if step.passed else "no output"
            lines.append(f"  {status} {step.id}: {detail}")
        return "\n".join(lines)

    def _compile_results(self) -> dict:
        """Compile final results from all stages."""
        all_passed = all(s.passed for s in self._stage_results.values())
        return {
            "passed": all_passed,
            "stages": {sid: s.to_dict() for sid, s in self._stage_results.items()},
        }


def _normalize_yaml_bool_keys(obj):
    """Recursively convert boolean keys to their string equivalents.

    PyYAML 1.1 parses unquoted ``on``, ``off``, ``yes``, ``no``, ``true``,
    ``false`` as Python bools.  When those appear as mapping keys they break
    lookups: ``stage_def.get("on")`` returns None because the real key is
    ``True``.  This walker converts them back to the lowercase string form.
    """
    if isinstance(obj, dict):
        fixed = {}
        for k, v in obj.items():
            if isinstance(k, bool):
                k = str(k).lower()  # True→"true", False→"false"
            fixed[k] = _normalize_yaml_bool_keys(v)
        return fixed
    if isinstance(obj, list):
        return [_normalize_yaml_bool_keys(i) for i in obj]
    return obj


# Mapping of Python bool → YAML 1.1 boolean keyword that would have
# produced it when used as a plain scalar key.
_YAML_BOOL_KEY_MAP: dict[bool, str] = {
    True: "on",
    False: "off",
}


def _fix_on_key(obj):
    """Post-processor specifically for the ``on`` / ``off`` key pitfall.

    YAML 1.1 interprets ``on: [...]`` as ``True: [...]``.  This second pass
    converts bool-to-string using the most-common-intent mapping
    (True→"on", False→"off") rather than the generic True→"true".
    """
    if isinstance(obj, dict):
        fixed = {}
        for k, v in obj.items():
            if isinstance(k, bool):
                k = _YAML_BOOL_KEY_MAP.get(k, str(k).lower())
            fixed[k] = _fix_on_key(v)
        return fixed
    if isinstance(obj, list):
        return [_fix_on_key(i) for i in obj]
    return obj


def _lint_step_run(lint_cmd: str) -> str:
    """Wrap *lint_cmd* with the guard's missing-linter semantics.

    ``GuardManager._check_lint`` returns PASS ("No linter found — skipped")
    when the linter binary is absent. The judge's lint step must behave the
    same way, or Tier 1 would FAIL on a machine that merely lacks ruff while
    ``gitreins guard`` passes on the identical tree (DF-GITREINS-POC-16).
    The binary is only checked for EXISTENCE; a lint finding still fails.
    """
    binary = lint_cmd.split()[0]
    return (
        f"if command -v {binary} >/dev/null 2>&1; then {lint_cmd}; "
        f'else echo "{SKIP_SENTINEL} lint=no linter on PATH ({binary} not found)"; '
        f"exit 0; fi"
    )


def harness_scan_gitleaks_config(workdir: str) -> str:
    """Render the gitleaks config the Tier 1 ``secrets`` step runs with.

    POC-17 / TRUST-002: the judge's secrets step used to invoke
    ``gitleaks detect --no-git`` bare. That mode walks the working tree and
    does NOT honour ``.gitignore``, so gitignored harness state
    (``.gitreins/logs/guard-*.log`` from DF-018 persistence) and tracked
    verdict artifacts (``.gitreins/history/**``) were graded as if they were
    repo code — a QA anti-tamper canary there failed Tier 1 while every
    source file was clean, which cost a full diagnosis cycle (tick 285).

    The rendered config extends the repo's own ``.gitleaks.toml`` when one
    exists, so its custom rules and path allowlists still apply. gitleaks
    rejects ``extend.path`` together with ``extend.useDefault``, hence the
    either/or: a repo config carries the default ruleset itself
    (``useDefault = true``); without one we set it directly.
    """
    repo_config = os.path.join(workdir, ".gitleaks.toml")
    lines = [
        "# Generated by GitReins for the Tier 1 secrets step (TRUST-002).",
        "# Not written into the repo: the step pipes it through a temp file.",
        "[extend]",
    ]
    if os.path.isfile(repo_config):
        # TOML literal string — Windows paths must not be escape-processed.
        lines.append(f"path = '{repo_config}'")
    else:
        lines.append("useDefault = true")
    lines.extend(
        [
            "",
            "[allowlist]",
            'description = "GitReins harness state — never graded"',
            "paths = [",
        ]
    )
    lines.extend(f"  '''{path_re}'''," for path_re in harness_state_allowlist_paths())
    lines.append("]")
    return "\n".join(lines)


def _secrets_step_run(workdir: str) -> str:
    """Shell command for the Tier 1 ``secrets`` step.

    DF-012: gitleaks' default rules (and the generated config) miss sk-/ghp_
    patterns, so "gitleaks clean" is not proof of clean. The built-in
    scanner (workdir mode — the judged changes are committed, not staged)
    ALWAYS runs, and the step fails if EITHER scanner finds anything.

    TRUST-002 / POC-17: gitleaks is handed the generated harness-state
    exclusion config, and the exclusion is echoed into the step output so a
    FAIL diagnosis reads it instead of re-chasing the canary. Fail-closed:
    if mktemp fails the missing config makes gitleaks exit non-zero rather
    than silently scanning with no scope.

    gitleaks absent → that half is skipped and the built-in cross-check
    still grades the tree. The built-in scanner runs under the interpreter
    that is executing gitreins (sys.executable), with PYTHONPATH pointing at
    the engine package root — a bare `python3` from PATH cannot import
    `engine`, which made this step fail with ModuleNotFoundError in any env
    where the package is only importable by the venv (2026-08-15 fix).
    """
    exclusions = ", ".join(f"{d}/**" for d in HARNESS_STATE_DIRS)
    return (
        "if command -v gitleaks >/dev/null 2>&1; then "
        '_glcfg="$(mktemp -t gitreins-gitleaks-XXXXXX.toml)"; '
        "cat > \"$_glcfg\" <<'GITREINS_GITLEAKS_CFG'\n"
        f"{harness_scan_gitleaks_config(workdir)}\n"
        "GITREINS_GITLEAKS_CFG\n"
        f'echo "secrets: harness state excluded from gitleaks scope ({exclusions})"; '
        # TRUST-003: name the scanners and each one's outcome in the step
        # evidence, so a judge FAIL says WHICH scanner raised it instead of
        # leaving "secrets" ambiguous (the ambiguity that cost POC-15 a cycle).
        'echo "secrets: scanners=gitleaks+builtin cross-check"; '
        # DF-GITREINS-POC-14: --no-color keeps gitleaks' logrus colour codes
        # out of the captured evidence — verdict.json recorded raw
        # `\x1b[32mINF\x1b[0m scanned ~5 MB` lines and the console summary
        # printed them when one landed first. The INFO lines themselves stay:
        # they are the scope evidence ("scanned ~5 MB") a post-mortem reads.
        'gitleaks detect --source . --no-git --no-banner --no-color --config "$_glcfg"; '
        '_glrc=$?; rm -f "$_glcfg"; '
        'if [ "$_glrc" -eq 0 ]; then echo "secrets: gitleaks: clean"; '
        'else echo "secrets: gitleaks: findings found (exit $_glrc)"; fi; '
        "else _glrc=0; "
        'echo "secrets: scanners=builtin cross-check only (gitleaks not on PATH)"; '
        'echo "secrets: gitleaks: not on PATH"; fi; g1=$_glrc; '
        f'PYTHONPATH="{_engine_root()}" {sys.executable} -c "from engine.guard_manager import GuardManager; '
        "import sys; gm = GuardManager('.'); "
        "r = gm._builtin_secrets_scan(staged_only=False); "
        "print('secrets: builtin cross-check: ' + r.output); "
        "print('secrets: builtin cross-check status: ' "
        "+ (r.scanners[0][1] if r.scanners else 'clean')); "
        'sys.exit(1 if not r.passed else 0)"; '
        'g2=$?; [ "$g1" -eq 0 ] && [ "$g2" -eq 0 ]'
    )


def tier1_plan(workdir: str, config: dict | None = None) -> tuple[list[dict], dict]:
    """Build the default Tier 1 steps plus their coverage marker.

    DF-GITREINS-POC-16: Tier 1 must grade the SAME set the guard would grade
    on this tree — secrets, lint (unless ``guards.lint: false``), tests — with
    the test command coming from ``guards.test_command`` (or the detected
    language's default) resolved through the guard's own
    ``_resolve_test_command``. Language detection is delegated to
    ``engine.lang_detect``, the single source of truth shared with the guard
    and ``init``; the signature-file table alone used to leave marker-less
    repos (plain ``.py`` trees) with a secrets-only Tier 1 that a green judge
    verdict then misreported as a full pass.

    Returns ``(steps, marker)`` where *marker* is always a dict:
    ``coverage`` (which checks ran, ``+``-joined step ids, or
    ``"secrets-only"``), ``degraded`` (bool), ``skipped_steps`` (list) and
    ``reason``. A degraded marker never flips the stage's verdict — it makes
    a narrower run honest.
    """
    guards_cfg = (config or {}).get("guards", {})
    configured_test_cmd = guards_cfg.get("test_command")
    test_timeout = int(guards_cfg.get("test_timeout", 120))
    steps: list[dict] = [
        {
            "id": "secrets",
            "type": "script",
            "run": _secrets_step_run(workdir),
            "on_fail": "continue",
        },
    ]

    language = lang_detect.detect_language(workdir)
    commands = lang_detect.lint_test_commands(language)
    if commands is None:
        # LOUD degradation (workstream c): say exactly what did not run and
        # why. Callers surface this in the CLI and in verdict.json.
        reason = (
            f"no language detected in {workdir}"
            if language is None
            else f"no lint/test commands declared for language '{language}'"
        )
        return steps, {
            "coverage": "secrets-only",
            "degraded": True,
            "skipped_steps": ["lint", "tests"],
            "reason": reason,
        }

    lint_cmd, test_cmd = commands
    # Honor guards.lint: false — the guard mode already skips lint when
    # disabled; the judge tier1 pipeline must match, otherwise repos with
    # no lint setup (e.g. no eslint dep / no eslint.config) get an
    # env-dependent lint FP from `npx eslint .` (npx fetches eslint from
    # cache/registry, so PATH hygiene cannot suppress it). Ring-runner
    # RR-GAP-040 / off-by-one answer 1286.
    if guards_cfg.get("lint", True):
        steps.append({"id": "lint", "type": "script", "run": _lint_step_run(lint_cmd)})

    # Same command the guard would run, resolved by the guard's own helper so
    # a missing runner prefix (`uv run` on a pip-only machine) degrades the
    # same way in both engines.
    resolved_test_cmd, resolution_warning = _resolve_test_command(configured_test_cmd or test_cmd)
    test_step: dict = {"id": "tests", "type": "script", "run": resolved_test_cmd}
    if resolution_warning:
        test_step["resolution_warning"] = resolution_warning
    if test_timeout > 0:
        test_step["timeout"] = test_timeout
    steps.append(test_step)

    return steps, {
        "coverage": "+".join(s["id"] for s in steps),
        "degraded": False,
        "skipped_steps": [],
        "reason": "",
    }


def _default_tier1_steps(workdir: str, config: dict | None = None) -> list[dict]:
    """Return language-appropriate default Tier 1 pipeline steps.

    Thin wrapper over :func:`tier1_plan` (kept for the existing callers and
    tests); see that function for the parity contract.
    """
    steps, _marker = tier1_plan(workdir, config)
    return steps


def _engine_root() -> str:
    """Absolute path of the directory containing the `engine` package.

    Works in both source checkouts (…/gitreins/engine/pipeline.py) and
    installed layouts (…/site-packages/engine/pipeline.py) — the parent of
    the engine dir is the import root that must go on PYTHONPATH for the
    default-pipeline built-in scanner subprocess.
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_pipeline_config(workdir: str = ".") -> dict:
    """Load pipeline configuration from .gitreins/config.yaml."""
    config_path = os.path.join(workdir, ".gitreins", "config.yaml")
    if not os.path.exists(config_path):
        # No config file at all — the default pipeline (marker included, so a
        # degraded tier1 stays honest on a repo that was never `init`-ed).
        _missing_cfg_steps, missing_cfg_marker = tier1_plan(workdir, None)
        return {
            "pipeline": {
                "stages": [
                    {
                        "id": "tier1",
                        "parallel": True,
                        "on": ["pre-commit", "pre-eval"],
                        "steps": _missing_cfg_steps,
                        "coverage": missing_cfg_marker["coverage"],
                        "degraded": missing_cfg_marker["degraded"],
                        "skipped_steps": missing_cfg_marker["skipped_steps"],
                        "degradation_reason": missing_cfg_marker["reason"],
                    },
                    {
                        "id": "tier2",
                        "type": "ai_eval",
                        "on": ["pre-eval"],
                        "condition": "true",
                        "max_iterations": -1,
                        "tools": [
                            "read_file",
                            "run_command",
                            "search_pattern",
                            "read_diff",
                            "sandbox",
                        ],
                    },
                ]
            }
        }

    try:
        with open(config_path, "r") as f:
            config = yaml.safe_load(f) or {}
        # Fix YAML 1.1 boolean-key pitfall: unquoted ``on:`` / ``off:``
        # are parsed as ``True:`` / ``False:`` and break key lookups.
        config = _fix_on_key(config)
        if "pipeline" not in config:
            tier1_steps, tier1_marker = tier1_plan(workdir, config)
            config["pipeline"] = {
                "stages": [
                    {
                        "id": "tier1",
                        "parallel": True,
                        "on": ["pre-commit", "pre-eval"],
                        "steps": tier1_steps,
                        # DF-GITREINS-POC-16: what this tier1 actually graded
                        # ("secrets+lint+tests", "secrets-only", …) so a run
                        # narrower than the guard gate is machine-readable.
                        "coverage": tier1_marker["coverage"],
                        "degraded": tier1_marker["degraded"],
                        "skipped_steps": tier1_marker["skipped_steps"],
                        "degradation_reason": tier1_marker["reason"],
                    },
                    {
                        "id": "tier2",
                        "type": "ai_eval",
                        "on": ["pre-eval"],
                        "condition": "true",
                        "max_iterations": -1,
                        "tools": [
                            "read_file",
                            "run_command",
                            "search_pattern",
                            "read_diff",
                            "sandbox",
                        ],
                    },
                ]
            }
        return config
    except Exception as e:
        logger.warning("Failed to load pipeline config: %s", e)
        return {"pipeline": {"stages": []}}
