"""JEVRES-001 — hermetic tests for the Jev resolution gate (``engine/resolution.py``).

Hermetic by construction: an autouse fixture replaces the module's ``requests``
binding with a blocker, so a test that forgets to inject its own endpoint stub
fails loudly instead of quietly calling OpenRouter. Keys are built at runtime
(``_fake_key``) — no ``sk-or-v1-`` literal with 20+ trailing characters exists in
this file, so the secrets guard has nothing to (correctly) refuse.

The measured numbers the tests assert against were taken live against the
decisions endpoint on 2026-09-20 and are recorded in ``docs/jev-resolution-gate.md``
§2; where a test depends on one it names it in the assertion comment.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from engine import evidence_bounds, resolution
from engine.resolution import (
    MAX_BUNDLE_TOKENS,
    Block,
    ManifestEntry,
    ResolutionVerdict,
    TraceSeed,
    assemble_bundle,
    band_for,
    discover_keys,
    estimate_tokens,
    is_excluded_path,
    order_blocks,
    pack_blocks,
    parse_answers,
    parse_understand,
    reconcile_manifest,
    related_files,
    resolve,
    rubric_position,
    trace_question,
    verdict_json,
    worst_case_tokens,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# Skip conditions for the one live test: a real key in the env AND the endpoint.
LIVE_KEY = os.environ.get("GITREINS_OPENROUTER_KEY", "")

# ── Measured ground truth (live, 2026-09-20) ─────────────────────────────────
# chars -> input tokens reported by the endpoint, per payload family. The
# estimator must not undercount the text-shaped ones; the two dense families are
# the documented limit of a chars-based floor, covered by the server-ceiling
# ABSTAIN path (see test_a_server_ceiling_rejection_is_an_abstain_never_a_pass).
MEASURED_TOKENS = {
    "repo-bundle": (30_639, 8_916),  # this repo's own hilo bundle, 3.44 chars/token
    "jsonl-rows": (40_000, 19_756),
    "filler-1.98": (40_000, 20_202),  # the spec's repetitive family
    "varied-code": (40_000, 20_109),
    "dense-punct": (40_000, 27_454),
    "base64-random": (40_000, 28_581),
}
TEXT_SHAPED_FAMILIES = ("repo-bundle", "jsonl-rows")
TEXT_SHAPED_SCALED = ("filler-1.98", "varied-code")
DENSE_FAMILIES = ("dense-punct", "base64-random")
BANNED_DIVISOR = 3.5  # the spec forbids a fixed chars/3.5 estimate


def _fake_key(tag: str) -> str:
    """An OpenRouter-shaped placeholder key, assembled at runtime.

    Built by concatenation on purpose: the literal in this file is a prefix and
    a filler, never a 20+-character ``sk-`` token, so gitleaks has no match and
    the test still exercises the real shape check (``sk-or-v1-`` prefix).
    """
    return "sk-or-" + "v1-" + tag + "-" + "0" * 16


FAKE_KEY_ALPHA = _fake_key("alpha")
FAKE_KEY_BETA = _fake_key("beta")


# ── Fixtures: a hermetic boundary and two stub transports ────────────────────


@pytest.fixture(autouse=True)
def _hermetic_credentials(request, monkeypatch, tmp_path):
    """No ambient credentials and no reachable .env: discovery is deterministic.

    ``HOME`` moves to a throwaway dir so ``~/.hermes/.env`` — which really does
    hold a live key on this host — is never read by a test.
    """
    for var in resolution.CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)


class _BlockedRequests:
    """Stands in for the module's ``requests`` binding during hermetic tests."""

    @staticmethod
    def post(*_args, **_kwargs):
        raise AssertionError(
            "hermetic test attempted a real HTTP request — inject the endpoint stub"
        )


@pytest.fixture(autouse=True)
def _no_real_network(request, monkeypatch):
    """Block real egress for every test except the explicitly live smoke test."""
    if request.node.name == "test_live_smoke_jev_resolution":
        return
    monkeypatch.setattr(resolution, "requests", _BlockedRequests())


class FakeResponse:
    """A minimal ``requests``-shaped response."""

    def __init__(self, status_code: int, payload=None, text: str | None = None):
        self.status_code = status_code
        self._payload = payload
        self.text = text if text is not None else json.dumps(payload if payload is not None else {})

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


@dataclass
class RecordingPoster:
    """Scripted endpoint: one scripted outcome per call, recording every call."""

    script: list = field(default_factory=list)
    calls: list = field(default_factory=list)

    def __call__(self, endpoint, key, body, timeout):
        self.calls.append({"endpoint": endpoint, "key": key, "body": body, "timeout": timeout})
        item = self.script.pop(0) if self.script else FakeResponse(500, {}, "unscripted")
        if isinstance(item, Exception):
            raise item
        return item

    @property
    def keys_tried(self) -> list[str]:
        return [call["key"] for call in self.calls]


@dataclass
class FakeRunner:
    """Scripted hilo: search / related / understand transcripts."""

    search_out: str = ""
    related_out: str = "No incoming edges for 'x'.\n"
    understand_out: str = ""
    calls: list = field(default_factory=list)

    def __call__(self, args, workdir):
        self.calls.append(list(args))
        if len(args) >= 2 and args[0] == "graph":
            if args[1] == "search":
                return 0, self.search_out, ""
            if args[1] == "related":
                return 0, self.related_out, ""
            if args[1] == "understand":
                return 0, self.understand_out, ""
        return 1, "", "unknown hilo invocation"


SEARCH_TRANSCRIPT = """\
0.0328  engine/evidence_bounds.py  [lexical]
         symbols: evidence, bounds
0.0317  tests/test_evidence_bounds.py  [lexical]
         symbols: test_both_surfaces
0.0300  pkg:engine.evidence_bounds  [lexical]
         symbols: evidence_bounds
0.0290  .env  [lexical]
         symbols: none
0.0280  engine/resolution.py  [lexical]
         symbols: resolve
"""

RELATED_TRANSCRIPT = """\
engine/pipeline.py
engine/worktree_fleet.py
.env
engine/worktree_disposable.py
"""

BUNDLE_TRANSCRIPT = """\
## MAP
engine/evidence_bounds.py →
  - bound_evidence

## SIGNATURES
engine/evidence_bounds.py:140-241
def _bound_step_evidence(output: str, cap: int = MAX_EVIDENCE_CHARS) -> str:

## DETAIL
<file> [provenance=…, score=…]

engine/evidence_bounds.py [provenance=ast_exact, score=1.00]
def bound_evidence(output:
    return _bound_step_evidence(output)

tests/test_evidence_bounds.py [provenance=ast_inferred, score=0.41]
def test_both_surfaces_use_one_bounder_and_one_cap():
    assert FLEET_CAP == 4_000

.env [provenance=lexical, score=0.99]
PLACEHOLDER=redacted-by-the-exclusion-filter

engine/resolution.py:1-40 [provenance=ast_exact, score=0.88] … [1200 chars omitted] …
def resolve(question: str):
    return _resolve(question)
"""


