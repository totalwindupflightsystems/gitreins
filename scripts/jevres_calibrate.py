#!/usr/bin/env python3
"""JEV-RESOLUTION calibration harness (JEVRES-005).

Drives the REAL resolver — ``engine.resolution.resolve`` with its two seams
injected, never a re-implementation of the pipeline — over a labeled corpus of
(question, repo-state, ground-truth) cases, sweeps the band thresholds, prints
a confusion matrix per threshold pair, and FAILS the run when the production
bands (RESOLVED >= 0.85, REVIEW >= 0.50) produce a false positive on the
near-miss cases.

Ground truth is INDEPENDENT of the resolver: every case names a fixture tree
under ``tests/fixtures/jevres_cases/<workdir>`` whose files physically contain
(or lack) the behaviour the question asks about — a fixed ``validate_amount``
and a check exercising it for ``resolved``, the pre-fix pass-through for the
``unresolved`` half of the same pair (same question, measured in both states),
a mentions-only module / an implemented-but-unwired check / an implemented
limiter whose only test asserts the wrong thing for the three near-miss
kinds, and a budget-starved case whose honest answer cannot fit the ceiling.

Categories and the surface each must produce:

======================  ====================================================
category                honest surface
======================  ====================================================
``resolved``            RESOLVED at production bands (0.5 <= p < 0.85 is a
                        REVIEW — a weak-positive, never a false RESOLVED)
``unresolved``          UNRESOLVED at production bands; REVIEW tolerable,
                        RESOLVED is a false positive (counted as one)
``near_miss_negative``  same as unresolved: it must never read as RESOLVED
``budget_starved``      named exhaustion: ABSTAIN reason ``budget-exhausted``
                        OR a verdict with ``budget_exhausted=true`` — never
                        an UNRESOLVED band
======================  ====================================================

Modes
    hermetic (default)  no API key, no network, no hilo binary: a canned
                        poster supplies the typed answers the corpus records
                        per case, a fixture runner serves checked-in trees
                        through the resolver's real ``runner`` seam, and keys
                        are runtime-built placeholders.
    live                ``--live``: calibrates for real — ``keys=None`` and
                        ``poster=None`` hand the call to the production
                        ``discover_keys()``/``_default_poster`` with per-key
                        failover, and the Jev build id is recorded with every
                        row (spec §6.4: calibration drifts between builds).
                        Assembly still serves the checked-in labeled trees:
                        the corpus design requires the EXACT repo state per
                        case, which production hilo trace cannot guarantee
                        (verified live: toy trees graph as ``pkg:``, search
                        finds nothing).
    record/replay       ``--live --record`` persists per-case live numbers to
                        ``.gitreins/jevres-recorded-numbers.json``; hermetic
                        runs replay them so the sweep exercises REAL
                        probabilities without a network. Key material is
                        never written.

Usage
    python3 scripts/jevres_calibrate.py                    # gate + matrix
    python3 scripts/jevres_calibrate.py --sweep-only       # matrix, exit 0
    python3 scripts/jevres_calibrate.py --live             # real calibration
    python3 scripts/jevres_calibrate.py --live --record    # persist numbers
    python3 scripts/jevres_calibrate.py --json-out r.json  # machine-readable

Exit codes
    0  gate passed (or ``--sweep-only``)
    1  gate failure: a near-miss/unresolved case read as RESOLVED at the
       production bands, or a budget case misread as a band at all
    2  usage/environment error (unreadable corpus, unknown fixture tree)
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from engine import resolution  # noqa: E402  (path set above, deliberately)
from engine.resolution import (  # noqa: E402
    RESOLVED_AT,
    REVIEW_AT,
    ResolutionVerdict,
    VERDICT_RESOLVED,
    band_for,
)

CORPUS_PATH = REPO_ROOT / "tests" / "fixtures" / "jevres_cases" / "corpus.jsonl"
TREES_ROOT = REPO_ROOT / "tests" / "fixtures" / "jevres_cases"
RECORD_PATH = REPO_ROOT / ".gitreins" / "jevres-recorded-numbers.json"

CATEGORY_RESOLVED = "resolved"
CATEGORY_UNRESOLVED = "unresolved"
CATEGORY_NEAR_MISS = "near_miss_negative"
CATEGORY_BUDGET = "budget_starved"
ALL_CATEGORIES = (
    CATEGORY_RESOLVED,
    CATEGORY_UNRESOLVED,
    CATEGORY_NEAR_MISS,
    CATEGORY_BUDGET,
)
BAND_CATEGORIES = (CATEGORY_RESOLVED, CATEGORY_UNRESOLVED, CATEGORY_NEAR_MISS)

#: The sweep the acceptance criteria name: 0.50 to 0.95 inclusive, step 0.05.
SWEEP_STEPS = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]

PRODUCTION_RESOLVED_AT = RESOLVED_AT  # 0.85 — spec §3.5
PRODUCTION_REVIEW_AT = REVIEW_AT  # 0.50

#: The bundle ceiling the resolver enforces. The budget-starved case must hit
#: the named exhaustion surface at THIS ceiling, not at a special one.
BUDGET_CASE_CEILING = resolution.MAX_BUNDLE_TOKENS

SURFACE_BAND = "band"
SURFACE_BUDGET = "budget_exhausted"


def _fake_key() -> str:
    """An OpenRouter-shaped placeholder assembled at runtime.

    Concatenation on purpose: no ``sk-`` literal with 20+ trailing characters
    exists in this file, so the secrets guard has nothing to refuse — the same
    shape ``tests/test_resolution.py`` already ships.
    """
    return "sk-or-" + "v1-calibration-" + "0" * 16


# ── Corpus ───────────────────────────────────────────────────────────────────


@dataclass
class CorpusCase:
    """One labeled (question, repo-state, ground-truth) record."""

    id: str
    category: str
    question: str
    workdir: str
    seeds: list[str]
    expect_missing_kind: str | None
    expect_surface: str | None
    note: str
    # The noul the mock endpoint answers in hermetic mode. A --live --record
    # run OVERWRITES it from the recorded file so hermetic replays exercise
    # the real probabilities.
    mock_noul: float = 0.05
    model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "expect_missing_kind": self.expect_missing_kind,
            "expect_surface": self.expect_surface,
            "mock_noul": self.mock_noul,
            "model": self.model,
        }


def load_corpus(path: Path = CORPUS_PATH) -> list[CorpusCase]:
    """Parse the JSONL corpus; a malformed line is a loud failure, not a skip."""
    cases: list[CorpusCase] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            raw = json.loads(stripped)
            try:
                case = CorpusCase(
                    id=raw["id"],
                    category=raw["category"],
                    question=raw["question"],
                    workdir=raw["workdir"],
                    seeds=list(raw.get("seeds", [])),
                    expect_missing_kind=raw.get("expect_missing_kind"),
                    expect_surface=raw.get("expect_surface"),
                    note=raw.get("note", ""),
                    mock_noul=float(raw["mock_noul"]) if "mock_noul" in raw else 0.05,
                )
            except KeyError as exc:
                raise SystemExit(
                    f"corpus error: line {number} is missing required field {exc}"
                ) from exc
            if case.id in seen:
                raise SystemExit(f"corpus error: duplicate case id {case.id!r} on line {number}")
            seen.add(case.id)
            cases.append(case)
    return cases


def validate_corpus(cases: list[CorpusCase]) -> None:
    """Fail loudly when the corpus or its trees are malformed.

    Independent ground truth has structural requirements: the named fixture
    tree must exist, every ``resolved`` tree must carry a ``*_checks.py``
    acceptance file (the corpus label claims a check exercises the behaviour —
    that is exactly the file that must exist), and all four categories plus a
    resolved/unresolved PAIR (same question, both states) must be present.
    """
    if not cases:
        raise SystemExit("corpus error: no cases")
    categories = {case.category for case in cases}
    missing = [name for name in ALL_CATEGORIES if name not in categories]
    if missing:
        raise SystemExit(f"corpus error: categories with no cases: {', '.join(missing)}")

    for case in cases:
        tree = TREES_ROOT / case.workdir
        if not tree.is_dir():
            raise SystemExit(f"corpus error: case {case.id!r} names unknown tree {case.workdir!r}")
        if case.category == CATEGORY_RESOLVED and not any(
            path.name.endswith("_checks.py") for path in tree.iterdir()
        ):
            raise SystemExit(
                f"corpus error: resolved case {case.id!r} tree {case.workdir!r} "
                "carries no *_checks.py acceptance file"
            )

    resolved_questions = {case.question for case in cases if case.category == CATEGORY_RESOLVED}
    unresolved_questions = {case.question for case in cases if case.category == CATEGORY_UNRESOLVED}
    if not resolved_questions & unresolved_questions:
        raise SystemExit(
            "corpus error: no resolved/unresolved PAIR — the same question must be "
            "measured in both states (corpus design, not a preference)"
        )


# ── The two seams ────────────────────────────────────────────────────────────


class FixtureRunner:
    """Serves a checked-in fixture tree through the resolver's ``runner`` seam.

    Answers the three hilo shapes ``engine.resolution`` parses — ``graph
    search``, ``graph related``, ``graph understand`` — from the tree the case
    names, so hermetic mode exercises the REAL trace→assemble→pack pipeline
    over REAL files instead of a stubbed bundle. File contents are read from
    disk at call time.
    """

    def __init__(self, tree: Path) -> None:
        self.tree = tree
        self.calls: list[list[str]] = []
        self.seed_output: str = ""
        self._seeds: list[str] = []

    def __call__(self, args: list[str], workdir: str) -> tuple[int, str, str]:
        self.calls.append(args)
        if args[:2] == ["graph", "search"]:
            return 0, self.seed_output, ""
        if args[:2] == ["graph", "related"]:
            return 0, f"No incoming edges for {args[2]!r}.\n", ""
        if args[:2] == ["graph", "understand"]:
            return 0, self.bundle_transcript(), ""
        return 1, "", f"unexpected hilo invocation: {args}"

    def arm(self, seeds: list[str]) -> None:
        """Set the ranked seeds the search transcript will report."""
        self._seeds = list(seeds)
        lines = [
            f"{0.99 - rank * 0.01:.4f}  {seed}  [lexical]"
            for rank, seed in enumerate(self._seeds, start=1)
        ]
        self.seed_output = "\n".join(lines) + "\n" if lines else ""

    def bundle_transcript(self) -> str:
        """``hilo graph understand`` output: every non-excluded file as DETAIL."""
        files = sorted(
            path
            for path in self.tree.iterdir()
            if path.is_file() and not resolution.is_excluded_path(path.name)
        )
        sections = ["## MAP", "", "## SIGNATURES", "", "## DETAIL", ""]
        for path in files:
            text = path.read_text(encoding="utf-8", errors="replace").rstrip("\n")
            sections.append(f"{path.name} [provenance=ast_exact, score=0.99]")
            sections.append(text)
            sections.append("")
        return "\n".join(sections)


class MockEndpoint:
    """A canned decisions endpoint for hermetic mode.

    Builds the exact answer shape ``engine.resolution.parse_answers`` accepts
    (verified against the live payload recorded 2026-09-20). The probability
    is the corpus case's ``mock_noul``; the ``missing_kind`` choice is the
    case's expected kind, so the matrix exercises the full typed payload.
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.current_case: CorpusCase | None = None

    def __call__(self, endpoint: str, key: str, body: dict[str, Any], timeout: int):
        self.calls.append({"endpoint": endpoint, "body": body})
        return _CannedResponse(200, self._payload_for())

    def _payload_for(self) -> dict[str, Any]:
        case = self.current_case
        if case is None:  # pragma: no cover - the harness always sets it
            raise AssertionError("mock endpoint called outside a case")
        return {
            "model": case.model or "typesafe/jev-1.13-mock",
            "answers": {
                "resolves": {"type": "noul", "noul": case.mock_noul},
                "missing_kind": {
                    "type": "choice",
                    "choice": case.expect_missing_kind or "none",
                    "probabilities": {
                        "none": 0.05,
                        "implementation": 0.70,
                        "test": 0.15,
                        "wiring": 0.05,
                        "config": 0.03,
                        "docs": 0.02,
                    },
                    "confidence": 0.9,
                },
                "evidence_quality": {
                    "type": "score",
                    "score": 1.5,
                    "legend": {
                        "0": "the bundle only mentions the topic",
                        "1": "adjacent code",
                        "2": "the exact code path is present",
                        "3": "the exact path plus its test",
                    },
                    "probabilities": {"0": 0.2, "1": 0.4, "2": 0.3, "3": 0.1},
                    "confidence": 0.8,
                },
            },
            "usage": {"input_tokens": 512, "output_tokens": 96, "cost": 2.15e-05},
            "id": "gen-dec-calibration-0001",
            "provider": "TypeSafe",
        }


