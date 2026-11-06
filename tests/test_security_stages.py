"""Hermetic tests for the GR-156 staged security review pipeline.

Never spawns semgrep/ast-grep or the real LLM judge: every external tool is
replaced through engine.security_stages' injectable seams (``_tool_on_path``,
``_default_tool_runner``, ``_default_scanner_factory``, ``build_judge_fn`` —
all resolved at call time, so monkeypatch on the module works through
GuardManager too). The real AntaresScanner appears exactly once, in its
dependency-free keyword-heuristic mode, to prove heuristic findings can never
cross the confidence floor.

Coverage map (AC8): mode matrix (off/staged/full), stage labeling (AC5),
confidence floor (AC3), judge escalation count (AC4), per-stage timeout
(AC6), missing-tool policy per stage (AC7), staged-diff scoping (AC6).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from typing import Any

import pytest

from engine import security_stages
from engine.antares import AntaresFinding
from engine.config import GitReinsDefaults, SecurityReviewConfig
from engine.config_schema import validate_config
from engine.guard_manager import GuardManager
from engine.security_stages import (
    STAGE_JUDGE,
    STAGE_LOCALIZE,
    STAGE_RULES,
    SecurityFinding,
    StagedReviewOutcome,
    run_staged_review,
)

# ── Helpers ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _stage1_tools_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hermeticity: never grade the HOST's semgrep/ast-grep installation.

    The stage-1 PATH probe defaults to "both tools present" so a test's
    outcome depends only on its own fakes. Tests that exercise the
    missing-tool path re-patch ``_tool_on_path`` to return None (their
    monkeypatch.setattr overrides this fixture's binding; teardown restores
    both).
    """
    monkeypatch.setattr(security_stages, "_tool_on_path", lambda tool: f"/usr/bin/{tool}")


def _write_file(workdir: str, relpath: str, content: str) -> str:
    full = os.path.join(workdir, relpath)
    os.makedirs(os.path.dirname(full) or workdir, exist_ok=True)
    with open(full, "w") as f:
        f.write(content)
    return full


def _stage_file(workdir: str, relpath: str, content: str) -> str:
    """Write *relpath* and ``git add`` it (mirrors tests/test_antares.py)."""
    _write_file(workdir, relpath, content)
    subprocess.run(
        ["git", "add", relpath],
        cwd=workdir,
        capture_output=True,
        timeout=10,
    )
    return relpath


_SEMGREP_PAYLOAD = (
    '{"results": [{"check_id": "gitreins-eval-call", "path": "app.py", '
    '"start": {"line": 2, "col": 5}, "extra": {"message": "eval() used", '
    '"metadata": {"cwe": ["CWE-95: Eval Injection"]}}}]}'
)

_ASTGREP_PAYLOAD = (
    '[{"text": "os.system(cmd)", "range": {"start": {"line": 4, "column": 0}}, '
    '"file": "app.py", "ruleId": "gitreins-os-system", '
    '"message": "shell spawn (CWE-78)"}]'
)


@dataclass
class RecordedRunner:
    """Fake ToolRunner: returns canned payloads and records every argv."""

    semgrep_stdout: str = _SEMGREP_PAYLOAD
    astgrep_stdout: str = _ASTGREP_PAYLOAD
    raise_on: str = ""  # tool name to fail on: "timeout" | "missing" | "error"
    argvs: list[list[str]] = field(default_factory=list)
    rules_bodies: list[str] = field(default_factory=list)

    def __call__(self, workdir: str, argv: list[str], timeout: float) -> tuple[int, str, str]:
        self.argvs.append(list(argv))
        tool = argv[0]
        if tool == "semgrep":
            # The rules table is a temp file — capture its content before the
            # pipeline deletes it, so tests can assert it is local, not a
            # registry reference.
            rules_path = argv[argv.index("--config") + 1]
            with open(rules_path) as handle:
                self.rules_bodies.append(handle.read())
            if self.raise_on == "missing":
                raise FileNotFoundError(tool)
            if self.raise_on == "timeout":
                raise subprocess.TimeoutExpired(cmd=tool, timeout=timeout)
            if self.raise_on == "error":
                return 2, "", "fatal: broken"
            return 0, self.semgrep_stdout, ""
        rules_path = argv[argv.index("--rule") + 1]
        with open(rules_path) as handle:
            self.rules_bodies.append(handle.read())
        if self.raise_on == "missing":
            raise FileNotFoundError(tool)
        if self.raise_on == "timeout":
            raise subprocess.TimeoutExpired(cmd=tool, timeout=timeout)
        if self.raise_on == "error":
            return 3, "", "fatal: broken"
        return 0, self.astgrep_stdout, ""


