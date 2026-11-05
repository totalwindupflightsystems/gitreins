"""CLI completion records — OBS-GITREINS-2 (docs/observability-spec.md §4b/§5b).

Covers the shipped contract of ``engine/cli_metrics.record_run_completion``
and the dispatcher hook in ``gitreins.cli._run_subcommand_handler``: one
record per subcommand invocation on every exit path, best-effort transport
(pushgateway POST or atomic textfile write), closed-set failure reasons, and
zero behaviour change (exceptions and exit codes re-raised untouched).
No live pushgateway is required — ``urllib.request.urlopen`` is mocked.
"""

from __future__ import annotations

import argparse
import re
import urllib.error

import pytest
import requests

import gitreins.cli as cli_module
from engine import cli_metrics
from engine import eval_cap


def _prom_lines(path) -> str:
    return path.read_text(encoding="utf-8")


def _series(text: str, name: str) -> list[str]:
    """Every exposition line for a metric family, comments excluded."""
    return [
        line
        for line in text.splitlines()
        if line.startswith(name + "{") or line.startswith(name + " ")
    ]


def _set_textfile(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setenv("GITREINS_METRICS_TEXTFILE_DIR", str(tmp_path))
    monkeypatch.delenv("GITREINS_METRICS_PUSH_URL", raising=False)


# ── Emitter: series and transports ────────────────────────────────────────


def test_success_records_pass_duration_and_last_success(tmp_path, monkeypatch):
    _set_textfile(monkeypatch, tmp_path)
    cli_metrics.record_run_completion(
        "probe", outcome="pass", duration_s=1.5, tokens_in=120, tokens_out=30
    )
    text = _prom_lines(tmp_path / "gitreins-probe.prom")
    assert 'gitreins_cli_run_completed_total{command="probe",outcome="pass"} 1.0' in text
    assert 'gitreins_cli_run_duration_seconds_count{command="probe"} 1.0' in text
    assert 'gitreins_cli_run_tokens_total{command="probe",kind="input"} 120.0' in text
    assert 'gitreins_cli_run_tokens_total{command="probe",kind="output"} 30.0' in text
    last = _series(text, "gitreins_cli_run_last_success_timestamp_seconds")
    assert len(last) == 1 and 'command="probe"' in last[0]
    assert float(last[0].rsplit(" ", 1)[1]) > 0.0
    # A pass record carries no failure series.
    assert not _series(text, "gitreins_cli_run_failure_total")


def test_tokens_counter_omitted_when_zero(tmp_path, monkeypatch):
    _set_textfile(monkeypatch, tmp_path)
    cli_metrics.record_run_completion("probe", outcome="fail", duration_s=0.1)
    text = _prom_lines(tmp_path / "gitreins-probe.prom")
    assert not _series(text, "gitreins_cli_run_tokens_total")


def test_cap_outcome_records_cap_and_closed_reason(tmp_path, monkeypatch):
    _set_textfile(monkeypatch, tmp_path)
    cli_metrics.record_run_completion(
        "probe", outcome="cap", duration_s=0.2, reason="cap_iterations"
    )
    text = _prom_lines(tmp_path / "gitreins-probe.prom")
    assert 'outcome="cap"' in text
    assert 'gitreins_cli_run_failure_total{command="probe",reason="cap_iterations"} 1.0' in text
    # A cap-stop is not a success: no last_success gauge.
    assert not _series(text, "gitreins_cli_run_last_success_timestamp_seconds")


def test_textfile_payload_lines_are_only_these_series(tmp_path, monkeypatch):
    _set_textfile(monkeypatch, tmp_path)
    cli_metrics.record_run_completion("probe", outcome="pass", duration_s=0.01)
    text = _prom_lines(tmp_path / "gitreins-probe.prom")
    data_lines = [line for line in text.splitlines() if not line.startswith("#")]
    assert data_lines, "exposition must carry data lines"
    assert all(re.match(r"^gitreins_cli_run_", line) for line in data_lines)
    # Atomic replace left no tmp residue in the collector directory
    # (other entries in tmp_path belong to conftest fixtures, not this run).
    assert not list(tmp_path.glob("*.tmp"))


def test_push_transport_posts_to_pushgateway_job_path(tmp_path, monkeypatch):
    monkeypatch.delenv("GITREINS_METRICS_TEXTFILE_DIR", raising=False)
    monkeypatch.setenv("GITREINS_METRICS_PUSH_URL", "http://127.0.0.1:19090/")
    calls: list[tuple[str, float, bytes]] = []

    class _FakeResponse:
        def read(self) -> bytes:
            return b""

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return None

    def _fake_urlopen(request, timeout=None):
        calls.append((request.full_url, timeout, request.data))
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)
    cli_metrics.record_run_completion("probe", outcome="pass", duration_s=0.01)
    assert len(calls) == 1
    url, timeout, body = calls[0]
    assert url == "http://127.0.0.1:19090/metrics/job/gitreins_cli"
    assert timeout is not None and timeout <= 2.0
    assert b"gitreins_cli_run_completed_total" in body


