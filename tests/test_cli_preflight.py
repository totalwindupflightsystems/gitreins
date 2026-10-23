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


def _jev_payload(noul: float, choice: str = "none", evidence_quality_score: float = 2.6) -> dict:
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
                "score": evidence_quality_score,
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

    def __init__(
        self, noul: float, choice: str = "none", evidence_quality_score: float = 2.6
    ) -> None:
        self._noul = noul
        self._choice = choice
        self._evidence_quality_score = evidence_quality_score

    def json(self) -> object:
        return _jev_payload(self._noul, self._choice, self._evidence_quality_score)


def _enabled_resolution_defaults(surface: str) -> object:
    """Built-in defaults with *surface*'s enable flag flipped to True (J-GATE)."""
    from engine.config import GitReinsDefaults

    defaults = GitReinsDefaults()
    setattr(defaults, f"resolution_enabled_{surface}", True)
    return defaults


@pytest.fixture(autouse=True)
def _hermetic_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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


def _script_assembler(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace hilo with a fixed bundle naming the file the question targets."""
    from engine.resolution import ManifestEntry, TraceSeed

    def fake_assemble_bundle(question: object, **kwargs: object) -> object:
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


def _fresh_cli_module() -> object:
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


def run_preflight(monkeypatch: pytest.MonkeyPatch, workdir: str, *args: str) -> tuple:
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

    def test_json_record_shape_on_skip(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
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
        # The full verdict rides along — no blind skip (DF-GITREINS-POC-37:
        # a first-class OBJECT, the same dict `resolve --json` prints).
        verdict = record["verdict"]
        assert isinstance(verdict, dict)
        assert verdict["verdict"] == "RESOLVED"
        assert verdict["probability"] == pytest.approx(0.87)
        assert err == ""

    def test_json_verdict_is_a_first_class_object_with_resolves_shape(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """DF-GITREINS-POC-37 — one shape for one gate (dogfood run 7, Finding 3).

        `preflight --json` nested the verdict as an ESCAPED JSON STRING under
        `verdict_json`, while `resolve --json` printed the object, so a script
        keying on `.verdict` found nothing in a preflight record and a jq user
        had to parse twice. The record now carries `verdict` as exactly the
        dict `ResolutionVerdict.to_dict()` produces — the reference shape — and
        the escaped twin is gone.
        """
        from engine.resolution import ResolutionVerdict

        _script_assembler(monkeypatch)
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("cli"))
        monkeypatch.setattr(
            resolution,
            "requests",
            type("E", (), {"post": staticmethod(lambda *a, **k: _StubResponse(0.87))})(),
        )

        workdir = tmp_path / "repo"
        workdir.mkdir()
        code, out, _ = run_preflight(
            monkeypatch, str(workdir), "Is JEVRES-003 already implemented?", "--json"
        )

        assert code == 0
        record = json.loads(out)
        verdict = record["verdict"]
        assert isinstance(verdict, dict)
        # `resolve --json` prints exactly this dict (`verdict_json(verdict)` is
        # `json.dumps(verdict.to_dict())`) — same keys, same values.
        assert set(verdict) == set(ResolutionVerdict(question="q?", verdict="RESOLVED").to_dict())
        for key in ("verdict", "probability", "abstain_reason"):
            assert key in verdict
        assert verdict["verdict"] == "RESOLVED"
        assert verdict["probability"] == pytest.approx(0.87)
        assert verdict["abstain_reason"] is None
        assert "verdict_json" not in record, "one shape for one gate (POC-37)"
        # The dispatch record itself is unchanged — band/decision/reason/… keep
        # working for the foremen already keying on them.
        assert record["band"] == "RESOLVED"
        assert record["decision"] == "skip-dispatch"
        assert record["question"] == "Is JEVRES-003 already implemented?"
        assert record["reason"]
        assert record["abstain_reason"] is None
        assert record["missing_kind"] == "none"

    def test_human_output_names_decision_band_probability_missing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
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

    def test_json_record_names_the_evidence_floor_cause_on_demotion(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """JEVRES-007 — a consumer-repo record answers WHY it was demoted.

        probability 0.94 would be RESOLVED, but the Jev rubric position is 1
        (below MIN_RESOLVED_EVIDENCE_QUALITY = 3), so the band is REVIEW. The
        record must name the floor cause instead of the old generic
        "the evidence partially resolves the row", and must keep the
        dispatch-with-note action.
        """
        _script_assembler(monkeypatch)
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("cli-floor"))
        monkeypatch.setattr(
            resolution,
            "requests",
            type(
                "E",
                (),
                {
                    "post": staticmethod(
                        lambda *a, **k: _StubResponse(0.94, evidence_quality_score=1.2)
                    )
                },
            )(),
        )

        workdir = tmp_path / "repo"
        workdir.mkdir()
        code, out, err = run_preflight(
            monkeypatch, str(workdir), "Is the row already implemented?", "--json"
        )

        assert code == 0
        record = json.loads(out)
        assert record["band"] == "REVIEW"
        assert record["probability"] >= 0.85
        assert record["decision"] == "dispatch-with-note"
        assert record["verdict"]["evidence_quality"] == 1
        cause = record["demotion_reason"]
        assert cause, "a REVIEW at p >= 0.85 must carry the demotion cause"
        assert "evidence_quality 1 is below MIN_RESOLVED_EVIDENCE_QUALITY (3)" in cause
        assert "partially resolves" not in record["reason"]
        assert "evidence floor" in record["reason"]
        assert "dispatch-with-note" in record["reason"]
        assert err == ""

    def test_skip_record_has_no_demotion_reason(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The additive field is null on the paths that were never demoted."""
        _script_assembler(monkeypatch)
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("cli"))
        monkeypatch.setattr(
            resolution,
            "requests",
            type("E", (), {"post": staticmethod(lambda *a, **k: _StubResponse(0.87))})(),
        )

        workdir = tmp_path / "repo"
        workdir.mkdir()
        code, out, _ = run_preflight(
            monkeypatch, str(workdir), "Is JEVRES-003 already implemented?", "--json"
        )

        assert code == 0
        record = json.loads(out)
        assert record["band"] == "RESOLVED"
        assert record["demotion_reason"] is None

    def test_abstain_is_a_valid_dispatch_outcome_exit_zero(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No key: ABSTAIN -> decision dispatch, abstain_reason present, exit 0.

        DF-GITREINS-POC-37: the no-verdict case is not a second shape. An
        ABSTAIN record carries the SAME eight fields as a real band, with
        ``verdict`` the same OBJECT (`ResolutionVerdict.to_dict()`) — a
        consumer keying on ``.verdict`` must not need an ABSTAIN code path.
        """
        from engine.resolution import ResolutionVerdict

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
        # One record shape for every band — including the one with no verdict
        # to report (`probability: null`): same keys, `verdict` still an object.
        # `demotion_reason` is additive (JEVRES-007) and null wherever no floor
        # demotion happened, so the shape stays uniform across bands.
        assert set(record) == {
            "question",
            "band",
            "probability",
            "missing_kind",
            "decision",
            "reason",
            "demotion_reason",
            "abstain_reason",
            "verdict",
        }
        assert record["demotion_reason"] is None
        verdict = record["verdict"]
        assert isinstance(verdict, dict), "no second parse, on the ABSTAIN path either"
        assert set(verdict) == set(ResolutionVerdict(question="q?", verdict="ABSTAIN").to_dict())
        assert verdict["verdict"] == "ABSTAIN"
        assert verdict["abstain_reason"] == "no-credentials"
        assert verdict["question"] == "anything at all?", "the real verdict, not a stub"
        assert "abstain_detail" in verdict, "the ABSTAIN object keeps the gate's own detail field"
        assert "verdict_json" not in record

    def test_band_boundary_085_inclusive_is_resolved(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
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

    def test_band_boundary_050_inclusive_is_review(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
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

    def test_help_parses(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
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

    def test_missing_question_is_a_hard_usage_error(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
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
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
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
        assert "predispatch" in record["verdict"]["abstain_detail"]

    def test_disabled_record_still_rides_the_dispatch_hook(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
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

    def test_enabled_in_config_runs_the_real_policy(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
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


class TestPreflightSurfaceSelection:
    """DF-GITREINS-POC-81 — `--surface` names the enable flag that gates the run.

    The reported repo is this repo's own shape: `.gitreins/config.yaml` enables
    `cli` (so `gitreins resolve` answers) and leaves the judge-adjacent
    `predispatch` surface off, so `gitreins preflight` returned ABSTAIN /
    `abstain_reason: surface-disabled` for every question. Nothing in the
    resolver was wrong — the surface was never enabled — and `--surface cli`
    runs the same check on the surface the repo HAS turned on.

    These tests drive the gate from the real config FILE (not the module's
    stubbed defaults) so the enable flags, the abstain detail and the recorded
    `source` are all exercised through one loader.
    """

    @staticmethod
    def _write_config(workdir: Path, enabled: dict[str, bool]) -> None:
        import yaml

        config_dir = workdir / ".gitreins"
        config_dir.mkdir(parents=True, exist_ok=True)
        with open(config_dir / "config.yaml", "w") as handle:
            yaml.safe_dump({"resolution": {"enabled": enabled}}, handle)

    @staticmethod
    def _use_real_loader(monkeypatch: pytest.MonkeyPatch, workdir: Path) -> None:
        from engine.config import load_defaults

        monkeypatch.setattr(
            resolution, "resolution_config", lambda wd=".": load_defaults(str(workdir))
        )

    @staticmethod
    def _records(workdir: Path) -> list:
        return sorted((workdir / ".gitreins" / "history").glob("*/*/verdict.json"))

    def test_surface_cli_runs_the_check_in_a_cli_only_repo(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """cli on / predispatch off → --surface cli answers for real."""
        _script_assembler(monkeypatch)
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("poc81"))
        monkeypatch.setattr(
            resolution,
            "requests",
            type("E", (), {"post": staticmethod(lambda *a, **k: _StubResponse(0.87))})(),
        )

        workdir = tmp_path / "repo"
        workdir.mkdir()
        self._write_config(workdir, {"cli": True, "mcp": True, "predispatch": False})
        self._use_real_loader(monkeypatch, workdir)

        code, out, err = run_preflight(
            monkeypatch, str(workdir), "Is the premise already met?", "--surface", "cli", "--json"
        )

        assert code == 0
        assert err == ""
        record = json.loads(out)
        assert record["band"] == "RESOLVED"
        assert record["decision"] == "skip-dispatch"
        assert record["abstain_reason"] is None
        assert record["probability"] == pytest.approx(0.87)
        # The decision was filed by the surface whose enable flag permitted the
        # run — the record says which flag that was (docs/cli-reference.md §16).
        records = self._records(workdir)
        assert len(records) == 1
        persisted = json.loads(records[0].read_text())
        assert persisted["source"] == "cli"
        assert persisted["band"] == "RESOLVED"

    def test_default_surface_is_still_predispatch_and_abstains_fail_open(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """No --surface → unchanged predispatch behavior: abstain, dispatch, no egress."""
        posted: list = []
        monkeypatch.setattr(
            resolution,
            "requests",
            type("E", (), {"post": staticmethod(lambda *a, **k: posted.append(1))})(),
        )
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("poc81"))

        workdir = tmp_path / "repo"
        workdir.mkdir()
        self._write_config(workdir, {"cli": True, "predispatch": False})
        self._use_real_loader(monkeypatch, workdir)

        code, out, err = run_preflight(
            monkeypatch, str(workdir), "Is the premise already met?", "--json"
        )

        assert code == 0, "a disabled surface is a fail-open dispatch outcome"
        assert err == ""
        record = json.loads(out)
        assert record["band"] == "ABSTAIN"
        assert record["abstain_reason"] == "surface-disabled"
        assert record["decision"] == "dispatch"
        detail = record["verdict"]["abstain_detail"]
        assert "resolution.enabled.predispatch is false" in detail
        assert "now enabled: cli" in detail, "the enabled surface is named in the record"
        assert posted == [], "a disabled surface must not reach the provider"
        assert self._records(workdir) == [], "an ABSTAIN writes nothing"

    def test_a_disabled_named_surface_names_that_surface(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """--surface cli in a repo with nothing enabled abstains on 'cli', not predispatch."""
        workdir = tmp_path / "repo"
        workdir.mkdir()
        self._write_config(workdir, {"cli": False, "predispatch": False})
        self._use_real_loader(monkeypatch, workdir)

        code, out, _ = run_preflight(
            monkeypatch, str(workdir), "Is the premise already met?", "--surface", "cli", "--json"
        )

        assert code == 0
        record = json.loads(out)
        assert record["abstain_reason"] == "surface-disabled"
        assert record["decision"] == "dispatch"
        detail = record["verdict"]["abstain_detail"]
        assert "resolution.enabled.cli is false" in detail
        assert "no surface is enabled" in detail

    def test_human_output_names_the_surface_the_abstain_and_the_fix(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The text surface must not leave the operator with a bare 'ABSTAIN'."""
        workdir = tmp_path / "repo"
        workdir.mkdir()
        self._write_config(workdir, {"cli": True, "predispatch": False})
        self._use_real_loader(monkeypatch, workdir)

        code, out, _ = run_preflight(monkeypatch, str(workdir), "Is the premise already met?")

        assert code == 0
        assert "Surface:  predispatch" in out
        assert "Abstain:  surface-disabled" in out
        assert "Detail:   resolution.enabled.predispatch is false" in out
        assert "Fix:      " in out
        assert "--surface cli" in out, "the hint names the way out without a config change"

    def test_unknown_surface_is_a_hard_usage_error(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        workdir = tmp_path / "repo"
        workdir.mkdir()
        code, _, err = run_preflight(
            monkeypatch, str(workdir), "anything?", "--surface", "carrier-pigeon"
        )
        assert code != 0, "an unknown surface is a usage error, not an abstain"
        assert "carrier-pigeon" in err

    def test_help_names_the_surface_distinction(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """`--surface` and the independence rule are in the command's own help."""
        workdir = tmp_path / "repo"
        workdir.mkdir()
        code, out, _ = run_preflight(monkeypatch, str(workdir), "--help")

        assert code == 0
        # argparse wraps its help text, so compare on normalized whitespace.
        help_text = " ".join(out.split())
        assert "--surface" in help_text
        assert "{cli,mcp,predispatch,judge_prescreen}" in help_text
        assert "default: predispatch" in help_text
        assert "Every surface is enabled independently" in help_text
        assert "does NOT enable resolution.enabled.predispatch" in help_text
        lowered = help_text.lower()
        assert "resolved" in lowered and "abstain" in lowered


class TestPreflightPersistence:
    """DF-GITREINS-POC-36 — the predispatch surface records its decision.

    A skip-dispatch is the most consequential thing this surface does (no worker
    is spawned), so the record it leaves behind is the only audit trail for it.
    An ABSTAIN and a disabled surface still write nothing, and a disabled
    surface must not even touch the store.
    """

    @staticmethod
    def _records(workdir: str) -> list:
        return sorted((workdir / ".gitreins" / "history").glob("*/*/verdict.json"))

    @staticmethod
    def _usage_rows(workdir: str) -> list:
        path = workdir / ".gitreins" / "usage.jsonl"
        if not path.is_file():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def _script_endpoint(self, monkeypatch: pytest.MonkeyPatch, noul: float) -> None:
        monkeypatch.setattr(
            resolution,
            "requests",
            type("E", (), {"post": staticmethod(lambda *a, **k: _StubResponse(noul))})(),
        )

    def test_a_skip_dispatch_run_files_one_predispatch_record(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _script_assembler(monkeypatch)
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("cli"))
        self._script_endpoint(monkeypatch, 0.87)

        workdir = tmp_path / "repo"
        workdir.mkdir()
        code, out, _ = run_preflight(
            monkeypatch, str(workdir), "Is JEVRES-003 already implemented?", "--json"
        )

        assert code == 0
        assert json.loads(out)["decision"] == "skip-dispatch"
        records = self._records(workdir)
        assert len(records) == 1
        record = json.loads(records[0].read_text())
        assert record["kind"] == "resolution"
        assert record["source"] == "predispatch"
        assert record["band"] == "RESOLVED"
        assert record["question"] == "Is JEVRES-003 already implemented?"

        rows = self._usage_rows(workdir)
        assert [row["step"] for row in rows] == ["resolution"]
        assert rows[0]["tokens_in"] == 520
        assert rows[0]["tokens_out"] == 96

    def test_a_disabled_surface_records_nothing_and_never_touches_the_store(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from engine.config import GitReinsDefaults

        monkeypatch.setattr(resolution, "resolution_config", lambda workdir=".": GitReinsDefaults())

        workdir = tmp_path / "repo"
        workdir.mkdir()
        code, out, _ = run_preflight(monkeypatch, str(workdir), "row premise?", "--json")

        assert code == 0
        assert json.loads(out)["abstain_reason"] == "surface-disabled"
        assert not (workdir / ".gitreins" / "history").exists()
        assert not (workdir / ".gitreins" / "usage.jsonl").exists()

    def test_an_abstain_from_the_engine_records_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> object:
        """All keys dead: the dispatch still happens, and nothing is filed."""
        _script_assembler(monkeypatch)
        # A key must exist for the failure to be "rejected" rather than "absent".
        monkeypatch.setenv("GITREINS_OPENROUTER_KEY", _fake_key("dead"))

        def dead_post(*_a: object, **_k: object) -> object:
            response = _StubResponse(0.99)
            response.status_code = 401
            return response

        monkeypatch.setattr(
            resolution, "requests", type("E", (), {"post": staticmethod(dead_post)})()
        )

        workdir = tmp_path / "repo"
        workdir.mkdir()
        code, out, _ = run_preflight(monkeypatch, str(workdir), "row premise?", "--json")

        assert code == 0
        assert json.loads(out)["abstain_reason"] == "all-credentials-rejected"
        assert self._records(workdir) == []
        assert self._usage_rows(workdir) == []
