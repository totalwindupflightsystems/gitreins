"""JEVRES-005 — tests for the JEV-RESOLUTION calibration harness.

The script is not a package module, so it is loaded by path (the
``test_judgment_viewer_script.py`` pattern). Everything here is hermetic by
construction: the harness's hermetic mode injects both resolver seams, and
the corpus trees are checked-in fixtures — no key, no network, no hilo
binary. The one RED-proof test re-runs the gate over a drifted corpus to
prove criterion 4 (non-zero on near-miss false positives) rather than
trusting the gate's happy path.
"""

from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "jevres_calibrate.py"
CORPUS = REPO_ROOT / "tests" / "fixtures" / "jevres_cases" / "corpus.jsonl"


def _load_module():
    spec = importlib.util.spec_from_file_location("jevres_calibrate_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: the script's dataclasses resolve their string
    # annotations through sys.modules[cls.__module__] at class-creation time.
    sys.modules["jevres_calibrate_under_test"] = module
    spec.loader.exec_module(module)
    return module


cal = _load_module()


# ── The corpus itself ────────────────────────────────────────────────────────


def test_corpus_has_at_least_eight_cases_across_all_four_categories():
    cases = cal.load_corpus()
    assert len(cases) >= 8
    by_category = {case.category for case in cases}
    assert by_category == set(cal.ALL_CATEGORIES)


def test_corpus_resolved_and_unresolved_cases_share_one_question():
    """The pair design: the SAME question measured in both repo states."""
    cases = cal.load_corpus()
    resolved = {case.question for case in cases if case.category == cal.CATEGORY_RESOLVED}
    unresolved = {case.question for case in cases if case.category == cal.CATEGORY_UNRESOLVED}
    assert resolved & unresolved, "the resolved/unresolved pair must ask one question"


def test_corpus_trees_exist_and_resolved_trees_carry_checks():
    cases = cal.load_corpus()
    cal.validate_corpus(cases)  # must not raise


def test_ground_truth_is_grounded_in_the_trees_not_in_the_resolver():
    """Independence: the labels' claims are verifiable from the fixture files."""
    trees = cal.TREES_ROOT

    # resolved: the fixed tree rejects non-positive amounts BEFORE fee lookup
    fixed = (trees / "payments_fixed" / "validator.py").read_text(encoding="utf-8")
    assert "amount_cents <= 0" in fixed
    assert "raise ValidationError" in fixed
    assert fixed.index("amount_cents <= 0") < fixed.index("def fee_lookup")
    # ... and the same tree carries the check exercising the refusal
    checks = (trees / "payments_fixed" / "validator_checks.py").read_text(encoding="utf-8")
    assert "validate_amount(-1)" in checks

    # unresolved: the pre-fix half of the pair has no refusal at all
    vulnerable = (trees / "payments_vulnerable" / "validator.py").read_text(encoding="utf-8")
    assert "<= 0" not in vulnerable

    # near-miss (mentions): the topic is named, the check does not exist
    mentions = (trees / "payments_mentions" / "validator.py").read_text(encoding="utf-8")
    assert "negative amounts" in mentions
    assert "raise" not in mentions

    # near-miss (unwired): the check exists, the capture entry point never
    # CALLS it — asserted on the AST, so a docstring that merely names it
    # cannot fool the ground truth any more than it should fool Jev
    unwired_module = ast.parse(
        (trees / "payments_unwired" / "runner.py").read_text(encoding="utf-8")
    )
    capture = next(
        node
        for node in unwired_module.body
        if isinstance(node, ast.FunctionDef) and node.name == "capture"
    )
    called: set[str] = set()
    for node in ast.walk(capture):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            called.add(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            called.add(node.func.attr)
    assert "fee_lookup" in called
    assert "validate_amount" not in called

    # near-miss (mentions): the topic is named, no validation code exists
    mentions_module = ast.parse(
        (trees / "payments_mentions" / "validator.py").read_text(encoding="utf-8")
    )
    defined = {node.name for node in ast.walk(mentions_module) if isinstance(node, ast.FunctionDef)}
    assert "validate_amount" not in defined

    # budget-starved: the ledger genuinely cannot fit the ceiling
    ledger = (trees / "payments_ledger" / "incident_ledger.py").read_text(encoding="utf-8")
    assert len(ledger) > 100_000
    compile(ledger, "incident_ledger.py", "exec")  # real code, not filler


# ── Corpus validation fails loudly ───────────────────────────────────────────


def _full_case_set(workdir: str = "tree") -> list:
    """One case per category, all naming the same (existing) tree name."""
    specs = [
        (cal.CATEGORY_RESOLVED, "q", "none"),
        (cal.CATEGORY_UNRESOLVED, "other", "implementation"),
        (cal.CATEGORY_NEAR_MISS, "third", "test"),
        (cal.CATEGORY_BUDGET, "fourth", None),
    ]
    return [
        cal.CorpusCase(
            id=f"case-{index}",
            category=category,
            question=question,
            workdir=workdir,
            seeds=[],
            expect_missing_kind=kind,
            expect_surface=None,
            note="",
        )
        for index, (category, question, kind) in enumerate(specs)
    ]


def test_validate_corpus_rejects_an_unknown_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(cal, "TREES_ROOT", tmp_path)
    cases = cal.load_corpus()  # real corpus, real categories
    with pytest.raises(SystemExit, match="unknown tree"):
        cal.validate_corpus(cases)


def test_validate_corpus_requires_an_acceptance_file_on_resolved_trees(tmp_path, monkeypatch):
    monkeypatch.setattr(cal, "TREES_ROOT", tmp_path)
    for case in _full_case_set():
        tree = tmp_path / case.workdir
        tree.mkdir(exist_ok=True)
        (tree / "code.py").write_text("x = 1\n", encoding="utf-8")
    cases = _full_case_set()
    # the resolved case's acceptance file is missing — exactly the drift the
    # validator exists to catch (a "resolved" label with no check in the tree)
    (tmp_path / "tree" / "checks.py").unlink(missing_ok=True)
    with pytest.raises(SystemExit, match="no \\*_checks.py acceptance file"):
        cal.validate_corpus(cases)


def test_validate_corpus_requires_the_pair_design(tmp_path, monkeypatch):
    monkeypatch.setattr(cal, "TREES_ROOT", tmp_path)
    for case in _full_case_set():
        tree = tmp_path / case.workdir
        tree.mkdir(exist_ok=True)
        (tree / "acceptance_checks.py").write_text("x = 1\n", encoding="utf-8")
    # every category present, trees fine — but no unresolved case shares a
    # question with a resolved one, so the PAIR invariant fails
    with pytest.raises(SystemExit, match="PAIR"):
        cal.validate_corpus(_full_case_set())


# ── Hermetic run over the full corpus ────────────────────────────────────────


def test_full_hermetic_run_gate_passes_and_prints_matrix_and_sweep(capsys):
    exit_code = cal.main([])
    out = capsys.readouterr().out
    assert exit_code == 0
    assert "GATE PASS" in out
    assert out.count("surface=") == 8
    assert "Confusion matrix at production bands" in out
    assert "Threshold sweep" in out
    # every sweep row is one of the requested threshold pairs
    steps = cal.SWEEP_STEPS
    assert steps[0] == 0.50 and steps[-1] == 0.95 and len(steps) == 10
    assert "model build id(s):" in out  # spec §6.4: the build is recorded with results


def test_budget_case_surfaces_named_exhaustion_never_a_band():
    cases = cal.load_corpus()
    budget = next(case for case in cases if case.category == cal.CATEGORY_BUDGET)
    result = cal.run_hermetic_case(budget)
    assert result.surface == cal.SURFACE_BUDGET
    assert not result.verdict.ok or result.verdict.budget_exhausted
    assert result.verdict.abstain_reason in (None, "budget-exhausted")
    # the disclosure names what was dropped — "no room" is never "no code"
    if result.verdict.budget_exhausted and not result.verdict.abstain_reason:
        assert result.verdict.clip_disclosure or result.verdict.chars_dropped


def test_hermetic_case_ships_the_real_fixture_files_through_the_resolver_seam():
    """The runner seam serves real files: the manifest proves what was sent."""
    cases = cal.load_corpus()
    resolved = next(case for case in cases if case.id == "resolved-payments-negative-rejection")
    result = cal.run_hermetic_case(resolved)
    manifest_files = [entry.file for entry in result.verdict.manifest]
    assert "validator.py" in manifest_files
    assert "validator_checks.py" in manifest_files


def test_one_batched_jev_call_per_case():
    """The batch property (spec §2): one decisions request per case, three answers."""
    cases = cal.load_corpus()
    case = cases[0]
    runner = cal.FixtureRunner(cal.TREES_ROOT / case.workdir)
    runner.arm(case.seeds)
    endpoint = cal.MockEndpoint()
    endpoint.current_case = case
    from engine import resolution

    resolution.resolve(
        case.question,
        workdir=str(cal.TREES_ROOT / case.workdir),
        keys=[cal._fake_key()],
        poster=endpoint,
        runner=runner,
        traced=True,
        read_files=False,
    )
    assert len(endpoint.calls) == 1
    body = endpoint.calls[0]["body"]
    assert set(body["questions"]) == {"resolves", "missing_kind", "evidence_quality"}


# ── The gate: criterion 4, red-proven ────────────────────────────────────────


def test_gate_fails_loudly_when_near_miss_nouls_drift_above_resolved():
    """Calibration drift MUST trip the gate at the production bands."""
    cases = cal.load_corpus()
    for case in cases:
        if case.category == cal.CATEGORY_NEAR_MISS:
            case.mock_noul = 0.93
    results = [cal.run_hermetic_case(case) for case in cases]
    passed, failures = cal.build_gate(results)
    assert not passed
    assert any("FALSE POSITIVE" in failure for failure in failures)
    report = cal.gate_report(results)
    assert report.startswith("GATE FAIL")


def test_gate_passes_the_production_bands_on_the_checked_in_corpus():
    cases = cal.load_corpus()
    results = [cal.run_hermetic_case(case) for case in cases]
    passed, failures = cal.build_gate(results)
    assert passed, failures


def test_budget_case_that_bands_is_a_gate_failure():
    cases = cal.load_corpus()
    budget = next(case for case in cases if case.category == cal.CATEGORY_BUDGET)
    from engine.resolution import ResolutionVerdict

    verdict = ResolutionVerdict(question=budget.question, verdict="UNRESOLVED", probability=0.40)
    result = cal.CaseResult(case=budget, verdict=verdict, surface=cal.SURFACE_BAND)
    passed, failures = cal.build_gate([result])
    assert not passed
    assert any("BUDGET MISREAD" in failure for failure in failures)


# ── Sweep and matrix: the resolver's own band function does the bucketing ────


def test_sweep_rebands_with_the_resolver_band_function(monkeypatch):
    calls = []
    real_band_for = cal.band_for

    def spy(probability, *, resolved_at, review_at):
        calls.append(resolved_at)
        return real_band_for(probability, resolved_at=resolved_at, review_at=review_at)

    monkeypatch.setattr(cal, "band_for", spy)
    cases = cal.load_corpus()
    results = [cal.run_hermetic_case(case) for case in cases]
    rows = cal.run_sweep(results)
    assert len(rows) == 55  # 10x10 upper-triangular threshold pairs (review <= resolved)
    assert set(calls) == set(cal.SWEEP_STEPS)


def test_confusion_matrix_buckets_and_excludes_the_budget_surface():
    from engine.resolution import ResolutionVerdict

    def verdict_for(probability):
        return ResolutionVerdict(question="q", verdict="x", probability=probability)

    def case_for(case_id, category):
        return cal.CorpusCase(
            id=case_id,
            category=category,
            question="q",
            workdir="w",
            seeds=[],
            expect_missing_kind=None,
            expect_surface=None,
            note="",
        )

    results = [
        cal.CaseResult(case_for("r1", cal.CATEGORY_RESOLVED), verdict_for(0.90), cal.SURFACE_BAND),
        cal.CaseResult(case_for("r2", cal.CATEGORY_RESOLVED), verdict_for(0.60), cal.SURFACE_BAND),
        cal.CaseResult(case_for("n1", cal.CATEGORY_NEAR_MISS), verdict_for(0.90), cal.SURFACE_BAND),
        cal.CaseResult(case_for("n2", cal.CATEGORY_NEAR_MISS), verdict_for(0.40), cal.SURFACE_BAND),
        cal.CaseResult(
            case_for("u1", cal.CATEGORY_UNRESOLVED), verdict_for(0.10), cal.SURFACE_BAND
        ),
        cal.CaseResult(case_for("b1", cal.CATEGORY_BUDGET), verdict_for(0.90), cal.SURFACE_BUDGET),
    ]
    matrix = cal.confusion_matrix(results, 0.85, 0.50)
    assert (matrix[cal.CATEGORY_RESOLVED].tp, matrix[cal.CATEGORY_RESOLVED].fn) == (1, 1)
    assert matrix[cal.CATEGORY_NEAR_MISS].fp == 1
    assert matrix[cal.CATEGORY_UNRESOLVED].tn == 1
    assert cal.CATEGORY_BUDGET not in matrix  # no band, no bucket


def test_band_boundary_belongs_to_the_better_band_in_the_matrix():
    from engine.resolution import ResolutionVerdict

    verdict = ResolutionVerdict(question="q", verdict="x", probability=0.85)
    case = cal.CorpusCase(
        id="r",
        category=cal.CATEGORY_RESOLVED,
        question="q",
        workdir="w",
        seeds=[],
        expect_missing_kind=None,
        expect_surface=None,
        note="",
    )
    matrix = cal.confusion_matrix([cal.CaseResult(case, verdict, cal.SURFACE_BAND)], 0.85, 0.50)
    assert matrix[cal.CATEGORY_RESOLVED].tp == 1  # 0.85 >= 0.85 is RESOLVED


# ── Live mode: the production credential path, proven by seam ────────────────


def test_live_mode_hands_the_production_credential_path_to_the_resolver(monkeypatch):
    """keys=None + poster=None IS the production credential path: discover_keys,
    per-key failover, the real endpoint. Assembly stays on the labeled trees."""
    recorded = {}

    def fake_resolve(question, **kwargs):
        recorded.update(kwargs)
        from engine.resolution import ResolutionVerdict

        return ResolutionVerdict(question=question, verdict="UNRESOLVED", probability=0.1)

    monkeypatch.setattr(cal.resolution, "resolve", fake_resolve)
    cases = cal.load_corpus()
    result = cal.run_live_case(cases[0])
    assert recorded["keys"] is None  # -> engine.resolution.discover_keys()
    assert recorded["poster"] is None  # -> engine.resolution._default_poster
    assert isinstance(recorded["runner"], cal.FixtureRunner)  # labeled trees
    assert recorded["max_tokens"] == cal.resolution.MAX_BUNDLE_TOKENS
    assert result.surface in (cal.SURFACE_BAND, cal.SURFACE_BUDGET)


def test_record_rejects_nothing_and_replay_applies_only_valid_records(tmp_path):
    records = {
        "resolved-payments-negative-rejection": {
            "id": "resolved-payments-negative-rejection",
            "model": "typesafe/jev-1.13-live",
            "probability": 0.91,
        },
        "unresolved-payments-negative-rejection": {
            "id": "unresolved-payments-negative-rejection",
            "probability": 7.5,  # out of range — must be skipped
        },
        "near-miss-payments-positive-validation-mentions": {
            "id": "near-miss-payments-positive-validation-mentions",
            "probability": "0.61",  # string is not applied
        },
    }
    cases = cal.load_corpus()
    applied = cal.apply_records(cases, records)
    assert applied == ["resolved-payments-negative-rejection"]
    pinned = {case.id: case for case in cases}
    assert pinned["resolved-payments-negative-rejection"].mock_noul == 0.91
    assert pinned["resolved-payments-negative-rejection"].model == "typesafe/jev-1.13-live"
    assert pinned["unresolved-payments-negative-rejection"].mock_noul != 7.5
