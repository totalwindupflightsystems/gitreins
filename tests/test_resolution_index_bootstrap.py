"""GR-225 — a fresh clone must build its own code graph, not inherit a stale one.

Root cause (measured, dogfood ``gitreins-dogfood-2026-11-15-15-54-49``, HEAD
``60eb8fac``): ``.vfs/graph/edges.jsonl`` was tracked, so a hardlink-free clone
carried an index frozen at the commit that produced it. The clone indexed 32 of
53 tracked ``engine/*.py`` modules, the string ``preflight`` appeared zero times
in ``.vfs/graph/graph.db``, and ``gitreins resolve`` graded the premise
"does this repository implement the pre-dispatch premise check in a module named
engine/preflight.py?" as **REVIEW / evidence_quality 1 / missing_kind
implementation** — a tracked module called missing, on every clone.

The two halves of the fix, each pinned here:

1. the index is DERIVED state and is not committed (``git ls-files .vfs`` empty,
   ``.vfs/graph/edges.jsonl`` + ``.vfs/graph/*.db`` ignored);
2. ``gitreins resolve`` bootstraps a missing or stale index itself
   (:func:`engine.resolution.ensure_graph_index`), so the evidence it grades is
   the checkout's real code.

Why the acceptance arms below run on a FIXTURE corpus instead of re-cloning this
repository: the pipeline behaviour under test is "index → seed → bundle →
manifest", and the defect shape is "a committed index that predates a module".
A tiny corpus reproduces exactly that, in well under a second, where a clone of
this repo's history is minutes of I/O — and, under the pre-commit guard, a clone
of HEAD would describe the PARENT commit, so the test would grade code that does
not contain the fix. The real-clone runs (this repo at the fix commit, and at
``60eb8fac``) are the row's evidence and are quoted in the close-out.

The Jev call is replaced by an ORACLE that answers from the bundle it is handed —
the measured pair from that dogfood (evidence present → 0.96 / rubric 2.4,
absent → 0.59 / rubric 1.0). That keeps the assertion honest end to end: the
bands, the manifest and the seeds are the real pipeline's; only the third-party
judgement of "how good is this evidence" is stood in for.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from engine import resolution

REPO_ROOT = Path(__file__).resolve().parent.parent
HILO = shutil.which("hilo") or str(Path.home() / ".cargo" / "bin" / "hilo")
HILO_AVAILABLE = Path(HILO).is_file()

QUESTION = (
    "Does this repository implement the pre-dispatch premise check in a module "
    "named engine/preflight.py?"
)

#: Assembled at runtime — no key-shaped literal in the tree.
FAKE_KEY = "sk-or-" + "v1-" + "gr225-" + "0" * 16

#: The evidence-block header hilo's ``graph understand`` emits for a file it
#: actually read. The bare filename is NOT a usable oracle signal: the question
#: itself names ``engine/preflight.py``, so it is present in every state.
_EVIDENCE_MARKER = "engine/preflight.py [provenance="

#: A corpus shaped like this repo, small enough to warm in one pass. The module
#: the question asks about (``engine/preflight.py``) is deliberately absent from
#: ``INITIAL_CORPUS`` — it is added AFTER the index is warmed and committed,
#: which is the defect: a shipped index cannot see a module added since.
INITIAL_CORPUS = {
    "engine/__init__.py": "",
    "engine/types.py": '"""Typed records."""\n\nVERDICT_RESOLVED = "RESOLVED"\n',
    "engine/config.py": (
        '"""Config loader."""\n\nfrom engine.types import VERDICT_RESOLVED\n\n'
        "DEFAULT_BAND = VERDICT_RESOLVED\n"
    ),
    "engine/pipeline.py": (
        '"""Guard pipeline."""\n\nfrom engine.config import DEFAULT_BAND\n\n'
        "def run() -> str:\n    return DEFAULT_BAND\n"
    ),
    "gitreins/__init__.py": "",
    "gitreins/cli.py": (
        '"""CLI shell."""\n\nfrom engine.pipeline import run\n\n'
        "def main() -> int:\n    return 0 if run() else 1\n"
    ),
    "docs/notes.md": "# notes\n\nThe guard pipeline is described here.\n",
    "tests/test_pipeline.py": (
        "from engine.pipeline import run\n\n\ndef test_run() -> None:\n"
        '    assert run() == "RESOLVED"\n'
    ),
}

PREFLIGHT_MODULE = '''"""Pre-dispatch premise check.

