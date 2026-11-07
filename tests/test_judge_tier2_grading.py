"""JUDGE-TIER2-001 — a tier2 grading must survive untrusted bytes and report
per-criterion coverage/cost when a cap ends the run.

Measured on 2026-11-06 (duckbrain SDK-PARITY-001, 5 machine-proven criteria):

* run 7055fae6: tier1 PASS, tier2 INCOMPLETE — "Cap exceeded: Iteration cap
  (250) reached (250.2 used)", ``items=[]``, 9.0M/10M input tokens compacted;
* the 400-iteration re-judge ran 48 minutes, wrote 3.1M tier2 input tokens and
  died in ``command_hygiene.run_bounded`` → ``subprocess._translate_newlines``
  → ``UnicodeDecodeError: 'utf-8' codec can't decode byte 0x93``, after which
  the process hung until it was killed.

So: one non-UTF-8 byte in a tool's output must not end an evaluation, a cap
must name which criteria were graded, and the tokens a run spent must be
visible while it runs and in the ledger even when it dies.
"""

from __future__ import annotations

import json
import logging
import pathlib
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from engine import command_hygiene as ch  # noqa: E402
from engine.eval_cap import EvalCap  # noqa: E402
from engine.evaluator import AgenticEvaluator  # noqa: E402
from engine.llm import LLMClient  # noqa: E402


@pytest.fixture()
def emit_bad_byte(tmp_path: pathlib.Path) -> str:
    """A command whose stdout carries byte 0x93 (Windows-1252 curly quote)."""
    script = tmp_path / "emit_0x93.py"
    script.write_bytes(
        b'import sys\nsys.stdout.buffer.write(b"pre \\x93 post\\n")\nsys.stdout.buffer.flush()\n'
    )
    return f"{sys.executable} {script}"


# ── AC1: a non-UTF-8 line cannot abort the run ────────────────────────────


class TestUndecodableOutput:
    def test_control_strict_decode_still_raises(self, emit_bad_byte: str) -> None:
        """The falsifier: the PRE-FIX path (strict text=True) still blows up.

        Without this control the test below could pass for the wrong reason —
        a fixture that stopped emitting invalid bytes would make the fix look
        like it works when nothing was being tested.
        """
        with pytest.raises(UnicodeDecodeError):
            subprocess.run(emit_bad_byte.split(), capture_output=True, text=True, check=False)

    def test_run_bounded_returns_replacement_char(self, emit_bad_byte: str) -> None:
        result = ch.run_bounded(emit_bad_byte, timeout=30)
        assert "exception" not in str(result).lower()
        assert result["exit_code"] == 0
        assert "pre " in result["output"] and " post" in result["output"]
        assert "\ufffd" in result["output"], repr(result["output"])

    def test_evaluator_tool_result_carries_replacement_char(
        self, tmp_workdir: str, emit_bad_byte: str
    ) -> None:
        """The evaluation CONTINUES: a tool result, not an exception."""
        ev = AgenticEvaluator(LLMClient(), tmp_workdir)
        result = ev._tool_run_command(cmd=emit_bad_byte)
        assert isinstance(result, dict)
        assert result.get("exit_code") == 0, result
        assert "\ufffd" in result.get("output", ""), result.get("output")


# ── AC2: a cap names the criteria instead of returning items=[] ───────────


class TestCapVerdictNamesCriteria:
    def test_no_graded_criteria_still_yields_one_item_each(self, tmp_workdir: str) -> None:
        """The measured failure: 250 iterations, items=[] — impossible now."""
        ev = AgenticEvaluator(LLMClient(), tmp_workdir)
        verdict = ev._extract_partial_verdict(["c0", "c1", "c2"])
        assert len(verdict.items) == 3
        assert [i.status for i in verdict.items] == ["UNVERIFIED"] * 3
        assert verdict.verdict == "INCOMPLETE"
        assert "0 of 3 criteria graded" in verdict.summary
        assert "3 ungraded" in verdict.summary

    def test_cap_verdict_is_never_bare_incomplete(self, tmp_workdir: str) -> None:
        ev = AgenticEvaluator(LLMClient(), tmp_workdir)
        verdict = ev._cap_verdict(["c0", "c1"], "Iteration cap (250) reached (250.2 used)")
        assert verdict.items, "a cap-hit verdict must carry per-criterion items"
        assert verdict.verdict == "INCOMPLETE"
        assert verdict.summary.startswith("Cap exceeded: Iteration cap (250)")

    def test_graded_and_ungraded_are_both_named(self, tmp_workdir: str) -> None:
        ev = AgenticEvaluator(LLMClient(), tmp_workdir)
        ev._tool_sandbox_write("verified_0", "PASS — pytest: 1 passed")
        verdict = ev._extract_partial_verdict(["c0", "c1"])
        assert verdict.items[0].status == "PASS"
        assert "graded at iteration" in verdict.items[0].detail
        assert verdict.items[1].status == "UNVERIFIED"
        assert "Not examined" in verdict.items[1].detail
        assert "1 of 2 criteria graded" in verdict.summary

    def test_task_without_criteria_does_not_invent_items(self, tmp_workdir: str) -> None:
        ev = AgenticEvaluator(LLMClient(), tmp_workdir)
        verdict = ev._cap_verdict([], "Time cap (5s) exceeded")
        assert verdict.items == []
        assert verdict.verdict == "INCOMPLETE"
        assert "Cap exceeded" in verdict.summary


# ── AC3: cost per criterion, visible before and after the cap ─────────────


