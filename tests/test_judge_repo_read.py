"""GR-170 — repo-wide review is the default, with guardrails.

The owner's directive behind this work item: *"the reviewer needs to see the
repo … we had a flag and tools for this but maybe they got lost."* The flag
(`evaluator.file_scope: full`) and the tools (twelve, in ``EVALUATOR_TOOLS``)
both existed; what was missing was (a) the DEFAULT pointing at them and
(b) guardrails that make a whole-repo read safe.

These tests pin the four properties the change must keep:

1. ``full`` is the default, and ``changed`` still works as the explicit opt-out.
2. Repo-wide reads cannot escape the worktree (no ``..``, no absolute paths,
   no sibling that merely shares the worktree's prefix).
3. Repo-wide reads cannot open a credential store — the classifier is the
   commit guard's own ``SENSITIVE_PATH_PATTERNS`` class, not a new denylist —
   while deliberate test fixtures stay readable.
4. An over-cap whole-file read is refused with a message naming the knob, and
   range reads still work; a budget hit mid-exploration ends INCOMPLETE, never
   COMPLETE.

axiom:trace work_item=GR-170
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
import yaml

from engine.config import GitReinsDefaults
from engine.eval_cap import EvalCap
from engine.evaluator import (
    DEFAULT_FILE_SCOPE,
    DEFAULT_MAX_READ_BYTES,
    AgenticEvaluator,
    _is_sensitive_read_path,
)
from engine.llm import LLMClient, LLMResponse, ToolCall


def _write_config(workdir: str, evaluator_cfg: dict) -> None:
    cdir = os.path.join(workdir, ".gitreins")
    os.makedirs(cdir, exist_ok=True)
    with open(os.path.join(cdir, "config.yaml"), "w") as f:
        yaml.dump({"evaluator": evaluator_cfg}, f)


def _write(workdir: str, rel: str, content: str) -> str:
    full = os.path.join(workdir, rel)
    os.makedirs(os.path.dirname(full) or workdir, exist_ok=True)
    with open(full, "w") as f:
        f.write(content)
    return full


class TestDefaultScope:
    """`full` is the default; `changed` is the explicit opt-out."""

    def test_module_default_is_full(self) -> None:
        assert DEFAULT_FILE_SCOPE == "full"

    def test_config_dataclass_default_matches_evaluator(self) -> None:
        """Two defaults exist (engine/config.py + engine/evaluator.py) — pin them.

        A drift here would mean `gitreins config show` advertises one scope
        while a config-less judge runs the other.
        """
        assert GitReinsDefaults().file_scope == DEFAULT_FILE_SCOPE == "full"

    def test_configless_evaluate_uses_full_scope(
        self, evaluator: AgenticEvaluator, llm_client: LLMClient
    ) -> None:
        """Without config.yaml the judge gets repo-wide scope and is told so."""
        captured: dict[str, str] = {}

        def fake_chat(messages: list, tools: object = None, max_tokens: int = None) -> object:
            captured["system"] = messages[0]["content"]
            return LLMResponse(content='{"verdict":"COMPLETE","items":[],"summary":"ok"}')

        with patch.object(llm_client, "chat", side_effect=fake_chat):
            verdict = evaluator.evaluate({"id": "t", "title": "scope", "criteria": ["c"]})

        assert verdict.verdict == "COMPLETE"
        assert evaluator._allowed_files is None, "config-less default must be full scope"
        system = captured["system"]
        assert "File scope: full" in system
        assert "MAY read any file" in system
        # The discouragement that made the old prompt diff-only must be gone.
        assert "do NOT need to call read_file" not in system
        # Exploration is a required step, not an optional extra.
        assert "EXPLORE FIRST" in system
        assert "entrypoint that must reach the new behaviour" in system

    def test_explicit_changed_is_opt_out(self, llm_client: LLMClient, tmp_workdir: str) -> None:
        """`file_scope: changed` still restricts, and the prompt says it is the opt-out."""
        _write_config(tmp_workdir, {"file_scope": "changed"})
        ev = AgenticEvaluator(llm_client, tmp_workdir, max_iterations=3)

        def fake_chat(messages: list, tools: object = None, max_tokens: int = None) -> object:
            return LLMResponse(content='{"verdict":"COMPLETE","items":[],"summary":"ok"}')

        with patch.object(llm_client, "chat", side_effect=fake_chat):
            ev.evaluate({"id": "t", "title": "scope", "criteria": ["c"]})

        assert ev._allowed_files is not None, "changed must compute the allowed set"
        system = ev._system_prompt()
        assert "File scope: changed" in system
        assert "explicit opt-out" in system
        assert "MAY read any file" not in system


class TestPathEscape:
    """Repo-wide scope stays inside the worktree."""

    def test_dotdot_escape_refused(self, evaluator: AgenticEvaluator) -> None:
        result = evaluator._tool_read_file("../etc/passwd")
        assert "error" in result
        assert "Path outside working tree" in result["error"]

    def test_absolute_path_outside_refused(self, evaluator: AgenticEvaluator) -> None:
        result = evaluator._tool_read_file("/etc/passwd")
        assert "error" in result
        assert "Path outside working tree" in result["error"]

    def test_sibling_sharing_prefix_refused(
        self, evaluator: AgenticEvaluator, tmp_workdir: str
    ) -> None:
        """RED-PROOF: `real.startswith(workdir)` accepts `/…/repo-evil` for `/…/repo`.

        The fixture workdir is `<tmp>/repo`; the sibling is `<tmp>/repo-evil`.
        A plain prefix check reads straight through the escape.
        """
        sibling = os.path.realpath(tmp_workdir) + "-evil"
        os.makedirs(sibling, exist_ok=True)
        with open(os.path.join(sibling, "secret.txt"), "w") as f:
            f.write("outside\n")

        assert os.path.realpath(os.path.join(tmp_workdir, "..", "repo-evil")).startswith(
            os.path.realpath(tmp_workdir)
        ), "precondition: the sibling really does share the string prefix"

        result = evaluator._tool_read_file("../repo-evil/secret.txt")
        assert "error" in result
        assert "Path outside working tree" in result["error"]

    def test_inside_paths_still_readable(
        self, evaluator: AgenticEvaluator, tmp_workdir: str
    ) -> None:
        _write(tmp_workdir, "deep/nested/mod.py", "VALUE = 1\n")
        result = evaluator._tool_read_file("deep/nested/mod.py")
        assert "content" in result
        assert "VALUE = 1" in result["content"]


class TestSensitivePaths:
    """The credential-store class the commit guard enforces also closes the read path."""

    def test_env_file_refused(self, evaluator: AgenticEvaluator, tmp_workdir: str) -> None:
        _write(tmp_workdir, ".env", "API_KEY=super-secret\n")
        result = evaluator._tool_read_file(".env")
        assert "error" in result
        assert "Refusing to read sensitive path" in result["error"]
        assert "super-secret" not in str(result)

    def test_nested_credential_refused(self, evaluator: AgenticEvaluator, tmp_workdir: str) -> None:
        _write(tmp_workdir, "deploy/credentials.json", '{"key":"secret"}\n')
        result = evaluator._tool_read_file("deploy/credentials.json")
        assert "error" in result
        assert "Refusing to read sensitive path" in result["error"]

    def test_test_fixture_env_still_readable(
        self, evaluator: AgenticEvaluator, tmp_workdir: str
    ) -> None:
        """The guard's own fixture exemption is reused, not re-decided."""
        _write(tmp_workdir, "tests/fixtures/.env", "FAKE=1\n")
        result = evaluator._tool_read_file("tests/fixtures/.env")
        assert "content" in result
        assert "FAKE=1" in result["content"]

    def test_classifier_defers_to_guard_manager(self) -> None:
        assert _is_sensitive_read_path(".env") is True
        assert _is_sensitive_read_path("config/.env") is True
        assert _is_sensitive_read_path("engine/evaluator.py") is False