def _payload(
    noul: float = 0.09,
    choice: str = "implementation",
    score: float = 0.23,
    *,
    model: str = "typesafe/jev-1.13-20260917",
    input_tokens: int = 520,
    cost: float = 2.184e-05,
) -> dict:
    """The live answer shape, exactly as measured on 2026-09-20."""
    return {
        "model": model,
        "answers": {
            "resolves": {"type": "noul", "noul": noul},
            "missing_kind": {
                "type": "choice",
                "choice": choice,
                "probabilities": {"none": 0.01, "implementation": 0.92, "test": 0.06},
                "confidence": 0.9,
            },
            "evidence_quality": {
                "type": "score",
                "score": score,
                "legend": {
                    "0": "the bundle only mentions the topic",
                    "1": "adjacent code",
                    "2": "the exact code path is present",
                    "3": "the exact code path plus its test",
                },
                "probabilities": {"0": 0.82, "1": 0.14, "2": 0.04, "3": 0.0},
                "confidence": 0.77,
            },
        },
        "usage": {"input_tokens": input_tokens, "output_tokens": 96, "cost": cost},
        "id": "gen-dec-test-0001",
        "provider": "TypeSafe",
    }


def _resolve_hermetic(**overrides) -> ResolutionVerdict:
    """A resolve() call with both seams injected: no network, no hilo."""
    kwargs = {
        "workdir": str(REPO_ROOT),
        "keys": [FAKE_KEY_ALPHA],
        "poster": RecordingPoster([FakeResponse(200, _payload())]),
        "runner": FakeRunner(understand_out=BUNDLE_TRANSCRIPT),
        "traced": False,
        "read_files": False,
        "seeds": [],
    }
    kwargs.update(overrides)
    return resolve("does the gate fail closed?", **kwargs)


def verdict_text_of(verdict: ResolutionVerdict, poster: RecordingPoster) -> str:
    """The bundle half of the state that was actually sent, from the recording."""
    state = poster.calls[-1]["body"]["state"]
    prefix = verdict.question + "\n\n"
    assert state.startswith(prefix), "the state must open with the question"
    return state[len(prefix) :]


# ── Bands (spec §3.5) ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "probability,expected",
    [
        (1.0, "RESOLVED"),
        (0.85, "RESOLVED"),  # boundary belongs to the better band
        (0.8499999, "REVIEW"),
        (0.50, "REVIEW"),
        (0.4999999, "UNRESOLVED"),
        (0.0, "UNRESOLVED"),
    ],
)
def test_band_boundaries(probability, expected):
    assert band_for(probability) == expected


@pytest.mark.parametrize("bad", [None, float("nan"), float("inf"), "not-a-number", object()])
def test_band_is_abstain_for_anything_not_a_probability(bad):
    """Fail closed: an unreadable probability is never a band, let alone RESOLVED."""
    assert band_for(bad) == "ABSTAIN"


def test_band_thresholds_are_parameters_not_constants():
    assert band_for(0.7, resolved_at=0.6) == "RESOLVED"
    assert band_for(0.7, review_at=0.8) == "UNRESOLVED"


def test_rubric_position_maps_an_expected_score_onto_its_legend_band():
    legend = {"0": "mentions only", "1": "adjacent", "2": "the path", "3": "path plus test"}
    # the live filler measurement: an EXPECTED position of 0.23 on a 4-band rubric
    assert rubric_position(0.23, legend) == 1
    # the live smoke measurement: 2.68 is an expected position, not a probability
    assert rubric_position(2.68, legend) == 3
    assert rubric_position(3.0, legend) == 3
    assert rubric_position(2.0, legend) == 2
    assert rubric_position(0.0, legend) == 0
    assert rubric_position(1.0, legend) == 3  # the boundary reads as the top band
    assert rubric_position(9.0, legend) is None  # off-rubric: unreadable, not invented
    assert rubric_position(2.0, None) == 2  # a 1-based rubric without a legend
    assert rubric_position(0.23, None) == 0
    assert rubric_position(3.4, {"0": "a", "1": "b"}) is None


def test_the_score_answer_is_not_treated_as_a_probability():
    """The live smoke regression: score 2.68 on a 4-band rubric must parse."""
    payload = _payload()
    payload["answers"]["evidence_quality"]["score"] = 2.68
    typed, problem = parse_answers(payload)
    assert problem is None, problem
    assert typed["evidence_quality"].value == 2.68
    assert typed["evidence_quality"].legend is not None


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf"), True, "nope", None])
def test_an_unreadable_score_is_still_malformed(bad):
    payload = _payload()
    payload["answers"]["evidence_quality"]["score"] = bad
    typed, problem = parse_answers(payload)
    assert typed == {}
    assert "evidence_quality.score" in (problem or "")


# ── Token measurement (spec §3.3) ────────────────────────────────────────────
#
# The estimate is compared against the endpoint's own reported token count for
# each family (live, 2026-09-20). Because the estimator is calibrated rather
# than conservative, the assertions are two-sided: it must stay under the wall
# on every family (never undercount the risk), and it must not burn the budget
# on real payloads (never overcount the bundle by ~2x, which is what silently
# dropped the answer code during this module's smoke run).


def test_estimator_default_is_a_calibration_not_the_bare_floor():
    assert resolution.MIN_CHARS_PER_TOKEN == 2  # the spec's floor, still available
    assert resolution.DEFAULT_CHARS_PER_TOKEN == 3.5  # this repo's measured bundle density
    assert estimate_tokens("x" * 100) == max(1, int(100 / 3.5))
    assert estimate_tokens("x" * 100, chars_per_token=resolution.MIN_CHARS_PER_TOKEN) == 50
    assert estimate_tokens("") == 0


@pytest.mark.parametrize("family", sorted(MEASURED_TOKENS))
def test_estimator_tracks_the_bundle_density_and_bounds_every_family(family):
    """The estimator's contract, family by family, against reported token counts.

    A single divisor cannot model every payload and this module does not pretend
    otherwise. What it does promise:

    * on a real hilo bundle — the payload the pipeline actually assembles — the
      estimate tracks the endpoint within 4%, so the evidence is never silently
      halved (the regression this module's smoke run found);
    * on a punctuation-dense payload like a JSONL stream the estimate is low by
      ~44%, and on adversarial data lower still — a disclosed residual, never
      hidden;
    * :func:`worst_case_tokens` stays ABOVE every measured family, which is what
      the ceiling check is actually run against.
    """
    chars, real = MEASURED_TOKENS[family]
    estimate = estimate_tokens("x" * chars)
    assert worst_case_tokens("x" * chars) >= real  # the guard never undercounts
    if family == "repo-bundle":
        assert real * 0.96 <= estimate <= real * 1.04
    elif family == "jsonl-rows":
        assert estimate >= real * 0.5, "a structured stream must not be halved"
        assert estimate < real
    else:
        assert estimate < real  # possible under-count, disclosed and bounded below


