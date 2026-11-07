"""GR-176 — the judge derives its own criteria (and says when they fail).

The owner's layer-3 point: *"it needs to look at the task and decide what
criteria is extra needed for itself … we need a good judge base prompt that is
better than being so strict on everything it needs to actually search and find
things."*

The behaviour has four load-bearing parts, each pinned here:

1. A derived criterion is an ITEM: it arrives through the same ``Verdict.items``
   list, flagged ``derived=True``, and every persisted surface keeps the flag.
2. A derived FAIL FAILS THE VERDICT — enforced on the artifact, not just asked
   for in the prompt, so a model cannot grade a criterion FAIL and still write
   COMPLETE.
3. A run that dies before grading a derived criterion reports it UNVERIFIED
   (not absent, not FAIL), and the cap path still names it.
4. The prompt actually asks for it: explore first, at most five derived
   criteria, evidence per item, and no invented requirements.

The reachability fixture drives the whole loop: a flag added to ``worker.py``
that ``cli.py`` never passes is graded by a judge whose verdict is built from
the REAL ``search_pattern`` output, so the catch depends on tool wiring rather
than on a scripted string.

axiom:trace work_item=GR-176
"""

from __future__ import annotations

import json
import os
from unittest.mock import patch

import pytest

from engine.eval_cap import EvalCap
from engine.evaluator import (
    DERIVED_MARK_OFFSET,
    MAX_DERIVED_CRITERIA,
    AgenticEvaluator,
    VerdictItem,
)
from engine.llm import LLMClient, LLMResponse, ToolCall
from engine.pipeline import _verdict_item_data
from engine.persist import _verdict_item_dict


def _write(workdir: str, rel: str, content: str) -> str:
    full = os.path.join(workdir, rel)
    os.makedirs(os.path.dirname(full) or workdir, exist_ok=True)
    with open(full, "w") as f:
        f.write(content)
    return full


class TestParseAndVerdict:
    """Derived items parse into the same structure and can fail the verdict."""

    def test_derived_flag_parses(self, evaluator: AgenticEvaluator) -> None:
        verdict = evaluator._parse_verdict(
            '{"verdict":"COMPLETE","items":['
            '{"criterion":"given","status":"PASS","detail":"a.py:1"},'
            '{"criterion":"the flag is reachable from cli.py","derived":true,'
            '"status":"PASS","detail":"cli.py:9"}],"summary":"ok"}'
        )
        assert [i.derived for i in verdict.items] == [False, True]

    def test_given_item_has_no_derived_flag(self, evaluator: AgenticEvaluator) -> None:
        verdict = evaluator._parse_verdict(
            '{"verdict":"COMPLETE","items":[{"criterion":"g","status":"PASS","detail":"x:1"}]}'
        )
        assert verdict.items[0].derived is False

    def test_derived_fail_forces_incomplete(self, evaluator: AgenticEvaluator) -> None:
        """RED-PROOF: the model writes COMPLETE while its own derived item says FAIL."""
        verdict = evaluator._parse_verdict(
            '{"verdict":"COMPLETE","items":['
            '{"criterion":"given","status":"PASS","detail":"a.py:1"},'
            '{"criterion":"the flag is reachable from cli.py","derived":true,'
            '"status":"FAIL","detail":"cli.py:1-40 never mentions fast_retry"}],"summary":"ok"}'
        )
        assert verdict.verdict == "INCOMPLETE"
        assert any(i.derived and i.status == "FAIL" for i in verdict.items)

    def test_all_pass_stays_complete(self, evaluator: AgenticEvaluator) -> None:
        verdict = evaluator._parse_verdict(
            '{"verdict":"COMPLETE","items":['
            '{"criterion":"given","status":"PASS","detail":"a.py:1"},'
            '{"criterion":"derived","derived":true,"status":"PASS","detail":"b.py:2"}]}'
        )
        assert verdict.verdict == "COMPLETE"

    def test_persisted_surfaces_keep_the_flag(self) -> None:
        derived = VerdictItem(criterion="d", status="FAIL", detail="x", derived=True)
        given = VerdictItem(criterion="g", status="PASS", detail="y")
        assert _verdict_item_dict(derived)["derived"] is True
        assert "derived" not in _verdict_item_dict(given)
        assert _verdict_item_data(derived)["derived"] is True
        assert "derived" not in _verdict_item_data(given)