def test_push_transport_failure_is_swallowed(tmp_path, monkeypatch):
    _set_textfile(monkeypatch, tmp_path)  # textfile unset below; push only
    monkeypatch.delenv("GITREINS_METRICS_TEXTFILE_DIR", raising=False)
    monkeypatch.setenv("GITREINS_METRICS_PUSH_URL", "http://127.0.0.1:19090/")

    def _raising_urlopen(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _raising_urlopen)
    cli_metrics.record_run_completion("probe", outcome="pass", duration_s=0.01)


def test_neither_transport_configured_is_a_noop(tmp_path, monkeypatch):
    monkeypatch.delenv("GITREINS_METRICS_TEXTFILE_DIR", raising=False)
    monkeypatch.delenv("GITREINS_METRICS_PUSH_URL", raising=False)
    cli_metrics.record_run_completion("probe", outcome="pass", duration_s=0.01)
    assert not list(tmp_path.glob("gitreins-*.prom"))


def test_reason_outside_closed_set_is_dropped(tmp_path, monkeypatch):
    _set_textfile(monkeypatch, tmp_path)
    cli_metrics.record_run_completion(
        "probe", outcome="error", duration_s=0.1, reason="ValueError: boom"
    )
    text = _prom_lines(tmp_path / "gitreins-probe.prom")
    assert not _series(text, "gitreins_cli_run_failure_total")


# ── Closed-set reason mapping ─────────────────────────────────────────────


def test_cap_reason_maps_the_four_eval_cap_message_shapes():
    assert (
        cli_metrics.cap_reason(
            "Iteration cap (100) reached (100.9 used). Increase max_iterations or split criteria."
        )
        == "cap_iterations"
    )
    assert (
        cli_metrics.cap_reason(
            "Time cap (30s) exceeded (30s elapsed). Increase max_time or simplify criteria."
        )
        == "cap_seconds"
    )
    assert (
        cli_metrics.cap_reason(
            "Input token budget (200k) exceeded (201k used). Increase max_input_tokens or reduce message context."
        )
        == "cap_tokens"
    )
    assert (
        cli_metrics.cap_reason(
            "Output token budget (50k) exceeded (50k used). Increase max_output_tokens or simplify criteria."
        )
        == "cap_tokens"
    )
    assert cli_metrics.cap_reason(None) is None
    assert cli_metrics.cap_reason("totally unrelated") is None


def test_failure_reason_maps_typed_llm_transport_errors_only():
    assert cli_metrics.failure_reason(requests.HTTPError("500")) == "llm_http"
    assert cli_metrics.failure_reason(requests.ConnectionError("reset by peer")) == "llm_network"
    assert cli_metrics.failure_reason(requests.Timeout("timed out")) == "llm_network"
    assert cli_metrics.failure_reason(ValueError("boom")) is None
    assert cli_metrics.failure_reason(RuntimeError("no provider")) is None


# ── Dispatcher hook: behaviour is unchanged, records still emitted ────────


def _install_handler(monkeypatch: pytest.MonkeyPatch, name: str, fn) -> None:
    monkeypatch.setitem(cli_module.__dict__, name, fn)


def test_dispatcher_success_records_pass(tmp_path, monkeypatch):
    _set_textfile(monkeypatch, tmp_path)

    def _cmd_obs_probe(args):
        return None

    _install_handler(monkeypatch, "cmd_obs_probe", _cmd_obs_probe)
    cli_module._run_subcommand_handler("cmd_obs_probe", argparse.Namespace())
    text = _prom_lines(tmp_path / "gitreins-obs_probe.prom")
    assert 'outcome="pass"' in text
    assert 'gitreins_cli_run_duration_seconds_count{command="obs_probe"}' in text


def test_dispatcher_raising_handler_records_error_and_reraises(tmp_path, monkeypatch):
    _set_textfile(monkeypatch, tmp_path)

    def _cmd_obs_probe(args):
        raise ValueError("boom")

    _install_handler(monkeypatch, "cmd_obs_probe", _cmd_obs_probe)
    with pytest.raises(ValueError, match="boom"):
        cli_module._run_subcommand_handler("cmd_obs_probe", argparse.Namespace())
    text = _prom_lines(tmp_path / "gitreins-obs_probe.prom")
    assert 'gitreins_cli_run_completed_total{command="obs_probe",outcome="error"} 1.0' in text
    # Unmappable error: no failure series (closed set holds, no free text).
    assert not _series(text, "gitreins_cli_run_failure_total")


