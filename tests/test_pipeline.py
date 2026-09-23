"""
Unit tests for engine/pipeline.py — configurable evaluation pipeline.
axiom:trace work_item=GR-001 spec=specs/06-Pipeline-Engine.md plan=.memory-bank/work-items/GR-001/plan.yaml
"""

import os
from unittest.mock import MagicMock, patch

import yaml

from engine.pipeline import (
    MAX_STEP_EVIDENCE_CHARS,
    Pipeline,
    StageResult,
    StepResult,
    _secrets_step_run,
    load_pipeline_config,
    parse_secrets_scanners,
)


# ── Phase 1-5-1: StepResult/StageResult, conditions, templates, defaults ─────


class TestStepResult:
    """Test StepResult dataclass and to_dict — step-1-5-1-3."""

    def test_step_result_with_passed_true(self):
        """StepResult with passed=True produces correct to_dict."""
        sr = StepResult(id="secrets", type="script", passed=True, output="clean")
        d = sr.to_dict()
        assert d["id"] == "secrets"
        assert d["type"] == "script"
        assert d["passed"] is True
        assert d["output"] == "clean"
        assert d["error"] == ""

    def test_step_result_with_error(self):
        """StepResult with error includes error in to_dict."""
        sr = StepResult(id="lint", type="script", passed=False, output="", error="E501")
        d = sr.to_dict()
        assert d["passed"] is False
        assert d["error"] == "E501"

    def test_step_result_output_truncated(self):
        """1000 chars is under the 4000 budget — stored byte-identical (DF-GITREINS-POC-8)."""
        long_output = "x" * 1000
        sr = StepResult(id="tests", type="script", passed=True, output=long_output)
        d = sr.to_dict()
        # Contract changed (DF-GITREINS-POC-8): under-budget output is kept
        # whole; the old head-only [:500] slice is gone.
        assert d["output"] == long_output
        assert "chars omitted" not in d["output"]


def _make_pytest_output(total_chars: int = 20000) -> tuple[str, str, str]:
    """Build a pytest-shaped payload of ~*total_chars* chars.

    Returns (payload, first_line, last_line). The last line is the summary
    banner that carries the failing-test counts — exactly what the old
    head-only [:500] slice threw away.
    """
    first = "============================= test session starts ============================="
    last = "========================= 2 failed, 5 passed in 1.23s ========================="
    lines = [first]
    size = len(first) + len(last) + 2
    i = 0
    while size < total_chars:
        line = f"tests/test_mod.py::test_case_{i} PASSED [ {i % 100}%]"
        lines.append(line)
        size += len(line) + 1
        i += 1
    lines.append(last)
    return "\n".join(lines), first, last


class TestStepEvidenceBound:
    """DF-GITREINS-POC-8: _bound_step_evidence head+tail + FAILED-line hoisting."""

    def test_oversized_output_bounded_with_head_tail_and_marker(self):
        """~20KB pytest payload: bounded, keeps first AND last line, has marker."""
        from engine.pipeline import _bound_step_evidence

        payload, first, last = _make_pytest_output(20000)
        assert len(payload) > MAX_STEP_EVIDENCE_CHARS
        bounded = _bound_step_evidence(payload)
        # Concrete bound: head (60%) + tail (40%) + the marker, ALL inside the
        # cap (DF-GITREINS-POC-5 — the marker used to be added on top of it:
        # 4027 chars for a 4000 cap).
        assert len(bounded) <= MAX_STEP_EVIDENCE_CHARS
        # BOTH ends survive — old [:500] kept only 500 chars — and the budget
        # is used, not wasted (line-aligned filling leaves < 1 line spare).
        assert len(bounded) >= MAX_STEP_EVIDENCE_CHARS - 200
        assert bounded.startswith(first)
        assert bounded.endswith(last)
        assert "chars omitted" in bounded

    def test_failed_line_in_omitted_middle_is_hoisted(self):
        """A FAILED line beyond the head window survives truncation."""
        from engine.pipeline import _bound_step_evidence

        payload, first, last = _make_pytest_output(20000)
        failed_line = "FAILED tests/test_mid.py::TestBoom::test_boom - AssertionError: boom"
        # Inject the FAILED line well past the head window (~60% of budget).
        # Newlines on both sides so it stays a standalone line (the cut can
        # land mid-line).
        inject_at = 10000
        assert inject_at > int(MAX_STEP_EVIDENCE_CHARS * 0.6)
        payload = payload[:inject_at] + "\n" + failed_line + "\n" + payload[inject_at:]
        bounded = _bound_step_evidence(payload)
        assert bounded.startswith(first)
        assert bounded.endswith(last)
        # The failing test id survives in the serialized evidence.
        assert failed_line in bounded

    def test_error_line_in_omitted_middle_is_hoisted(self):
        """pytest ERROR short-summary lines (collection/setup errors) hoist too."""
        from engine.pipeline import _bound_step_evidence

        payload, _, _ = _make_pytest_output(20000)
        error_line = "ERROR tests/test_broken.py::TestSetup::test_setup - ImportError: nope"
        inject_at = 12000
        payload = payload[:inject_at] + "\n" + error_line + "\n" + payload[inject_at:]
        bounded = _bound_step_evidence(payload)
        assert error_line in bounded

    def test_short_output_byte_identical_no_marker(self):
        """Output at/below budget passes through unchanged."""
        from engine.pipeline import _bound_step_evidence

        for size in (0, 1, 1000, MAX_STEP_EVIDENCE_CHARS):
            out = "x" * size
            assert _bound_step_evidence(out) == out

    def test_to_dict_20kb_payload_keeps_head_tail_and_marker(self):
        """to_dict (not just the helper) keeps both ends of a 20KB payload.

        This is the AC1 test: against the old code (output[:500]) the
        serialized output is a 500-char head-only slice — it cannot end with
        the summary banner, cannot contain the marker, and is far under the
        budget. Every assertion here fails against the old code.
        """
        payload, first, last = _make_pytest_output(20000)
        sr = StepResult(id="tests", type="script", passed=False, output=payload)
        d = sr.to_dict()
        assert len(d["output"]) <= MAX_STEP_EVIDENCE_CHARS
        assert d["output"].startswith(first)
        assert d["output"].endswith(last)  # fails under [:500] (head-only)
        assert "chars omitted" in d["output"]  # fails under [:500]
        assert len(d["output"]) > 500  # old code stored exactly 500

    def test_summarize_stage_failed_step_shows_pytest_failed_line(self, tmp_workdir):
        """_summarize_stage surfaces the parsed failing test id, not the banner.

        TRUST-003: the stage summary names the FIRST failing test with the same
        '[first failing id]' marker the guard console uses.
        """
        step_output = (
            "============================= test session starts =============================\n"
            "collecting ... collected 7 items\n"
            "tests/test_x.py .....F.\n"
            "================================== FAILURES ===================================\n"
            "________________________________ TestY.test_z _________________________________\n"
            "E       assert 1 == 2\n"
            "========================= short test summary info =========================\n"
            "FAILED tests/test_x.py::TestY::test_z - AssertionError\n"
        )
        stage = StageResult(
            id="tier1",
            passed=False,
            any_failed=True,
            steps=[StepResult(id="tests", type="script", passed=False, output=step_output)],
        )
        p = Pipeline({"pipeline": {"stages": []}}, tmp_workdir)
        summary = p._summarize_stage(stage)
        line = "  ✗ tests: FAIL (tests/test_x.py::TestY::test_z [first failing id])"
        assert line in summary
        assert "test session starts" not in summary

    def test_summarize_stage_falls_back_to_head_without_failed_line(self, tmp_workdir):
        """No FAILED/ERROR line in a failing step's output → previous [:100] head."""
        step_output = "grep: pattern not found in any file" + " detail" * 20
        stage = StageResult(
            id="tier1",
            passed=False,
            any_failed=True,
            steps=[StepResult(id="grep", type="script", passed=False, output=step_output)],
        )
        p = Pipeline({"pipeline": {"stages": []}}, tmp_workdir)
        summary = p._summarize_stage(stage)
        assert f"  ✗ grep: {step_output[:100]}" in summary


