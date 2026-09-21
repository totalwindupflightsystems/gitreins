"""JEVRES-002 — hermetic tests for the ``gitreins resolve`` CLI surface.

The pipeline under test is ``engine/resolution.py`` (JEVRES-001); these tests
cover only the CLI shell around it: argument wiring, human output, --json,
and the exit-code contract (0 for RESOLVED/REVIEW, 1 for UNRESOLVED and for
ABSTAIN, the reason distinguishable in the JSON).

Hermetic by the same seams tests/test_resolution.py uses: an autouse fixture
clears ambient credentials, moves HOME to a throwaway dir (the host's real
``~/.hermes/.env`` holds a live key), and replaces the engine module's
``requests`` binding — a test that reaches the real OpenRouter endpoint fails
loudly instead of quietly.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from engine import resolution

REPO_ROOT = Path(__file__).resolve().parent.parent


def _fake_key(tag: str) -> str:
    """An OpenRouter-shaped key assembled at runtime — no sk- literal here."""
    return "sk-or-" + "v1-" + tag + "-" + "0" * 16


def _jev_payload(noul: float) -> dict:
    """The live decisions-endpoint answer shape, exactly as measured."""
    return {
        "model": "typesafe/jev-1.13-20260917",
        "answers": {
            "resolves": {"type": "noul", "noul": noul},
            "missing_kind": {
                "type": "choice",
                "choice": "none",
                "probabilities": {"none": 0.93, "implementation": 0.05, "test": 0.02},
                "confidence": 0.9,
            },
            "evidence_quality": {
                "type": "score",
                "score": 2.6,
                "legend": {
                    "0": "mentions only",
                    "1": "adjacent code",
                    "2": "the exact code path",
                    "3": "path plus its test",
                },
                "probabilities": {"0": 0.01, "1": 0.05, "2": 0.84, "3": 0.10},
                "confidence": 0.77,
            },
        },
        "usage": {"input_tokens": 520, "output_tokens": 96, "cost": 2.184e-05},
        "id": "gen-dec-test-cli-0001",
        "provider": "TypeSafe",
    }


class _StubResponse:
    status_code = 200

    def __init__(self, noul: float):
        self._noul = noul

    def json(self):
        return _jev_payload(self._noul)


class _ScriptedEndpoint:
    """Stands in for the engine module's ``requests`` binding (has .post)."""

    def __init__(self, calls: list, noul: float):
        self._calls = calls
        self._noul = noul

    def post(self, url, **kwargs):
        self._calls.append({"url": url, "json": kwargs.get("json")})
        return _StubResponse(self._noul)


@pytest.fixture(autouse=True)
def _hermetic_credentials(monkeypatch, tmp_path):
    """No ambient credentials, no reachable .env, no real egress."""
    for var in resolution.CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def script_endpoint(monkeypatch):
    """Install a scripted Jev endpoint; returns a list the test can inspect."""
    calls: list = []

    def _install(noul: float) -> list:
        monkeypatch.setattr(resolution, "requests", _ScriptedEndpoint(calls, noul))
        return calls

    return _install


def _script_assembler(monkeypatch) -> None:
    """Replace hilo with a fixed bundle naming the file the question targets."""
    from engine.resolution import ManifestEntry, TraceSeed

    def fake_assemble_bundle(question, **kwargs):
        return resolution.AssembledBundle(
            text="## MAP\nengine/evidence_bounds.py →\n  - bound_evidence\n",
            manifest=[
                ManifestEntry(
                    file="engine/evidence_bounds.py",
                    provenance="ast_exact",
                    score=1.0,
                    bytes=1024,
                    truncated=False,
                    source="understand",
                    lines=24,
                )
            ],
            seeds=[TraceSeed(file="engine/evidence_bounds.py", score=0.9)],
            tokens_estimated=300,
        )

    monkeypatch.setattr(resolution, "assemble_bundle", fake_assemble_bundle)