class _CannedResponse:
    """Minimal ``requests``-shaped response for the mock endpoint."""

    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self) -> dict[str, Any]:
        return self._payload


# ── Running the corpus ───────────────────────────────────────────────────────


@dataclass
class CaseResult:
    """One case's verdict plus the accounting the report needs."""

    case: CorpusCase
    verdict: ResolutionVerdict
    # Which surface the case produced, from the calibration's point of view:
    # "band" (a decision band) or "budget_exhausted" (named exhaustion).
    surface: str

    @property
    def probability(self) -> float | None:
        return self.verdict.probability

    @property
    def model(self) -> str | None:
        return self.verdict.model

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.case.id,
            "category": self.case.category,
            "surface": self.surface,
            "band": self.verdict.verdict,
            "probability": self.probability,
            "missing_kind": self.verdict.missing_kind,
            "budget_exhausted": self.verdict.budget_exhausted,
            "abstain_reason": self.verdict.abstain_reason,
            "model": self.model,
            "tokens_estimated": self.verdict.tokens_estimated,
            "clipped": self.verdict.clipped,
            "chars_dropped": self.verdict.chars_dropped,
            "manifest_files": [entry.file for entry in self.verdict.manifest],
        }


def classify_surface(verdict: ResolutionVerdict) -> str:
    """Read the verdict's surface for the confusion matrix.

    A named budget exhaustion (ABSTAIN reason ``budget-exhausted`` or a
    shipped bundle flagged ``budget_exhausted``) is its own surface — it is a
    DIFFERENT failure from an UNRESOLVED band ("no room" is not "no code") and
    the gate treats it that way.
    """
    if (
        verdict.verdict == resolution.VERDICT_ABSTAIN
        and verdict.abstain_reason == "budget-exhausted"
    ):
        return SURFACE_BUDGET
    if verdict.budget_exhausted:
        return SURFACE_BUDGET
    return SURFACE_BAND