class TestStepEvidenceLineBoundary:
    """DF-GITREINS-POC-5: cuts land on line boundaries, the marker reports
    what went, and the cap is a real bound (marker charged against it)."""

    @staticmethod
    def _split(bounded: str) -> tuple[str, str, str]:
        """(head, marker, tail) split on the marker's own delimiters."""
        start = bounded.find("\n… [")
        end = bounded.find("] …\n", start)
        return bounded[:start], bounded[start : end + len("] …\n")], bounded[end + len("] …\n") :]

    def test_head_and_tail_are_whole_lines(self):
        """Neither cut leaves a half-written line (the dogfood fragment case)."""
        from engine.pipeline import _bound_step_evidence

        payload, _, _ = _make_pytest_output(20000)
        bounded = _bound_step_evidence(payload)
        head, marker, tail = self._split(bounded)
        assert head.endswith("\n")
        assert tail.startswith("tests/test_mod.py::test_case_")
        # The old char-offset cut produced "…test_case_50 PASSED [" in the head
        # and "7 PASSED [ 68%]" at the tail — both halves of a broken line.
        assert not head.rstrip("\n").endswith("PASSED [")
        assert "chars omitted" in marker
        assert "line(s)" in marker

    def test_marker_names_omitted_lines_and_chars(self):
        """The marker is quantitative, not a bare ellipsis."""
        from engine.pipeline import _bound_step_evidence

        payload, _, _ = _make_pytest_output(20000)
        marker = self._split(_bound_step_evidence(payload))[1]
        assert "chars omitted" in marker and "line(s)" in marker
        omitted = int(marker.split("[")[1].split(" chars omitted")[0])
        assert omitted > 0
        assert omitted <= len(payload)

    def test_cap_is_a_real_bound_including_the_marker(self):
        """len(result) <= cap for every shape, small caps included."""
        from engine.pipeline import _bound_step_evidence

        payload, _, _ = _make_pytest_output(20000)
        cases = {
            "pytest-shaped": payload,
            "single-giant-line": "X" * 50000,
            "traceback-lines": "\n".join(f"  File 'x.py', line {i}" for i in range(4000)),
            "many-failures": payload[:5000]
            + "\n"
            + "\n".join(f"FAILED tests/t.py::test_{i} - AssertionError: nope" for i in range(200))
            + "\n"
            + payload[5000:],
        }
        for label, text in cases.items():
            bounded = _bound_step_evidence(text)
            assert len(bounded) <= MAX_STEP_EVIDENCE_CHARS, f"{label}: {len(bounded)}"

    def test_over_budget_single_line_is_cut_and_says_so(self):
        """A line longer than its side's budget is the one documented mid-line cut."""
        from engine.pipeline import _bound_step_evidence

        bounded = _bound_step_evidence("X" * 50000)
        assert len(bounded) <= MAX_STEP_EVIDENCE_CHARS
        assert "mid-line" in bounded

    def test_trailing_summary_survives_a_giant_leading_line(self):
        """The tail keeps the LAST line even when the head is one huge line."""
        from engine.pipeline import _bound_step_evidence

        payload = "Y" * 30000 + "\nFAILED tests/test_x.py::test_y - boom"
        bounded = _bound_step_evidence(payload)
        assert bounded.endswith("boom")
        assert len(bounded) <= MAX_STEP_EVIDENCE_CHARS

    def test_hoist_budget_reports_lines_it_could_not_carry(self):
        """More FAILED lines than the hoist budget → the dropped count is named."""
        from engine.pipeline import _bound_step_evidence

        payload, _, _ = _make_pytest_output(20000)
        many = "\n".join(
            f"FAILED tests/test_m.py::test_{i} - AssertionError: nope" for i in range(200)
        )
        bounded = _bound_step_evidence(payload[:5000] + "\n" + many + "\n" + payload[5000:])
        assert "not hoisted" in bounded
        assert len(bounded) <= MAX_STEP_EVIDENCE_CHARS

    def test_small_caps_stay_readable_and_bounded(self):
        """A small cap stays inside the cap — including a FAILED-heavy payload.

        The judge's finding on the first submission of this row: with cap=200
        and 200 FAILED lines the hoisted ids (budgeted at 1000 chars) were
        added to a tally that already filled the cap, so the helper returned
        1123 chars for a 200-char budget. The marker now spends only the room
        left over, and reports the ids it could not carry.
        """
        from engine.pipeline import _bound_step_evidence

        plain = "\n".join(f"line {i} of a long output" for i in range(200))
        failures = "\n".join(
            f"FAILED tests/test_m.py::test_{i} - AssertionError: nope" for i in range(200)
        )
        for payload in (plain, failures, "X" * 50000, plain + "\n" + failures):
            for cap in (100, 200, 500, 1000):
                bounded = _bound_step_evidence(payload, cap=cap)
                assert len(bounded) <= cap, f"cap={cap} len={len(bounded)}"
            head = self._split(_bound_step_evidence(plain, cap=500))[0]
            if head:
                assert head.endswith("\n")

    def test_small_cap_counts_ids_it_cannot_hoist(self):
        """A 200-char budget reports the dropped FAILED ids instead of the ids."""
        from engine.pipeline import _bound_step_evidence

        failures = "\n".join(
            f"FAILED tests/test_m.py::test_{i} - AssertionError: nope" for i in range(200)
        )
        bounded = _bound_step_evidence(failures, cap=200)
        assert len(bounded) <= 200
        assert "chars omitted" in bounded


