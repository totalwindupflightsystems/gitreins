"""
Shared pytest fixtures for GitReins tests.
axiom:trace work_item=GR-001 spec=specs/05-Task-Manager.md plan=.memory-bank/work-items/GR-001/plan.yaml step=step-1-1-1-1
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import tempfile

import pytest

# DF-GITREINS-POC-61: the tier-1 stamp is IMPORTED from the module that sets it
# (``engine.pipeline`` tier1_plan → the tests step's per-step ``env``) — one
# definition, two sides of the contract, so the name can never drift apart.
# `import engine.pipeline` is ~0.08s and already happens for most test modules.
from engine.pipeline import TIER1_ENV_VAR

# ── DF-GITREINS-POC-61: live/egress tests never grade a deterministic gate ───
#
# A judge's Tier 1 runs the guard's test command VERBATIM (``guards.test_mode:
# diff`` is a guard-only narrowing), so the FULL suite — including a
# skipif-guarded live smoke test that calls OpenRouter + hilo — executed on
# every judge run. Under concurrent judges the live call came back
# rate-limited/5xx, pytest exited non-zero, and the tier-1 FAIL short-circuited
# Tier 2: one host-global flake burned a whole judge cycle.
#
# Two hooks below: skip ``live``-marked tests inside tier 1, and flock-serialize
# them everywhere else so two concurrent runs can never fire the live call at
# the same moment.

LIVE_MARKER = "live"

_LIVE_SKIP_REASON = (
    "live/egress test excluded from judge tier-1 (DF-GITREINS-POC-61): a "
    "non-deterministic host-global network smoke must not decide a "
    "deterministic gate — run `pytest -m live` outside the judge to exercise it"
)

# fd/handle of the per-repo live-run lock, held for the duration of the test.
_LIVE_LOCK_FD: int | None = None


def _in_tier1() -> bool:
    """True when this pytest process is the child of a judge Tier 1 tests step."""
    return os.environ.get(TIER1_ENV_VAR) == "1"


def _live_lock_path() -> str:
    """Per-repo lock file, OUTSIDE the repo — a run never writes an in-tree file.

    Keyed by the repo root so two checkouts of different repos never contend,
    while two concurrent runs of THIS repo do.
    """
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    digest = hashlib.sha1(repo_root.encode("utf-8")).hexdigest()[:12]
    return os.path.join(tempfile.gettempdir(), f"gitreins-live-{digest}.lock")


def pytest_collection_modifyitems(config, items):
    """Skip ``live``-marked (real-egress) tests when running inside judge tier-1.

    The skip is applied to the collected item, so the reason lands in the run's
    short summary and in ``-rs`` output; nothing is silently dropped.
    """
    if not _in_tier1():
        return
    skip = pytest.mark.skip(reason=_LIVE_SKIP_REASON)
    for item in items:
        if item.get_closest_marker(LIVE_MARKER) is not None:
            item.add_marker(skip)


def pytest_runtest_setup(item):
    """Take the per-repo live lock, or skip if another process holds it.

    Outside tier 1 the live test still runs for real (manual ``pytest`` with a
    key, ``gitreins guard``) — it just cannot run CONCURRENTLY with another
    live run in the same repo. Non-blocking acquire: the loser skips instead of
    queueing, because a skipped egress smoke is honest and a queued judge is
    not.
    """
    global _LIVE_LOCK_FD
    if item.get_closest_marker(LIVE_MARKER) is None or _in_tier1():
        return
    path = _live_lock_path()
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        pytest.skip(
            reason=(
                "another process is already running the live/egress tests "
                f"(flock on {path} is held) — skipped so only one live call is "
                "in flight at a time (DF-GITREINS-POC-61)"
            )
        )
    _LIVE_LOCK_FD = fd


def pytest_runtest_teardown(item, nextitem):
    """Release the live-run lock taken in :func:`pytest_runtest_setup`."""
    global _LIVE_LOCK_FD
    if _LIVE_LOCK_FD is None:
        return
    fd, _LIVE_LOCK_FD = _LIVE_LOCK_FD, None
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def init_fake_git_workdir(workdir) -> None:
    """Give *workdir* the minimal ``.git`` a GitReins workspace fixture needs.

    Not a usable repository — just enough for ``git rev-parse --show-toplevel``
    to resolve the workdir itself, which is how the CLI locates the
    ``.gitreins/`` store it reads and writes.
    """
    git_dir = workdir / ".git"
    git_dir.mkdir()
    # Create minimal git config so git commands work
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    (git_dir / "config").write_text("[core]\n\trepositoryformatversion = 0\n\tbare = false\n")
    (git_dir / "objects").mkdir()
    (git_dir / "refs").mkdir()
    (git_dir / "refs" / "heads").mkdir()


@pytest.fixture
def tmp_workdir(tmp_path):
    """Create a temporary git repository with .gitreins/ directory.

    Returns a clean workdir path that acts as a realistic GitReins workspace.
    """
    workdir = tmp_path / "repo"
    workdir.mkdir()
    init_fake_git_workdir(workdir)
    return str(workdir)


@pytest.fixture
def workdir_factory(tmp_path):
    """Build several isolated fake git workdirs inside one test.

    ``tmp_workdir`` is function-scoped, so a test that drives concurrent CLI
    sequences needs its own factory to give each sequence a workspace of its
    own — otherwise the sequences would share one task store and the test
    could not tell isolation from luck.

    Usage::

        def test_x(workdir_factory):
            first, second = workdir_factory(), workdir_factory()
    """
    made: list[str] = []

    def _make() -> str:
        workdir = tmp_path / f"repo-{len(made) + 1}"
        workdir.mkdir()
        init_fake_git_workdir(workdir)
        made.append(str(workdir))
        return str(workdir)

    return _make


@pytest.fixture(autouse=True)
def isolated_job_store(tmp_path, monkeypatch):
    """Point the shared disk job store (DF-006) at a temp dir.

    Autouse so no test ever reads/writes real jobs under
    ~/.local/share/gitreins/jobs — including subprocess children, which
    inherit the overridden GITREINS_JOB_DIR from the environment.
    """
    d = str(tmp_path / "gitreins-jobs")
    monkeypatch.setenv("GITREINS_JOB_DIR", d)
    return d


@pytest.fixture
def task_manager(tmp_workdir):
    """Create a TaskManager with a clean temp directory."""
    from engine.task_manager import TaskManager

    tm = TaskManager(tmp_workdir)
    return tm


@pytest.fixture
def sample_task_dict():
    """Return a sample task dict for testing."""
    return {
        "id": "test-task-1",
        "title": "Implement login endpoint",
        "criteria": [
            "Accepts email+password",
            "Returns JWT on success",
            "Returns 401 on failure",
        ],
    }


@pytest.fixture
def guard_manager(tmp_workdir):
    """Create a GuardManager with a clean temp directory."""
    from engine.guard_manager import GuardManager

    return GuardManager(tmp_workdir)


@pytest.fixture
def llm_client():
    """Create an LLMClient with default (non-functioning) settings.

    Tests that use this must mock requests.post to avoid real HTTP calls.
    """
    from engine.llm import LLMClient

    return LLMClient(base_url="https://test.local/v1", api_key="test-key-12345")


@pytest.fixture
def evaluator(llm_client, tmp_workdir):
    """Create an AgenticEvaluator with a real workdir and mockable LLM."""
    from engine.evaluator import AgenticEvaluator

    return AgenticEvaluator(llm_client, tmp_workdir, max_iterations=5)


@pytest.fixture
def judge(llm_client, tmp_workdir):
    """Create a Judge with a clean temp directory."""
    from engine.judge import Judge

    return Judge(llm_client, tmp_workdir)


@pytest.fixture
def pipeline_config_default():
    """Return a default pipeline configuration dict."""
    return {
        "pipeline": {
            "stages": [
                {
                    "id": "tier1",
                    "parallel": True,
                    "on": ["pre-commit", "pre-eval"],
                    "steps": [
                        {
                            "id": "secrets",
                            "type": "script",
                            "run": "echo ok",
                            "on_fail": "continue",
                        },
                        {"id": "lint", "type": "script", "run": "echo ok"},
                        {"id": "tests", "type": "script", "run": "echo ok"},
                    ],
                },
                {
                    "id": "tier2",
                    "type": "ai_eval",
                    "on": ["pre-eval"],
                    "condition": "true",
                    "max_iterations": 20,
                },
            ]
        }
    }