def test_dispatcher_cap_trip_via_the_eval_cap_seam(tmp_path, monkeypatch):
    _set_textfile(monkeypatch, tmp_path)

    def _cmd_obs_probe(args):
        # The production shape: EvalCap trips mid-run, the evaluator degrades,
        # the subcommand still exits non-zero.
        eval_cap._trip(
            "Iteration cap (100) reached (100.0 used). Increase max_iterations or split criteria."
        )
        raise SystemExit(1)

    _install_handler(monkeypatch, "cmd_obs_probe", _cmd_obs_probe)
    with pytest.raises(SystemExit) as excinfo:
        cli_module._run_subcommand_handler("cmd_obs_probe", argparse.Namespace())
    assert excinfo.value.code == 1
    text = _prom_lines(tmp_path / "gitreins-obs_probe.prom")
    assert 'gitreins_cli_run_completed_total{command="obs_probe",outcome="cap"} 1.0' in text
    assert 'gitreins_cli_run_failure_total{command="obs_probe",reason="cap_iterations"} 1.0' in text
    # The dispatcher resets per run: a second, clean run is not poisoned.

    def _cmd_obs_clean(args):
        return None

    _install_handler(monkeypatch, "cmd_obs_clean", _cmd_obs_clean)
    cli_module._run_subcommand_handler("cmd_obs_clean", argparse.Namespace())
    text = _prom_lines(tmp_path / "gitreins-obs_clean.prom")
    assert 'outcome="pass"' in text
    assert 'reason="cap_iterations"' not in text


def test_typed_failure_exit_records_fail_without_reason(tmp_path, monkeypatch):
    _set_textfile(monkeypatch, tmp_path)

    def _cmd_obs_probe(args):
        raise SystemExit(2)

    _install_handler(monkeypatch, "cmd_obs_probe", _cmd_obs_probe)
    with pytest.raises(SystemExit) as excinfo:
        cli_module._run_subcommand_handler("cmd_obs_probe", argparse.Namespace())
    assert excinfo.value.code == 2
    text = _prom_lines(tmp_path / "gitreins-obs_probe.prom")
    assert 'outcome="fail"' in text
    assert not _series(text, "gitreins_cli_run_failure_total")
    assert not _series(text, "gitreins_cli_run_last_success_timestamp_seconds")


