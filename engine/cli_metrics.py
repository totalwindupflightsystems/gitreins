"""CLI completion records (OBS-GITREINS-2, docs/observability-spec.md §4b/§5b).

Every ephemeral CLI subcommand emits exactly one completion record on exit —
success, typed failure, crash and cap-stop alike — from the single dispatcher
``gitreins.cli._run_subcommand_handler``. An ephemeral process cannot be
scraped, so the record is PUSHED, never served:

* ``GITREINS_METRICS_PUSH_URL`` set → POST the text exposition to
  ``<url>/metrics/job/gitreins_cli`` (pushgateway shape), 2 s timeout.
* else ``GITREINS_METRICS_TEXTFILE_DIR`` set → write the same text ATOMICALLY
  (tmp file + :func:`os.replace`) to ``<dir>/gitreins-<command>.prom`` for a
  node-exporter textfile collector.
* else → no-op. The dispatcher pays one ``os.environ.get`` and this module
  never touches ``prometheus_client``.

Series — names and labels are SPEC'D in §4b, do not rename::

    gitreins_cli_run_completed_total{command,outcome}        outcome ∈ pass|fail|error|cap
    gitreins_cli_run_duration_seconds{command}               histogram
    gitreins_cli_run_tokens_total{command,kind}              kind ∈ input|output, when > 0
    gitreins_cli_run_failure_total{command,reason}           reason from the CLOSED set below
    gitreins_cli_run_last_success_timestamp_seconds{command} set only on outcome="pass"

The ``command`` label is the dispatcher's handler name minus the ``cmd_``
prefix (``config_validate``, ``worktree_merge``, ``qa_record``) — bounded by
the static handler tables in ``gitreins/cli.py``, never free text and never a
path, task id or error message.

Best-effort by contract, exactly like ``engine.usage.append_usage_row``
(``engine/usage.py``): ANY failure — bad URL, unwritable directory, registry
error — is swallowed. This module never raises, never prints, and never
changes the process exit code. A fresh ``CollectorRegistry`` is built per
record, so the payload carries only these series.

Token counts: the dispatcher currently passes none. There is no cheap
in-process source reachable at dispatch time (EvalCap instances are
per-evaluator and die with the run; ``usage.jsonl`` is the persisted ledger
the pipeline already writes per step), so the tokens counter is emitted only
when a caller supplies a count rather than adding a second accounting path.

``reason`` mapping lives in :func:`cap_reason` and :func:`failure_reason`.
A reason outside :data:`REASONS` is dropped, never emitted as free text.
"""

from __future__ import annotations

import os
import time
import urllib.request

# The closed reason set from spec §4b. NEVER extend it with error text.
REASONS = frozenset(
    {
        "llm_http",
        "llm_network",
        "cap_iterations",
        "cap_seconds",
        "cap_tokens",
        "orphaned",
    }
)

OUTCOMES = frozenset({"pass", "fail", "error", "cap"})

PUSH_JOB = "gitreins_cli"
PUSH_TIMEOUT_S = 2.0

_PROM_HELP = {
    "completed": "Completed CLI subcommand runs, by outcome.",
    "duration": "CLI subcommand run duration in seconds.",
    "tokens": "Tokens consumed by the run, by kind.",
    "failure": "Failed CLI subcommand runs, by reason from the closed set.",
    "last_success": "Unix time of the last run that returned cleanly.",
}


def cap_reason(message: str | None) -> str | None:
    """Map an evaluator cap message to its closed-set reason, else ``None``.

    The four message shapes are the ones ``engine.eval_cap`` produces (its
    ``Iteration cap`` / ``Time cap`` / ``Input token budget`` /
    ``Output token budget`` strings). Unknown text maps to ``None`` — never a
    free-text label.
    """
    if not message:
        return None
    if "Iteration cap" in message:
        return "cap_iterations"
    if "Time cap" in message:
        return "cap_seconds"
    if "Input token budget" in message or "Output token budget" in message:
        return "cap_tokens"
    return None