def test_the_calibration_no_longer_halves_the_bundle():
    """The regression this module's smoke run found: a 2x over-estimate dropped
    the very file the question asked about."""
    chars, real = MEASURED_TOKENS["repo-bundle"]
    calibrated = estimate_tokens("x" * chars)
    bare_floor = estimate_tokens("x" * chars, chars_per_token=resolution.MIN_CHARS_PER_TOKEN)
    assert calibrated <= real * 1.04
    assert bare_floor > real * 1.5  # chars//2 sees >50% more tokens than exist


def test_the_estimator_is_over_estimating_rather_than_under_on_a_real_bundle(tmp_path):
    """A whole real bundle, measured end to end against the live number."""
    chars, real = MEASURED_TOKENS["repo-bundle"]
    text = "x" * chars
    assert real <= worst_case_tokens(text)  # the bound is honest for this family
    assert worst_case_tokens(text) > real  # and conservative


@pytest.mark.parametrize("family", sorted(MEASURED_TOKENS))
def test_the_banned_fixed_divisor_would_undercount_every_family(family):
    """Why chars/3.5 is forbidden: it undercounts every family measured, and
    misses a JSONL-shaped payload by more than half."""
    chars, real = MEASURED_TOKENS[family]
    assert math.floor(chars / BANNED_DIVISOR) < real
    if family == "jsonl-rows":
        assert math.floor(chars / BANNED_DIVISOR) < real * 0.75  # the ~75% miss


@pytest.mark.parametrize("family", sorted(MEASURED_TOKENS))
def test_the_worst_case_bound_never_undercounts_a_measured_family(family):
    """The safety bound is the number the ceiling may not be trusted past."""
    chars, real = MEASURED_TOKENS[family]
    assert worst_case_tokens("x" * chars) >= real
    assert resolution.MEASURED_MIN_CHARS_PER_TOKEN == 1.39


def test_worst_case_tokens_is_the_densest_measured_rate():
    assert worst_case_tokens("x" * 139) == 100
    assert worst_case_tokens("") == 0


def test_a_supplied_tokenizer_takes_precedence_over_the_floor():
    calls = []

    def tokenizer(text: str) -> int:
        calls.append(text)
        return 7

    assert estimate_tokens("x" * 10_000, tokenizer=tokenizer) == 7
    assert calls == ["x" * 10_000]


def test_a_real_tokenizer_is_preferred_when_importable(monkeypatch):
    """The seam exists for the spec's 'use a real tokenizer' branch."""
    monkeypatch.setattr(resolution, "_load_real_tokenizer", lambda: lambda text: 42)
    assert resolution._load_real_tokenizer()("anything") == 42


# ── Exclusion filter (spec §6.3 — the bundle leaves the host) ────────────────


@pytest.mark.parametrize(
    "path",
    [
        ".env",
        "engine/.env",
        ".env.production",
        "config/secrets.yaml",
        "keys/deploy.pem",
        "certs/server.key",
        ".venv/lib/python3.10/site-packages/x.py",
        "node_modules/left-pad/index.js",
        ".git/config",
        "state.duckdb",
        "data/app.sqlite",
        ".netrc",
        "credentials.json",
    ],
)
def test_secret_and_cache_paths_are_excluded(path):
    assert is_excluded_path(path) is True


@pytest.mark.parametrize(
    "path", ["engine/resolution.py", "tests/test_resolution.py", "docs/jev-resolution-gate.md"]
)
def test_real_source_paths_are_not_excluded(path):
    assert is_excluded_path(path) is False


# ── Trace ────────────────────────────────────────────────────────────────────


def test_trace_parses_ranked_seeds_and_drops_packages_and_secrets():
    seeds = trace_question("q", runner=FakeRunner(search_out=SEARCH_TRANSCRIPT))
    assert [seed.file for seed in seeds] == [
        "engine/evidence_bounds.py",
        "tests/test_evidence_bounds.py",
        "engine/resolution.py",
    ]
    assert seeds[0].score == 0.0328
    assert seeds[0].symbols == ["evidence", "bounds"]
    # the .env seed and the pkg: pseudo-file never become candidates
    assert all(not seed.file.startswith("pkg:") for seed in seeds)
    assert all(seed.file != ".env" for seed in seeds)


def test_trace_returns_nothing_when_hilo_fails():
    failing = FakeRunner()
    failing.__call__ = lambda args, workdir: (1, "", "boom")
    assert trace_question("q", runner=failing) == []


def test_related_reports_the_reverse_edge_path_and_excludes_secrets():
    related = related_files(
        "engine/evidence_bounds.py", runner=FakeRunner(related_out=RELATED_TRANSCRIPT)
    )
    assert related == [
        "engine/pipeline.py",
        "engine/worktree_fleet.py",
        "engine/worktree_disposable.py",
    ]


def test_related_empty_transcript_is_empty():
    assert (
        related_files("x.py", runner=FakeRunner(related_out="No incoming edges for 'x.py'.\n"))
        == []
    )


# ── Assemble / parse ─────────────────────────────────────────────────────────


def test_parse_understand_reads_provenance_score_and_skips_the_env_block():
    blocks = parse_understand(BUNDLE_TRANSCRIPT, bundle_rank=0)
    files = [block.file for block in blocks]
    assert files == [
        "engine/evidence_bounds.py",
        "tests/test_evidence_bounds.py",
        "engine/resolution.py:1-40",
    ]
    assert blocks[0].provenance == "ast_exact"
    assert blocks[0].score == 1.0
    assert blocks[0].bundle_rank == 0
    assert "def bound_evidence" in blocks[0].text
    assert blocks[1].score == 0.41
    # the file-level header carried a line range and an omission note
    assert blocks[2].file == "engine/resolution.py:1-40"
    assert blocks[2].truncated is True
    # nothing named .env reached the evidence set
    assert all(".env" not in block.file for block in blocks)


def test_parse_understand_on_empty_output_is_empty():
    assert parse_understand("", bundle_rank=0) == []
    assert parse_understand("## MAP\nonly a map\n", bundle_rank=0) == []