class FakeScanner:
    """Fake Antares-shaped scanner: one finding per (file, line, confidence)."""

    def __init__(self, findings: list[tuple[str, int, float]] | None = None) -> None:
        self.specs = findings or []
        self.scanned: list[str] = []
        self.used_heuristic = False

    def scan_file(self, filepath: str) -> list[AntaresFinding]:
        self.scanned.append(filepath)
        rel = os.path.relpath(filepath)
        return [
            AntaresFinding(
                file=rel,
                line=line,
                cve_id=f"CVE-2024-{1000 + i}",
                confidence=confidence,
                description=f"localized {i}",
            )
            for i, (_, line, confidence) in enumerate(self.specs)
        ]


@dataclass
class FakeClock:
    """Deterministic monotonic clock: jumps *jump* seconds per tick()."""

    now: float = 1_000.0
    jump: float = 0.0

    def monotonic(self) -> float:
        return self.now

    def tick(self) -> None:
        self.now += self.jump


def _recording_judge(
    verdicts: list[dict[str, Any]],
) -> tuple[Any, list[list[dict[str, Any]]]]:
    calls: list[list[dict[str, Any]]] = []

    def judge(hypotheses: list[dict[str, Any]]) -> list[dict[str, Any]]:
        calls.append(list(hypotheses))
        return list(verdicts)

    return judge, calls


def _hide_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(security_stages, "_tool_on_path", lambda tool: None)


def _show_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(security_stages, "_tool_on_path", lambda tool: f"/usr/bin/{tool}")


def _staged_review(
    workdir: str,
    files: list[str],
    config: SecurityReviewConfig | None = None,
    **kwargs: Any,
) -> StagedReviewOutcome:
    cfg = config or SecurityReviewConfig(mode="staged")
    # A staged file exists on disk by definition; give the scope real content
    # so stage 2's isfile gate and the scanner see it, like a real index would.
    for relpath in files:
        if not os.path.isfile(os.path.join(workdir, relpath)):
            _write_file(workdir, relpath, "x = 1\n")
    return run_staged_review(workdir, files, cfg, **kwargs)


# ── Config block (AC1) ───────────────────────────────────────────────────────


class TestSecurityReviewConfig:
    def test_defaults_are_off_with_documented_floors(self) -> None:
        cfg = SecurityReviewConfig()
        assert cfg.mode == "off"
        assert cfg.min_confidence == 0.7
        assert cfg.per_stage_timeout_seconds == 60
        assert cfg.missing_tool_policy == "fail"
        assert GitReinsDefaults().security_review == cfg

    def test_from_dict_overlays_every_key(self) -> None:
        cfg = SecurityReviewConfig.from_dict(
            {
                "mode": "full",
                "min_confidence": 0.9,
                "per_stage_timeout_seconds": 30,
                "missing_tool_policy": "skip",
            }
        )
        assert cfg == SecurityReviewConfig(
            mode="full",
            min_confidence=0.9,
            per_stage_timeout_seconds=30,
            missing_tool_policy="skip",
        )

    def test_invalid_mode_degrades_to_off(self) -> None:
        assert SecurityReviewConfig.from_dict({"mode": "sometimes"}).mode == "off"

    def test_invalid_policy_degrades_to_fail(self) -> None:
        assert (
            SecurityReviewConfig.from_dict({"missing_tool_policy": "ignore"}).missing_tool_policy
            == "fail"
        )

    def test_non_positive_timeout_raises(self) -> None:
        with pytest.raises(ValueError, match="per_stage_timeout_seconds"):
            SecurityReviewConfig.from_dict({"per_stage_timeout_seconds": 0})
        with pytest.raises(ValueError, match="per_stage_timeout_seconds"):
            SecurityReviewConfig.from_dict({"per_stage_timeout_seconds": True})

    def test_out_of_range_confidence_raises(self) -> None:
        with pytest.raises(ValueError, match="min_confidence"):
            SecurityReviewConfig.from_dict({"min_confidence": 1.5})
        with pytest.raises(ValueError, match="min_confidence"):
            SecurityReviewConfig.from_dict({"min_confidence": -0.1})

    def test_overlay_without_block_keeps_off(self) -> None:
        defaults = GitReinsDefaults().overlay({})
        assert defaults.security_review.mode == "off"

    def test_overlay_and_roundtrip(self) -> None:
        block = {"mode": "staged", "min_confidence": 0.5, "per_stage_timeout_seconds": 10}
        defaults = GitReinsDefaults().overlay({"security_review": block})
        assert defaults.security_review.mode == "staged"
        assert defaults.to_config_dict()["security_review"]["mode"] == "staged"
        assert defaults.to_config_dict()["security_review"]["min_confidence"] == 0.5

    def test_schema_recognizes_the_block(self) -> None:
        assert validate_config({"security_review": {"mode": "staged", "min_confidence": 0.7}}) == []

    def test_schema_warns_on_bad_enum_and_unknown_key(self) -> None:
        issues = validate_config({"security_review": {"mode": "sometimes", "floor": 0.5}})
        paths = {issue.path for issue in issues}
        assert "security_review.mode" in paths
        assert "security_review.floor" in paths


# ── Mode matrix (AC1 / AC4 / AC8) ────────────────────────────────────────────