def run_hermetic_case(case: CorpusCase) -> CaseResult:
    """Run one case with both seams injected: no key, no network, no hilo."""
    tree = TREES_ROOT / case.workdir
    runner = FixtureRunner(tree)
    runner.arm(case.seeds)
    endpoint = MockEndpoint()
    endpoint.current_case = case

    verdict = resolution.resolve(
        case.question,
        workdir=str(tree),
        keys=[_fake_key()],
        poster=endpoint,
        runner=runner,
        traced=True,
        read_files=False,
        max_tokens=BUDGET_CASE_CEILING,
    )
    return CaseResult(case=case, verdict=verdict, surface=classify_surface(verdict))


def run_live_case(case: CorpusCase) -> CaseResult:
    """Run one case against the REAL endpoint with the production key path.

    What is live, and what is not:

    - LIVE — credentials and transport: ``keys=None`` hands the call to
      ``engine.resolution.discover_keys()`` (env first, then the known .env
      files) with per-key failover inside :func:`engine.resolution.call_jev`,
      and ``poster=None`` posts through the real ``_default_poster`` to the
      real endpoint. The probabilities and the model build id this returns
      are the calibration numbers (spec §6.4).
    - HERMETIC — assembly: the checked-in tree is served through
      :class:`FixtureRunner`. Real hilo cannot trace these trees at all (a
      one-file tree graphs as ``pkg:validator``, which ``trace_question``
      drops by design, and its BM25 search needs a real corpus — both
      verified live), and the corpus design requires the EXACT labeled repo
      state per case, so production trace would defeat the labels.

    No key material crosses this script in either direction: candidates are
    reported by label inside the resolver's own attempt log only.
    """
    tree = TREES_ROOT / case.workdir
    runner = FixtureRunner(tree)
    runner.arm(case.seeds)
    verdict = resolution.resolve(
        case.question,
        workdir=str(tree),
        keys=None,
        poster=None,
        runner=runner,
        traced=True,
        read_files=False,
        max_tokens=BUDGET_CASE_CEILING,
    )
    return CaseResult(case=case, verdict=verdict, surface=classify_surface(verdict))


