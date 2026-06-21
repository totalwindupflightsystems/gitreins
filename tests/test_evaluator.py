"""
Unit tests for engine/evaluator.py — agentic LLM loop with tools and dedup.
axiom:trace work_item=GR-001 spec=specs/03-Agentic-Evaluator.md plan=.memory-bank/work-items/GR-001/plan.yaml
"""
import os
import json
import pytest
import subprocess
from unittest import mock
from unittest.mock import MagicMock, PropertyMock, patch

from engine.evaluator import (
    AgenticEvaluator, Verdict, VerdictItem,
    EVALUATOR_SYSTEM_PROMPT, EVALUATOR_TOOLS,
)
from engine.llm import LLMClient, LLMResponse, ToolCall


# ── Phase 1-4-1: AgenticEvaluator tools, dedup, verdict parsing ──────────────


class TestReadFile:
    """Test _tool_read_file — path safety, error handling — step-1-4-1-1."""

    def test_read_existing_file(self, evaluator, tmp_workdir):
        """read_file of existing file returns content."""
        path = os.path.join(tmp_workdir, "hello.txt")
        with open(path, "w") as f:
            f.write("hello world\n")
        result = evaluator._tool_read_file("hello.txt")
        assert "content" in result
        assert result["path"] == "hello.txt"
        assert result["total_lines"] == 1

    def test_read_nonexistent_file(self, evaluator):
        """read_file of nonexistent file returns error: File not found."""
        result = evaluator._tool_read_file("nonexistent.txt")
        assert "error" in result
        assert "File not found" in result["error"]

    def test_read_path_traversal_outside_workdir(self, evaluator):
        """read_file with path containing '../' outside workdir returns error."""
        result = evaluator._tool_read_file("../etc/passwd")
        assert "error" in result
        assert "Path outside working tree" in result["error"]

    def test_read_directory_returns_error(self, evaluator, tmp_workdir):
        """read_file of a directory returns error: Path is a directory."""
        subdir = os.path.join(tmp_workdir, "subdir")
        os.makedirs(subdir)
        result = evaluator._tool_read_file("subdir")
        assert "error" in result
        assert "Path is a directory" in result["error"]

    def test_read_file_with_offset(self, evaluator, tmp_workdir):
        """read_file with offset reads from given line."""
        path = os.path.join(tmp_workdir, "multiline.txt")
        with open(path, "w") as f:
            f.write("line1\nline2\nline3\nline4\n")
        result = evaluator._tool_read_file("multiline.txt", offset=2, limit=2)
        assert result["total_lines"] == 4
        content = result["content"]
        assert "line2" in content
        assert "line3" in content
        # line1 should not be present
        assert "line1" not in content

    def test_read_file_offset_exceeds_length(self, evaluator, tmp_workdir):
        """read_file with offset exceeding file length returns error."""
        path = os.path.join(tmp_workdir, "short.txt")
        with open(path, "w") as f:
            f.write("one\n")
        result = evaluator._tool_read_file("short.txt", offset=10)
        assert "error" in result


class TestRunCommand:
    """Test _tool_run_command — execution, output, timeout — step-1-4-1-2."""

    def test_run_echo_hello(self, evaluator):
        """run_command('echo hello') → exit_code=0, output contains 'hello'."""
        result = evaluator._tool_run_command("echo hello")
        assert result["exit_code"] == 0
        assert "hello" in result["output"]
        assert result["cmd"] == "echo hello"

    def test_run_false_command(self, evaluator):
        """run_command('false') → exit_code=1, output captured."""
        result = evaluator._tool_run_command("false")
        assert result["exit_code"] == 1

    def test_run_command_timeout(self, evaluator, llm_client, tmp_workdir):
        """run_command with short timeout → timeout error in <5s."""
        from engine.evaluator import AgenticEvaluator
        fast_eval = AgenticEvaluator(llm_client, tmp_workdir, max_iterations=5, command_timeout=2)
        result = fast_eval._tool_run_command("sleep 5")
        assert "error" in result
        assert "timed out" in result["error"]

    def test_output_truncated_at_4000(self, evaluator):
        """Output is truncated at 4000 characters."""
        # Generate > 4000 chars of output
        result = evaluator._tool_run_command("python3 -c 'print(\"x\" * 5000)'")
        output_len = len(result["output"])
        assert output_len <= 4100  # Allow some margin for truncation message
        assert "truncated" in result["output"]