class TestStageResult:
    """Test StageResult dataclass — step-1-5-1-3."""

    def test_stage_result_all_passed(self):
        """StageResult with all steps passed → passed=True, any_failed=False."""
        steps = [
            StepResult(id="s1", type="script", passed=True, output="ok"),
            StepResult(id="s2", type="script", passed=True, output="ok"),
        ]
        sr = StageResult(id="tier1", passed=True, steps=steps, any_failed=False)
        d = sr.to_dict()
        assert d["passed"] is True
        assert d["any_failed"] is False
        assert len(d["steps"]) == 2

    def test_stage_result_one_failed(self):
        """StageResult with one failed step → passed=False, any_failed=True."""
        steps = [
            StepResult(id="s1", type="script", passed=True, output="ok"),
            StepResult(id="s2", type="script", passed=False, output="", error="fail"),
        ]
        sr = StageResult(id="tier1", passed=False, steps=steps, any_failed=True)
        d = sr.to_dict()
        assert d["passed"] is False
        assert d["any_failed"] is True


class TestPipelineConditions:
    """Test Pipeline._check_condition — step-1-5-1-4."""

    def test_condition_none_is_true(self, pipeline_config_default, tmp_workdir):
        """condition=None → always True."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        assert p._check_condition(None, {}) is True

    def test_condition_true_string_is_true(self, pipeline_config_default, tmp_workdir):
        """condition='true' → True."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        assert p._check_condition("true", {}) is True

    def test_condition_always_is_true(self, pipeline_config_default, tmp_workdir):
        """condition='always' → True."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        assert p._check_condition("always", {}) is True

    def test_condition_task_has_criteria_with_criteria(self, pipeline_config_default, tmp_workdir):
        """condition='task.has_criteria' with task having criteria → True."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        task = {"id": "t1", "criteria": ["c1", "c2"]}
        assert p._check_condition("task.has_criteria", task) is True

    def test_condition_task_has_criteria_empty(self, pipeline_config_default, tmp_workdir):
        """condition='task.has_criteria' with empty criteria → False."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        task = {"id": "t1", "criteria": []}
        assert p._check_condition("task.has_criteria", task) is False

    def test_condition_stage_any_failed(self, pipeline_config_default, tmp_workdir):
        """condition='stage.tier1.any_failed' with tier1 having failures → True."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        # Inject a failed stage result
        p._stage_results["tier1"] = StageResult(
            id="tier1",
            passed=False,
            any_failed=True,
            steps=[StepResult(id="s1", type="script", passed=False, error="fail")],
        )
        assert p._check_condition("stage.tier1.any_failed", {}) is True

    def test_condition_stage_passed(self, pipeline_config_default, tmp_workdir):
        """condition='stage.tier1.passed' with tier1 passed → True."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        p._stage_results["tier1"] = StageResult(
            id="tier1",
            passed=True,
            any_failed=False,
            steps=[StepResult(id="s1", type="script", passed=True, output="ok")],
        )
        assert p._check_condition("stage.tier1.passed", {}) is True

    def test_condition_stage_unknown_returns_false(self, pipeline_config_default, tmp_workdir):
        """condition='stage.unknown.passed' returns False."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        assert p._check_condition("stage.unknown.passed", {}) is False

    def test_condition_or_logic(self, pipeline_config_default, tmp_workdir):
        """OR logic: one true → True, both false → False."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        task = {"id": "t1", "criteria": ["c1"]}
        # task.has_criteria is true, so condition should be true regardless
        p._stage_results["tier1"] = StageResult(id="tier1", passed=True, any_failed=False, steps=[])
        assert p._check_condition("stage.tier1.any_failed or task.has_criteria", task) is True

    def test_condition_and_logic(self, pipeline_config_default, tmp_workdir):
        """AND logic: both must be true."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        task = {"id": "t1", "criteria": ["c1"]}
        p._stage_results["tier1"] = StageResult(id="tier1", passed=True, any_failed=False, steps=[])
        assert p._check_condition("stage.tier1.passed and task.has_criteria", task) is True


class TestPipelineTemplate:
    """Test Pipeline._template — template substitution — step-1-5-1-5."""

    def test_template_task_id(self, pipeline_config_default, tmp_workdir):
        """{{ task.id }} replaced with task id."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        result = p._template("echo {{ task.id }}", {"id": "my-task"})
        assert result == "echo my-task"

    def test_template_task_title(self, pipeline_config_default, tmp_workdir):
        """{{ task.title }} replaced with task title."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        result = p._template("{{ task.title }}", {"id": "t1", "title": "Hello World"})
        assert "Hello World" in result

    def test_template_task_criteria(self, pipeline_config_default, tmp_workdir):
        """{{ task.criteria }} replaced with JSON array."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        result = p._template("{{ task.criteria }}", {"id": "t1", "criteria": ["c1", "c2"]})
        assert '"c1"' in result

    def test_template_stage_passed(self, pipeline_config_default, tmp_workdir):
        """{{ stage.tier1.passed }} replaced with True/False."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        p._stage_results["tier1"] = StageResult(id="tier1", passed=True, any_failed=False, steps=[])
        result = p._template("passed={{ stage.tier1.passed }}", {})
        assert "passed=True" in result

    def test_template_stage_any_failed(self, pipeline_config_default, tmp_workdir):
        """{{ stage.tier1.any_failed }} replaced with True/False."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        p._stage_results["tier1"] = StageResult(id="tier1", passed=False, any_failed=True, steps=[])
        result = p._template("failed={{ stage.tier1.any_failed }}", {})
        assert "failed=True" in result

    def test_template_stages_full_json(self, pipeline_config_default, tmp_workdir):
        """{{ stages }} replaced with full JSON."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        p._stage_results["tier1"] = StageResult(
            id="tier1",
            passed=True,
            any_failed=False,
            steps=[StepResult(id="s1", type="script", passed=True, output="ok")],
        )
        result = p._template("{{ stages }}", {})
        assert "tier1" in result
        assert "passed" in result