# ── Confusion matrix and sweep ───────────────────────────────────────────────


@dataclass
class Cell:
    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None


@dataclass
class SweepRow:
    """One threshold pair's confusion summary over the band categories."""

    resolved_at: float
    review_at: float
    cells: dict[str, Cell] = field(default_factory=dict)

    @property
    def near_miss_false_positives(self) -> int:
        total = 0
        for category in (CATEGORY_NEAR_MISS, CATEGORY_UNRESOLVED):
            total += self.cells.get(category, Cell()).fp
        return total

    def as_dict(self) -> dict[str, Any]:
        return {
            "resolved_at": self.resolved_at,
            "review_at": self.review_at,
            "cells": {category: vars(cell) for category, cell in self.cells.items()},
            "near_miss_and_unresolved_false_positives": self.near_miss_false_positives,
        }


def confusion_matrix(
    results: list[CaseResult], resolved_at: float, review_at: float
) -> dict[str, Cell]:
    """Per-category band confusion at one threshold pair.

    Positive class = "the resolver says RESOLVED". Ground truth per category:
    ``resolved`` cases should be positive; ``unresolved`` and
    ``near_miss_negative`` should be negative; ``budget_starved`` cases do not
    take a band at all (their honest surface is named exhaustion) so they sit
    outside the matrix.
    """

    def bucket(case_category: str, band: str) -> str:
        if case_category == CATEGORY_RESOLVED:
            return "tp" if band == VERDICT_RESOLVED else "fn"
        return "fp" if band == VERDICT_RESOLVED else "tn"

    matrix: dict[str, Cell] = {}
    for result in results:
        if result.surface != SURFACE_BAND:
            continue
        band = band_for(result.probability, resolved_at=resolved_at, review_at=review_at)
        cell = matrix.setdefault(result.case.category, Cell())
        slot = bucket(result.case.category, band)
        setattr(cell, slot, getattr(cell, slot) + 1)
    return matrix