class TestCapPath:
    """A budget hit reports derived criteria instead of dropping them."""

    def test_partial_verdict_includes_derived_items(self, evaluator: AgenticEvaluator) -> None:
        evaluator._sandbox["derived_0"] = "the failure path returns an error, not a silent pass"
        evaluator._sandbox["verified_derived_0"] = "FAIL: handler.py:88 returns None"
        evaluator._sandbox["verified_0"] = "PASS: a.py:1"

        verdict = evaluator._extract_partial_verdict(["given criterion"])

        assert verdict.verdict == "INCOMPLETE"
        derived_items = [i for i in verdict.items if i.derived]
        assert len(derived_items) == 1
        assert derived_items[0].status == "FAIL"
        assert "handler.py:88" in derived_items[0].detail
        assert derived_items[0].criterion.startswith("the failure path")

    def test_derived_not_reached_is_unverified(self, evaluator: AgenticEvaluator) -> None:
        evaluator._sandbox["derived_1"] = "the migration is idempotent on re-run"
        verdict = evaluator._extract_partial_verdict(["given"])
        derived_items = [i for i in verdict.items if i.derived]
        assert derived_items and derived_items[0].status == "UNVERIFIED"
        assert verdict.verdict == "INCOMPLETE"

    def test_derived_list_is_capped(self, evaluator: AgenticEvaluator) -> None:
        for k in range(MAX_DERIVED_CRITERIA + 5):
            evaluator._sandbox[f"derived_{k}"] = f"criterion {k}"
        assert len(evaluator._derived_criteria_from_sandbox()) == MAX_DERIVED_CRITERIA

    def test_derived_marks_use_offset(self, evaluator: AgenticEvaluator) -> None:
        evaluator._tool_sandbox_write("verified_derived_2", "PASS: x.py:1")
        assert DERIVED_MARK_OFFSET + 2 in evaluator._criterion_marks
        # A given criterion's mark is never displaced by a derived one.
        evaluator._tool_sandbox_write("verified_1", "PASS: y.py:1")
        assert 1 in evaluator._criterion_marks

    def test_compacted_prompt_carries_derived_criteria(self, evaluator: AgenticEvaluator) -> None:
        evaluator._sandbox["derived_0"] = "the new flag is reachable from cli.py"
        evaluator._sandbox["verified_derived_0"] = "FAIL: cli.py:9 never passes it"
        prompt = evaluator._build_compacted_prompt(
            {"id": "t", "title": "t", "criteria": ["given"]}, "", 1
        )
        assert "the new flag is reachable from cli.py" in prompt
        assert '"derived": true' in prompt


class TestPrompt:
    """The base prompt asks for the owner's five behaviours."""

    def test_prompt_declares_derivation(self, evaluator: AgenticEvaluator) -> None:
        system = evaluator._system_prompt()
        assert "SET YOUR OWN BAR" in system
        assert f"at most {MAX_DERIVED_CRITERIA}" in system
        assert '"derived": true' in system
        assert "A derived FAIL fails the verdict" in system
        assert 'sandbox_write("derived_0"' in system
        # No unfilled placeholders may survive substitution.
        assert "{max_derived}" not in system
        assert "{max_index}" not in system

    def test_prompt_is_precise_not_universally_strict(self, evaluator: AgenticEvaluator) -> None:
        system = evaluator._system_prompt()
        assert "PRECISION" in system
        assert "not failures" in system
        assert "Prefer ONE correct, evidenced complaint" in system

    def test_prompt_requires_evidence_per_item(self, evaluator: AgenticEvaluator) -> None:
        system = evaluator._system_prompt()
        assert "EVIDENCE FOR EVERY ITEM" in system
        assert "INCOMPLETE, not PASS" in system

    def test_prompt_keeps_hard_test_rule(self, evaluator: AgenticEvaluator) -> None:
        """The pre-existing hard rule must survive the rewrite."""
        system = evaluator._system_prompt()
        assert "MANDATORY TEST VERIFICATION" in system
        assert "ACTUAL command output" in system

    def test_custom_template_override_still_wins(self, evaluator: AgenticEvaluator) -> None:
        evaluator._system_prompt_override = "CUSTOM {file_scope}"
        evaluator._allowed_files = None
        assert evaluator._system_prompt() == "CUSTOM full"