def _fresh_cli_module():
    """Load a PRIVATE copy of gitreins/cli.py, not the shared sys.modules one.

    ``tests/test_cli_doc_sync.py``'s ``live_surface`` stubs every ``cmd_*``
    handler on the ``gitreins.cli`` module object and never restores them, so
    an in-process caller that imports the shared module after that test runs
    silently gets no-op handlers (observed as an empty --json stdout when the
    two files shared an xdist worker). Loading the file under a fresh module
    name makes these tests order-independent without touching the doc-sync
    tests' contract.
    """
    import importlib.util

    cli_path = REPO_ROOT / "gitreins" / "cli.py"
    spec = importlib.util.spec_from_file_location("_gitreins_cli_jevres002", cli_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_resolve(monkeypatch, workdir, *args: str):
    """Invoke the CLI handler in-process and capture its exit + streams."""
    import contextlib
    import io

    cli_module = _fresh_cli_module()

    out, err = io.StringIO(), io.StringIO()
    code = 0
    with monkeypatch.context() as m:
        m.setattr("sys.argv", ["gitreins", "resolve", *args])
        m.setattr(cli_module, "get_workdir", lambda: workdir)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli_module.main()
            except SystemExit as exc:
                code = exc.code or 0
    return code, out.getvalue(), err.getvalue()


class TestResolveCLI:
    """`gitreins resolve` — the human/CI shell of the resolution gate."""

    def test_json_output_is_the_full_verdict_object(self, monkeypatch, tmp_path, script_endpoint):
        """--json emits valid JSON carrying every verdict field."""
        _script_assembler(monkeypatch)
        calls = script_endpoint(0.87)
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("cli"))

        workdir = tmp_path / "repo"
        workdir.mkdir()
        code, out, err = run_resolve(
            monkeypatch,
            str(workdir),
            "Does engine/evidence_bounds.py truncate text?",
            "--json",
        )

        assert code == 0
        verdict = json.loads(out)
        assert verdict["verdict"] == "RESOLVED"
        assert verdict["probability"] == pytest.approx(0.87)
        assert verdict["missing_kind"] == "none"
        assert verdict["model"] == "typesafe/jev-1.13-20260917"
        assert verdict["question"] == "Does engine/evidence_bounds.py truncate text?"
        assert verdict["manifest"], "manifest ships with the verdict"
        assert verdict["manifest"][0]["file"] == "engine/evidence_bounds.py"
        assert verdict["manifest"][0]["provenance"] == "ast_exact"
        assert "abstain_reason" in verdict
        assert "exit_code" in verdict
        assert len(calls) == 1
        assert calls[0]["url"] == resolution.JEV_ENDPOINT

    def test_human_output_names_band_probability_missing_and_manifest(
        self, monkeypatch, tmp_path, script_endpoint, capsys
    ):
        """Plain output: verdict band, probability, missing_kind, bundle."""
        _script_assembler(monkeypatch)
        script_endpoint(0.91)
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("cli"))

        workdir = tmp_path / "repo"
        workdir.mkdir()
        cli_module = _fresh_cli_module()

        verdict = resolution.resolve(
            "Does engine/evidence_bounds.py truncate text?",
            workdir=str(workdir),
        )
        cli_module._print_resolve_verdict(verdict)
        out = capsys.readouterr().out

        assert "RESOLVED" in out
        assert "0.91" in out
        assert "Missing:  none" in out
        assert "Bundle:" in out
        assert "engine/evidence_bounds.py" in out
        assert "provenance=ast_exact" in out
        assert "score=1.00" in out

    def test_unresolved_exits_nonzero_and_is_not_an_abstain(
        self, monkeypatch, tmp_path, script_endpoint
    ):
        """A low score exits 1 and stays a decision, not an abstention."""
        _script_assembler(monkeypatch)
        script_endpoint(0.09)
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("cli"))

        workdir = tmp_path / "repo"
        workdir.mkdir()
        code, out, _ = run_resolve(
            monkeypatch, str(workdir), "does the repo handle negatives?", "--json"
        )

        assert code == 1
        verdict = json.loads(out)
        assert verdict["verdict"] == "UNRESOLVED"
        assert verdict["abstain_reason"] is None

    def test_abstain_without_credentials_exits_nonzero_with_named_reason(
        self, monkeypatch, tmp_path, script_endpoint
    ):
        """No key: ABSTAIN, exit 1, reason + suggested fix in the JSON."""
        # A non-empty bundle must reach the credential check for the reason to
        # be no-credentials (an empty repo abstains earlier, at empty-bundle —
        # assembly runs first and is itself a named refusal).
        _script_assembler(monkeypatch)
        script_endpoint(0.99)  # never reached — discovery fails first

        workdir = tmp_path / "repo"
        workdir.mkdir()
        code, out, _ = run_resolve(monkeypatch, str(workdir), "anything at all?", "--json")

        assert code == 1
        verdict = json.loads(out)
        assert verdict["verdict"] == "ABSTAIN"
        assert verdict["abstain_reason"] == "no-credentials"
        assert verdict["abstain_action"]
        assert verdict["probability"] is None

    def test_budget_flag_reaches_the_engine(self, monkeypatch, tmp_path):
        """--budget N forwards N as the engine's max_tokens."""
        import contextlib

        from engine.resolution import MAX_BUNDLE_TOKENS

        seen: dict = {}

        def fake_assemble_bundle(question, *, max_tokens, **kwargs):
            seen["max_tokens"] = max_tokens
            return resolution.AssembledBundle(text="", notes=["nothing to assemble"])

        monkeypatch.setattr(resolution, "assemble_bundle", fake_assemble_bundle)
        monkeypatch.setattr("gitreins.cli.get_workdir", lambda: str(tmp_path))

        cli_module = _fresh_cli_module()

        args = argparse.Namespace(question="q?", budget=None, json=False)
        # An empty bundle abstains and cmd_resolve exits 1 — expected here;
        # the test observes the budget the assembler received, not the band.
        with contextlib.suppress(SystemExit):
            cli_module.cmd_resolve(args)
        assert seen["max_tokens"] == MAX_BUNDLE_TOKENS

        args.budget = 4000
        with contextlib.suppress(SystemExit):
            cli_module.cmd_resolve(args)
        assert seen["max_tokens"] == 4000