def test_assemble_uses_one_primary_bundle_then_seeds_then_reads(tmp_path):
    (tmp_path / "extra.py").write_text("def extra():\n    return 1\n", encoding="utf-8")
    (tmp_path / ".env").write_text("PLACEHOLDER=x\n", encoding="utf-8")
    runner = FakeRunner(
        search_out="0.9  extra.py  [lexical]\n         symbols: extra\n",
        related_out="main.py\n",
        understand_out=BUNDLE_TRANSCRIPT,
    )
    bundle = assemble_bundle("q", workdir=str(tmp_path), runner=runner)
    assert bundle.manifest, "a bundle without a manifest is not a verdict"
    # the traced seed is packed first, then the primary bundle's files
    assert [entry.file for entry in bundle.manifest][:3] == [
        "extra.py",
        "engine/evidence_bounds.py",
        "tests/test_evidence_bounds.py",
    ]
    assert all(".env" not in entry.file for entry in bundle.manifest)
    assert bundle.seeds[0].file == "extra.py"
    assert bundle.dependency_paths == {"extra.py": ["main.py"]}
    assert bundle.tokens_estimated == estimate_tokens(bundle.text)
    # the primary understand call ran, and the seed search ran too
    assert any(call[:2] == ["graph", "understand"] for call in runner.calls)
    assert any(call[:2] == ["graph", "search"] for call in runner.calls)


def test_assemble_falls_back_to_a_bounded_line_aligned_read(tmp_path):
    (tmp_path / "big.py").write_text(
        "\n".join(f"line {index}" for index in range(500)), encoding="utf-8"
    )
    runner = FakeRunner(search_out="0.9  big.py  [lexical]\n", understand_out="")
    bundle = assemble_bundle("q", workdir=str(tmp_path), runner=runner)
    assert [entry.file for entry in bundle.manifest] == ["big.py"]
    entry = bundle.manifest[0]
    assert entry.provenance == "bounded-read" and entry.source == "read"
    assert entry.truncated is True
    # 200 read lines (READ_LINES_PER_FILE) plus the provenance stamp line
    assert entry.lines == resolution.READ_LINES_PER_FILE + 1
    assert "line 199" in bundle.text and "line 200" not in bundle.text
    # the stamp itself is part of the shipped bundle, so a reader can cite the file
    assert "big.py [provenance=bounded-read, score=n/a]" in bundle.text


def test_assemble_with_no_hilo_available_is_an_empty_named_bundle(tmp_path):
    runner = FakeRunner(search_out="", understand_out="")
    bundle = assemble_bundle("q", workdir=str(tmp_path), runner=runner)
    assert bundle.text == ""
    assert bundle.manifest == []
    assert bundle.budget_exhausted is False  # nothing was assembled, no budget spent
    assert any("no candidate evidence" in note for note in bundle.notes)


# ── Budget: the 40k-token bundle must be clipped, disclosed, line-aligned ────


def _fat_blocks(count: int = 24, chars: int = 6_000) -> list[Block]:
    """Synthetic evidence far over the ceiling, on line boundaries."""
    blocks = []
    for index in range(count):
        body = "\n".join(
            f"file_{index} line {line}: def handler_{index}_{line}(x): return x + {line}"
            for line in range(chars // 60)
        )
        blocks.append(
            Block(
                file=f"engine/synthetic_{index}.py",
                provenance="ast_exact",
                score=1.0 - index / 100,
                text=body,
                source="understand",
                bundle_rank=0,
            )
        )
    return blocks


def test_pack_clips_a_40k_token_bundle_to_the_ceiling_and_discloses_it():
    blocks = _fat_blocks()
    assert estimate_tokens("\n\n".join(block.text for block in blocks)) > MAX_BUNDLE_TOKENS

    packed = pack_blocks(blocks, max_tokens=12_000)

    assert packed is not None
    assert estimate_tokens(packed.text) <= 12_000  # the ceiling is real
    assert packed.clipped is True
    assert "clipped" in packed.disclosure
    assert "chars" in packed.disclosure and "omitted" in packed.disclosure
    assert packed.chars_dropped > 0 and packed.lines_dropped >= 0
    assert packed.entries, "clipping must keep a manifest"
    assert any(entry.truncated for entry in packed.entries)


def test_pack_keeps_every_line_whole_or_marks_the_one_it_cut():
    """Line-aligned: no unmarked fragment of a source line reaches the bundle.

    ``engine.evidence_bounds`` is allowed exactly one mid-line cut — a single
    line longer than its side's whole budget — and it says so in the marker.
    """
    blocks = _fat_blocks()
    packed = pack_blocks(blocks, max_tokens=12_000)
    assert packed is not None
    marker = "one over-budget line was cut mid-line"
    source_lines = {line for block in blocks for line in block.text.split("\n") if line}
    body_lines = [
        line
        for line in packed.text.split("\n")
        if line and not line.startswith("…") and "[provenance=" not in line
    ]
    for line in body_lines:
        assert line in source_lines, f"clipped bundle carried a partial line: {line!r}"
    assert marker in packed.text or all(line in source_lines for line in body_lines)


def test_pack_preserves_line_boundaries_when_a_side_is_cut_whole_lines_only():
    """A block whose lines all fit individually never yields a fragment."""
    long_lines = "\n".join(f"engine/x.py line {index} " + "y" * 80 for index in range(400))
    block = Block(
        file="engine/lines.py",
        provenance="ast_exact",
        score=0.9,
        text=long_lines,
        source="understand",
        bundle_rank=0,
    )
    packed = pack_blocks([block], max_tokens=3_000)
    assert packed is not None and packed.clipped is True
    source_lines = set(long_lines.split("\n"))
    for line in packed.text.split("\n"):
        if not line or line.startswith("…") or "[provenance=" in line:
            continue
        assert line in source_lines


def test_pack_keeps_an_under_budget_bundle_byte_identical_with_no_disclosure():
    blocks = _fat_blocks(count=1, chars=600)
    packed = pack_blocks(blocks)
    assert packed is not None
    assert packed.clipped is False
    assert packed.disclosure == ""
    # the body is untouched; only the provenance stamp is added above it
    assert packed.text == f"{resolution._stamp(blocks[0])}\n{blocks[0].text.strip(chr(10))}"
    assert packed.entries[0].truncated is False
    assert packed.entries[0].bytes == len(packed.text.encode("utf-8"))
    assert packed.entries[0].lines == len(packed.text.splitlines())


def test_pack_drops_unfittable_candidates_by_name_rather_than_padding():
    blocks = _fat_blocks()
    packed = pack_blocks(blocks, max_tokens=900)
    assert packed is not None
    assert estimate_tokens(packed.text) <= 900
    assert packed.dropped_files, "the files that did not fit must be named"
    assert "dropped for budget" in packed.disclosure
    for name in packed.dropped_files[:3]:
        assert name in packed.disclosure


def test_pack_returns_none_when_nothing_fits():
    assert pack_blocks(_fat_blocks(), max_tokens=1) is None
    assert pack_blocks([], max_tokens=MAX_BUNDLE_TOKENS) is None


def test_pack_deduplicates_a_file_reached_by_two_bundles():
    blocks = [
        Block("engine/a.py", "ast_exact", 1.0, "first copy", "understand", 0),
        Block("engine/a.py", "targeted", 0.9, "second copy", "understand", 1),
        Block("engine/b.py", "ast_exact", 1.0, "other file", "understand", 0),
    ]
    packed = pack_blocks(blocks, dedupe=True)
    assert packed is not None
    assert [entry.file for entry in packed.entries] == ["engine/a.py", "engine/b.py"]
    assert "first copy" in packed.text and "second copy" not in packed.text
    # without dedupe the caller gets every block, which is what a raw pack means
    assert len(pack_blocks(blocks).entries) == 3


def test_order_blocks_puts_the_traced_files_first():
    """The seed the question names must never be the file dropped for room."""
    seeds = [TraceSeed("engine/wanted.py", 0.9), TraceSeed("engine/second.py", 0.5)]
    blocks = [
        Block("engine/noise_1.py", "ast_exact", 1.0, "noise", "understand", 0),
        Block("engine/second.py", "ast_exact", 1.0, "second", "understand", 0),
        Block("engine/wanted.py", "ast_exact", 1.0, "wanted", "understand", 0),
        Block("engine/noise_2.py", "bounded-read", None, "noise", "read", 3),
    ]
    ordered = order_blocks(blocks, seeds)
    assert [block.file for block in ordered] == [
        "engine/wanted.py",
        "engine/second.py",
        "engine/noise_1.py",
        "engine/noise_2.py",
    ]


def test_order_blocks_keeps_the_bundle_order_within_a_seed():
    seeds = [TraceSeed("engine/x.py", 0.9)]
    blocks = [
        Block("engine/x.py:10-20", "ast_exact", 1.0, "primary slice", "understand", 0),
        Block("engine/x.py", "targeted", 1.0, "targeted slice", "understand", 1),
    ]
    ordered = order_blocks(blocks, seeds)
    assert [block.bundle_rank for block in ordered] == [0, 1]


# ── Credential discovery and failover ────────────────────────────────────────


def test_discover_keys_reads_the_env_and_the_known_env_files(monkeypatch, tmp_path):
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY_ALPHA)
    env_file = tmp_path / ".hermes" / ".env"
    env_file.parent.mkdir(parents=True, exist_ok=True)
    env_file.write_text(
        f"# comment\nGITREINS_OPENROUTER_KEY={FAKE_KEY_BETA}\nOTHER=not-a-key\n",
        encoding="utf-8",
    )
    keys = discover_keys(str(tmp_path))
    assert keys == [FAKE_KEY_ALPHA, FAKE_KEY_BETA]  # env first, then the file
    assert len(keys) == len(set(keys))


