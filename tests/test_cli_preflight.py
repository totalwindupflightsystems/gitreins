"""JEVRES-003 — hermetic tests for the ``gitreins preflight`` CLI surface.

The policy under test is ``engine/preflight.py``; the pipeline under it is
``engine/resolution.py`` (JEVRES-001). These tests cover only the CLI shell:
argument wiring, human output, --json, and the exit-code contract (0 for
EVERY verdict including an ABSTAIN — the fail-open doctrine — non-zero only
for hard usage errors), plus the band boundaries this surface owns.

Hermetic by the same seams tests/test_cli_resolve.py uses: an autouse
fixture clears ambient credentials, moves HOME to a throwaway dir, and
replaces the engine module's ``requests`` binding; the hilo assembler is
stubbed to a fixed bundle.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
from pathlib import Path

import pytest

from engine import resolution

REPO_ROOT = Path(__file__).resolve().parent.parent


def _fake_key(tag: str) -> str:
    """An OpenRouter-shaped key assembled at runtime — no sk- literal here."""
    return "sk-or-" + "v1-" + tag + "-" + "0" * 16


def _jev_payload(noul: float, choice: str = "none") -> dict:
    """The live decisions-endpoint answer shape, exactly as measured."""
    return {
        "model": "typesafe/jev-1.13-20260917",
        "answers": {
            "resolves": {"type": "noul", "noul": noul},
            "missing_kind": {
                "type": "choice",
                "choice": choice,
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
        "id": "gen-dec-test-pfcli-0001",
        "provider": "TypeSafe",
    }


class _StubResponse:
    status_code = 200

    def __init__(self, noul: float, choice: str = "none"):
        self._noul = noul
        self._choice = choice

    def json(self):
        return _jev_payload(self._noul, self._choice)


def _enabled_resolution_defaults(surface: str):
    """Built-in defaults with *surface*'s enable flag flipped to True (J-GATE)."""
    from engine.config import GitReinsDefaults

    defaults = GitReinsDefaults()
    setattr(defaults, f"resolution_enabled_{surface}", True)
    return defaults