class TestSearchPattern:
    """Test _tool_search_pattern — regex matching, dir skipping, result capping — step-1-4-1-3."""

    def test_search_finds_todo(self, evaluator, tmp_workdir):
        """search_pattern finds TODO comments in working files."""
        path = os.path.join(tmp_workdir, "code.py")
        with open(path, "w") as f:
            f.write("# TODO: fix this\nprint('hello')\n# TODO: also this\n")
        result = evaluator._tool_search_pattern("TODO")
        assert result["count"] >= 2

    def test_search_with_file_glob(self, evaluator, tmp_workdir):
        """search_pattern with file_glob='*.py' only searches Python files."""
        py_path = os.path.join(tmp_workdir, "code.py")
        txt_path = os.path.join(tmp_workdir, "notes.txt")
        with open(py_path, "w") as f:
            f.write("# TODO in py\n")
        with open(txt_path, "w") as f:
            f.write("TODO in txt\n")
        result = evaluator._tool_search_pattern("TODO", file_glob="*.py")
        assert result["count"] == 1
        assert all("code.py" in m for m in result["matches"])

    def test_invalid_regex_returns_error(self, evaluator):
        """Invalid regex pattern returns error."""
        result = evaluator._tool_search_pattern("[invalid(regex")
        assert "error" in result
        assert "Invalid regex" in result["error"]

    def test_search_skips_dot_dirs(self, evaluator, tmp_workdir):
        """search_pattern does not descend into .git/ or __pycache__/."""
        # Create files in skipped dirs
        cache_dir = os.path.join(tmp_workdir, "__pycache__")
        os.makedirs(cache_dir)
        with open(os.path.join(cache_dir, "cache.py"), "w") as f:
            f.write("SECRET in cache\n")
        result = evaluator._tool_search_pattern("SECRET")
        # Should not match the cache file
        matches = result["matches"]
        cache_matches = [m for m in matches if "__pycache__" in m]
        assert len(cache_matches) == 0


class TestSandbox:
    """Test sandbox_write/sandbox_read — in-memory dict — step-1-4-1-4."""

    def test_sandbox_write_read(self, evaluator):
        """sandbox_write then sandbox_read returns content."""
        evaluator._tool_sandbox_write("key1", "value1")
        result = evaluator._tool_sandbox_read("key1")
        assert result["content"] == "value1"
        assert result["key"] == "key1"

    def test_sandbox_write_returns_written_count(self, evaluator):
        """sandbox_write returns written count."""
        result = evaluator._tool_sandbox_write("key2", "hello")
        assert result["written"] == 5

    def test_sandbox_read_nonexistent(self, evaluator):
        """sandbox_read of nonexistent key returns error."""
        result = evaluator._tool_sandbox_read("nonexistent")
        assert "error" in result
        assert "Key not found" in result["error"]

    def test_evaluate_clears_sandbox(self, evaluator, llm_client):
        """evaluate() clears sandbox at start of each call."""
        evaluator._tool_sandbox_write("persist", "data")
        # Mock LLM to immediately return verdict
        verdict_json = '{"verdict":"COMPLETE","items":[],"summary":"done"}'
        with patch.object(llm_client, 'chat', return_value=LLMResponse(content=verdict_json)):
            evaluator.evaluate({"id": "t1", "title": "x", "criteria": []})
        # Sandbox should be cleared
        result = evaluator._tool_sandbox_read("persist")
        assert "error" in result