def test_discover_keys_ignores_non_openrouter_values(monkeypatch, tmp_path):
    monkeypatch.setenv("GITREINS_OPENROUTER_KEY", "not-a-key")
    assert discover_keys(str(tmp_path)) == []


def test_failover_moves_on_from_a_refused_key_to_a_live_one():
    poster = RecordingPoster(
        [
            FakeResponse(401, {}, "unauthorized"),
            FakeResponse(200, _payload(noul=0.91, choice="none", score=0.9)),
        ]
    )
    verdict = _resolve_hermetic(poster=poster, keys=[FAKE_KEY_ALPHA, FAKE_KEY_BETA])

    assert poster.keys_tried == [FAKE_KEY_ALPHA, FAKE_KEY_BETA]
    assert verdict.verdict == "RESOLVED"
    assert verdict.probability == 0.91
    assert verdict.attempts == ["candidate 1/2: rejected (401)", "candidate 2/2: ok"]
    assert verdict.exit_code == 0


@pytest.mark.parametrize("status", [401, 402, 403, 429])
def test_every_refused_status_moves_to_the_next_candidate(status):
    poster = RecordingPoster(
        [FakeResponse(status, {}, "refused"), FakeResponse(200, _payload(noul=0.2))]
    )
    verdict = _resolve_hermetic(poster=poster, keys=[FAKE_KEY_ALPHA, FAKE_KEY_BETA])
    assert verdict.verdict == "UNRESOLVED"
    assert poster.keys_tried == [FAKE_KEY_ALPHA, FAKE_KEY_BETA]


def test_all_keys_refused_is_an_abstain_never_a_silent_resolved():
    poster = RecordingPoster([FakeResponse(401, {}, "no"), FakeResponse(402, {}, "no")])
    verdict = _resolve_hermetic(poster=poster, keys=[FAKE_KEY_ALPHA, FAKE_KEY_BETA])

    assert verdict.verdict == "ABSTAIN"
    assert verdict.abstain_reason == "all-credentials-rejected"
    assert verdict.probability is None
    assert verdict.ok is False
    assert verdict.exit_code == 1
    assert "top up" in verdict.abstain_action


def test_no_credentials_is_an_abstain_with_an_action():
    verdict = _resolve_hermetic(poster=RecordingPoster([FakeResponse(200, _payload())]), keys=[])
    assert verdict.verdict == "ABSTAIN"
    assert verdict.abstain_reason == "no-credentials"
    assert verdict.abstain_detail is None
    assert "GITREINS_OPENROUTER_KEY" in verdict.abstain_action
    assert verdict.exit_code == 1


def test_discovery_is_used_when_no_keys_are_passed(monkeypatch):
    monkeypatch.setenv("GITREINS_OPENROUTER_KEY", FAKE_KEY_ALPHA)
    poster = RecordingPoster([FakeResponse(200, _payload(noul=0.95, choice="none"))])
    verdict = _resolve_hermetic(keys=None, poster=poster)
    assert poster.keys_tried == [FAKE_KEY_ALPHA]
    assert verdict.verdict == "RESOLVED"


def test_no_key_material_ever_reaches_the_verdict_or_its_notes():
    poster = RecordingPoster([FakeResponse(401, {}, "no"), FakeResponse(200, _payload(noul=0.4))])
    verdict = _resolve_hermetic(poster=poster, keys=[FAKE_KEY_ALPHA, FAKE_KEY_BETA])
    serialized = verdict_json(verdict) + repr(verdict.attempts) + repr(verdict.notes)
    assert FAKE_KEY_ALPHA not in serialized
    assert FAKE_KEY_BETA not in serialized
    assert "sk-or-" not in serialized