class TestReachabilityFixture:
    """A change that no entrypoint reaches is caught by the judge's own criterion."""

    def test_unreachable_flag_is_caught(self, llm_client: LLMClient, tmp_workdir: str) -> None:
        # Seeded fixture: `worker.run()` grows a fast_retry flag, but the CLI
        # entrypoint never passes it — the classic "wired but unreachable".
        _write(
            tmp_workdir,
            "worker.py",
            "def run(fast_retry: bool = False) -> str:\n"
            "    return 'fast' if fast_retry else 'slow'\n",
        )
        _write(
            tmp_workdir,
            "cli.py",
            "from worker import run\n\n\ndef main() -> str:\n    return run()\n",
        )
        _write(
            tmp_workdir,
            "tests/test_worker.py",
            "from worker import run\n\n\ndef test_run() -> None:\n    assert run() == 'slow'\n",
        )
        ev = AgenticEvaluator(llm_client, tmp_workdir, max_iterations=6)

        calls: list[str] = []

        def judge_chat(messages: list, tools: object = None, max_tokens: int = None) -> object:
            calls.append("chat")
            if len(calls) == 1:
                # Step 2+3 of EXPLORE FIRST: find the call sites of the new flag.
                return LLMResponse(
                    content="exploring call sites",
                    tool_calls=[
                        ToolCall(id="tc1", name="search_pattern", arguments={"regex": "fast_retry"})
                    ],
                )
            # The tool result the judge actually received — the verdict below
            # depends on it, so this is real wiring, not a scripted string.
            tool_payload = messages[-1]["content"]
            reachable_from_cli = "cli.py" in tool_payload
            items = [
                {
                    "criterion": "run() accepts a fast_retry flag",
                    "status": "PASS",
                    "detail": "worker.py:1 defines fast_retry",
                }
            ]
            if not reachable_from_cli:
                items.append(
                    {
                        "criterion": "the fast_retry flag is reachable from cli.main",
                        "derived": True,
                        "status": "FAIL",
                        "detail": "cli.py:5 calls run() with no fast_retry argument",
                    }
                )
            return LLMResponse(
                content=(
                    '{"verdict":"COMPLETE","items":'
                    + json.dumps(items)
                    + ',"summary":"flag added"}'
                )
            )

        with patch.object(llm_client, "chat", side_effect=judge_chat):
            verdict = ev.evaluate(
                {
                    "id": "GR-176-fixture",
                    "title": "add fast_retry flag",
                    "criteria": ["run() accepts a fast_retry flag"],
                }
            )

        derived = [i for i in verdict.items if i.derived]
        assert len(derived) == 1, f"judge produced no derived criterion: {verdict.items}"
        assert derived[0].status == "FAIL"
        assert "cli" in derived[0].criterion
        assert "cli.py" in derived[0].detail
        assert verdict.verdict == "INCOMPLETE", (
            "a derived FAIL must fail the verdict even though the model wrote COMPLETE"
        )

    def test_reachable_flag_passes(self, llm_client: LLMClient, tmp_workdir: str) -> None:
        """Control: the same fixture where the CLI DOES pass the flag is COMPLETE."""
        _write(
            tmp_workdir,
            "worker.py",
            "def run(fast_retry: bool = False) -> str:\n"
            "    return 'fast' if fast_retry else 'slow'\n",
        )
        _write(
            tmp_workdir,
            "cli.py",
            "from worker import run\n\n\ndef main(fast: bool) -> str:\n    return run(fast_retry=fast)\n",
        )
        ev = AgenticEvaluator(llm_client, tmp_workdir, max_iterations=6)

        calls: list[str] = []

        def judge_chat(messages: list, tools: object = None, max_tokens: int = None) -> object:
            calls.append("chat")
            if len(calls) == 1:
                return LLMResponse(
                    content="exploring call sites",
                    tool_calls=[
                        ToolCall(id="tc1", name="search_pattern", arguments={"regex": "fast_retry"})
                    ],
                )
            tool_payload = messages[-1]["content"]
            reachable = "cli.py" in tool_payload
            return LLMResponse(
                content='{"verdict":"COMPLETE","items":['
                '{"criterion":"run() accepts a fast_retry flag","status":"PASS",'
                '"detail":"worker.py:1"},'
                '{"criterion":"the fast_retry flag is reachable from cli.main","derived":true,'
                '"status":"' + ("PASS" if reachable else "FAIL") + '",'
                '"detail":"' + ("cli.py:5 passes it" if reachable else "missing") + '"}]}'
            )

        with patch.object(llm_client, "chat", side_effect=judge_chat):
            verdict = ev.evaluate(
                {
                    "id": "GR-176-control",
                    "title": "add fast_retry flag",
                    "criteria": ["run() accepts a fast_retry flag"],
                }
            )

        assert verdict.verdict == "COMPLETE"
        assert [i.status for i in verdict.items if i.derived] == ["PASS"]


class TestBudgetStillDegraded:
    """The derived-criteria bar must not turn a capped run into a pass."""

    def test_cap_mid_derivation_is_incomplete(
        self, llm_client: LLMClient, tmp_workdir: str
    ) -> None:
        _write(tmp_workdir, "mod.py", "VALUE = 1\n")
        ev = AgenticEvaluator(
            llm_client, tmp_workdir, eval_cap=EvalCap(max_iterations=1, source="test")
        )

        def chat(messages: list, tools: object = None, max_tokens: int = None) -> object:
            # One round of real tool calls inside the (one-iteration) budget:
            # the criterion is proposed, never graded — then the cap fires.
            return LLMResponse(
                content="proposing a derived criterion",
                tool_calls=[
                    ToolCall(
                        id="tc1",
                        name="sandbox_write",
                        arguments={
                            "key": "derived_0",
                            "content": "the new flag is reachable from cli.py",
                        },
                    ),
                    ToolCall(
                        id="tc2",
                        name="sandbox_write",
                        arguments={"key": "verified_0", "content": "PASS: mod.py:1"},
                    ),
                ],
            )

        with patch.object(llm_client, "chat", side_effect=chat):
            verdict = ev.evaluate({"id": "t", "title": "cap", "criteria": ["given"]})

        assert verdict.verdict == "INCOMPLETE"
        assert any(i.derived and i.status == "UNVERIFIED" for i in verdict.items)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