class TestSizeGuard:
    """A whole-file read over the cap is refused, not silently truncated."""

    def test_default_cap_value(self, evaluator: AgenticEvaluator) -> None:
        assert evaluator.max_read_bytes == DEFAULT_MAX_READ_BYTES == 524_288

    def test_oversized_whole_read_refused(
        self, evaluator: AgenticEvaluator, tmp_workdir: str
    ) -> None:
        _write(tmp_workdir, "blob.txt", "x" * (DEFAULT_MAX_READ_BYTES + 1))
        result = evaluator._tool_read_file("blob.txt")
        assert "error" in result
        assert "File too large to read whole" in result["error"]
        assert "evaluator.max_read_bytes" in result["error"]
        assert result["total_bytes"] > DEFAULT_MAX_READ_BYTES

    def test_oversized_range_read_still_allowed(
        self, evaluator: AgenticEvaluator, tmp_workdir: str
    ) -> None:
        _write(tmp_workdir, "blob.txt", "".join(f"line{i}\n" for i in range(80_000)))
        result = evaluator._tool_read_file("blob.txt", offset=1, limit=3)
        assert "content" in result
        assert "line0" in result["content"]

    def test_cap_is_configurable(self, llm_client: LLMClient, tmp_workdir: str) -> None:
        _write_config(tmp_workdir, {"max_read_bytes": 64})
        ev = AgenticEvaluator(llm_client, tmp_workdir, max_iterations=3)
        assert ev.max_read_bytes == 64
        _write(tmp_workdir, "small.txt", "y" * 200)
        result = ev._tool_read_file("small.txt")
        assert "error" in result
        assert "File too large to read whole" in result["error"]

    def test_small_file_unaffected(self, evaluator: AgenticEvaluator, tmp_workdir: str) -> None:
        _write(tmp_workdir, "small.txt", "hello\n")
        result = evaluator._tool_read_file("small.txt")
        assert "content" in result


class TestBudgetDuringExploration:
    """Exploration must not turn a cap hit into a COMPLETE verdict."""

    def test_cap_hit_ends_incomplete_never_complete(
        self, llm_client: LLMClient, tmp_workdir: str
    ) -> None:
        ev = AgenticEvaluator(
            llm_client, tmp_workdir, eval_cap=EvalCap(max_iterations=1, source="test")
        )
        _write(tmp_workdir, "mod.py", "VALUE = 1\n")

        def exploring_chat(messages: list, tools: object = None, max_tokens: int = None) -> object:
            return LLMResponse(
                content="exploring",
                tool_calls=[ToolCall(id="tc1", name="read_file", arguments={"path": "mod.py"})],
            )

        with patch.object(llm_client, "chat", side_effect=exploring_chat):
            verdict = ev.evaluate({"id": "t", "title": "budget", "criteria": ["c0", "c1"]})

        assert verdict.verdict == "INCOMPLETE", "a capped run is never COMPLETE"
        assert "Cap exceeded" in verdict.summary
        assert verdict.items, "a capped run must still name every criterion"
        assert all(item.status != "PASS" for item in verdict.items)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