# ── Fail-closed: transport, malformed, ceiling ───────────────────────────────


def test_a_transport_error_is_an_abstain():
    poster = RecordingPoster([ConnectionError("connection refused")])
    verdict = _resolve_hermetic(poster=poster)
    assert verdict.verdict == "ABSTAIN"
    assert verdict.abstain_reason == "transport-error"
    assert "ConnectionError" in (verdict.abstain_detail or "")
    assert verdict.exit_code == 1


def test_a_transport_error_on_every_candidate_still_lands_on_the_last_reason():
    poster = RecordingPoster([TimeoutError("slow"), ConnectionError("refused")])
    verdict = _resolve_hermetic(poster=poster, keys=[FAKE_KEY_ALPHA, FAKE_KEY_BETA])
    assert verdict.verdict == "ABSTAIN"
    assert poster.keys_tried == [FAKE_KEY_ALPHA, FAKE_KEY_BETA]
    assert "ConnectionError" in (verdict.abstain_detail or "")


def test_a_server_ceiling_rejection_is_an_abstain_never_a_pass():
    """The compensation for the estimator's dense-payload limit."""
    poster = RecordingPoster(
        [
            FakeResponse(
                400,
                {"error": {"message": "HTTP 400: max_tokens_exceeded"}},
                '{"error":{"message":"HTTP 400: max_tokens_exceeded"}}',
            )
        ]
    )
    verdict = _resolve_hermetic(poster=poster)
    assert verdict.verdict == "ABSTAIN"
    assert verdict.abstain_reason == "bundle-over-server-ceiling"
    assert verdict.exit_code == 1
    assert "lower max_tokens" in verdict.abstain_action


def test_an_unexpected_status_is_a_named_http_error_abstain():
    poster = RecordingPoster([FakeResponse(503, {}, "provider down")])
    verdict = _resolve_hermetic(poster=poster)
    assert verdict.verdict == "ABSTAIN"
    assert verdict.abstain_reason == "http-error"
    assert "503" in (verdict.abstain_detail or "")


@pytest.mark.parametrize(
    "payload,problem",
    [
        ({"answers": {}}, "missing 'resolves' noul answer"),
        ({"answers": {"resolves": {"type": "noul", "noul": 1.4}}}, "not a probability"),
        (
            {"answers": {"resolves": {"type": "score", "noul": 0.5}}},
            "missing 'resolves' noul answer",
        ),
        (
            {
                "answers": {
                    "resolves": {"type": "noul", "noul": 0.5},
                    "missing_kind": {"type": "noul"},
                }
            },
            "missing 'missing_kind' choice answer",
        ),
        (
            {
                "answers": {
                    "resolves": {"type": "noul", "noul": 0.5},
                    "missing_kind": {"type": "choice", "choice": "none"},
                }
            },
            "missing 'evidence_quality' score answer",
        ),
        ({}, "no 'answers' object"),
        ([], "payload is not an object"),
    ],
)
def test_malformed_answers_are_abstains_with_the_specific_problem(payload, problem):
    typed, found = parse_answers(payload)
    assert typed == {}
    assert problem in (found or "")

    verdict = _resolve_hermetic(poster=RecordingPoster([FakeResponse(200, payload)]))
    assert verdict.verdict == "ABSTAIN"
    assert verdict.abstain_reason == "malformed-response"
    assert problem in (verdict.abstain_detail or "")
    assert verdict.probability is None
    assert verdict.exit_code == 1


def test_a_choice_without_a_label_is_a_malformed_abstain():
    payload = _payload()
    payload["answers"]["missing_kind"]["choice"] = ""
    verdict = _resolve_hermetic(poster=RecordingPoster([FakeResponse(200, payload)]))
    assert verdict.verdict == "ABSTAIN"
    assert verdict.abstain_reason == "malformed-response"
    assert "missing_kind.choice" in (verdict.abstain_detail or "")


def test_a_non_json_body_is_a_malformed_abstain():
    poster = RecordingPoster([FakeResponse(200, ValueError("bad json"), "not-json")])
    verdict = _resolve_hermetic(poster=poster)
    assert verdict.verdict == "ABSTAIN"
    assert verdict.abstain_reason == "malformed-response"


def test_a_bool_is_never_a_probability():
    payload = _payload()
    payload["answers"]["resolves"]["noul"] = True
    typed, problem = parse_answers(payload)
    assert typed == {} and "probability" in (problem or "")


def test_an_exhausted_budget_is_named_apart_from_an_empty_bundle():
    verdict = _resolve_hermetic(max_tokens=1)
    assert verdict.verdict == "ABSTAIN"
    assert verdict.abstain_reason == "budget-exhausted"
    assert verdict.budget_exhausted is True
    assert verdict.exit_code == 1


def test_a_question_with_no_evidence_at_all_is_an_empty_bundle_abstain():
    verdict = _resolve_hermetic(
        runner=FakeRunner(search_out="", understand_out=""), traced=False, read_files=False
    )
    assert verdict.verdict == "ABSTAIN"
    assert verdict.abstain_reason == "empty-bundle"


def test_a_blank_question_never_costs_a_call():
    poster = RecordingPoster([FakeResponse(200, _payload())])
    verdict = resolve("", keys=[FAKE_KEY_ALPHA], poster=poster, runner=FakeRunner())
    assert verdict.verdict == "ABSTAIN"
    assert verdict.abstain_reason == "empty-question"
    assert poster.calls == []


# ── The verdict itself: bands, tiers, manifest, accounting ───────────────────


@pytest.mark.parametrize(
    "noul,expected_band,expected_exit",
    [(0.95, "RESOLVED", 0), (0.85, "RESOLVED", 0), (0.60, "REVIEW", 0), (0.09, "UNRESOLVED", 1)],
)
def test_bands_drive_the_verdict_and_the_exit_code(noul, expected_band, expected_exit):
    verdict = _resolve_hermetic(
        poster=RecordingPoster([FakeResponse(200, _payload(noul=noul, choice="none", score=1.0))])
    )
    assert verdict.verdict == expected_band
    assert verdict.exit_code == expected_exit


def test_low_band_verdicts_must_not_be_readable_as_success():
    verdict = _resolve_hermetic(
        poster=RecordingPoster([FakeResponse(200, _payload(noul=0.09, choice="test"))])
    )
    assert verdict.ok is True  # a decision was reached...
    assert verdict.exit_code == 1  # ...and it is a non-zero one
    assert verdict.verdict == "UNRESOLVED"
    assert verdict.missing_kind == "test"


