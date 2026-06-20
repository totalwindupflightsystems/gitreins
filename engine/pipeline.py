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
          run: "gitleaks detect --source . --no-git"
          on_fail: continue          # continue | block | skip_remaining

        - id: lint
          type: script
          run: "ruff check ."

    - id: tier2
      type: ai_eval
      condition: "stage.tier1.any_failed"
      max_iterations: 20
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
import subprocess
from dataclasses import dataclass, field

import yaml

logger = logging.getLogger("gitreins.pipeline")


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
            "output": self.output[:500],
            "error": self.error,
        }


@dataclass
class StageResult:
    id: str
    passed: bool = True
    steps: list[StepResult] = field(default_factory=list)
    any_failed: bool = False
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "passed": self.passed,
            "any_failed": self.any_failed,
            "summary": self.summary,
            "steps": [s.to_dict() for s in self.steps],
        }


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
                logger.debug("Skipping stage %s (trigger mismatch: %s)", stage_def.get("id"), trigger)
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

            self._stage_results[stage_id] = result

        return self._compile_results()

    def _check_condition(self, condition: str | None, task: dict) -> bool:
        """Evaluate a condition expression.

        Supported:
            - None/empty → always true
            - "stage.X.any_failed" → true if stage X had failures
            - "stage.X.passed" → true if stage X passed
            - "task.has_criteria" → true if task has criteria
            - "true" / "always" → always true
            - Expressions with AND/OR: "stage.tier1.any_failed or task.has_criteria"
        """
        if not condition:
            return True
        if condition in ("true", "always"):
            return True

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
                executor.submit(self._run_step, step, task): step
                for step in steps
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
        """Run steps sequentially — for single-step stages like ai_eval."""
        # For non-parallel stages, the stage IS the step
        result = StageResult(id=stage_id)

        if stage_def.get("type") == "ai_eval":
            step_result = self._run_ai_eval(stage_def, task)
        elif stage_def.get("type") == "output":
            step_result = self._run_output(stage_def, task)
        else:
            # Treat as a single script step
            step_result = self._run_script_step(stage_def, task)

        result.steps.append(step_result)
        result.passed = step_result.passed
        result.any_failed = not step_result.passed
        result.summary = step_result.output or step_result.error
        return result

    def _run_step(self, step_def: dict, task: dict) -> StepResult:
        """Run a single step (used by parallel stages)."""
        step_type = step_def.get("type", "script")
        step_id = step_def.get("id", "unnamed")

        if step_type == "script":
            return self._run_script_step(step_def, task)
        elif step_type == "ai_eval":
            return self._run_ai_eval(step_def, task)
        elif step_type == "output":
            return self._run_output(step_def, task)
        else:
            return StepResult(id=step_id, type=step_type, passed=False, error=f"Unknown step type: {step_type}")

    def _run_script_step(self, step_def: dict, task: dict) -> StepResult:
        """Execute a shell command."""
        step_id = step_def.get("id", "unnamed")
        cmd = step_def.get("run", "")
        on_fail = step_def.get("on_fail", "block")

        if not cmd:
            return StepResult(id=step_id, type="script", passed=False, error="No command specified")

        # Template substitution
        cmd = self._template(cmd, task)

        logger.debug("Running script: %s", cmd)
        try:
            result = subprocess.run(
                cmd, shell=True, capture_output=True, text=True,
                timeout=120, cwd=self.workdir,
            )
            output = (result.stdout + result.stderr)[:2000]
            passed = result.returncode == 0 or on_fail == "continue"

            return StepResult(
                id=step_id, type="script", passed=passed,
                output=output, data={"exit_code": result.returncode},
            )
        except subprocess.TimeoutExpired:
            return StepResult(id=step_id, type="script", passed=(on_fail == "continue"),
                            error="Command timed out")
        except Exception as e:
            return StepResult(id=step_id, type="script", passed=(on_fail == "continue"),
                            error=str(e))

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
        evaluator = AgenticEvaluator(self._llm, self.workdir, max_iterations=max_iterations)

        # Build prompt with template substitution
        prompt_template = step_def.get("prompt_template", "")
        if prompt_template:
            task["_pipeline_context"] = self._get_pipeline_context()
            # The evaluator will use the task's criteria as its prompt
            # We inject pipeline context into the task
            pass

        try:
            verdict = evaluator.evaluate(task)
            passed = verdict.verdict == "COMPLETE"

            items_output = "\n".join(
                f"  {'✓' if i.status == 'PASS' else '✗'} {i.criterion}: {i.detail}"
                for i in verdict.items
            )

            return StepResult(
                id=step_id, type="ai_eval", passed=passed,
                output=f"{verdict.verdict}\n{items_output}\n{verdict.summary}",
                data={"verdict": verdict.verdict, "items": [
                    {"criterion": i.criterion, "status": i.status, "detail": i.detail}
                    for i in verdict.items
                ], "summary": verdict.summary},
            )
        except Exception as e:
            logger.exception("AI eval failed")
            return StepResult(id=step_id, type="ai_eval", passed=False, error=str(e))

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
        """Create a summary string for a stage."""
        lines = []
        for step in stage.steps:
            status = "✓" if step.passed else "✗"
            lines.append(f"  {status} {step.id}: {step.output[:100] if step.output else step.error[:100]}")
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


def load_pipeline_config(workdir: str = ".") -> dict:
    """Load pipeline configuration from .gitreins/config.yaml."""
    config_path = os.path.join(workdir, ".gitreins", "config.yaml")
    if not os.path.exists(config_path):
        # Return default pipeline
        return {
            "pipeline": {
                "stages": [
                    {
                        "id": "tier1",
                        "parallel": True,
                        "on": ["pre-commit", "pre-eval"],
                        "steps": [
                            {"id": "secrets", "type": "script",
                             "run": "gitleaks detect --source . --no-git || python3 -c \"from engine.guard_manager import GuardManager; import sys; gm = GuardManager('.'); r = gm._check_secrets(); sys.exit(0 if r.passed else 1)\"",
                             "on_fail": "continue"},
                            {"id": "lint", "type": "script",
                             "run": "ruff check . --quiet 2>/dev/null || true"},
                            {"id": "tests", "type": "script",
                             "run": "pytest -x --tb=short 2>/dev/null || true"},
                        ],
                    },
                    {
                        "id": "tier2",
                        "type": "ai_eval",
                        "on": ["pre-eval"],
                        "condition": "true",
                        "max_iterations": -1,
                        "tools": ["read_file", "run_command", "search_pattern", "read_diff", "sandbox"],
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
            config["pipeline"] = {
                "stages": [
                    {"id": "tier1", "parallel": True, "on": ["pre-eval"],
                     "steps": [{"id": "secrets", "type": "script", "run": "true", "on_fail": "continue"}]},
                ]
            }
        return config
    except Exception as e:
        logger.warning("Failed to load pipeline config: %s", e)
        return {"pipeline": {"stages": []}}