def failure_reason(exc: BaseException) -> str | None:
    """Map an unexpected error object to a closed-set reason, else ``None``.

    Typed LLM transport failures map to ``llm_http`` / ``llm_network``
    (``engine.llm`` raises ``requests.HTTPError`` and ``RequestException``
    subclasses). Anything else — including ``str(exc)`` — maps to ``None``:
    an unmappable error contributes NO ``gitreins_cli_run_failure_total``
    series rather than an unbounded label.
    """
    try:
        import requests
    except Exception:  # pragma: no cover - requests is a declared dependency
        return None
    if isinstance(exc, requests.HTTPError):
        return "llm_http"
    if isinstance(
        exc,
        (requests.ConnectionError, requests.Timeout),
    ):
        return "llm_network"
    return None


def _transport() -> tuple[str, str]:
    """The configured transport: ``("push", url)``, ``("textfile", dir)`` or ``("none", "")``."""
    push_url = os.environ.get("GITREINS_METRICS_PUSH_URL")
    if push_url:
        return "push", push_url
    textfile_dir = os.environ.get("GITREINS_METRICS_TEXTFILE_DIR")
    if textfile_dir:
        return "textfile", textfile_dir
    return "none", ""


def _exposition(
    command: str,
    outcome: str,
    duration_s: float,
    tokens_in: int,
    tokens_out: int,
    reason: str | None,
) -> str | None:
    """Build the text-exposition payload from a DEDICATED registry, or ``None``."""
    try:
        from prometheus_client import (
            CollectorRegistry,
            Counter,
            Gauge,
            Histogram,
            generate_latest,
        )
    except Exception:
        return None
    registry = CollectorRegistry()
    completed = Counter(
        "gitreins_cli_run_completed_total",
        _PROM_HELP["completed"],
        ("command", "outcome"),
        registry=registry,
    )
    completed.labels(command=command, outcome=outcome).inc()
    duration = Histogram(
        "gitreins_cli_run_duration_seconds",
        _PROM_HELP["duration"],
        ("command",),
        registry=registry,
    )
    duration.labels(command=command).observe(max(duration_s, 0.0))
    if tokens_in > 0 or tokens_out > 0:
        tokens = Counter(
            "gitreins_cli_run_tokens_total",
            _PROM_HELP["tokens"],
            ("command", "kind"),
            registry=registry,
        )
        if tokens_in > 0:
            tokens.labels(command=command, kind="input").inc(tokens_in)
        if tokens_out > 0:
            tokens.labels(command=command, kind="output").inc(tokens_out)
    if reason:
        failures = Counter(
            "gitreins_cli_run_failure_total",
            _PROM_HELP["failure"],
            ("command", "reason"),
            registry=registry,
        )
        failures.labels(command=command, reason=reason).inc()
    if outcome == "pass":
        last_success = Gauge(
            "gitreins_cli_run_last_success_timestamp_seconds",
            _PROM_HELP["last_success"],
            ("command",),
            registry=registry,
        )
        last_success.labels(command=command).set(time.time())
    payload: bytes = generate_latest(registry)
    return payload.decode("utf-8", "replace")


def _push(push_url: str, payload: str) -> None:
    """POST the payload to the pushgateway. Raises on transport failure."""
    url = push_url.rstrip("/") + "/metrics/job/" + PUSH_JOB
    request = urllib.request.Request(
        url,
        data=payload.encode("utf-8"),
        headers={"Content-Type": "text/plain; version=0.0.4"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=PUSH_TIMEOUT_S) as response:
        response.read()


def _write_textfile(textfile_dir: str, command: str, payload: str) -> None:
    """Write the payload atomically to ``<dir>/gitreins-<command>.prom``."""
    target = os.path.join(textfile_dir, f"gitreins-{command}.prom")
    tmp_path = f"{target}.{os.getpid()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        handle.write(payload)
    os.replace(tmp_path, target)


def record_run_completion(
    command: str,
    *,
    outcome: str,
    duration_s: float,
    tokens_in: int = 0,
    tokens_out: int = 0,
    reason: str | None = None,
) -> None:
    """Emit one CLI completion record on the configured transport.

    Best-effort by contract (``append_usage_row``): never raises, never
    prints, never changes the exit code. With neither environment variable
    set this is a single early return.
    """
    if not command:
        command = "unknown"
    if outcome not in OUTCOMES:
        outcome = "error"
    if reason not in REASONS:
        reason = None
    mode, target = _transport()
    if mode == "none":
        return
    try:
        payload = _exposition(command, outcome, duration_s, tokens_in, tokens_out, reason)
        if payload is None:
            return
        if mode == "push":
            _push(target, payload)
        else:
            _write_textfile(target, command, payload)
    except Exception:
        return