The pre-dispatch premise check asks whether the work a board row describes is
already implemented, before a worker is dispatched.
"""

from engine.config import DEFAULT_BAND


def premisedispatch(question: str) -> str:
    """Answer the pre-dispatch premise check for one question."""
    return DEFAULT_BAND
'''


# ── Harness ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _no_leaked_git_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """DF-008 hardening: a leaked ``GIT_DIR``/``GIT_INDEX_FILE`` would point every
    fixture ``git`` call at the OUTER repo (the pre-commit hook sets them), and
    these tests create, clone and commit into throwaway repositories."""
    for key in [name for name in os.environ if name.startswith("GIT_")]:
        if key == "GIT_CONFIG_GLOBAL":
            continue  # conftest's isolate_git_fixtures_from_host_config owns it
        monkeypatch.delenv(key, raising=False)


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.email=gr225@example.invalid",
            "-c",
            "user.name=gr225",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=check,
        capture_output=True,
        text=True,
    )


def _hilo(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [HILO, *args], cwd=repo, check=True, capture_output=True, text=True, timeout=300
    )


def _small_repo(root: Path) -> Path:
    """A two-commit git repo with an index AND a manifest, for the unit arms."""
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    (root / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "one")
    graph = root / ".vfs" / "graph"
    graph.mkdir(parents=True, exist_ok=True)
    (root / ".vfs" / "manifest.yaml").write_text("version: 2\n", encoding="utf-8")
    return root


def _stale_index_fixture(root: Path) -> Path:
    """A repo that carries the GR-225 defect: a committed index older than a module.

    Uses the REAL hilo binary and the REAL ``.gitignore`` from this repo, so the
    fixture also proves the ignore rules are sufficient for a bootstrap to leave
    a consumer's tree clean.
    """
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    shutil.copy(REPO_ROOT / ".gitignore", root / ".gitignore")
    for rel, body in INITIAL_CORPUS.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "initial corpus")
    # Ship the index NOW, before engine/preflight.py exists — the stale snapshot.
    _hilo(root, "init", "--no-hooks")
    _hilo(root, "graph", "warm")
    _git(root, "add", "-f", ".vfs/graph/edges.jsonl")
    _git(root, "commit", "-q", "-m", "chore(index): ship the warmed graph index")
    # ... then add the module the shipped index cannot see.
    (root / "engine" / "preflight.py").write_text(PREFLIGHT_MODULE, encoding="utf-8")
    _git(root, "add", "engine/preflight.py")
    _git(root, "commit", "-q", "-m", "feat: add engine/preflight.py")
    return root


def _clone(src: Path, dest: Path) -> Path:
    subprocess.run(
        ["git", "clone", "--no-hardlinks", "-q", str(src), str(dest)],
        check=True,
        capture_output=True,
        text=True,
    )
    return dest


class _Response:
    """A ``requests``-shaped response carrying one scripted Jev answer."""

    status_code = 200

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> object:
        return self._payload


def _jev_payload(present: bool) -> dict:
    """The measured dogfood pair: exact path present → 0.96 / rubric 2.

    ``score`` is an EXPECTED rubric position on the legend's own 0..3 scale
    (``rubric_position``), so the two carriers are ``2.4`` (→ band 2, the live
    warm answer) and ``0.23`` (→ band 1, the live cold answer, and the value this
    repo's own resolution tests use for weak evidence).
    """
    noul, score = (0.96, 2.4) if present else (0.59, 0.23)
    probabilities = (
        {"0": 0.02, "1": 0.06, "2": 0.82, "3": 0.1}
        if present
        else {"0": 0.2, "1": 0.7, "2": 0.09, "3": 0.01}
    )
    return {
        "model": "typesafe/jev-1.13-oracle",
        "answers": {
            "resolves": {"type": "noul", "noul": noul},
            "missing_kind": {
                "type": "choice",
                "choice": "none" if present else "implementation",
                "probabilities": {"none": 0.9, "implementation": 0.06, "test": 0.04},
                "confidence": 0.9,
            },
            "evidence_quality": {
                "type": "score",
                "score": score,
                "legend": {
                    "0": "mentions only",
                    "1": "adjacent code",
                    "2": "the exact code path",
                    "3": "path plus its test",
                },
                "probabilities": probabilities,
                "confidence": 0.8,
            },
        },
        "usage": {"input_tokens": 400, "output_tokens": 90, "cost": 2e-05},
        "id": "gen-dec-gr225-0001",
        "provider": "TypeSafe",
    }


class OraclePoster:
    """Answers from the bundle it receives: exact-path evidence, or none."""

    def __init__(self) -> None:
        self.states: list[str] = []

    def __call__(self, endpoint: object, key: str, body: dict, timeout: int) -> object:
        state = body["state"]
        self.states.append(state)
        return _Response(_jev_payload(_EVIDENCE_MARKER in state))

    @property
    def bundle(self) -> str:
        """The evidence half of the last state (the question is echoed first)."""
        return self.states[-1].split("\n\n", 1)[-1]


def _manifest_files(verdict: resolution.ResolutionVerdict) -> list[str]:
    return [entry.file for entry in verdict.manifest]


# ── AC1: no committed index, and the ignore rules cover it ───────────────────


def test_repository_tracks_no_graph_index() -> None:
    """``git ls-files .vfs`` is empty and the index paths are ignored (GR-225 AC1)."""
    tracked = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "ls-files", ".vfs"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert tracked == "", f"a clone still ships a graph index: {tracked!r}"

    entries = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    for entry in (".vfs/graph/edges.jsonl", ".vfs/graph/*.db", ".vfs/manifest.yaml"):
        assert entry in entries, f"{entry} missing from .gitignore"

    for path in (".vfs/graph/edges.jsonl", ".vfs/graph/graph.db", ".vfs/manifest.yaml"):
        ignored = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "check-ignore", "-q", path],
            capture_output=True,
            text=True,
        )
        assert ignored.returncode == 0, f"git does not ignore {path}"


def test_gitignore_carries_hilos_managed_block_verbatim() -> None:
    """The bootstrap's `hilo init --no-hooks` must not edit a TRACKED file.

    ``hilo init`` APPENDS its managed block whenever the marker line is absent —
    measured in this repo: a live resolve bootstrapped the graph and left a
    second copy of the block in ``.gitignore``, i.e. the tool rewrote a tracked
    file of the checkout it was asked to read. Shipping the marker and its eight
    entries verbatim is what makes the bootstrap a no-op on a clean clone.
    """
    lines = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    marker = "# --- hilo managed: rebuildable .vfs cache state (installed by `hilo init`) ---"
    assert marker in lines, "hilo would append a duplicate block on the next bootstrap"
    assert lines.count(marker) == 1, "the managed block must appear exactly once"
    start = lines.index(marker)
    assert lines[start + 1 : start + 9] == [
        ".vfs/graph/graph.db",
        ".vfs/graph/graph.db.wal",
        ".vfs/graph/graph.duckdb",
        ".vfs/graph/graph.duckdb.wal",
        ".vfs/graph/.last_warm",
        ".vfs/graph/.parse_cache.json",
        ".vfs/graph/.symbols_cache.json",
        ".vfs/graph/.last_reconcile",
    ]


# ── The staleness signal ─────────────────────────────────────────────────────


def test_missing_index_needs_a_full_warm(tmp_path: Path) -> None:
    repo = _small_repo(tmp_path / "repo")
    state = resolution.graph_index_state(str(repo))
    assert state.reason == resolution.GRAPH_STALE_MISSING
    assert state.present is False
    assert state.needs_warm is True
    assert state.incremental is False
    assert state.head, "the index state must carry the HEAD it was read at"


def test_unstamped_index_needs_a_full_warm(tmp_path: Path) -> None:
    """An index with no warm record is the SHIPPED-stale case: never a diff warm."""
    repo = _small_repo(tmp_path / "repo")
    (repo / ".vfs" / "graph" / "edges.jsonl").write_text('{"from":"a","to":"b"}\n')
    state = resolution.graph_index_state(str(repo))
    assert state.reason == resolution.GRAPH_STALE_UNSTAMPED
    assert state.present is True
    assert state.incremental is False


def test_warm_record_at_head_is_fresh_and_a_moved_head_is_incremental(tmp_path: Path) -> None:
    repo = _small_repo(tmp_path / "repo")
    graph = repo / ".vfs" / "graph"
    (graph / "edges.jsonl").write_text('{"from":"a","to":"b"}\n')
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (graph / resolution.GRAPH_INDEX_STAMP).write_text(json.dumps({"version": 1, "head": head}))

    state = resolution.graph_index_state(str(repo))
    assert state.reason == resolution.GRAPH_INDEX_FRESH
    assert state.needs_warm is False

    (repo / "b.py").write_text("y = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "two")
    moved = resolution.graph_index_state(str(repo))
    assert moved.reason == resolution.GRAPH_STALE_HEAD_MOVED
    assert moved.incremental is True, "a moved HEAD is the --changed (incremental) case"
    assert moved.warmed_head == head


def test_hilo_state_file_is_honoured_as_a_warm_record(tmp_path: Path) -> None:
    """A checkout whose post-commit hook warmed is not warmed a second time."""
    repo = _small_repo(tmp_path / "repo")
    graph = repo / ".vfs" / "graph"
    (graph / "edges.jsonl").write_text('{"from":"a","to":"b"}\n')
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (graph / resolution.GRAPH_HILO_STATE).write_text(
        json.dumps({"version": 1, "lastWarmHead": head})
    )
    assert resolution.graph_index_state(str(repo)).reason == resolution.GRAPH_INDEX_FRESH


# ── The bootstrap itself (injected runner: no hilo needed) ───────────────────


class RecordingHilo:
    """Records the hilo argv it is asked to run; optionally writes an index."""

    def __init__(self, *, index_written: bool = True, init_rc: int = 0, warm_rc: int = 0) -> None:
        self.calls: list[list[str]] = []
        self.index_written = index_written
        self.init_rc = init_rc
        self.warm_rc = warm_rc

    def __call__(self, args: list[str], workdir: str) -> tuple[int, str, str]:
        self.calls.append(list(args))
        repo = Path(workdir)
        if args[0] == "init":
            if self.init_rc == 0:
                (repo / ".vfs").mkdir(parents=True, exist_ok=True)
                (repo / ".vfs" / "manifest.yaml").write_text("version: 2\n")
            return self.init_rc, "", "" if self.init_rc == 0 else "init refused"
        if args[0] == "graph" and args[1] == "warm":
            if self.warm_rc == 0 and self.index_written:
                graph = repo / ".vfs" / "graph"
                graph.mkdir(parents=True, exist_ok=True)
                graph.joinpath("edges.jsonl").write_text('{"from":"a","to":"b"}\n')
            return self.warm_rc, "parsing 8/8 files", "" if self.warm_rc == 0 else "warm broke"
        return 1, "", "unknown hilo invocation"


def test_bootstrap_inits_then_fully_warms_an_unstamped_index(tmp_path: Path) -> None:
    repo = _small_repo(tmp_path / "repo")
    (repo / ".vfs" / "graph" / "edges.jsonl").write_text('{"from":"a","to":"b"}\n')
    runner = RecordingHilo()

    result = resolution.ensure_graph_index(str(repo), runner=runner)

    assert result.status == resolution.GRAPH_INDEX_REBUILT
    # the manifest already exists, so no `init` — a full warm, not `--changed`
    assert runner.calls == [["graph", "warm"]]
    assert result.notes and "unstamped-index" in result.notes[0]
    stamp = json.loads((repo / ".vfs" / "graph" / resolution.GRAPH_INDEX_STAMP).read_text())
    assert stamp["head"] == _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert stamp["mode"] == "full"
    # the fresh stamp makes the NEXT resolve free
    assert resolution.graph_index_state(str(repo)).reason == resolution.GRAPH_INDEX_FRESH


def test_bootstrap_inits_a_project_with_no_manifest(tmp_path: Path) -> None:
    repo = tmp_path / "bare"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "one")
    runner = RecordingHilo()

    result = resolution.ensure_graph_index(str(repo), runner=runner)

    assert result.status == resolution.GRAPH_INDEX_REBUILT
    assert runner.calls == [["init", "--no-hooks"], ["graph", "warm"]]


def test_a_moved_head_uses_the_incremental_warm(tmp_path: Path) -> None:
    repo = _small_repo(tmp_path / "repo")
    graph = repo / ".vfs" / "graph"
    graph.joinpath("edges.jsonl").write_text('{"from":"a","to":"b"}\n')
    graph.joinpath(resolution.GRAPH_INDEX_STAMP).write_text(
        json.dumps({"version": 1, "head": "0" * 40})
    )
    runner = RecordingHilo()

    result = resolution.ensure_graph_index(str(repo), runner=runner)

    assert result.status == resolution.GRAPH_INDEX_REBUILT
    assert runner.calls == [["graph", "warm", "--changed"]]
    assert json.loads(graph.joinpath(resolution.GRAPH_INDEX_STAMP).read_text())["mode"] == "changed"


def test_a_failed_warm_is_named_in_a_note_and_never_raised(tmp_path: Path) -> None:
    repo = _small_repo(tmp_path / "repo")
    runner = RecordingHilo(init_rc=0, warm_rc=2, index_written=False)

    result = resolution.ensure_graph_index(str(repo), runner=runner)

    assert result.status == resolution.GRAPH_INDEX_FAILED
    assert result.notes and "graph index bootstrap failed" in result.notes[0]
    assert "warm" in result.notes[0]


def test_a_warm_that_produces_no_index_is_a_named_failure(tmp_path: Path) -> None:
    repo = _small_repo(tmp_path / "repo")
    runner = RecordingHilo(index_written=False)

    result = resolution.ensure_graph_index(str(repo), runner=runner)

    assert result.status == resolution.GRAPH_INDEX_FAILED
    assert result.notes and "no .vfs/graph/edges.jsonl" in result.notes[0]


def test_a_missing_binary_adds_no_note_so_the_trace_leg_keeps_the_detail(
    tmp_path: Path,
) -> None:
    """DF-GITREINS-POC-86: ``notes[-1]`` stays the searched-path message verbatim."""
    repo = _small_repo(tmp_path / "repo")
    missing = str(tmp_path / ".cargo" / "bin" / "hilo")

    def runner(args: list[str], workdir: str) -> tuple[int, str, str]:
        return 127, "", f"hilo not found (searched PATH, then {missing})"

    result = resolution.ensure_graph_index(str(repo), runner=runner)

    assert result.status == resolution.GRAPH_INDEX_FAILED
    assert result.notes == [], "the missing-binary message must not be duplicated or displaced"


# ── The end-to-end arm: a FRESH CLONE of a repo that carries the defect ──────


@pytest.mark.skipif(not HILO_AVAILABLE, reason=f"hilo binary not installed (looked for {HILO})")
def test_fresh_clone_bootstraps_the_index_and_finds_the_preflight_module(
    tmp_path: Path,
) -> None:
    """GR-225 AC2/AC3/AC4 on a fixture clone: the gate sees what the checkout has.

    Four assertions, in the order the row states them:

    * before bootstrap, the stale clone cannot search for ``engine/preflight.py``;
      the resolution gate now records it as missing evidence and abstains rather
      than treating it as an absent implementation;
    * after the tool's own bootstrap, ``hilo graph search`` lists the module;
    * the same resolve answers RESOLVED with ``evidence_quality >= 2`` and
      ``engine/preflight.py`` in ``manifest[].file``;
    * the bootstrap leaves the clone's tree clean — no ``.vfs`` dirt.
    """
    source = _stale_index_fixture(tmp_path / "source")
    broken = _clone(source, tmp_path / "clone-before")
    fixed = _clone(source, tmp_path / "clone-after")

    # Premise, asserted so a later fixture change cannot make this test vacuous:
    # the clone DOES carry a committed index, and that index predates the module.
    tracked = _git(broken, "ls-files", ".vfs").stdout.split()
    assert ".vfs/graph/edges.jsonl" in tracked, "the fixture must ship a stale index"
    assert "preflight" not in (broken / ".vfs" / "graph" / "edges.jsonl").read_text(
        encoding="utf-8", errors="replace"
    ), "the shipped index must predate engine/preflight.py"
    before_search = _hilo(broken, "graph", "search", "pre-dispatch premise check").stdout
    assert "engine/preflight.py" not in before_search, (
        "the stale index must not be able to answer for the module: " + before_search
    )

    # ── GR-226: the missing named path is explicit, never graded as absent code ──
    stale_oracle = OraclePoster()
    stale = resolution.resolve(
        QUESTION,
        workdir=str(broken),
        keys=[FAKE_KEY],
        poster=stale_oracle,
        bootstrap_index=False,
    )
    assert stale.verdict == resolution.VERDICT_ABSTAIN, stale.verdict
    assert stale.named_paths_missing == ["engine/preflight.py"]
    assert (stale.evidence_quality or 0) <= 1
    assert stale.missing_kind != "implementation"
    assert "engine/preflight.py" in (stale.abstain_detail or "")
    assert "engine/preflight.py" not in _manifest_files(stale)
    assert _EVIDENCE_MARKER not in stale_oracle.bundle

    # ── AC3: the fixed arm — resolve bootstraps the index itself ────────────
    oracle = OraclePoster()
    verdict = resolution.resolve(
        QUESTION,
        workdir=str(fixed),
        keys=[FAKE_KEY],
        poster=oracle,
    )

    assert any("graph index rebuilt" in note for note in verdict.notes), verdict.notes

    # ── AC2: after the tool's bootstrap, hilo can find the module ───────────
    after_search = _hilo(fixed, "graph", "search", "pre-dispatch premise check").stdout
    assert "engine/preflight.py" in after_search, after_search
    assert "engine/preflight.py" in (fixed / ".vfs" / "graph" / "edges.jsonl").read_text(
        encoding="utf-8", errors="replace"
    ), "the warmed index must record the module's edges"

    assert verdict.verdict == resolution.VERDICT_RESOLVED, (verdict.verdict, verdict.notes)
    assert verdict.evidence_quality is not None and verdict.evidence_quality >= 2, (
        verdict.evidence_quality
    )
    assert "engine/preflight.py" in _manifest_files(verdict), _manifest_files(verdict)

    # ── The bootstrap must not dirty a consumer's tree ──────────────────────
    status = _git(fixed, "status", "--porcelain", "--untracked-files=all").stdout.splitlines()
    vfs_dirt = [line for line in status if line[3:].startswith(".vfs/") and line.startswith("??")]
    assert vfs_dirt == [], f"bootstrap left untracked .vfs artifacts: {vfs_dirt}"