def test_transport_failure_does_not_change_the_cli_exit_code(tmp_path, monkeypatch):
    monkeypatch.delenv("GITREINS_METRICS_TEXTFILE_DIR", raising=False)
    monkeypatch.setenv("GITREINS_METRICS_PUSH_URL", "http://127.0.0.1:19090/")

    def _raising_urlopen(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(urllib.request, "urlopen", _raising_urlopen)

    def _cmd_obs_probe(args):
        raise SystemExit(3)

    _install_handler(monkeypatch, "cmd_obs_probe", _cmd_obs_probe)
    with pytest.raises(SystemExit) as excinfo:
        cli_module._run_subcommand_handler("cmd_obs_probe", argparse.Namespace())
    assert excinfo.value.code == 3


# ── Rework: dispatcher wiring (issues a/b/c of the OBS-GITREINS-2 judge) ──


def test_dispatcher_emits_tokens_from_the_usage_ledger_accumulator(tmp_path, monkeypatch):
    """Issue (a) proof: the REAL dispatcher carries real token counts.

    The judge pipeline's LLM steps already record their counts through the
    ONE accounting path, ``engine.usage.append_usage_row``; the run-scoped
    accumulator rides that same write, and the dispatcher resets it before
    the handler and takes it after. Here the run's LLM step is stubbed to
    append one row (11 in / 5 out) with the REAL writer, and the
    dispatcher-produced exposition must carry both token series.
    """
    from engine.usage import append_usage_row

    _set_textfile(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()

    def _cmd_obs_probe(args):
        assert append_usage_row(str(repo), step="tier2", tokens_in=11, tokens_out=5)

    _install_handler(monkeypatch, "cmd_obs_probe", _cmd_obs_probe)
    cli_module._run_subcommand_handler("cmd_obs_probe", argparse.Namespace())
    text = _prom_lines(tmp_path / "gitreins-obs_probe.prom")
    assert 'gitreins_cli_run_tokens_total{command="obs_probe",kind="input"} 11.0' in text
    assert 'gitreins_cli_run_tokens_total{command="obs_probe",kind="output"} 5.0' in text
    assert 'gitreins_cli_run_completed_total{command="obs_probe",outcome="pass"} 1.0' in text

    # Reset per dispatch (like the cap-trip note): a follow-up run with no
    # LLM step must not carry the previous run's counts.
    _install_handler(monkeypatch, "cmd_obs_probe", lambda args: None)
    cli_module._run_subcommand_handler("cmd_obs_probe", argparse.Namespace())
    text = _prom_lines(tmp_path / "gitreins-obs_probe.prom")
    assert not _series(text, "gitreins_cli_run_tokens_total")


def test_run_token_accumulator_takes_and_resets(tmp_path, monkeypatch):
    """The accumulator counts only rows that reached the ledger, and taking consumes."""
    from engine import usage

    monkeypatch.delenv("GITREINS_METRICS_TEXTFILE_DIR", raising=False)
    monkeypatch.delenv("GITREINS_METRICS_PUSH_URL", raising=False)
    usage.reset_run_tokens()
    assert usage.take_run_tokens() == (0, 0)
    assert usage.append_usage_row(str(tmp_path), step="tier2", tokens_in=3, tokens_out=1)
    assert usage.take_run_tokens() == (3, 1)
    assert usage.take_run_tokens() == (0, 0)  # taking consumes the counts


def test_orphan_reap_records_closed_reason_orphaned(tmp_path, monkeypatch):
    """Issue (b) proof: the dead-pid reap path produces ``reason="orphaned"``.

    A synthetic ``running`` job record with a dead worker pid, reaped by the
    REAL dispatcher path (``gitreins judge <id> --status``, the QA-GITR-002
    flip) — the run's completion record must carry
    ``gitreins_cli_run_failure_total{...,reason="orphaned"}`` with the
    command's own typed outcome (exit 3 → ``fail``).
    """
    import subprocess
    import sys

    from engine.job_store import load_job, make_job, save_job

    _set_textfile(monkeypatch, tmp_path)
    monkeypatch.setenv("GITREINS_JOB_DIR", str(tmp_path / "jobs"))

    exited = subprocess.Popen([sys.executable, "-c", "pass"])
    exited.wait()  # reaped: the pid is gone

    job = make_job("orphan-task", str(tmp_path))
    job["id"] = "job-orphan0001"
    job["pid"] = exited.pid
    save_job(job)

    monkeypatch.setattr(sys, "argv", ["gitreins", "judge", "job-orphan0001", "--status"])
    with pytest.raises(SystemExit) as excinfo:
        cli_module.main()
    assert excinfo.value.code == 3

    text = _prom_lines(tmp_path / "gitreins-judge.prom")
    assert 'gitreins_cli_run_failure_total{command="judge",reason="orphaned"} 1.0' in text
    assert 'gitreins_cli_run_completed_total{command="judge",outcome="fail"} 1.0' in text
    # The reap really happened: the stored record is terminal.
    assert load_job("job-orphan0001")["status"] == "error"


def test_orphan_note_is_reset_and_consumed():
    """The note is per-dispatch state: consumed once, never carried over."""
    cli_metrics.reset_orphan_reap()
    assert not cli_metrics.take_orphan_reap()
    cli_metrics.note_orphan_reap()
    assert cli_metrics.take_orphan_reap()
    assert not cli_metrics.take_orphan_reap()


def test_task_dispatch_records_a_bounded_task_label(tmp_path, monkeypatch):
    """Issue (c) proof: `gitreins task list` writes a record via the shared seam.

    The ``task`` family used to bypass the dispatcher entirely; now the one
    seam covers it and the label is the bounded ``task_<action>`` derived
    from the resolved subcommand — never argv text, a task id or a path.
    """
    import sys

    _set_textfile(monkeypatch, tmp_path)
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()  # get_workdir(): a (fake) repo root
    monkeypatch.chdir(repo)

    monkeypatch.setattr(sys, "argv", ["gitreins", "task", "list"])
    cli_module.main()  # empty store → "No tasks found.", exit 0

    text = _prom_lines(tmp_path / "gitreins-task_list.prom")
    assert 'gitreins_cli_run_completed_total{command="task_list",outcome="pass"} 1.0' in text
    assert not _series(text, "gitreins_cli_run_failure_total")

    # A bare `gitreins task` prints help and dispatches nothing —
    # no record, no invented label.
    (tmp_path / "gitreins-task_list.prom").unlink()
    monkeypatch.setattr(sys, "argv", ["gitreins", "task"])
    cli_module.main()
    assert not (tmp_path / "gitreins-task_list.prom").exists()