@pytest.fixture(autouse=True)
def _hermetic_credentials(monkeypatch, tmp_path):
    """No ambient credentials, no reachable .env, no real egress."""
    for var in resolution.CREDENTIAL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    # JEVRES-006: the predispatch surface ships config-disabled; these tests
    # pin it OPEN so they keep grading the policy they were written for. The
    # disabled (fail-open dispatch) contract has its own class below.
    monkeypatch.setattr(
        resolution,
        "resolution_config",
        lambda workdir=".": _enabled_resolution_defaults("predispatch"),
    )


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

    Same reason as tests/test_cli_resolve.py: tests/test_cli_doc_sync.py
    stubs every ``cmd_*`` handler on the shared module object and never
    restores them; a fresh module makes these tests order-independent.
    """
    cli_path = REPO_ROOT / "gitreins" / "cli.py"
    spec = importlib.util.spec_from_file_location("_gitreins_cli_jevres003", cli_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_preflight(monkeypatch, workdir, *args: str):
    """Invoke the CLI in-process and capture its exit code + streams."""
    cli_module = _fresh_cli_module()

    out, err = io.StringIO(), io.StringIO()
    code = 0
    with monkeypatch.context() as m:
        m.setattr("sys.argv", ["gitreins", "preflight", *args])
        m.setattr(cli_module, "get_workdir", lambda: workdir)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                cli_module.main()
            except SystemExit as exc:
                code = exc.code or 0
    return code, out.getvalue(), err.getvalue()


class TestPreflightCLI:
    """`gitreins preflight` — the foreman-facing shell of the premise check."""

    def test_json_record_shape_on_skip(self, monkeypatch, tmp_path):
        """--json prints the full machine record; RESOLVED premise, exit 0."""
        _script_assembler(monkeypatch)
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("cli"))
        monkeypatch.setattr(
            resolution,
            "requests",
            type("E", (), {"post": staticmethod(lambda *a, **k: _StubResponse(0.87))})(),
        )

        workdir = tmp_path / "repo"
        workdir.mkdir()
        code, out, err = run_preflight(
            monkeypatch, str(workdir), "Is JEVRES-003 already implemented?", "--json"
        )

        assert code == 0
        record = json.loads(out)
        assert record["decision"] == "skip-dispatch"
        assert record["band"] == "RESOLVED"
        assert record["probability"] == pytest.approx(0.87)
        assert record["missing_kind"] == "none"
        assert record["reason"]
        assert record["abstain_reason"] is None
        # The full verdict rides along — no blind skip.
        verdict = json.loads(record["verdict_json"])
        assert verdict["verdict"] == "RESOLVED"
        assert verdict["probability"] == pytest.approx(0.87)
        assert err == ""

    def test_human_output_names_decision_band_probability_missing(
        self, monkeypatch, tmp_path, capsys
    ):
        _script_assembler(monkeypatch)
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("cli"))
        monkeypatch.setattr(
            resolution,
            "requests",
            type("E", (), {"post": staticmethod(lambda *a, **k: _StubResponse(0.91))})(),
        )

        workdir = tmp_path / "repo"
        workdir.mkdir()
        cli_module = _fresh_cli_module()
        from engine.preflight import preflight

        record = preflight("Is the premise already met?", workdir=str(workdir))
        cli_module._print_preflight_record(record)
        out = capsys.readouterr().out

        assert "Decision: skip-dispatch" in out
        assert "RESOLVED" in out
        assert "0.91" in out
        assert "Missing:  none" in out
        assert "Abstain:" not in out

    def test_abstain_is_a_valid_dispatch_outcome_exit_zero(self, monkeypatch, tmp_path):
        """No key: ABSTAIN -> decision dispatch, abstain_reason present, exit 0."""
        _script_assembler(monkeypatch)
        # Deliberately NO credential: discovery must fail, the policy must not.

        workdir = tmp_path / "repo"
        workdir.mkdir()
        code, out, _ = run_preflight(monkeypatch, str(workdir), "anything at all?", "--json")

        assert code == 0, "an ABSTAIN dispatches fail-open — it is not a usage error"
        record = json.loads(out)
        assert record["decision"] == "dispatch"
        assert record["band"] == "ABSTAIN"
        assert record["abstain_reason"] == "no-credentials"
        assert record["probability"] is None

    def test_band_boundary_085_inclusive_is_resolved(self, monkeypatch, tmp_path):
        """0.85 belongs to the better band: RESOLVED -> skip-dispatch."""
        _script_assembler(monkeypatch)
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("cli"))
        monkeypatch.setattr(
            resolution,
            "requests",
            type("E", (), {"post": staticmethod(lambda *a, **k: _StubResponse(0.85))})(),
        )

        workdir = tmp_path / "repo"
        workdir.mkdir()
        code, out, _ = run_preflight(monkeypatch, str(workdir), "boundary?", "--json")

        assert code == 0
        record = json.loads(out)
        assert record["band"] == "RESOLVED"
        assert record["decision"] == "skip-dispatch"

    def test_band_boundary_050_inclusive_is_review(self, monkeypatch, tmp_path):
        """0.50 belongs to REVIEW -> dispatch-with-note; 0.49 is UNRESOLVED."""
        _script_assembler(monkeypatch)
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("cli"))
        responses = iter([_StubResponse(0.50), _StubResponse(0.49)])
        monkeypatch.setattr(
            resolution,
            "requests",
            type("E", (), {"post": staticmethod(lambda *a, **k: next(responses))})(),
        )

        workdir = tmp_path / "repo"
        workdir.mkdir()
        code, out, _ = run_preflight(monkeypatch, str(workdir), "boundary?", "--json")
        assert code == 0
        record = json.loads(out)
        assert record["band"] == "REVIEW"
        assert record["decision"] == "dispatch-with-note"
        assert record["probability"] == pytest.approx(0.50)

        code, out, _ = run_preflight(monkeypatch, str(workdir), "boundary?", "--json")
        assert code == 0
        record = json.loads(out)
        assert record["band"] == "UNRESOLVED"
        assert record["decision"] == "dispatch"

    def test_help_parses(self, monkeypatch, capsys):
        """`gitreins preflight --help` parses (argparse) and exits 0."""
        cli_module = _fresh_cli_module()
        code = 0
        with monkeypatch.context() as m:
            m.setattr("sys.argv", ["gitreins", "preflight", "--help"])
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                try:
                    cli_module.main()
                except SystemExit as exc:
                    code = exc.code or 0
        assert code == 0

    def test_missing_question_is_a_hard_usage_error(self, monkeypatch, capsys):
        """No question: argparse errors non-zero — the one non-zero path."""
        cli_module = _fresh_cli_module()
        code = 0
        with monkeypatch.context() as m:
            m.setattr("sys.argv", ["gitreins", "preflight"])
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                try:
                    cli_module.main()
                except SystemExit as exc:
                    code = exc.code or 0
        assert code != 0


class TestPreflightConfigGate:
    """JEVRES-006 — the predispatch surface is config-gated, fail OPEN.

    Disabled (the default), no resolution runs; the record is the usual
    ABSTAIN-shaped dispatch record with the named ``surface-disabled`` reason
    and the command still exits 0 — a dead config must never stop work.
    """

    def test_disabled_dispatches_with_named_reason_and_skips_resolution(
        self, monkeypatch, tmp_path
    ):
        from engine.config import GitReinsDefaults

        monkeypatch.setattr(resolution, "resolution_config", lambda workdir=".": GitReinsDefaults())

        workdir = tmp_path / "repo"
        workdir.mkdir()
        code, out, _ = run_preflight(monkeypatch, str(workdir), "row premise?", "--json")

        assert code == 0, "disabled is a fail-open dispatch outcome, not an error"
        record = json.loads(out)
        assert record["decision"] == "dispatch"
        assert record["band"] == "ABSTAIN"
        assert record["abstain_reason"] == "surface-disabled"
        assert record["probability"] is None
        verdict = json.loads(record["verdict_json"])
        assert "predispatch" in verdict["abstain_detail"]

    def test_disabled_record_still_rides_the_dispatch_hook(self, monkeypatch, tmp_path):
        from engine.config import GitReinsDefaults

        from engine.preflight import preflight

        monkeypatch.setattr(resolution, "resolution_config", lambda workdir=".": GitReinsDefaults())
        dispatched = []
        record = preflight(
            "row premise?",
            workdir=str(tmp_path),
            defaults=GitReinsDefaults(),
            dispatch=lambda: dispatched.append(True),
        )
        assert record["decision"] == "dispatch"
        assert dispatched == [True], "the foreman's dispatch step still runs"

    def test_enabled_in_config_runs_the_real_policy(self, monkeypatch, tmp_path):
        """enabled.predispatch: true in config.yaml → the gate runs for real."""
        import yaml

        from engine.config import load_defaults

        _script_assembler(monkeypatch)
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("pf"))
        monkeypatch.setattr(
            resolution,
            "requests",
            type("E", (), {"post": staticmethod(lambda *a, **k: _StubResponse(0.87))})(),
        )

        workdir = tmp_path / "repo"
        config_dir = workdir / ".gitreins"
        config_dir.mkdir(parents=True)
        with open(config_dir / "config.yaml", "w") as f:
            yaml.safe_dump({"resolution": {"enabled": {"predispatch": True}}}, f)
        monkeypatch.setattr(
            resolution, "resolution_config", lambda wd=".": load_defaults(str(workdir))
        )

        code, out, _ = run_preflight(
            monkeypatch, str(workdir), "Is JEVRES-003 already implemented?", "--json"
        )
        assert code == 0
        record = json.loads(out)
        assert record["band"] == "RESOLVED"
        assert record["decision"] == "skip-dispatch"
        assert record["probability"] == pytest.approx(0.87)