class TestGradingCostIsVisible:
    def test_each_graded_criterion_carries_its_own_spend(self, tmp_workdir: str) -> None:
        ev = AgenticEvaluator(LLMClient(), tmp_workdir)
        cap = ev.eval_cap
        cap.iteration_credit = 40.0
        cap.cumulative_input_tokens = 1_500_000
        cap.cumulative_output_tokens = 20_000
        ev._tool_sandbox_write("verified_0", "PASS — first")
        cap.iteration_credit = 90.0
        cap.cumulative_input_tokens = 3_000_000
        cap.cumulative_output_tokens = 45_000
        ev._tool_sandbox_write("verified_1", "FAIL — second")
        verdict = ev._extract_partial_verdict(["c0", "c1"])
        first, second = verdict.items
        assert "1,500,000 in" in first.detail, first.detail
        assert "1,500,000 in" in second.detail, second.detail  # the delta, not the total
        assert "3,000,000 in / 45,000 out over 90.0 iterations" in verdict.summary

    def test_progress_line_is_logged_before_the_cap(self, tmp_workdir: str, caplog) -> None:
        ev = AgenticEvaluator(LLMClient(), tmp_workdir)
        ev.eval_cap = EvalCap(max_iterations=250, max_input_tokens=10_000_000)
        ev.eval_cap.start()
        ev.eval_cap.iteration_credit = 120.0
        ev.eval_cap.cumulative_input_tokens = 4_000_000
        ev._tool_sandbox_write("verified_0", "PASS — one")
        with caplog.at_level(logging.INFO, logger="gitreins.evaluator"):
            ev._log_grading_progress(121, 250, 5)
        line = [r.getMessage() for r in caplog.records if "Grading progress" in r.getMessage()]
        assert line, [r.getMessage() for r in caplog.records]
        assert "1/5 criteria graded" in line[0]
        assert "4,000,000/10,000,000" in line[0]
        assert "4,000,000 in tokens per graded criterion" in line[0]


# ── AC3 (ledger): the cost row lands even when the run dies ───────────────


class TestUsageRowOnFailedRun:
    def _pipeline(self, workdir: str, llm: LLMClient):
        from engine.pipeline import Pipeline

        config = {
            "pipeline": {
                "stages": [
                    {
                        "id": "tier2",
                        "type": "ai_eval",
                        "on": ["pre-eval"],
                        "condition": "true",
                    }
                ]
            }
        }
        return Pipeline(config, workdir, llm=llm)

    def _usage_rows(self, workdir: str) -> list[dict]:
        path = pathlib.Path(workdir) / ".gitreins" / "usage.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def test_row_written_when_evaluate_raises(self, tmp_workdir: str, llm_client) -> None:
        p = self._pipeline(tmp_workdir, llm_client)
        cap = SimpleNamespace(
            cumulative_input_tokens=3_100_000,
            cumulative_output_tokens=40_000,
            cumulative_cache_read=900_000,
            cumulative_cache_write=10_000,
        )
        with patch("engine.evaluator.AgenticEvaluator") as mock_eval:
            mock_eval.return_value.eval_cap = cap
            mock_eval.return_value.evaluate.side_effect = UnicodeDecodeError(
                "utf-8", b"\x93", 0, 1, "invalid start byte"
            )
            result = p.run({"id": "t1", "title": "x", "criteria": ["c1"]}, trigger="pre-eval")
        assert result["passed"] is False
        rows = self._usage_rows(tmp_workdir)
        assert len(rows) == 1, rows
        assert rows[0]["tokens_in"] == 3_100_000
        assert rows[0]["tokens_out"] == 40_000
        assert rows[0]["step"] == "tier2"
        assert rows[0]["outcome"] == "error"

    def test_row_written_when_a_verdict_lands(self, tmp_workdir: str, llm_client) -> None:
        p = self._pipeline(tmp_workdir, llm_client)
        verdict = MagicMock()
        verdict.verdict = "INCOMPLETE"
        verdict.summary = "Cap exceeded: Iteration cap (250) reached"
        verdict.items = []
        with patch("engine.evaluator.AgenticEvaluator") as mock_eval:
            mock_eval.return_value.eval_cap = SimpleNamespace(
                cumulative_input_tokens=9_000_000,
                cumulative_output_tokens=120_000,
                cumulative_cache_read=2_000_000,
                cumulative_cache_write=0,
            )
            mock_eval.return_value.evaluate.return_value = verdict
            p.run({"id": "t1", "title": "x", "criteria": ["c1"]}, trigger="pre-eval")
        rows = self._usage_rows(tmp_workdir)
        assert len(rows) == 1, rows
        assert rows[0]["tokens_in"] == 9_000_000
        assert rows[0]["outcome"] == "graded"

    def test_ephemeral_run_still_writes_nothing(self, tmp_workdir: str, llm_client) -> None:
        """persist_telemetry=False keeps the documented skip (EVID-003)."""
        from engine.pipeline import Pipeline

        p = Pipeline(
            {"pipeline": {"stages": []}}, tmp_workdir, llm=llm_client, persist_telemetry=False
        )
        p._append_judge_usage(
            SimpleNamespace(eval_cap=SimpleNamespace()), "tier2", outcome="graded"
        )
        assert self._usage_rows(tmp_workdir) == []

    def test_cap_error_is_not_double_punctuated(self, tmp_workdir: str) -> None:
        """Cap messages already end with '. ' — the joined summary must not '..'."""
        ev = AgenticEvaluator(LLMClient(), tmp_workdir)
        verdict = ev._cap_verdict(
            ["c0"], "Iteration cap (2) reached (2.2 used). Increase max_iterations."
        )
        assert ".." not in verdict.summary, verdict.summary
        assert verdict.summary.startswith("Cap exceeded: Iteration cap (2) reached")