def print_matrix(
    title: str,
    matrix: dict[str, Cell],
    *,
    stream: Any = None,
) -> None:
    """The acceptance-criteria table: rows = categories, cols = the 4 outcomes.

    ``stream=None`` lets ``print`` resolve the CURRENT ``sys.stdout`` per call
    (a default of ``sys.stdout`` would bind the object at import time and
    silently escape any caller's output capture).
    """
    print(f"\n{title}", file=stream)
    header = f"{'category':<22}{'TP':>6}{'FP':>6}{'FN':>6}{'TN':>6}{'precision':>11}{'recall':>9}"
    print(header, file=stream)
    print("-" * len(header), file=stream)
    for category in BAND_CATEGORIES:
        cell = matrix.get(category) or Cell()
        precision = "n/a" if cell.precision is None else f"{cell.precision:.2f}"
        recall = "n/a" if cell.recall is None else f"{cell.recall:.2f}"
        print(
            f"{category:<22}{cell.tp:>6}{cell.fp:>6}{cell.fn:>6}{cell.tn:>6}"
            f"{precision:>11}{recall:>9}",
            file=stream,
        )


def run_sweep(results: list[CaseResult], *, stream: Any = None) -> list[SweepRow]:
    """Re-band every measured probability over the 0.50→0.95 sweep.

    Re-banding uses the resolver's OWN :func:`engine.resolution.band_for` —
    the bands live in code (spec §3.5), so the sweep evaluates the real band
    function rather than a harness-side copy of it. ``stream=None`` resolves
    the current ``sys.stdout`` per call (see :func:`print_matrix`).
    """
    rows: list[SweepRow] = []
    print("\nThreshold sweep (per-category TP/FP/FN/TN at each threshold pair):", file=stream)
    for resolved_at in SWEEP_STEPS:
        for review_at in SWEEP_STEPS:
            if review_at > resolved_at:
                continue
            cells = confusion_matrix(results, resolved_at, review_at)
            rows.append(SweepRow(resolved_at=resolved_at, review_at=review_at, cells=cells))
            summary = "  ".join(
                f"{category}={cells[category].tp}/{cells[category].fp}/{cells[category].fn}/{cells[category].tn}"
                if category in cells
                else f"{category}=-"
                for category in BAND_CATEGORIES
            )
            print(f"  >={resolved_at:.2f}/>={review_at:.2f}  {summary}", file=stream)
    return rows