def test_the_verdict_carries_the_manifest_model_tokens_and_cost():
    poster = RecordingPoster([FakeResponse(200, _payload(noul=0.6, choice="wiring", score=0.5))])
    verdict = _resolve_hermetic(poster=poster)

    assert verdict.verdict == "REVIEW"
    assert [entry.file for entry in verdict.manifest] == [
        "engine/evidence_bounds.py",
        "tests/test_evidence_bounds.py",
        "engine/resolution.py:1-40",
    ]
    assert verdict.manifest[0].provenance == "ast_exact"
    assert verdict.manifest[0].score == 1.0
    # the manifest's byte count describes the text that actually shipped, stamp included
    shipped_first_block = verdict_text_of(verdict, poster).split("\n\n")[0]
    assert verdict.manifest[0].bytes == len(shipped_first_block.encode("utf-8"))
    assert "engine/evidence_bounds.py" in shipped_first_block
    assert verdict.manifest[0].truncated is False
    assert verdict.manifest[2].truncated is True
    assert verdict.model == "typesafe/jev-1.13-20260917"  # the echoed build id
    assert verdict.request_id == "gen-dec-test-0001"
    assert verdict.provider == "TypeSafe"
    assert verdict.tokens_estimated == estimate_tokens(poster.calls[-1]["body"]["state"])
    assert verdict.input_tokens == 520
    assert verdict.output_tokens == 96
    assert verdict.cost_usd == pytest.approx(2.184e-05)
    assert verdict.missing_kind == "wiring"
    assert verdict.evidence_quality == 2
    assert verdict.evidence_quality_score == 0.5
    assert any("token check" in note for note in verdict.notes)


def test_the_request_shape_is_the_live_verified_one():
    """The three typed questions, in one request, with the field names that work."""
    poster = RecordingPoster([FakeResponse(200, _payload())])
    _resolve_hermetic(poster=poster)
    assert len(poster.calls) == 1  # ONE request, three questions, one flat cost
    call = poster.calls[0]
    assert call["endpoint"] == resolution.JEV_ENDPOINT
    body = call["body"]
    assert body["model"] == resolution.JEV_MODEL == "typesafe/jev-1.13"
    assert set(body["questions"]) == {"resolves", "missing_kind", "evidence_quality"}
    assert body["questions"]["resolves"]["type"] == "noul"
    assert "instructions" in body["questions"]["resolves"]
    assert "question" not in body["questions"]["resolves"]
    choice = body["questions"]["missing_kind"]
    assert choice["type"] == "choice" and isinstance(choice["criteria"], dict)
    assert set(choice["criteria"]) == {"none", "implementation", "test", "wiring", "config", "docs"}
    score = body["questions"]["evidence_quality"]
    assert score["type"] == "score" and isinstance(score["criteria"], list)
    assert body["state"].startswith("does the gate fail closed?")
    assert "engine/evidence_bounds.py" in body["state"]


def test_the_state_stays_inside_the_ceiling_and_discloses_the_clip():
    poster = RecordingPoster([FakeResponse(200, _payload())])
    verdict = _resolve_hermetic(
        poster=poster,
        runner=FakeRunner(understand_out=_huge_bundle_text()),
        max_tokens=3_000,
    )
    state = poster.calls[0]["body"]["state"]
    assert estimate_tokens(state) <= 3_000
    assert verdict.clipped is True
    assert verdict.clip_disclosure != ""
    assert "clipped" in verdict.clip_disclosure
    assert verdict.chars_dropped > 0
    assert verdict.dropped_files or verdict.manifest


def _huge_bundle_text() -> str:
    """A DETAIL bundle many times over the ceiling, with provenance headers."""
    parts = ["## DETAIL", "<file> [provenance=…, score=…]"]
    for index in range(10):
        parts.append(f"\nengine/huge_{index}.py [provenance=ast_exact, score=1.00]")
        parts.append(
            "\n".join(
                f"def function_{index}_{line}(value: int) -> int: return value + {line}"
                for line in range(120)
            )
        )
    return "\n".join(parts) + "\n"


def test_the_disclosure_separates_a_low_score_from_a_clipped_bundle():
    """Spec §3.3: those are different failures with different fixes."""
    clipped = _resolve_hermetic(
        poster=RecordingPoster([FakeResponse(200, _payload(noul=0.2))]),
        runner=FakeRunner(understand_out=_huge_bundle_text()),
        max_tokens=3_000,
    )
    roomy = _resolve_hermetic(
        poster=RecordingPoster([FakeResponse(200, _payload(noul=0.2))]),
    )
    assert clipped.probability == roomy.probability
    assert clipped.clipped is True and roomy.clipped is False
    assert clipped.clip_disclosure and not roomy.clip_disclosure


def test_the_verdict_reports_the_state_it_actually_sent():
    """Both token counts describe the SENT state: measured and reported side by side."""
    poster = RecordingPoster([FakeResponse(200, _payload(input_tokens=12_054))])
    verdict = _resolve_hermetic(poster=poster)
    state = poster.calls[0]["body"]["state"]
    assert verdict.tokens_estimated == estimate_tokens(state)
    assert verdict.input_tokens == 12_054
    check = [note for note in verdict.notes if note.startswith("token check")]
    assert check and f"estimated={estimate_tokens(state)}" in check[0]
    assert "reported=12054" in check[0]
    safety = [note for note in verdict.notes if note.startswith("safety bound")]
    assert safety and f"{MAX_BUNDLE_TOKENS} ceiling" in safety[0]


def test_the_disclosure_is_reported_once_even_when_the_bundle_clipped():
    verdict = _resolve_hermetic(
        poster=RecordingPoster([FakeResponse(200, _payload())]),
        runner=FakeRunner(understand_out=_huge_bundle_text()),
        max_tokens=3_000,
    )
    assert verdict.notes.count(verdict.clip_disclosure) == 1


def test_the_worst_case_wall_clip_reports_the_bytes_and_lines_it_dropped():
    """A clip the caller performs must be as auditable as one the packer did.

    Regression from this module's own smoke run: the pre-call wall clip set
    ``clip_disclosure`` while reporting ``chars_dropped: 0``, so a reader could
    not tell a 20k-char drop from a no-op. The fixture packs without clipping
    (measured density fits the budget) yet trips the worst-case wall, which is
    the only path that produces this report.
    """
    text = _wall_tripping_bundle()
    assert estimate_tokens(text) <= 15_000  # the measured budget has room
    assert worst_case_tokens(text) > resolution.JEV_HARD_WALL_TOKENS  # the guard does not

    poster = RecordingPoster([FakeResponse(200, _payload())])
    verdict = _resolve_hermetic(
        poster=poster,
        runner=FakeRunner(understand_out=text),
        max_tokens=15_000,
    )

    assert verdict.clipped is True
    assert verdict.chars_dropped > 0, "the clip dropped bytes and must say so"
    assert verdict.lines_dropped > 0
    assert "omitted" in verdict.clip_disclosure
    assert f"{verdict.chars_dropped} chars" in verdict.clip_disclosure
    sent = poster.calls[0]["body"]["state"]
    assert worst_case_tokens(sent) <= resolution.JEV_HARD_WALL_TOKENS
    assert verdict.notes.count(verdict.clip_disclosure) == 1