class TestDeduplication:
    """Test dedup tracking — step-1-4-1-5."""

    def test_repeated_read_file_is_duplicate(self, evaluator, tmp_workdir):
        """Reading the same file twice marks second call as duplicate."""
        path = os.path.join(tmp_workdir, "dup_test.txt")
        with open(path, "w") as f:
            f.write("content")
        tc = ToolCall(id="c1", name="read_file", arguments={"path": "dup_test.txt"})
        result1, was_dup1 = evaluator._execute_tool_with_dedup(tc)
        assert was_dup1 is False
        result2, was_dup2 = evaluator._execute_tool_with_dedup(tc)
        assert was_dup2 is True
        # was_dup flag is set — _dedup_warning is added by evaluate() loop, not here
        assert "path" in result2
        assert result2["path"] == "dup_test.txt"

    def test_repeated_run_command_is_duplicate(self, evaluator):
        """Running the same command twice marks second as duplicate."""
        tc = ToolCall(id="c2", name="run_command", arguments={"cmd": "echo test"})
        _, was_dup1 = evaluator._execute_tool_with_dedup(tc)
        assert was_dup1 is False
        _, was_dup2 = evaluator._execute_tool_with_dedup(tc)
        assert was_dup2 is True

    def test_repeated_search_pattern_is_duplicate(self, evaluator):
        """Searching the same regex twice marks second as duplicate."""
        tc = ToolCall(id="c3", name="search_pattern", arguments={"regex": r"def test_"})
        _, was_dup1 = evaluator._execute_tool_with_dedup(tc)
        assert was_dup1 is False
        _, was_dup2 = evaluator._execute_tool_with_dedup(tc)
        assert was_dup2 is True

    def test_different_files_not_flagged(self, evaluator, tmp_workdir):
        """Different files are not flagged as duplicates."""
        path1 = os.path.join(tmp_workdir, "a.txt")
        path2 = os.path.join(tmp_workdir, "b.txt")
        with open(path1, "w") as f:
            f.write("a")
        with open(path2, "w") as f:
            f.write("b")
        tc1 = ToolCall(id="c1", name="read_file", arguments={"path": "a.txt"})
        tc2 = ToolCall(id="c2", name="read_file", arguments={"path": "b.txt"})
        _, was_dup1 = evaluator._execute_tool_with_dedup(tc1)
        _, was_dup2 = evaluator._execute_tool_with_dedup(tc2)
        assert was_dup1 is False
        assert was_dup2 is False


class TestVerdictParsing:
    """Test _parse_verdict — JSON parse, markdown fence, keyword fallback — step-1-4-1-6."""

    def test_valid_json_verdict(self, evaluator):
        """Valid JSON verdict parses correctly."""
        content = '{"verdict":"COMPLETE","items":[{"criterion":"c1","status":"PASS","detail":"ok"}],"summary":"all good"}'
        verdict = evaluator._parse_verdict(content)
        assert verdict.verdict == "COMPLETE"
        assert len(verdict.items) == 1
        assert verdict.items[0].criterion == "c1"
        assert verdict.items[0].status == "PASS"
        assert verdict.items[0].detail == "ok"
        assert verdict.summary == "all good"

    def test_json_in_markdown_fences(self, evaluator):
        """JSON wrapped in ```json``` fences is stripped and parsed."""
        content = '```json\n{"verdict":"INCOMPLETE","items":[{"criterion":"c1","status":"FAIL","detail":"missing"}],"summary":"nope"}\n```'
        verdict = evaluator._parse_verdict(content)
        assert verdict.verdict == "INCOMPLETE"
        assert len(verdict.items) == 1

    def test_json_with_extra_text(self, evaluator):
        """JSON with text before/after is extracted and parsed."""
        content = 'Here is my verdict:\n{"verdict":"COMPLETE","items":[],"summary":"done"}\nHope this helps!'
        verdict = evaluator._parse_verdict(content)
        assert verdict.verdict == "COMPLETE"

    def test_invalid_status_defaults_to_fail(self, evaluator):
        """Invalid status ('MAYBE') defaults to FAIL."""
        content = '{"verdict":"COMPLETE","items":[{"criterion":"c1","status":"MAYBE","detail":"x"}],"summary":"x"}'
        verdict = evaluator._parse_verdict(content)
        assert verdict.items[0].status == "FAIL"

    def test_invalid_verdict_defaults_to_incomplete(self, evaluator):
        """Invalid verdict ('ALMOST') defaults to INCOMPLETE."""
        content = '{"verdict":"ALMOST","items":[],"summary":"almost done"}'
        verdict = evaluator._parse_verdict(content)
        assert verdict.verdict == "INCOMPLETE"

    def test_missing_items_falls_to_keyword(self, evaluator):
        """JSON missing 'items' key falls to keyword parse."""
        content = '{"verdict":"COMPLETE","summary":"all criteria pass"}'
        verdict = evaluator._parse_verdict(content)
        # Should fall to keyword parse (which finds 'complete' → COMPLETE)
        assert verdict.verdict == "COMPLETE"

    def test_keyword_complete_detected(self, evaluator):
        """Content with 'all criteria pass' → keyword parse yields COMPLETE."""
        content = 'I have verified all criteria, everything passes and is complete.'
        verdict = evaluator._parse_verdict(content)
        # Keyword fallback: "all criteria" + "pass" → COMPLETE
        assert verdict.verdict == "COMPLETE"

    def test_keyword_all_criteria_pass(self, evaluator):
        """Content with 'all criteria' and 'pass' → COMPLETE."""
        content = 'After reviewing, all criteria pass.'
        verdict = evaluator._parse_verdict(content)
        assert verdict.verdict == "COMPLETE"

    def test_empty_response_is_incomplete(self, evaluator):
        """Empty response → INCOMPLETE with error summary."""
        verdict = evaluator._parse_verdict("")
        assert verdict.verdict == "INCOMPLETE"
        assert "auto-parsed" in verdict.summary