class TestModeMatrix:
    def test_off_runs_no_stage(self) -> None:
        outcome = run_staged_review("/nonexistent", ["app.py"], SecurityReviewConfig(mode="off"))
        assert outcome.stages == []
        assert outcome.findings == []
        assert outcome.passed is True

    def test_staged_mode_runs_rules_and_localize_only(self, tmp_workdir: str) -> None:
        judge, calls = _recording_judge([])
        outcome = _staged_review(
            tmp_workdir,
            ["app.py"],
            SecurityReviewConfig(mode="staged"),
            tool_runner=RecordedRunner(),
            scanner_factory=lambda _: FakeScanner(),
            judge_fn=judge,
        )
        assert [s.stage for s in outcome.stages] == [STAGE_RULES, STAGE_LOCALIZE]
        assert outcome.escalations == 0  # stage 3 runs in full mode ONLY (AC4)
        assert calls == []

    def test_full_mode_runs_the_judge(self, tmp_workdir: str) -> None:
        judge, calls = _recording_judge(
            [{"rule_id": "gitreins-eval-call", "confirmed": True, "citation": "app.py:2"}]
        )
        outcome = _staged_review(
            tmp_workdir,
            ["app.py"],
            SecurityReviewConfig(mode="full"),
            tool_runner=RecordedRunner(),
            scanner_factory=lambda _: FakeScanner(),
            judge_fn=judge,
        )
        assert [s.stage for s in outcome.stages] == [STAGE_RULES, STAGE_LOCALIZE, STAGE_JUDGE]
        assert len(calls) == 1
        assert outcome.escalations == len(calls[0])

    def test_guard_runs_no_lane_when_mode_off(self, tmp_workdir: str) -> None:
        _stage_file(tmp_workdir, "app.py", "x = eval('1')\n")
        for block in ({"mode": "off"}, None):
            config = {"security_review": block} if block else {}
            manager = GuardManager(tmp_workdir, config=config)
            names = [r.name for r in manager.run_all().results]
            assert "security_review" not in names  # behavior-identical (AC1)

    def test_guard_lane_runs_when_staged(
        self, tmp_workdir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stage_file(tmp_workdir, "app.py", "x = eval('1')\n")
        _show_tools(monkeypatch)
        monkeypatch.setattr(security_stages, "_default_tool_runner", RecordedRunner())
        monkeypatch.setattr(
            security_stages,
            "_default_scanner_factory",
            lambda _: FakeScanner([("app.py", 9, 0.9)]),
        )
        manager = GuardManager(tmp_workdir, config={"security_review": {"mode": "staged"}})
        result = manager.run_all()
        lane = next(r for r in result.results if r.name == "security_review")
        assert lane.passed is False  # findings fail the lane
        assert "[rules]" in lane.output and "[localize]" in lane.output
        assert result.extra["security_review"]["mode"] == "staged"
        assert result.extra["security_review"]["stages"] == {
            STAGE_RULES: "ok",
            STAGE_LOCALIZE: "ok",
        }


# ── Stage labeling (AC5) ─────────────────────────────────────────────────────


class TestStageLabeling:
    def _full_outcome(self, tmp_workdir: str) -> StagedReviewOutcome:
        judge, _calls = _recording_judge(
            [
                {
                    "rule_id": "gitreins-eval-call",
                    "confirmed": True,
                    "citation": "app.py:2",
                    "note": "real",
                }
            ]
        )
        return _staged_review(
            tmp_workdir,
            ["app.py"],
            SecurityReviewConfig(mode="full"),
            tool_runner=RecordedRunner(),
            scanner_factory=lambda _: FakeScanner([("app.py", 9, 0.9)]),
            judge_fn=judge,
        )

    def test_label_propagation_for_all_three_stages(self, tmp_workdir: str) -> None:
        outcome = self._full_outcome(tmp_workdir)
        by_stage = {f.stage: f for f in outcome.findings}
        assert set(by_stage) == {STAGE_RULES, STAGE_LOCALIZE, STAGE_JUDGE}
        assert by_stage[STAGE_RULES].stage_label() == "[rules]"
        assert by_stage[STAGE_LOCALIZE].stage_label() == "[localize]"
        assert by_stage[STAGE_JUDGE].stage_label() == "[judge]"
        assert by_stage[STAGE_JUDGE].verdict_label() == "model-confirmed"

    def test_rule_hit_is_never_model_confirmed(self, tmp_workdir: str) -> None:
        outcome = self._full_outcome(tmp_workdir)
        rules_hit = next(f for f in outcome.findings if f.stage == STAGE_RULES)
        assert rules_hit.verdict_label() == "rule hit"
        rendered = security_stages.render_outcome(outcome, 0.7)
        rules_line = next(
            line for line in rendered.splitlines() if line.strip().startswith("[rules]")
        )
        assert "model-confirmed" not in rules_line
        assert "[rules]" in rules_line

    def test_every_rendered_finding_line_carries_its_stage_tag(self, tmp_workdir: str) -> None:
        outcome = self._full_outcome(tmp_workdir)
        rendered = security_stages.render_outcome(outcome, 0.7)
        for finding in outcome.findings:
            line = next(
                line
                for line in rendered.splitlines()
                # rules and judge hits can share rule_id+location; the verdict
                # label is the discriminator that pins the producing stage.
                if finding.rule_id in line
                and finding.location in line
                and finding.verdict_label() in line
            )
            assert line.strip().startswith(f"[{finding.stage}]")

    def test_finding_dicts_carry_stage(self, tmp_workdir: str) -> None:
        outcome = self._full_outcome(tmp_workdir)
        stages = {f.to_dict()["stage"] for f in outcome.findings}
        assert stages == {STAGE_RULES, STAGE_LOCALIZE, STAGE_JUDGE}


# ── Confidence floor (AC3) ───────────────────────────────────────────────────


class TestConfidenceFloor:
    def test_below_floor_dropped_and_counted_never_promoted(self, tmp_workdir: str) -> None:
        scanner = FakeScanner([("app.py", 9, 0.9), ("app.py", 20, 0.4)])
        outcome = _staged_review(
            tmp_workdir,
            ["app.py"],
            tool_runner=RecordedRunner(semgrep_stdout='{"results": []}', astgrep_stdout="[]"),
            scanner_factory=lambda _: scanner,
        )
        localize = next(s for s in outcome.stages if s.stage == STAGE_LOCALIZE)
        assert localize.status == "ok"
        assert [f.confidence for f in localize.findings] == [0.9]
        assert localize.dropped_below_floor == 1
        assert all(f.confidence >= 0.7 for f in outcome.findings)

    def test_floor_is_configurable(self, tmp_workdir: str) -> None:
        scanner = FakeScanner([("app.py", 9, 0.4), ("app.py", 20, 0.2)])
        outcome = _staged_review(
            tmp_workdir,
            ["app.py"],
            SecurityReviewConfig(mode="staged", min_confidence=0.3),
            tool_runner=RecordedRunner(semgrep_stdout='{"results": []}', astgrep_stdout="[]"),
            scanner_factory=lambda _: scanner,
        )
        localize = next(s for s in outcome.stages if s.stage == STAGE_LOCALIZE)
        assert len(localize.findings) == 1
        assert localize.findings[0].confidence == 0.4
        assert localize.dropped_below_floor == 1

    def test_render_names_the_dropped_count(self, tmp_workdir: str) -> None:
        outcome = _staged_review(
            tmp_workdir,
            ["app.py"],
            tool_runner=RecordedRunner(semgrep_stdout='{"results": []}', astgrep_stdout="[]"),
            scanner_factory=lambda _: FakeScanner([("app.py", 9, 0.1)]),
        )
        rendered = security_stages.render_outcome(outcome, 0.7)
        assert "1 dropped below confidence 0.7" in rendered
        assert "app.py:9" not in rendered  # a dropped finding is not reported

    def test_real_heuristic_findings_cannot_pass_the_floor(self, tmp_workdir: str) -> None:
        """The REAL AntaresScanner keyword fallback emits confidence 0.0 — it
        must land below the floor and be counted, never promoted (AC3)."""
        _write_file(tmp_workdir, "app.py", "unsafe = True\n# exploit note\n")
        outcome = security_stages.run_localization_stage(
            tmp_workdir, ["app.py"], min_confidence=0.7, timeout=60.0
        )
        localize = outcome
        assert localize.status == "ok"
        assert localize.findings == []
        assert localize.dropped_below_floor == 2
        # heuristic mode must disclose itself (DF-GITREINS-POC-59 contract)
        assert "keyword fallback" in localize.detail


# ── Judge escalation (AC4) ───────────────────────────────────────────────────


class TestJudgeEscalation:
    def _tools_with_one_above_and_one_below(self) -> tuple[RecordedRunner, FakeScanner]:
        return (
            RecordedRunner(semgrep_stdout='{"results": []}', astgrep_stdout="[]"),
            FakeScanner([("app.py", 9, 0.9), ("app.py", 20, 0.4)]),
        )

    def test_escalation_count_counts_only_surviving_hypotheses(self, tmp_workdir: str) -> None:
        runner, scanner = self._tools_with_one_above_and_one_below()
        judge, calls = _recording_judge([])
        outcome = _staged_review(
            tmp_workdir,
            ["app.py"],
            SecurityReviewConfig(mode="full"),
            tool_runner=runner,
            scanner_factory=lambda _: scanner,
            judge_fn=judge,
        )
        assert outcome.escalations == 1
        assert len(calls[0]) == 1
        assert calls[0][0]["confidence"] == 0.9  # the 0.4 finding never escalates

    def test_staged_mode_has_zero_escalations(self, tmp_workdir: str) -> None:
        runner, scanner = self._tools_with_one_above_and_one_below()
        judge, calls = _recording_judge([])
        outcome = _staged_review(
            tmp_workdir,
            ["app.py"],
            SecurityReviewConfig(mode="staged"),
            tool_runner=runner,
            scanner_factory=lambda _: scanner,
            judge_fn=judge,
        )
        assert outcome.escalations == 0
        assert calls == []

    def test_confirmed_finding_requires_a_citation(self, tmp_workdir: str) -> None:
        judge, _ = _recording_judge(
            [
                {"rule_id": "r1", "confirmed": True, "citation": "src/app.py:12"},
                {"rule_id": "r2", "confirmed": True, "citation": ""},
                {"rule_id": "r3", "confirmed": True, "citation": "no line number"},
                {"rule_id": "r4", "confirmed": False, "citation": "src/app.py:3"},
            ]
        )
        stage = security_stages.run_judge_stage(
            [{"rule_id": r} for r in ("r1", "r2", "r3", "r4")], judge, timeout=10.0
        )
        assert [(f.file, f.line) for f in stage.findings] == [("src/app.py", 12)]
        assert len(stage.unconfirmed) == 3  # uncited -> unconfirmed, never a pass (AC4)

    def test_unconfirmed_is_reported_not_silently_dropped(self, tmp_workdir: str) -> None:
        runner, scanner = self._tools_with_one_above_and_one_below()
        judge, _ = _recording_judge(
            [{"rule_id": "CVE-2024-1000", "confirmed": True, "citation": "", "note": "cannot cite"}]
        )
        outcome = _staged_review(
            tmp_workdir,
            ["app.py"],
            SecurityReviewConfig(mode="full"),
            tool_runner=runner,
            scanner_factory=lambda _: scanner,
            judge_fn=judge,
        )
        assert outcome.unconfirmed
        rendered = security_stages.render_outcome(outcome, 0.7)
        assert "unconfirmed (judge could not cite file:line): 1" in rendered
        # unconfirmed is NOT a confirmed finding, but it is not a pass either:
        assert not any(f.stage == STAGE_JUDGE and f.confirmed for f in outcome.findings)


# ── Bounded execution (AC6) ──────────────────────────────────────────────────


class TestBoundedExecution:
    def test_rules_stage_timeout_degrades_per_policy(self, tmp_workdir: str) -> None:
        for policy, expected_status, expected_pass in (
            ("fail", "timeout", False),
            ("skip", "skipped", True),
        ):
            outcome = _staged_review(
                tmp_workdir,
                ["app.py"],
                SecurityReviewConfig(
                    mode="staged", per_stage_timeout_seconds=5, missing_tool_policy=policy
                ),
                tool_runner=RecordedRunner(raise_on="timeout"),
                scanner_factory=lambda _: FakeScanner(),
            )
            rules = next(s for s in outcome.stages if s.stage == STAGE_RULES)
            assert rules.status == expected_status, policy
            assert outcome.passed is expected_pass, policy
            if policy == "skip":
                assert "timeout" in rules.detail  # named skip, never silent

    def test_localize_stage_timeout_degrades_per_policy(self, tmp_workdir: str) -> None:
        _write_file(tmp_workdir, "a.py", "x = 1\n")
        _write_file(tmp_workdir, "b.py", "y = 2\n")
        clock = FakeClock(jump=10.0)
        scanner = FakeScanner([("app.py", 1, 0.9)])
        monkey_target = security_stages.time
        original_monotonic = monkey_target.monotonic

        class AdvancingScanner(FakeScanner):
            def scan_file(self, filepath: str) -> list[AntaresFinding]:
                clock.tick()  # each scan burns the whole stage budget
                return super().scan_file(filepath)

        monkey_target.monotonic = clock.monotonic  # type: ignore[assignment]
        try:
            outcome = security_stages.run_localization_stage(
                tmp_workdir,
                ["a.py", "b.py"],
                min_confidence=0.7,
                timeout=5.0,
                scanner_factory=lambda _: AdvancingScanner(),
            )
        finally:
            monkey_target.monotonic = original_monotonic  # type: ignore[assignment]
        assert outcome.status == "timeout"
        assert "per-stage timeout" in outcome.detail

    def test_judge_stage_timeout(self, tmp_workdir: str) -> None:
        import time as time_module

        def slow_judge(hypotheses: list[dict[str, Any]]) -> list[dict[str, Any]]:
            time_module.sleep(0.5)
            return []

        stage = security_stages.run_judge_stage(
            [{"rule_id": "r1"}, {"rule_id": "r2"}], slow_judge, timeout=0.1
        )
        assert stage.status == "timeout"
        assert stage.escalations == 2  # hypotheses were handed off, timed out
        assert stage.findings == []

    def test_rules_are_local_files_no_registry_no_network(self, tmp_workdir: str) -> None:
        runner = RecordedRunner()
        _staged_review(
            tmp_workdir, ["app.py"], tool_runner=runner, scanner_factory=lambda _: FakeScanner()
        )
        assert len(runner.rules_bodies) == 2
        for body in runner.rules_bodies:
            assert body.strip()  # a real local rule table was handed to the tool
            assert "auto" not in body  # no registry lookup (--config auto)
        for argv in runner.argvs:
            for part in argv:
                # every absolute path argument is a temp rules file OUTSIDE
                # the graded repository — no rule table is ever written in-tree
                if part.startswith("/"):
                    assert not part.startswith(tmp_workdir), part

    def test_staged_diff_scoping_tools_see_only_the_scope(self, tmp_workdir: str) -> None:
        _write_file(tmp_workdir, "app.py", "x = 1\n")
        _write_file(tmp_workdir, "other.py", "y = eval('2')\n")  # in tree, NOT in scope
        runner = RecordedRunner()
        scanner = FakeScanner()
        _staged_review(
            tmp_workdir,
            ["app.py"],
            tool_runner=runner,
            scanner_factory=lambda _: scanner,
        )
        for argv in runner.argvs:
            targeted = [p for p in argv if p.endswith(".py")]
            assert targeted == ["app.py"]  # the whole tree is never graded
        assert [os.path.relpath(p, tmp_workdir) for p in scanner.scanned] == ["app.py"]

    def test_no_writes_into_the_graded_repository(self, tmp_workdir: str) -> None:
        _write_file(tmp_workdir, "app.py", "x = 1\n")
        before = sorted(os.listdir(tmp_workdir))
        _staged_review(
            tmp_workdir,
            ["app.py"],
            SecurityReviewConfig(mode="full"),
            tool_runner=RecordedRunner(),
            scanner_factory=lambda _: FakeScanner(),
            judge_fn=lambda hyps: [],
        )
        assert sorted(os.listdir(tmp_workdir)) == before


# ── Missing-tool policy (AC7) ────────────────────────────────────────────────


class TestMissingToolPolicy:
    def test_rules_stage_missing_tools_fail_by_default(
        self, tmp_workdir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _hide_tools(monkeypatch)
        outcome = _staged_review(
            tmp_workdir,
            ["app.py"],
            tool_runner=RecordedRunner(),
            scanner_factory=lambda _: FakeScanner(),
        )
        rules = next(s for s in outcome.stages if s.stage == STAGE_RULES)
        assert rules.status == "missing_tool"
        assert outcome.passed is False  # negative test, stage 1: never a silent pass
        assert "semgrep" in rules.detail and "ast-grep" in rules.detail

    def test_rules_stage_missing_tools_skip_when_policy_skip(
        self, tmp_workdir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _hide_tools(monkeypatch)
        outcome = _staged_review(
            tmp_workdir,
            ["app.py"],
            SecurityReviewConfig(mode="staged", missing_tool_policy="skip"),
            tool_runner=RecordedRunner(),
            scanner_factory=lambda _: FakeScanner(),
        )
        rules = next(s for s in outcome.stages if s.stage == STAGE_RULES)
        assert rules.status == "skipped"
        assert "missing_tool_policy=skip" in rules.detail
        rendered = security_stages.render_outcome(outcome, 0.7)
        assert "skipped stage rules" in rendered  # named, not silent

    def test_localize_stage_missing_scanner_fails_by_default(self, tmp_workdir: str) -> None:
        def broken_factory(workdir: str) -> Any:
            raise ImportError("huggingface_hub is required")

        outcome = _staged_review(
            tmp_workdir,
            ["app.py"],
            tool_runner=RecordedRunner(semgrep_stdout='{"results": []}', astgrep_stdout="[]"),
            scanner_factory=broken_factory,
        )
        localize = next(s for s in outcome.stages if s.stage == STAGE_LOCALIZE)
        assert localize.status == "missing_tool"
        assert outcome.passed is False  # negative test, stage 2

    def test_localize_stage_missing_scanner_skips_when_policy_skip(self, tmp_workdir: str) -> None:
        def broken_factory(workdir: str) -> Any:
            raise ImportError("huggingface_hub is required")

        outcome = _staged_review(
            tmp_workdir,
            ["app.py"],
            SecurityReviewConfig(mode="staged", missing_tool_policy="skip"),
            tool_runner=RecordedRunner(semgrep_stdout='{"results": []}', astgrep_stdout="[]"),
            scanner_factory=broken_factory,
        )
        localize = next(s for s in outcome.stages if s.stage == STAGE_LOCALIZE)
        assert localize.status == "skipped"
        assert "missing_tool_policy=skip" in localize.detail

    def test_judge_stage_missing_fails_by_default(self, tmp_workdir: str) -> None:
        outcome = _staged_review(
            tmp_workdir,
            ["app.py"],
            SecurityReviewConfig(mode="full"),  # policy defaults to fail
            tool_runner=RecordedRunner(),
            scanner_factory=lambda _: FakeScanner(),
            judge_fn=None,  # no judge available
        )
        judge_stage = next(s for s in outcome.stages if s.stage == STAGE_JUDGE)
        assert judge_stage.status == "missing_tool"
        assert outcome.passed is False  # negative test, stage 3
        assert "no judge" in judge_stage.detail

    def test_judge_stage_missing_skips_when_policy_skip(self, tmp_workdir: str) -> None:
        outcome = _staged_review(
            tmp_workdir,
            ["app.py"],
            SecurityReviewConfig(mode="full", missing_tool_policy="skip"),
            tool_runner=RecordedRunner(),
            scanner_factory=lambda _: FakeScanner(),
            judge_fn=None,
        )
        judge_stage = next(s for s in outcome.stages if s.stage == STAGE_JUDGE)
        assert judge_stage.status == "skipped"
        assert "missing_tool_policy=skip" in judge_stage.detail

    def test_guard_lane_surfaces_missing_tools_by_policy(
        self, tmp_workdir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stage_file(tmp_workdir, "app.py", "x = 1\n")
        _hide_tools(monkeypatch)
        monkeypatch.setattr(security_stages, "_default_scanner_factory", lambda _: FakeScanner())
        manager = GuardManager(tmp_workdir, config={"security_review": {"mode": "staged"}})
        result = manager.run_all()
        lane = next(r for r in result.results if r.name == "security_review")
        assert lane.passed is False
        assert "rules (missing_tool)" in lane.error

        manager_skip = GuardManager(
            tmp_workdir,
            config={"security_review": {"mode": "staged", "missing_tool_policy": "skip"}},
        )
        result_skip = manager_skip.run_all()
        lane_skip = next(r for r in result_skip.results if r.name == "security_review")
        assert lane_skip.passed is True
        assert lane_skip.skipped is True
        assert "missing_tool_policy=skip" in lane_skip.skip_reason


# ── Guard lane integration ───────────────────────────────────────────────────


class TestGuardIntegration:
    def test_empty_scope_is_a_named_skip(self, tmp_workdir: str) -> None:
        manager = GuardManager(tmp_workdir, config={"security_review": {"mode": "staged"}})
        result = manager.run_all()
        lane = next(r for r in result.results if r.name == "security_review")
        assert lane.skipped is True
        assert lane.skip_reason == "no files in scope"

    def test_deleted_files_do_not_count_as_scope(self, tmp_workdir: str) -> None:
        staged = _stage_file(tmp_workdir, "gone.py", "x = 1\n")
        os.unlink(os.path.join(tmp_workdir, staged))
        manager = GuardManager(tmp_workdir, config={"security_review": {"mode": "staged"}})
        result = manager.run_all()
        lane = next(r for r in result.results if r.name == "security_review")
        assert lane.skipped is True

    def test_judge_seam_is_used_in_full_mode(
        self, tmp_workdir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stage_file(tmp_workdir, "app.py", "x = eval('1')\n")
        _show_tools(monkeypatch)
        monkeypatch.setattr(security_stages, "_default_tool_runner", RecordedRunner())
        monkeypatch.setattr(security_stages, "_default_scanner_factory", lambda _: FakeScanner())
        built: list[Any] = []

        def fake_build_judge(config: Any) -> Any:
            built.append(config)
            judge, _ = _recording_judge(
                [{"rule_id": "gitreins-eval-call", "confirmed": True, "citation": "app.py:2"}]
            )
            return judge

        monkeypatch.setattr(security_stages, "build_judge_fn", fake_build_judge)
        manager = GuardManager(tmp_workdir, config={"security_review": {"mode": "full"}})
        result = manager.run_all()
        assert len(built) == 1  # the module seam produced THE judge
        lane = next(r for r in result.results if r.name == "security_review")
        assert "[judge]" in lane.output
        assert "model-confirmed" in lane.output

    def test_full_mode_without_judge_credential_degrades(
        self, tmp_workdir: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _stage_file(tmp_workdir, "app.py", "x = eval('1')\n")
        _show_tools(monkeypatch)
        monkeypatch.setattr(security_stages, "_default_tool_runner", RecordedRunner())
        monkeypatch.setattr(security_stages, "_default_scanner_factory", lambda _: FakeScanner())
        monkeypatch.setattr(security_stages, "build_judge_fn", lambda config: None)
        manager = GuardManager(tmp_workdir, config={"security_review": {"mode": "full"}})
        result = manager.run_all()
        lane = next(r for r in result.results if r.name == "security_review")
        assert lane.passed is False
        assert "judge (missing_tool)" in lane.error

    def test_tool_error_is_a_named_stage_failure(self, tmp_workdir: str) -> None:
        outcome = _staged_review(
            tmp_workdir,
            ["app.py"],
            tool_runner=RecordedRunner(raise_on="error"),
            scanner_factory=lambda _: FakeScanner(),
        )
        rules = next(s for s in outcome.stages if s.stage == STAGE_RULES)
        assert rules.status == "tool_error"
        assert outcome.passed is False
        assert "exited 2" in rules.detail and "fatal: broken" in rules.detail

    def test_summary_dict_is_machine_readable(self, tmp_workdir: str) -> None:
        outcome = _staged_review(
            tmp_workdir,
            ["app.py"],
            tool_runner=RecordedRunner(),
            scanner_factory=lambda _: FakeScanner([("app.py", 9, 0.4)]),
        )
        summary = security_stages.outcome_summary(outcome)
        assert summary["mode"] == "staged"
        assert summary["stages"] == {STAGE_RULES: "ok", STAGE_LOCALIZE: "ok"}
        assert summary["dropped_below_floor"] == 1
        assert summary["passed"] is False
        assert summary["findings"][0]["stage"] in (STAGE_RULES, STAGE_LOCALIZE)


# ── Parser robustness (hostile tool output must never crash the lane) ───────


class TestParsers:
    def test_semgrep_cwe_list_and_missing_metadata(self) -> None:
        payload = (
            '{"results": ['
            '{"check_id": "r1", "path": "a.py", "start": {"line": 3}, '
            '"extra": {"message": "m", "metadata": {"cwe": ["CWE-79: XSS"]}}},'
            '{"check_id": "r2", "path": "b.py", "start": {"line": 4}, "extra": {}}'
            "]}"
        )
        findings, error = security_stages._parse_semgrep_output(payload, 0, "")
        assert error == ""
        assert [f.cwe for f in findings] == ["CWE-79", "CWE-UNSPECIFIED"]
        assert [f.location for f in findings] == ["a.py:3", "b.py:4"]

    def test_semgrep_unparseable_output_is_a_tool_error(self) -> None:
        findings, error = security_stages._parse_semgrep_output("not json", 1, "boom")
        assert findings == []
        assert "not JSON" in error

    def test_astgrep_zero_based_lines_are_converted(self) -> None:
        payload = (
            '[{"text": "x", "range": {"start": {"line": 0, "column": 0}}, '
            '"file": "a.py", "ruleId": "r1", "message": "cwe CWE-78 here"}]'
        )
        findings, error = security_stages._parse_astgrep_output(payload, 0, "")
        assert error == ""
        assert findings[0].line == 1
        assert findings[0].cwe == "CWE-78"

    def test_astgrep_rules_array_form(self) -> None:
        payload = (
            '[{"range": {"start": {"line": 5}}, "file": "a.py", '
            '"rules": ["r-multi"], "message": "m"}]'
        )
        findings, error = security_stages._parse_astgrep_output(payload, 0, "")
        assert error == ""
        assert findings[0].rule_id == "r-multi"
        assert findings[0].line == 6

    def test_empty_tool_output_is_clean_not_an_error(self) -> None:
        assert security_stages._parse_semgrep_output("", 0, "") == ([], "")
        assert security_stages._parse_astgrep_output("", 0, "") == ([], "")

    def test_judge_response_prose_is_tolerated(self) -> None:
        content = 'Sure! Here are my verdicts:\n[{"rule_id": "r1", "confirmed": true, "citation": "a.py:1"}]'
        verdicts = security_stages._parse_judge_response(content)
        assert verdicts == [{"rule_id": "r1", "confirmed": True, "citation": "a.py:1"}]

    def test_judge_verdict_wrapper_object_is_tolerated(self) -> None:
        content = '{"verdicts": [{"rule_id": "r1", "confirmed": false}]}'
        verdicts = security_stages._parse_judge_response(content)
        assert verdicts == [{"rule_id": "r1", "confirmed": False}]

    def test_judge_garbage_yields_no_verdicts(self) -> None:
        assert security_stages._parse_judge_response("no json at all") == []


# ── Findings fail the lane; clean passes stay clean ──────────────────────────


class TestVerdict:
    def test_findings_fail_the_pipeline(self, tmp_workdir: str) -> None:
        outcome = _staged_review(
            tmp_workdir,
            ["app.py"],
            tool_runner=RecordedRunner(),
            scanner_factory=lambda _: FakeScanner(),
        )
        assert outcome.findings
        assert outcome.passed is False

    def test_clean_stages_pass(self, tmp_workdir: str) -> None:
        outcome = _staged_review(
            tmp_workdir,
            ["app.py"],
            tool_runner=RecordedRunner(semgrep_stdout='{"results": []}', astgrep_stdout="[]"),
            scanner_factory=lambda _: FakeScanner(),
        )
        assert outcome.findings == []
        assert outcome.passed is True

    def test_off_mode_outcome_is_neutral(self) -> None:
        outcome = run_staged_review("/x", [], SecurityReviewConfig(mode="off"))
        assert isinstance(outcome, StagedReviewOutcome)
        assert security_stages.render_outcome(outcome, 0.7).startswith("security review (off)")

    def test_security_finding_location_and_labels(self) -> None:
        finding = SecurityFinding(
            stage=STAGE_RULES, rule_id="r", cwe="CWE-1", file="a.py", line=2, confidence=1.0
        )
        assert finding.location == "a.py:2"
        assert finding.stage_label() == "[rules]"
        assert finding.verdict_label() == "rule hit"