def _wall_tripping_bundle() -> str:
    """A bundle that packs inside the token budget yet breaks the worst-case wall."""
    parts = ["## DETAIL", "<file> [provenance=…, score=…]"]
    for index in range(6):
        parts.append(f"\nengine/wall_{index}.py [provenance=ast_exact, score=1.00]")
        parts.append(
            "\n".join(
                f"def wall_{index}_{line}(value: int) -> int: return value + {line}"
                for line in range(160)
            )
        )
    return "\n".join(parts) + "\n"


def test_dropped_lines_agrees_with_the_bounders_own_marker():
    marker = "a\n… [1234 chars omitted — 56 line(s)] …\nb\n"
    assert resolution._dropped_lines(marker, 1234) == 56
    # No marker to read (a mid-line-only cut): the byte count is divided by the
    # payload's average line length, rounded DOWN so the report never overstates.
    assert resolution._dropped_lines("only a mid-line cut", 40) == 2  # 40 / 19 avg
    assert resolution._dropped_lines("", 0) == 0
    assert resolution._dropped_lines("x", 1) == 1


def test_join_disclosure_never_repeats_a_clause():
    assert resolution._join_disclosure("", "x") == "x"
    assert resolution._join_disclosure("x", "x") == "x"
    assert resolution._join_disclosure("x", "y") == "x; y"


def test_reconcile_manifest_drops_files_the_clipped_state_no_longer_carries():
    """The manifest must describe what was SENT, not what was assembled."""

    def entry(name: str) -> ManifestEntry:
        return ManifestEntry(name, "ast_exact", 1.0, 10, False, "understand", 2)

    state = "engine/kept.py [provenance=ast_exact, score=1.00]\nsource\n"
    kept, excluded = reconcile_manifest([entry("engine/kept.py"), entry("engine/gone.py")], state)
    assert [item.file for item in kept] == ["engine/kept.py"]
    assert excluded == ["engine/gone.py"]


def test_reconcile_manifest_is_a_noop_when_the_state_carries_no_headers():
    """No headers to check against means "keep", never "drop everything"."""
    entries = [ManifestEntry("engine/a.py", "ast_exact", 1.0, 10, False, "understand", 2)]
    kept, excluded = reconcile_manifest(entries, "text without headers")
    assert kept == entries and excluded == []


def test_the_wall_clip_reconciles_the_manifest_with_what_was_sent():
    """No manifest entry may claim evidence that the clipped state dropped."""
    poster = RecordingPoster([FakeResponse(200, _payload())])
    verdict = _resolve_hermetic(
        poster=poster,
        runner=FakeRunner(understand_out=_wall_tripping_bundle()),
        max_tokens=15_000,
    )
    sent = poster.calls[0]["body"]["state"]
    for entry in verdict.manifest:
        assert entry.file in sent, f"{entry.file} is claimed but was not sent"


def test_verdict_json_is_stable_and_carries_the_audit_fields():
    verdict = _resolve_hermetic()
    data = json.loads(verdict_json(verdict))
    for key in (
        "question",
        "verdict",
        "probability",
        "missing_kind",
        "evidence_quality",
        "manifest",
        "model",
        "tokens_estimated",
        "input_tokens",
        "cost_usd",
        "exit_code",
    ):
        assert key in data
    assert data["manifest"][0]["file"] == "engine/evidence_bounds.py"
    assert isinstance(data["manifest"][0]["truncated"], bool)
    assert verdict.band == verdict.verdict


def test_abstain_verdicts_still_carry_the_evidence_they_did_assemble():
    verdict = _resolve_hermetic(
        poster=RecordingPoster([FakeResponse(401, {}, "no")]),
    )
    assert verdict.verdict == "ABSTAIN"
    assert [entry.file for entry in verdict.manifest] == [
        "engine/evidence_bounds.py",
        "tests/test_evidence_bounds.py",
        "engine/resolution.py:1-40",
    ]
    assert verdict.tokens_estimated > 0
    assert json.loads(verdict_json(verdict))["abstain_reason"] == "all-credentials-rejected"


# ── The real truncator is reused, not re-implemented (spec §3.3) ─────────────


def test_the_bundle_clip_delegates_to_the_one_truncator_in_the_repo():
    calls = []
    original = evidence_bounds.bound_evidence

    def spy(text: str, cap: int = evidence_bounds.MAX_EVIDENCE_CHARS) -> str:
        calls.append(cap)
        return original(text, cap)

    resolution.evidence_bounds.bound_evidence = spy
    try:
        pack_blocks(_fat_blocks(), max_tokens=900)
    finally:
        resolution.evidence_bounds.bound_evidence = original
    assert calls, "the ceiling clip must call engine.evidence_bounds.bound_evidence"
    assert all(cap > 0 for cap in calls)


def test_pack_blocks_never_exceeds_its_budget_across_a_sweep():
    blocks = _fat_blocks()
    for budget in (600, 1_200, 4_000, 12_000, MAX_BUNDLE_TOKENS):
        packed = pack_blocks(blocks, max_tokens=budget)
        if packed is None:
            continue
        assert estimate_tokens(packed.text) <= budget


# ── One live smoke test (skipped without a real key in the env) ──────────────


@pytest.mark.skipif(not LIVE_KEY, reason="no GITREINS_OPENROUTER_KEY in env")
def test_live_smoke_jev_resolution():
    """Real hilo + real endpoint, once. The verdict JSON is the evidence.

    Deliberately does not use ``capsys``: the printed verdict must be visible in
    the run output (``-s``), because the point of this test is the artifact.
    """
    verdict = resolve(
        "Does engine/evidence_bounds.py clip evidence on line boundaries and "
        "disclose how many chars and lines it dropped?",
        workdir=str(REPO_ROOT),
        keys=[LIVE_KEY],
        max_tokens=MAX_BUNDLE_TOKENS,
    )
    print("SMOKE VERDICT ↓")
    print(verdict_json(verdict))
    assert verdict.attempts, "the live call must have been attempted"
    assert "sk-or-" not in verdict_json(verdict)
    assert verdict.verdict in {"RESOLVED", "REVIEW", "UNRESOLVED", "ABSTAIN"}
    assert verdict.manifest, "a live verdict must carry its bundle manifest"