# ── The gate ─────────────────────────────────────────────────────────────────


def build_gate(results: list[CaseResult]) -> tuple[bool, list[str]]:
    """The regression gate at the PRODUCTION bands (spec §3.5).

    Failures, and nothing else is a failure:
      1. any near-miss or unresolved case whose surface is a band reads
         RESOLVED at 0.85/0.50 — the false positive this harness exists to
         catch;
      2. any budget case whose surface is a band at all — a budget-starved
         question must surface named exhaustion, NEVER a decision band;
      3. a budget case that landed neither named exhaustion nor a band means
         the surface classification failed — also loud.
    """
    failures: list[str] = []
    for result in results:
        category = result.case.category
        if category in (CATEGORY_NEAR_MISS, CATEGORY_UNRESOLVED):
            if result.surface == SURFACE_BAND:
                band = band_for(
                    result.probability,
                    resolved_at=PRODUCTION_RESOLVED_AT,
                    review_at=PRODUCTION_REVIEW_AT,
                )
                if band == VERDICT_RESOLVED:
                    failures.append(
                        f"FALSE POSITIVE [{category}] {result.case.id}: banded RESOLVED "
                        f"(p={result.probability}) on content that does not resolve the question"
                    )
        elif category == CATEGORY_BUDGET:
            if result.surface == SURFACE_BAND:
                failures.append(
                    f"BUDGET MISREAD {result.case.id}: banded {result.verdict.verdict} — "
                    "a budget-starved question must surface named exhaustion, never a band"
                )
            elif result.surface != SURFACE_BUDGET:
                failures.append(
                    f"BUDGET UNCLASSIFIED {result.case.id}: surface was {result.surface!r}"
                )
    return (not failures), failures


def gate_report(results: list[CaseResult]) -> str:
    passed, failures = build_gate(results)
    if passed:
        return (
            "GATE PASS: no near-miss/unresolved false positives and no budget misreads "
            f"at the production bands (RESOLVED>={PRODUCTION_RESOLVED_AT}, "
            f"REVIEW>={PRODUCTION_REVIEW_AT})"
        )
    return "\n".join(["GATE FAIL:"] + [f"  - {failure}" for failure in failures])


# ── Record/replay (live numbers persisted for hermetic replay) ───────────────