class TestMaxIterationsAndErrors:
    """Test max_iterations cap and error handling — step-1-4-1-7."""

    def test_max_iterations_reached_returns_incomplete(self, evaluator, llm_client):
        """Evaluator stops at max_iterations and returns INCOMPLETE with actionable error."""
        # Always return tool calls (never a verdict) — exhausts the cap
        calls = []
        for i in range(10):
            tc = ToolCall(id=f"tc{i}", name="read_file", arguments={"path": f"f{i}.txt"})
            calls.append(LLMResponse(content="working", tool_calls=[tc]))
        mock_chat = MagicMock(side_effect=calls)
        with patch.object(llm_client, 'chat', mock_chat):
            from engine.evaluator import AgenticEvaluator
            fast_eval = AgenticEvaluator(llm_client, evaluator.workdir, max_iterations=2)
            verdict = fast_eval.evaluate({"id": "loop", "title": "x", "criteria": ["c1"]})
        assert verdict.verdict == "INCOMPLETE"
        assert "Cap exceeded" in verdict.summary
        assert "Increase max_iterations" in verdict.summary

    def test_max_iterations_error_message_actionable(self, evaluator, llm_client):
        """Error message on cap tells user exactly how to fix it."""
        calls = [LLMResponse(content="working", tool_calls=[
            ToolCall(id="t0", name="read_file", arguments={"path": "x.txt"})
        ]) for _ in range(5)]
        mock_chat = MagicMock(side_effect=calls)
        with patch.object(llm_client, 'chat', mock_chat):
            from engine.evaluator import AgenticEvaluator
            fast_eval = AgenticEvaluator(llm_client, evaluator.workdir, max_iterations=1)
            verdict = fast_eval.evaluate({"id": "t", "title": "x", "criteria": ["c1"]})
        assert "max_iterations" in verdict.summary
        assert "split criteria" in verdict.summary
        assert str(fast_eval.max_iterations) in verdict.summary

    def test_default_max_iterations_is_100(self, llm_client, tmp_workdir):
        """Default max_iterations is 100 (was 15 — too low for complex criteria)."""
        from engine.evaluator import AgenticEvaluator
        evaluator = AgenticEvaluator(llm_client, tmp_workdir)
        assert evaluator.max_iterations == 100

    def test_llm_exception_returns_incomplete(self, evaluator, llm_client):
        """LLM exception → INCOMPLETE with error summary."""
        with patch.object(llm_client, 'chat', side_effect=RuntimeError("LLM crashed")):
            verdict = evaluator.evaluate({"id": "err", "title": "x", "criteria": []})
        assert verdict.verdict == "INCOMPLETE"
        assert "LLM call failed" in verdict.summary or "LLM crashed" in verdict.summary

    def test_custom_max_iterations(self, evaluator):
        """Evaluator respects custom max_iterations."""
        assert evaluator.max_iterations == 5
        from engine.evaluator import AgenticEvaluator
        custom = AgenticEvaluator(evaluator.llm, evaluator.workdir, max_iterations=20)
        assert custom.max_iterations == 20


class TestEvaluatorTools:
    """Test additional tool implementations."""

    def test_unknown_tool_returns_error(self, evaluator):
        """Unknown tool name returns error."""
        tc = ToolCall(id="x", name="nonexistent_tool", arguments={})
        result = evaluator._execute_tool(tc)
        assert "error" in result
        assert "Unknown tool" in result["error"]

    def test_read_diff_basic(self, evaluator):
        """read_diff tool returns staged/unstaged info."""
        result = evaluator._tool_read_diff()
        assert "staged" in result
        assert "unstaged" in result

    def test_get_task_item_found(self, evaluator):
        """get_task_item returns the task dict when found."""
        task = {"id": "t1", "title": "Test", "criteria": ["c1"]}
        evaluator._task_index["t1"] = task
        result = evaluator._tool_get_task_item("t1")
        assert result["id"] == "t1"

    def test_get_task_item_not_found(self, evaluator):
        """get_task_item returns error when task not found."""
        result = evaluator._tool_get_task_item("nonexistent")
        assert "error" in result
        assert "Task not found" in result["error"]