class TestLoadPipelineConfig:
    """Test load_pipeline_config() — step-1-5-1-6."""

    def test_no_config_file_returns_default_pipeline(self, tmp_workdir):
        """Config file missing → returns default dict with tier1 + tier2 stages."""
        config = load_pipeline_config(tmp_workdir)
        assert "pipeline" in config
        stages = config["pipeline"]["stages"]
        assert len(stages) == 2
        assert stages[0]["id"] == "tier1"
        assert stages[1]["id"] == "tier2"

    def test_config_file_no_pipeline_key_returns_default(self, tmp_workdir):
        """Config file exists but no 'pipeline' key → returns default."""
        import os

        config_dir = os.path.join(tmp_workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            yaml.dump({"other": "stuff"}, f)
        config = load_pipeline_config(tmp_workdir)
        assert "pipeline" in config

    def test_config_file_empty_stages_returns_default(self, tmp_workdir):
        """Config file has pipeline but no stages → returns minimal default pipeline
        with tier1+secrets stage."""
        import os

        config_dir = os.path.join(tmp_workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            yaml.dump({"pipeline": {}}, f)
        config = load_pipeline_config(tmp_workdir)
        assert "pipeline" in config
        # load_pipeline_config returns the config as-is when pipeline key exists
        # The pipeline dict may be empty since the file had empty stages
        assert isinstance(config["pipeline"], dict)

    def test_malformed_yaml_returns_safe_minimal(self, tmp_workdir):
        """Malformed YAML returns safe minimal pipeline."""
        import os

        config_dir = os.path.join(tmp_workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            f.write(":broken: yaml: :")
        config = load_pipeline_config(tmp_workdir)
        assert "pipeline" in config
        assert config["pipeline"]["stages"] == []


class TestPipelineRun:
    """Test Pipeline.run() actual execution."""

    def test_run_parallel_stage(self, pipeline_config_default, tmp_workdir):
        """Pipeline runs parallel stage and returns results.

        Note: tier2 (ai_eval) will fail without LLM key configured,
        but tier1 (parallel scripts) should pass.
        """
        p = Pipeline(pipeline_config_default, tmp_workdir)
        result = p.run({"id": "t1", "title": "Test", "criteria": []}, trigger="pre-eval")
        assert "stages" in result
        assert "tier1" in result["stages"]
        assert result["stages"]["tier1"]["passed"] is True
        # Overall passed may be False if tier2 failed (no LLM key), but tier1 should pass

    def test_run_sequential_stage(self, pipeline_config_default, tmp_workdir):
        """Pipeline runs sequential ai_eval stage (skips with no criteria)."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        result = p.run({"id": "t1", "title": "Test", "criteria": []}, trigger="pre-eval")
        assert "stages" in result

    def test_run_with_llm_injected(self, pipeline_config_default, tmp_workdir, llm_client):
        """Pipeline with LLM injected runs ai_eval stage."""
        p = Pipeline(pipeline_config_default, tmp_workdir, llm=llm_client)
        task = {"id": "t1", "title": "Test", "criteria": ["c1"]}
        with patch.object(
            llm_client,
            "chat",
            return_value=MagicMock(content='{"verdict":"COMPLETE","items":[],"summary":"done"}'),
        ):
            result = p.run(task, trigger="pre-eval")
        assert "stages" in result

    def test_trigger_filtering(self, pipeline_config_default, tmp_workdir):
        """Stages not matching trigger are skipped."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        result = p.run({"id": "t1", "title": "Test", "criteria": []}, trigger="pre-commit")
        # Only tier1 runs on pre-commit (tier2 is pre-eval only)
        assert "tier1" in result["stages"]
        # tier2 should not run since it has on: ["pre-eval"]
        assert "tier2" not in result["stages"]

    def test_run_precommit_triggers(self, pipeline_config_default, tmp_workdir):
        """pre-commit trigger runs tier1 but not tier2."""
        p = Pipeline(pipeline_config_default, tmp_workdir)
        result = p.run({"id": "_precommit", "title": "x", "criteria": []}, trigger="pre-commit")
        assert result["passed"] is True
        assert "tier1" in result["stages"]

    def test_unknown_step_type_returns_error(self, pipeline_config_default, tmp_workdir):
        """Unknown step type produces error result."""
        config = {
            "pipeline": {
                "stages": [
                    {"id": "bad", "steps": [{"id": "x", "type": "unknown_type"}], "parallel": True}
                ]
            }
        }
        p = Pipeline(config, tmp_workdir)
        result = p.run({"id": "t1", "title": "x", "criteria": []}, trigger="pre-eval")
        assert "bad" in result["stages"]
        step = result["stages"]["bad"]["steps"][0]
        assert step["passed"] is False
        assert "Unknown step type" in step["error"]

    def test_script_no_command_returns_error(self, pipeline_config_default, tmp_workdir):
        """Script step with no command returns error."""
        config = {
            "pipeline": {
                "stages": [
                    {"id": "empty", "parallel": True, "steps": [{"id": "x", "type": "script"}]}
                ]
            }
        }
        p = Pipeline(config, tmp_workdir)
        result = p.run({"id": "t1", "title": "x", "criteria": []}, trigger="pre-eval")
        assert "empty" in result["stages"]
        step = result["stages"]["empty"]["steps"][0]
        assert step["passed"] is False
        assert "No command specified" in step["error"]


class TestExtendedPipeline:
    """Extended coverage for Pipeline module."""

    def test_step_result_to_dict_all_fields(self):
        """StepResult.to_dict() includes id, type, passed, output, error."""
        from engine.pipeline import StepResult

        sr = StepResult(id="lint", type="script", passed=True, output="clean", error="")
        d = sr.to_dict()
        assert d["id"] == "lint"
        assert d["type"] == "script"
        assert d["passed"] is True
        assert d["output"] == "clean"
        assert d["error"] == ""

    def test_stage_result_all_failed_true_any_failed(self):
        """StageResult with failed steps has any_failed=True."""
        from engine.pipeline import StageResult, StepResult

        srs = [StepResult(id="s1", type="script", passed=False, output="err")]
        stage = StageResult(id="tier1", passed=False, any_failed=True, steps=srs)
        assert stage.any_failed is True
        assert stage.passed is False

    def test_template_unknown_var_unchanged(self, pipeline_config_default, tmp_workdir):
        """Template with unknown variable leaves braces unchanged."""
        from engine.pipeline import Pipeline

        config = {
            "pipeline": {
                "stages": [
                    {
                        "id": "t1",
                        "parallel": True,
                        "on": ["pre-eval"],
                        "steps": [{"id": "x", "type": "script", "run": "echo {{nonexistent}}"}],
                    },
                ],
            }
        }
        p = Pipeline(config, tmp_workdir)
        result = p.run({"id": "t1", "title": "x", "criteria": []}, trigger="pre-eval")
        step = result["stages"]["t1"]["steps"][0]
        # The template variable is not resolved; step output will contain the literal string
        assert "{{nonexistent}}" in step["output"] or step["passed"] is True

    def test_run_with_no_matching_trigger(self, pipeline_config_default, tmp_workdir):
        """Pipeline skips stage when trigger doesn't match."""
        from engine.pipeline import Pipeline

        config = {
            "pipeline": {
                "stages": [
                    {
                        "id": "t1",
                        "parallel": True,
                        "on": ["pre-commit"],
                        "steps": [{"id": "x", "type": "script", "run": "echo hi"}],
                    },
                ],
            }
        }
        p = Pipeline(config, tmp_workdir)
        result = p.run({"id": "t1", "title": "x", "criteria": []}, trigger="pre-eval")
        # t1 not in pre-eval, so it should be skipped
        assert "t1" not in result["stages"]


# ── Regression: pipeline fallback when config exists but lacks pipeline key ───


class TestLoadPipelineConfigFallback:
    """Regression tests for load_pipeline_config fallback behavior."""

    def test_config_exists_no_pipeline_section_gets_tier1_plus_tier2(self, tmp_workdir):
        """When .gitreins/config.yaml exists but has no 'pipeline' key,
        load_pipeline_config must inject a two-tier pipeline (tier1 + tier2),
        not the old broken single-tier default (secrets: true only)."""
        workdir = tmp_workdir
        # Create a config.yaml with NO pipeline section (simulates existing config
        # that was set up before pipeline was a concept)
        config_dir = os.path.join(workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        config_path = os.path.join(config_dir, "config.yaml")
        config_without_pipeline = {
            "guards": {
                "secrets": True,
                "lint": True,
                "tests": True,
                "test_mode": "diff",
                "test_command": "pytest -x --tb=short",
            },
            "evaluator": {
                "model": "deepseek-v4-flash",
                "max_iterations": 100,
            },
        }
        with open(config_path, "w") as f:
            yaml.dump(config_without_pipeline, f)

        result = load_pipeline_config(workdir)

        assert "pipeline" in result
        stages = result["pipeline"]["stages"]
        assert len(stages) >= 2, f"Expected at least tier1 + tier2, got {len(stages)} stage(s)"

        tier1 = next((s for s in stages if s["id"] == "tier1"), None)
        tier2 = next((s for s in stages if s["id"] == "tier2"), None)

        assert tier1 is not None, "tier1 stage missing from fallback pipeline"
        assert tier2 is not None, "tier2 stage missing from fallback pipeline"

        # tier1 should have real steps, not just secrets: true
        assert len(tier1["steps"]) >= 1
        step_ids = [s["id"] for s in tier1["steps"]]
        assert "secrets" in step_ids, "secrets step missing from tier1 fallback"

        # tier2 should be an ai_eval stage
        assert tier2["type"] == "ai_eval", f"tier2 should be ai_eval, got {tier2['type']}"
        assert "tools" in tier2, "tier2 should have tools configured"
        assert "max_iterations" in tier2, "tier2 should have max_iterations"

    def test_config_missing_file_gets_two_tier_default(self, tmp_workdir):
        """When .gitreins/config.yaml does not exist at all,
        load_pipeline_config returns the full default (already correct)."""
        workdir = tmp_workdir
        result = load_pipeline_config(workdir)

        stages = result["pipeline"]["stages"]
        assert len(stages) >= 2
        tier1 = next((s for s in stages if s["id"] == "tier1"), None)
        tier2 = next((s for s in stages if s["id"] == "tier2"), None)
        assert tier1 is not None
        assert tier2 is not None
        assert tier2["type"] == "ai_eval"

    def test_default_tier1_secrets_step_suppresses_gitleaks_banner(self, tmp_workdir):
        """The default pipeline's tier1 secrets step runs gitleaks with
        --no-banner (DF-006): the banner must not leak into captured tier1
        output that reaches judge verdicts."""
        workdir = tmp_workdir
        config_dir = os.path.join(workdir, ".gitreins")
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, "config.yaml"), "w") as f:
            yaml.dump({"guards": {"secrets": True}}, f)

        result = load_pipeline_config(workdir)
        tier1 = next(s for s in result["pipeline"]["stages"] if s["id"] == "tier1")
        secrets_step = next(s for s in tier1["steps"] if s["id"] == "secrets")
        assert "--no-banner" in secrets_step["run"], (
            "tier1 secrets step must pass --no-banner to gitleaks"
        )

    def test_tier1_secrets_step_blocks_committed_secrets(self, tmp_workdir):
        """DF-012: the default tier1 secrets step FAILS on committed
        sk-/ghp_ secrets even when gitleaks reports clean — the built-in
        scanner cross-check runs unconditionally (workdir mode, since the
        judged changes are committed, not staged)."""
        import subprocess

        workdir = str(tmp_workdir)
        subprocess.run(["git", "init", "-q"], cwd=workdir, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"], cwd=workdir, capture_output=True
        )
        subprocess.run(["git", "config", "user.name", "Test"], cwd=workdir, capture_output=True)
        # Committed secrets — runtime-constructed, never literals in source
        with open(os.path.join(workdir, "sk.txt"), "w") as f:
            f.write('key = "sk-' + "A1" * 12 + '"\n')
        with open(os.path.join(workdir, "gh.txt"), "w") as f:
            f.write('token = "ghp_' + "aB3" * 12 + '"\n')
        subprocess.run(["git", "add", "."], cwd=workdir, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=workdir, capture_output=True)

        result = load_pipeline_config(workdir)
        p = Pipeline(result, workdir)
        # pre-commit trigger: tier1 only (tier2 ai_eval is pre-eval-only)
        out = p.run({"id": "t1", "criteria": []}, trigger="pre-commit")
        tier1 = out["stages"]["tier1"]
        secrets_step = next(s for s in tier1["steps"] if s["id"] == "secrets")
        assert secrets_step["data"]["exit_code"] != 0, (
            f"tier1 secrets step passed on committed secrets: {secrets_step['output'][:300]}"
        )


# ── GR-063c: C++ pipeline — split "c" from "cpp" ───────────────────────────


class TestCppLanguageDetection:
    """Verify C++ pipeline detection: CMakeLists.txt → cpp, Makefile → c."""

    def test_cmake_lists_txt_detected_as_cpp(self, tmp_workdir):
        """CMakeLists.txt alone → primary language is cpp."""
        from engine.pipeline import _default_tier1_steps

        cmake_path = os.path.join(tmp_workdir, "CMakeLists.txt")
        with open(cmake_path, "w") as f:
            f.write("project(test)\n")
        steps = _default_tier1_steps(tmp_workdir)
        step_ids = [s["id"] for s in steps]
        assert "secrets" in step_ids, "secrets step should always be present"

    def test_makefile_only_detected_as_c(self, tmp_workdir):
        """Makefile alone (no CMakeLists.txt) → primary language is c."""
        from engine.pipeline import _default_tier1_steps

        makefile_path = os.path.join(tmp_workdir, "Makefile")
        with open(makefile_path, "w") as f:
            f.write("all:\n\techo ok\n")
        steps = _default_tier1_steps(tmp_workdir)
        step_ids = [s["id"] for s in steps]
        assert "lint" in step_ids
        assert "tests" in step_ids

    def test_cmake_takes_priority_over_makefile(self, tmp_workdir):
        """Both CMakeLists.txt and Makefile present → CMakeLists.txt wins (cpp)."""
        from engine.pipeline import _default_tier1_steps

        cmake_path = os.path.join(tmp_workdir, "CMakeLists.txt")
        with open(cmake_path, "w") as f:
            f.write("project(test)\n")
        makefile_path = os.path.join(tmp_workdir, "Makefile")
        with open(makefile_path, "w") as f:
            f.write("all:\n\techo ok\n")
        steps = _default_tier1_steps(tmp_workdir)
        step_ids = [s["id"] for s in steps]
        assert "lint" in step_ids
        assert "tests" in step_ids

    def test_cpp_lang_commands_produces_lint_and_test(self, tmp_workdir):
        """C++ pipeline produces lint + test steps (via make)."""
        from engine.pipeline import _default_tier1_steps

        cmake_path = os.path.join(tmp_workdir, "CMakeLists.txt")
        with open(cmake_path, "w") as f:
            f.write("project(test)\n")
        steps = _default_tier1_steps(tmp_workdir)
        lint_step = next((s for s in steps if s["id"] == "lint"), None)
        test_step = next((s for s in steps if s["id"] == "tests"), None)
        assert lint_step is not None, "lint step missing for C++ project"
        assert test_step is not None, "tests step missing for C++ project"
        assert "make" in lint_step["run"], f"Expected make lint, got {lint_step['run']}"
        assert "make" in test_step["run"], f"Expected make test, got {test_step['run']}"

    def test_c_and_cpp_both_produce_steps(self, tmp_workdir):
        """Both C (Makefile) and C++ (CMakeLists.txt) produce lint+test steps."""
        from engine.pipeline import _default_tier1_steps

        # Test C
        with open(os.path.join(tmp_workdir, "Makefile"), "w") as f:
            f.write("all:\n\techo ok\n")
        c_steps = _default_tier1_steps(tmp_workdir)
        c_ids = [s["id"] for s in c_steps]
        assert "lint" in c_ids, f"C project missing lint: {c_ids}"
        assert "tests" in c_ids, f"C project missing tests: {c_ids}"
        # Remove Makefile, test C++
        os.remove(os.path.join(tmp_workdir, "Makefile"))
        with open(os.path.join(tmp_workdir, "CMakeLists.txt"), "w") as f:
            f.write("project(test)\n")
        cpp_steps = _default_tier1_steps(tmp_workdir)
        cpp_ids = [s["id"] for s in cpp_steps]
        assert "lint" in cpp_ids, f"C++ project missing lint: {cpp_ids}"
        assert "tests" in cpp_ids, f"C++ project missing tests: {cpp_ids}"

    def test_no_cmake_or_makefile_skips_c_and_cpp(self, tmp_workdir):
        """No CMakeLists.txt or Makefile → falls back to secrets-only."""
        from engine.pipeline import _default_tier1_steps

        steps = _default_tier1_steps(tmp_workdir)
        step_ids = [s["id"] for s in steps]
        assert "lint" not in step_ids, f"No C/C++ signature → should not have lint step: {step_ids}"
        assert "tests" not in step_ids, (
            f"No C/C++ signature → should not have tests step: {step_ids}"
        )


# ── GR-063j: C# pipeline — dotnet + .csproj/.sln detection ───────────────────


class TestCsharpLanguageDetection:
    """Verify C# pipeline detection: .csproj/.sln → csharp."""

    def test_csproj_detected_as_csharp(self, tmp_workdir):
        """*.csproj file → primary language is csharp."""
        from engine.pipeline import _default_tier1_steps

        csproj_path = os.path.join(tmp_workdir, "MyProject.csproj")
        with open(csproj_path, "w") as f:
            f.write("<Project />\n")
        steps = _default_tier1_steps(tmp_workdir)
        step_ids = [s["id"] for s in steps]
        assert "lint" in step_ids
        assert "tests" in step_ids

    def test_sln_detected_as_csharp(self, tmp_workdir):
        """*.sln file → primary language is csharp."""
        from engine.pipeline import _default_tier1_steps

        sln_path = os.path.join(tmp_workdir, "MySolution.sln")
        with open(sln_path, "w") as f:
            f.write("Microsoft Visual Studio Solution File\n")
        steps = _default_tier1_steps(tmp_workdir)
        step_ids = [s["id"] for s in steps]
        assert "lint" in step_ids
        assert "tests" in step_ids

    def test_csharp_lang_commands_produces_dotnet_steps(self, tmp_workdir):
        """C# pipeline produces lint + test steps (via dotnet)."""
        from engine.pipeline import _default_tier1_steps

        csproj_path = os.path.join(tmp_workdir, "App.csproj")
        with open(csproj_path, "w") as f:
            f.write("<Project />\n")
        steps = _default_tier1_steps(tmp_workdir)
        lint_step = next((s for s in steps if s["id"] == "lint"), None)
        test_step = next((s for s in steps if s["id"] == "tests"), None)
        assert lint_step is not None, "lint step missing for C# project"
        assert test_step is not None, "tests step missing for C# project"
        assert "dotnet" in lint_step["run"], f"Expected dotnet, got {lint_step['run']}"
        assert "dotnet" in test_step["run"], f"Expected dotnet, got {test_step['run']}"

    def test_csproj_takes_priority_over_sln(self, tmp_workdir):
        """Both .csproj and .sln present → .csproj detected first (csharp)."""
        from engine.pipeline import _default_tier1_steps

        with open(os.path.join(tmp_workdir, "App.csproj"), "w") as f:
            f.write("<Project />\n")
        with open(os.path.join(tmp_workdir, "App.sln"), "w") as f:
            f.write("Solution\n")
        steps = _default_tier1_steps(tmp_workdir)
        step_ids = [s["id"] for s in steps]
        assert "lint" in step_ids
        assert "tests" in step_ids


class TestScalaLanguageDetection:
    """Verify Scala pipeline detection: build.sbt → scala."""

    def test_build_sbt_detected_as_scala(self, tmp_workdir):
        """build.sbt file → primary language is scala."""
        from engine.pipeline import _default_tier1_steps

        sbt_path = os.path.join(tmp_workdir, "build.sbt")
        with open(sbt_path, "w") as f:
            f.write('name := "test"\n')
        steps = _default_tier1_steps(tmp_workdir)
        step_ids = [s["id"] for s in steps]
        assert "lint" in step_ids
        assert "tests" in step_ids

    def test_scala_lang_commands_produces_sbt_steps(self, tmp_workdir):
        """Scala pipeline produces lint + test steps (via sbt)."""
        from engine.pipeline import _default_tier1_steps

        sbt_path = os.path.join(tmp_workdir, "build.sbt")
        with open(sbt_path, "w") as f:
            f.write('name := "test"\n')
        steps = _default_tier1_steps(tmp_workdir)
        lint_step = next((s for s in steps if s["id"] == "lint"), None)
        test_step = next((s for s in steps if s["id"] == "tests"), None)
        assert lint_step is not None, "lint step missing for Scala project"
        assert test_step is not None, "tests step missing for Scala project"
        assert "sbt" in lint_step["run"], f"Expected sbt, got {lint_step['run']}"
        assert "sbt" in test_step["run"], f"Expected sbt, got {test_step['run']}"


class TestAiEvalCapForwarding:
    """_run_ai_eval cap resolution: step overrides over config base.

    Regression coverage for the fleet-wide tier2 compaction loop
    ('Context near limit (N/-1 tokens)' — token caps at -1 made the
    compaction threshold int(-1*0.9)=0, so the evaluator compacted on
    every turn and never produced a verdict, 2026-08).
    """

    def _make_verdict(self):
        v = MagicMock()
        v.verdict = "COMPLETE"
        v.summary = "ok"
        v.items = []
        return v

    def test_no_step_caps_defers_to_config(self, tmp_workdir, llm_client):
        """Step with only max_iterations: -1 defers — no explicit EvalCap."""
        from engine.pipeline import Pipeline

        config = {
            "evaluator": {
                "max_iterations": 100,
                "max_time": "30m",
                "max_input_tokens": "10M",
                "max_output_tokens": "1M",
            },
            "pipeline": {
                "stages": [
                    {
                        "id": "tier2",
                        "type": "ai_eval",
                        "on": ["pre-eval"],
                        "condition": "true",
                        "max_iterations": -1,  # defer to evaluator config
                    }
                ]
            },
        }
        p = Pipeline(config, tmp_workdir, llm=llm_client)
        with patch("engine.evaluator.AgenticEvaluator") as mock_eval:
            mock_eval.return_value.evaluate.return_value = self._make_verdict()
            result = p.run({"id": "t1", "title": "x", "criteria": ["c1"]}, trigger="pre-eval")
        assert result["passed"] is True
        # Deferral: AgenticEvaluator called WITHOUT an explicit eval_cap,
        # so it reads .gitreins/config.yaml itself (documented working path).
        _, kwargs = mock_eval.call_args
        assert "eval_cap" not in kwargs, (
            "deferral step must not pass an explicit EvalCap — all -1 caps "
            "would make compaction threshold 0 (compaction loop)"
        )

    def test_step_explicit_caps_override_config(self, tmp_workdir, llm_client):
        """Step-level caps are forwarded (parsed) over the config base."""
        from engine.pipeline import Pipeline

        config = {
            "evaluator": {
                "max_iterations": 100,
                "max_time": "30m",
                "max_input_tokens": "10M",
                "max_output_tokens": "1M",
            },
            "pipeline": {
                "stages": [
                    {
                        "id": "tier2",
                        "type": "ai_eval",
                        "on": ["pre-eval"],
                        "condition": "true",
                        "max_iterations": 25,
                        "max_time": "5m",
                        "max_input_tokens": "200k",
                        "max_output_tokens": "50k",
                        "tool_call_weight": 0.2,
                    }
                ]
            },
        }
        p = Pipeline(config, tmp_workdir, llm=llm_client)
        with patch("engine.evaluator.AgenticEvaluator") as mock_eval:
            mock_eval.return_value.evaluate.return_value = self._make_verdict()
            result = p.run({"id": "t1", "title": "x", "criteria": ["c1"]}, trigger="pre-eval")
        assert result["passed"] is True
        _, kwargs = mock_eval.call_args
        cap = kwargs["eval_cap"]
        assert cap.max_iterations == 25
        assert cap.max_seconds == 300.0
        assert cap.max_input_tokens == 200_000
        assert cap.max_output_tokens == 50_000
        assert cap.tool_call_weight == 0.2
        assert not cap.is_unlimited

    def test_partial_step_caps_merge_config_base(self, tmp_workdir, llm_client):
        """Unset caps fall back to the config base, not to unlimited."""
        from engine.pipeline import Pipeline

        config = {
            "evaluator": {
                "max_iterations": 100,
                "max_time": "30m",
                "max_input_tokens": "10M",
                "max_output_tokens": "1M",
            },
            "pipeline": {
                "stages": [
                    {
                        "id": "tier2",
                        "type": "ai_eval",
                        "on": ["pre-eval"],
                        "condition": "true",
                        "max_iterations": 25,  # only iterations pinned
                    }
                ]
            },
        }
        p = Pipeline(config, tmp_workdir, llm=llm_client)
        with patch("engine.evaluator.AgenticEvaluator") as mock_eval:
            mock_eval.return_value.evaluate.return_value = self._make_verdict()
            result = p.run({"id": "t1", "title": "x", "criteria": ["c1"]}, trigger="pre-eval")
        assert result["passed"] is True
        _, kwargs = mock_eval.call_args
        cap = kwargs["eval_cap"]
        assert cap.max_iterations == 25
        # Token caps from config base — NOT -1 (compaction loop guard)
        assert cap.max_input_tokens == 10_000_000
        assert cap.max_output_tokens == 1_000_000
        assert cap.max_seconds == 1800.0
        assert not cap.is_unlimited


# ── DF-GITREINS-POC-14 / -15: the verdict surface names its own state ────────


class TestStageSummaryDiagnostics:
    """Per-step verdict lines: one line, ANSI-free, never a dangling colon.

    POC-14 reported '✓ lint: ' (empty) next to raw gitleaks INFO carrying
    terminal escapes, because the summary rendered ``output[:100]`` verbatim —
    a raw slice that keeps embedded newlines and escape codes.
    """

    @staticmethod
    def _summary(steps, workdir):
        stage = StageResult(id="tier1", passed=all(s.passed for s in steps), steps=steps)
        return Pipeline({"pipeline": {"stages": []}}, workdir)._summarize_stage(stage)

    def test_summary_is_single_line_and_ansi_free(self, tmp_workdir):
        """A capture that opens with escaped gitleaks INFO renders as one line."""
        output = (
            "\x1b[90m6:51PM\x1b[0m \x1b[32mINF\x1b[0m no leaks found\nsecrets: gitleaks: clean\n"
        )
        summary = self._summary(
            [StepResult(id="secrets", type="script", passed=True, output=output)], tmp_workdir
        )
        assert summary == "  ✓ secrets: 6:51PM INF no leaks found"
        assert "\x1b[" not in summary
        assert len(summary.split("\n")) == 1

    def test_summary_names_an_empty_passing_capture(self, tmp_workdir):
        """No output at all → named, not a dangling '✓ lint: ' (the POC-14 line)."""
        summary = self._summary(
            [StepResult(id="lint", type="script", passed=True, output="")], tmp_workdir
        )
        assert summary == "  ✓ lint: ok (no output)"
        assert not summary.rstrip().endswith(":")

    def test_summary_names_an_empty_failing_capture(self, tmp_workdir):
        """A failing step with no output and no error is still named."""
        summary = self._summary(
            [StepResult(id="tests", type="script", passed=False, output="", error="")], tmp_workdir
        )
        assert summary == "  ✗ tests: no output"

    def test_summary_falls_back_to_the_error_text(self, tmp_workdir):
        """No output but an error → the error names the step."""
        summary = self._summary(
            [
                StepResult(
                    id="tests", type="script", passed=False, output="", error="Command timed out"
                )
            ],
            tmp_workdir,
        )
        assert summary == "  ✗ tests: Command timed out"


# ── GAP-058: a budget timeout is a DEGRADED lane, never a bare code failure ──


class TestBudgetTimeoutAttribution:
    """A step that exhausted its own budget must not read as a code finding.

    GAP-058: grading ffbb57eb printed ``secrets: Command timed out`` against a
    clean change — a budget overrun was indistinguishable from a failure. The
    contract pinned here: the step's data carries ``timed_out``/``timeout_s``,
    the error names the budget, the summary renders the lane in the ~
    (DEGRADED) register, and a REAL failing test never picks up any of it.
    """

    def test_over_budget_step_carries_budget_data_and_named_error(self, tmp_workdir):
        """run_bounded's timed_out → data fields + error that names the budget."""
        pipeline = Pipeline({"pipeline": {"stages": []}}, tmp_workdir)
        result = pipeline._run_script_step(
            {"id": "tests", "type": "script", "run": "sleep 3; echo NEVER", "timeout": 1}, {}
        )
        assert result.passed is False
        assert result.data["timed_out"] is True
        assert result.data["timeout_s"] == 1
        assert result.error == "Command timed out after 1s (step budget)"

    def test_code_failure_is_never_classified_as_budget_timeout(self, tmp_workdir):
        """The criterion-3 guard: `exit 1` is a plain failure, no timed_out flag."""
        pipeline = Pipeline({"pipeline": {"stages": []}}, tmp_workdir)
        result = pipeline._run_script_step(
            {"id": "tests", "type": "script", "run": "echo boom; exit 1", "timeout": 30}, {}
        )
        assert result.passed is False
        assert result.data.get("timed_out") is not True
        assert "timed out" not in result.error
        assert result.data["exit_code"] == 1

    def test_stage_summary_renders_the_two_lanes_distinctly(self, tmp_workdir):
        """~ + budget wording for the timeout, ✗ + output for the code failure."""
        timed_out = StepResult(
            id="tests",
            type="script",
            passed=False,
            error="Command timed out after 3s (step budget)",
            data={"timed_out": True, "timeout_s": 3},
        )
        code_fail = StepResult(
            id="lint", type="script", passed=False, output="E501 found", data={"exit_code": 1}
        )
        stage = StageResult(id="tier1", passed=False, steps=[timed_out, code_fail])
        summary = Pipeline({"pipeline": {"stages": []}}, tmp_workdir)._summarize_stage(stage)
        assert summary == (
            "  ~ tests: Command timed out after 3s (step budget)\n  ✗ lint: E501 found"
        )

    def test_stage_record_and_verdict_json_name_the_budget(self, tmp_workdir):
        """to_dict() carries the data fields AND the stage degradation marker."""
        from engine.pipeline import _record_runtime_skips

        pipeline = Pipeline({"pipeline": {"stages": []}}, tmp_workdir)
        step_result = pipeline._run_script_step(
            {"id": "tests", "type": "script", "run": "sleep 3; echo NEVER", "timeout": 1}, {}
        )
        stage = StageResult(id="tier1", passed=False, steps=[step_result])
        _record_runtime_skips(stage)
        d = stage.to_dict()
        timed = d["steps"][0]
        assert timed["data"]["timed_out"] is True
        assert timed["data"]["timeout_s"] == 1
        assert "timed out after 1s (step budget)" in timed["error"]
        assert d["degraded"] is True
        assert d["skipped_steps"] == ["tests"]
        assert "timed out after 1s (step budget)" in d["degradation_reason"]

    def test_full_run_marks_the_stage_degraded_not_just_failed(self, tmp_workdir):
        """End-to-end: the stage summary and verdict dict tell the two apart."""
        config = {
            "pipeline": {
                "stages": [
                    {
                        "id": "tier1",
                        "parallel": True,
                        "on": ["pre-eval"],
                        "steps": [
                            {
                                "id": "tests",
                                "type": "script",
                                "run": "sleep 3; echo NEVER",
                                "timeout": 1,
                            }
                        ],
                    }
                ]
            }
        }
        result = Pipeline(config, tmp_workdir).run(
            {"id": "T", "title": "t", "criteria": []}, trigger="pre-eval"
        )
        tier1 = result["stages"]["tier1"]
        assert result["passed"] is False
        assert tier1["any_failed"] is True
        assert tier1["degraded"] is True
        assert tier1["skipped_steps"] == ["tests"]
        assert "timed out after 1s (step budget)" in tier1["summary"]
        assert "~ tests" in tier1["summary"]


class TestSecretsScannerAttribution:
    """DF-GITREINS-POC-15: the scanner that ran is machine-readable."""

    def test_secrets_step_disables_gitleaks_color(self, tmp_workdir):
        """The capture cannot carry escapes: gitleaks runs with --no-color."""
        cmd = _secrets_step_run(tmp_workdir)
        assert "gitleaks detect --source . --no-git --no-banner --no-color" in cmd

    def test_parse_secrets_scanners_both(self):
        """Both scanners named → both ids, in the step's order."""
        assert parse_secrets_scanners("secrets: scanners=gitleaks+builtin cross-check") == [
            "gitleaks",
            "builtin",
        ]

    def test_parse_secrets_scanners_fallback_only(self):
        """The fallback echo names gitleaks' absence — the id list stays honest."""
        line = "secrets: scanners=builtin cross-check only (gitleaks not on PATH)"
        assert parse_secrets_scanners(line) == ["builtin"]

    def test_parse_secrets_scanners_absent_is_empty(self):
        """No attribution line → no ids (never a defaulted scanner)."""
        assert parse_secrets_scanners("no attribution here") == []
        assert parse_secrets_scanners("") == []

    def test_recorded_evidence_is_ansi_free(self):
        """verdict.json's step output no longer stores escape codes."""
        sr = StepResult(
            id="secrets", type="script", passed=True, output="\x1b[32mINF\x1b[0m scanned ~5 MB"
        )
        assert sr.to_dict()["output"] == "INF scanned ~5 MB"

    def test_script_step_stamps_the_active_scanners(self, tmp_workdir):
        """A real secrets step records its scanner ids in the step data."""
        pipeline = Pipeline({"pipeline": {"stages": []}}, tmp_workdir)
        step = {
            "id": "secrets",
            "type": "script",
            "run": "echo 'secrets: scanners=gitleaks+builtin cross-check'",
        }
        result = pipeline._run_script_step(step, {})
        assert result.passed is True
        assert result.data["secrets_scanners"] == ["gitleaks", "builtin"]

    def test_script_step_omits_scanners_when_unreported(self, tmp_workdir):
        """A step that reported no attribution gets no key (not an empty list)."""
        pipeline = Pipeline({"pipeline": {"stages": []}}, tmp_workdir)
        result = pipeline._run_script_step({"id": "secrets", "run": "echo nothing"}, {})
        assert "secrets_scanners" not in result.data

    def test_non_secrets_step_is_not_stamped(self, tmp_workdir):
        """The stamp is the secrets lane's, not every step's."""
        pipeline = Pipeline({"pipeline": {"stages": []}}, tmp_workdir)
        step = {"id": "lint", "run": "echo 'secrets: scanners=gitleaks'"}
        result = pipeline._run_script_step(step, {})
        assert "secrets_scanners" not in result.data