def record_live_numbers(cases: list[CorpusCase], results: list[CaseResult], path: Path) -> None:
    """Persist per-case live numbers so a hermetic replay can pin them.

    Calibration is against REAL probabilities; hermetic mode replays the last
    ``--record``ed run instead of the corpus defaults. Key material is never
    written — model id, probability, missing kind only.
    """
    by_id = {result.case.id: result for result in results}
    rows = []
    for case in cases:
        result = by_id.get(case.id)
        rows.append(
            {
                "id": case.id,
                "category": case.category,
                "model": result.model if result else None,
                "probability": result.probability if result else None,
                "missing_kind": result.verdict.missing_kind if result else None,
            }
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    print(f"recorded {len(rows)} case record(s) to {path}")


def load_record(path: Path) -> dict[str, dict[str, Any]]:
    """Load the recorded-numbers file; a missing or unreadable file is empty."""
    if not path.is_file():
        return {}
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(rows, list):
        return {}
    return {row["id"]: row for row in rows if isinstance(row, dict) and "id" in row}


def apply_records(cases: list[CorpusCase], records: dict[str, dict[str, Any]]) -> list[str]:
    """Pin hermetic mock answers to the last --live --record numbers.

    A record with a missing/unreadable probability is skipped — replay then
    uses the corpus default for that case rather than failing.
    """
    applied: list[str] = []
    for case in cases:
        row = records.get(case.id) or {}
        probability = row.get("probability")
        if isinstance(probability, (int, float)) and not isinstance(probability, bool):
            if 0.0 <= float(probability) <= 1.0:
                case.mock_noul = float(probability)
                applied.append(case.id)
        model = row.get("model")
        if isinstance(model, str) and model:
            case.model = model
    return applied


# ── Main ─────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--corpus",
        type=Path,
        default=CORPUS_PATH,
        help="path to the labeled corpus (default: tests/fixtures/jevres_cases/corpus.jsonl)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "calibrate for real: production key discovery/failover and the real "
            "endpoint (assembly still serves the checked-in labeled trees)"
        ),
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help="with --live: persist per-case numbers for hermetic replay",
    )
    parser.add_argument(
        "--sweep-only",
        action="store_true",
        help="print the sweep and exit 0 without enforcing the gate",
    )
    parser.add_argument(
        "--json-out",
        type=Path,
        default=None,
        help="write machine-readable results (per-case + sweep + gate) to this path",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.record and not args.live:
        parser.error("--record requires --live")

    cases = load_corpus(args.corpus)
    validate_corpus(cases)

    if not args.live:
        applied = apply_records(cases, load_record(RECORD_PATH))
        if applied:
            print(
                f"replaying recorded calibration numbers for {len(applied)}/{len(cases)} "
                f"case(s) ({RECORD_PATH.name}); run with --live --record to refresh"
            )

    results: list[CaseResult] = []
    for case in cases:
        runner = run_live_case if args.live else run_hermetic_case
        result = runner(case)
        results.append(result)
        probability = result.probability
        print(
            f"[{result.case.category:<18}] {result.case.id:<48} "
            f"surface={result.surface:<16} band={result.verdict.verdict:<10} "
            f"p={'n/a' if probability is None else format(probability, '.3f')}"
        )

    models = sorted({result.model for result in results if result.model})
    if models:
        print(f"\nmodel build id(s): {', '.join(models)}")

    matrix = confusion_matrix(results, PRODUCTION_RESOLVED_AT, PRODUCTION_REVIEW_AT)
    print_matrix(
        f"Confusion matrix at production bands "
        f"(RESOLVED>={PRODUCTION_RESOLVED_AT}, REVIEW>={PRODUCTION_REVIEW_AT})",
        matrix,
    )
    sweep_rows = run_sweep(results)

    if args.live and args.record:
        record_live_numbers(cases, results, RECORD_PATH)

    passed, failures = build_gate(results)
    print()
    print(gate_report(results))

    if args.json_out:
        args.json_out.write_text(
            json.dumps(
                {
                    "mode": "live" if args.live else "hermetic",
                    "models": models,
                    "cases": [result.to_dict() for result in results],
                    "sweep": [row.as_dict() for row in sweep_rows],
                    "gate": {"passed": passed, "failures": failures},
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"wrote {args.json_out}")

    if args.sweep_only:
        return 0
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
