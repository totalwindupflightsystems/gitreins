"""
Judge Orchestrator — Runs the evaluation pipeline.

Now delegate to the Pipeline engine for configurable multi-stage evaluation.
"""

import logging

from engine.evaluator import AgenticEvaluator
from engine.guard_manager import GuardManager
from engine.llm import LLMClient
from engine.pipeline import Pipeline, load_pipeline_config
from engine.task_manager import Task

logger = logging.getLogger("gitreins.judge")


class Judge:
    """Run the evaluation pipeline against a task."""

    def __init__(
        self,
        llm: LLMClient,
        workdir: str = ".",
        guard_config: dict | None = None,
        eval_cap: str | None = None,
    ):
        self.workdir = workdir
        self.llm = llm
        self.guard_config = guard_config or {}
        self.guard_manager = GuardManager(workdir, self.guard_config)
        self.eval_cap = eval_cap

    def evaluate_task(self, task: Task) -> "JudgeResult":
        """Run the pipeline against a task.

        Uses the configurable pipeline from .gitreins/config.yaml.
        Falls back to the simple Tier 1 → Tier 2 if no pipeline defined.
        """
        config = load_pipeline_config(self.workdir)

        if config.get("pipeline", {}).get("stages"):
            return self._run_pipeline(task, config)
        else:
            return self._run_legacy(task)

    def _run_pipeline(self, task: Task, config: dict) -> "JudgeResult":
        """Run the configurable pipeline."""
        from engine.pipeline import Pipeline

        pipeline = Pipeline(config, self.workdir, llm=self.llm)

        task_dict = {
            "id": task.id,
            "title": task.title,
            "criteria": task.criteria,
            "status": task.status,
        }

        try:
            result = pipeline.run(task_dict, trigger="pre-eval")
            return JudgeResult(
                task_id=task.id,
                passed=result.get("passed", False),
                pipeline_result=result,
            )
        except Exception as e:
            logger.exception("Pipeline execution failed")
            return JudgeResult(task_id=task.id, passed=False, pipeline_result={"error": str(e)})

    def _run_legacy(self, task: Task) -> "JudgeResult":
        """Fallback: simple Tier 1 → Tier 2."""

        result = JudgeResult(task_id=task.id)

        print("  Tier 1: Running static guards...")
        tier1 = self.guard_manager.run_all()
        result.tier1 = tier1

        if not tier1.passed:
            print("  Tier 1 FAILED — skipping evaluator")
            result.passed = False
            return result

        print("  Tier 1 PASSED")
        print("  Tier 2: Running agentic evaluator...")

        evaluator = AgenticEvaluator(self.llm, self.workdir, eval_cap=self.eval_cap)
        task_dict = {
            "id": task.id,
            "title": task.title,
            "criteria": task.criteria,
        }
        tier2 = evaluator.evaluate(task_dict)
        result.tier2 = tier2
        result.passed = tier2.verdict == "COMPLETE"
        result.verdict = tier2
        return result

    def run_precommit(self) -> bool:
        """Run pre-commit pipeline stages only. Returns True if commit should proceed."""
        config = load_pipeline_config(self.workdir)
        pipeline = Pipeline(config, self.workdir, llm=self.llm)
        result = pipeline.run({"id": "_precommit", "title": "pre-commit", "criteria": []}, trigger="pre-commit")
        return result.get("passed", True)


class JudgeResult:
    """Result of running the judge pipeline."""

    def __init__(self, task_id: str, passed: bool = False, pipeline_result: dict = None, verdict=None, tier1=None, tier2=None):
        self.task_id = task_id
        self.passed = passed
        self.pipeline_result = pipeline_result or {}
        self.verdict = verdict
        self.tier1 = tier1   # Tier1Result from guard_manager.run_all()
        self.tier2 = tier2   # Verdict from AgenticEvaluator.evaluate()

    @property
    def summary(self) -> str:
        lines = [f"Judge Result: {self.task_id}"]

        if self.pipeline_result:
            stages = self.pipeline_result.get("stages", {})
            for stage_id, stage in stages.items():
                status = "PASS" if stage.get("passed") else "FAIL"
                lines.append(f"\nStage {stage_id}: {status}")
                summary = stage.get("summary", "")
                if summary:
                    lines.append(f"  {summary}")

        # Legacy verdict items
        if self.verdict:
            lines.append(f"\nTier 2 (Agentic Evaluator): {self.verdict.verdict}")
            for item in self.verdict.items:
                status = "✓" if item.status == "PASS" else "✗"
                lines.append(f"  {status} {item.criterion}: {item.detail}")

        lines.append(f"\nOverall: {'PASS ✓' if self.passed else 'FAIL ✗'}")
        return "\n".join(lines)