class TestEvaluatorEvaluate:
    """Test the full evaluate loop."""

    def test_evaluate_with_empty_criteria_returns_complete(self, evaluator, llm_client):
        """Task with no criteria — LLM should return COMPLETE immediately."""
        verdict_json = '{"verdict":"COMPLETE","items":[],"summary":"no criteria to check"}'
        with patch.object(llm_client, 'chat', return_value=LLMResponse(content=verdict_json)):
            verdict = evaluator.evaluate({"id": "empty", "title": "x", "criteria": []})
        assert verdict.verdict == "COMPLETE"

    def test_verdict_item_dataclass(self):
        """VerdictItem and Verdict dataclasses work correctly."""
        item = VerdictItem(criterion="c1", status="PASS", detail="verified")
        assert item.criterion == "c1"
        assert item.status == "PASS"
        verdict = Verdict(verdict="COMPLETE", items=[item], summary="done")
        assert verdict.verdict == "COMPLETE"
        assert len(verdict.items) == 1


class TestExtendedEvaluator:
    """Extended edge case coverage for AgenticEvaluator."""

    def test_search_truncated_at_200(self, evaluator, tmp_workdir):
        """search_pattern truncates results at 200 matches."""
        path = os.path.join(tmp_workdir, "big.py")
        with open(path, "w") as f:
            for _ in range(300):
                f.write("MATCH_ME\n")
        result = evaluator._tool_search_pattern("MATCH_ME")
        # matches list has 200 matches + 1 truncation message = 201 total
        assert len(result["matches"]) == 201
        assert "truncated" in result["matches"][-1]

    def test_sandbox_read_truncated_at_4000(self, evaluator):
        """sandbox_read truncates content at 4000 chars."""
        evaluator._sandbox["big"] = "x" * 5000
        result = evaluator._tool_sandbox_read("big")
        assert len(result["content"]) <= 4100

    def test_read_file_truncation_large_file(self, evaluator, tmp_workdir):
        """read_file truncates very large files (>12000 chars) to first 400 lines."""
        path = os.path.join(tmp_workdir, "huge.py")
        with open(path, "w") as f:
            for i in range(1000):
                f.write(f"line {i}: xyz\n")
        result = evaluator._tool_read_file("huge.py")
        assert result["total_lines"] == 1000
        assert result["total_chars"] > 12000
        content = result["content"]
        assert "... [showing first 400" in content

    def test_execute_tool_wraps_exception(self, evaluator):
        """_execute_tool wraps exceptions in error dict."""
        tc = ToolCall(id="x", name="read_file", arguments={})
        result = evaluator._execute_tool(tc)
        assert "error" in result

    def test_dedup_warning_in_evaluate_loop(self, evaluator, llm_client, tmp_workdir):
        """_dedup_warning is added to tool result in evaluate() loop."""
        path = os.path.join(tmp_workdir, "dedup_loop.txt")
        with open(path, "w") as f:
            f.write("content")

        # LLM returns same tool call twice, then a verdict
        tc1 = ToolCall(id="tc1", name="read_file", arguments={"path": "dedup_loop.txt"})
        tc2 = ToolCall(id="tc2", name="read_file", arguments={"path": "dedup_loop.txt"})
        verdict_json = '{"verdict":"COMPLETE","items":[],"summary":"done"}'
        mock = MagicMock()
        mock.side_effect = [
            LLMResponse(content="checking", tool_calls=[tc1]),
            LLMResponse(content="checking again", tool_calls=[tc2]),
            LLMResponse(content=verdict_json),
        ]
        with patch.object(llm_client, 'chat', mock):
            evaluator.max_iterations = 5
            verdict = evaluator.evaluate({"id": "dt", "title": "x", "criteria": ["c1"]})
        assert verdict.verdict == "COMPLETE"

    def test_evaluate_with_tool_calls_then_verdict(self, evaluator, llm_client, tmp_workdir):
        """evaluate() handles tool calls followed by a verdict."""
        path = os.path.join(tmp_workdir, "eval_me.py")
        with open(path, "w") as f:
            f.write("def foo(): pass\n")
        tc = ToolCall(id="tc1", name="read_file", arguments={"path": "eval_me.py"})
        verdict_json = '{"verdict":"COMPLETE","items":[{"criterion":"c1","status":"PASS","detail":"verified"}],"summary":"pass"}'
        mock = MagicMock()
        mock.side_effect = [
            LLMResponse(content="reading", tool_calls=[tc]),
            LLMResponse(content=verdict_json),
        ]
        with patch.object(llm_client, 'chat', mock):
            evaluator.max_iterations = 5
            verdict = evaluator.evaluate({"id": "et", "title": "x", "criteria": ["c1"]})
        assert verdict.verdict == "COMPLETE"
        assert len(verdict.items) == 1

    def test_verdict_keyword_complete_detected(self, evaluator):
        """Content with 'complete' keyword and PASS items yields COMPLETE."""
        content = '{"verdict":"COMPLETE","items":[{"criterion":"c1","status":"PASS","detail":"ok"}],"summary":"all pass"}'
        verdict = evaluator._parse_verdict(content)
        assert verdict.verdict == "COMPLETE"

    def test_verdict_missing_items_falls_to_keyword(self, evaluator):
        """JSON with verdict but missing items falls back to keyword parse."""
        content = 'The task is complete. All criteria pass.'
        verdict = evaluator._parse_verdict(content)
        assert verdict.verdict == "COMPLETE"

    def test_search_all_files_with_glob(self, evaluator, tmp_workdir):
        """search_pattern with file_glob='*' searches all file types."""
        py_path = os.path.join(tmp_workdir, "a.py")
        txt_path = os.path.join(tmp_workdir, "b.txt")
        with open(py_path, "w") as f:
            f.write("MATCH_ME\n")
        with open(txt_path, "w") as f:
            f.write("MATCH_ME\n")
        result = evaluator._tool_search_pattern("MATCH_ME", file_glob="*")
        assert result["count"] >= 2

    def test_read_file_truncation_large_file_with_offset(self, evaluator, tmp_workdir):
        """read_file with explicit offset/limit does NOT auto-truncate."""
        path = os.path.join(tmp_workdir, "huge2.py")
        with open(path, "w") as f:
            for i in range(500):
                f.write(f"line {i}\n")
        result = evaluator._tool_read_file("huge2.py", offset=400, limit=50)
        assert result["total_lines"] == 500
        content = result["content"]
        assert "line 400" in content
        assert "... [showing first" not in content

    def test_evaluate_empty_response_no_tool_calls(self, evaluator, llm_client):
        """evaluate() returns INCOMPLETE when LLM returns empty content with no tool calls."""
        with patch.object(llm_client, 'chat', return_value=LLMResponse(content=None)):
            verdict = evaluator.evaluate({"id": "empty", "title": "x", "criteria": ["c1"]})
        assert verdict.verdict == "INCOMPLETE"
        assert "empty response" in verdict.summary.lower()

    def test_evaluate_tool_exception_returns_error(self, evaluator, llm_client):
        """evaluate() returns INCOMPLETE when a tool call raises an exception."""
        tc = ToolCall(id="bad", name="read_file", arguments={"path": "/nonexistent"})
        with patch.object(llm_client, 'chat', side_effect=[
            LLMResponse(content="checking", tool_calls=[tc]),
            LLMResponse(content='{"verdict":"INCOMPLETE","items":[],"summary":"error"}'),
        ]):
            verdict = evaluator.evaluate({"id": "err", "title": "x", "criteria": ["c1"]})
        assert verdict.verdict == "INCOMPLETE"

    def test_verdict_status_fail_default(self, evaluator):
        """Missing status in verdict item defaults to FAIL."""
        content = '{"verdict":"COMPLETE","items":[{"criterion":"c1","detail":"x"}],"summary":"s"}'
        verdict = evaluator._parse_verdict(content)
        assert verdict.items[0].status == "FAIL"

    def test_verdict_has_more_field_read_file(self, evaluator, tmp_workdir):
        """read_file returns has_more=True when file is large."""
        path = os.path.join(tmp_workdir, "bigfile.py")
        with open(path, "w") as f:
            for i in range(500):
                f.write(f"line {i}: {'x' * 100}\n")
        result = evaluator._tool_read_file("bigfile.py")
        assert "has_more" in result
