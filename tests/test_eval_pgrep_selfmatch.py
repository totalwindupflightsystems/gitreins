"""
DF-031 — pgrep/pkill -f self-match neutralization in judge shell snippets.

A criteria snippet runs through run_command → command_hygiene.run_bounded as
``sh -c "<snippet>"``, so the snippet text is verbatim in the wrapper's own
command line. A literal ``pgrep -f PATTERN`` wait-probe therefore ALWAYS
matches its own wrapper (it polls to the iteration cap and never concludes)
and ``pkill -f PATTERN`` kills the judge shell. These tests pin the
evaluator-side detection/rewrite seam (engine.evaluator._neutralize_pgrep_selfmatch,
wired into AgenticEvaluator._tool_run_command) — including the regression
proof that the RAW (un-rewritten) form self-matches a real wrapper.
"""

import contextlib
import os
import shutil
import signal
import subprocess
import time

import pytest

from engine import command_hygiene
from engine.evaluator import AgenticEvaluator, _neutralize_pgrep_selfmatch

pytestmark = pytest.mark.skipif(
    not (shutil.which("pgrep") and shutil.which("pkill")),
    reason="pgrep/pkill required for the live-process semantics tests",
)


# ── Pure detection/rewrite tests (no processes) ──────────────────────────────


class TestNeutralizeFunction:
    """Unit tests for _neutralize_pgrep_selfmatch itself."""

    def test_plain_pgrep_rewritten(self) -> None:
        """A plain literal pattern is bracket-tricked inside a full snippet."""
        snippet = "for i in 1 2 3; do pgrep -f my-daemon && break; sleep 1; done"
        new, warning, refusal = _neutralize_pgrep_selfmatch(snippet)
        assert refusal is None
        assert warning is not None
        assert "pgrep" in warning
        assert 'pgrep -f "[m]y-daemon"' in new
        # No plain-form occurrence of the pattern may survive anywhere in the
        # snippet — the wrapper argv must not contain what the probe matches.
        assert "pgrep -f my-daemon" not in new

    def test_bracket_tricked_untouched(self) -> None:
        """An already bracket-tricked pattern passes through unchanged."""
        snippet = 'until pgrep -f "[s]entinel-xyz"; do sleep 1; done'
        new, warning, refusal = _neutralize_pgrep_selfmatch(snippet)
        assert new == snippet
        assert warning is None
        assert refusal is None

    def test_leading_char_class_untouched(self) -> None:
        """A pattern opening with a real char class is not rewritten."""
        snippet = 'pgrep -f "[0-9]daemon-name"'
        new, warning, refusal = _neutralize_pgrep_selfmatch(snippet)
        assert new == snippet
        assert warning is None
        assert refusal is None

    def test_double_quoted_plain_rewritten_in_place(self) -> None:
        """Quoting does not prevent a self-match; rewrite keeps the quotes."""
        new, warning, refusal = _neutralize_pgrep_selfmatch('pgrep -f "my daemon"')
        assert refusal is None
        assert warning is not None
        assert new == 'pgrep -f "[m]y daemon"'

    def test_pkill_plain_rewritten(self) -> None:
        """pkill -f gets the same mandatory rewrite (it would self-KILL)."""
        new, warning, refusal = _neutralize_pgrep_selfmatch("pkill -f runaway-worker")
        assert refusal is None
        assert warning is not None
        assert "pkill" in warning
        assert new == 'pkill -f "[r]unaway-worker"'

    def test_combined_short_flags(self) -> None:
        """Bundled short flags (pgrep -fl) are handled."""
        new, warning, refusal = _neutralize_pgrep_selfmatch("pgrep -fl my-daemon")
        assert refusal is None
        assert new == 'pgrep -fl "[m]y-daemon"'

    def test_options_after_f_preserved(self) -> None:
        """Options following -f are kept out of the pattern span."""
        new, warning, refusal = _neutralize_pgrep_selfmatch("pgrep -f -x -i my-daemon")
        assert refusal is None
        assert new == 'pgrep -f -x -i "[m]y-daemon"'

    def test_redirect_tail_not_pattern(self) -> None:
        """A trailing redirection is not swallowed into the pattern."""
        new, warning, refusal = _neutralize_pgrep_selfmatch("pgrep -f my-daemon > /tmp/log 2>&1")
        assert refusal is None
        assert new == 'pgrep -f "[m]y-daemon" > /tmp/log 2>&1'

    def test_subshell_operand(self) -> None:
        """`$(pgrep -f foo)` — the closing paren is not part of the pattern."""
        new, warning, refusal = _neutralize_pgrep_selfmatch("kill $(pgrep -f rogue-proc)")
        assert refusal is None
        assert new == 'kill $(pgrep -f "[r]ogue-proc")'

    def test_dynamic_unquoted_refused(self) -> None:
        """A variable-built pattern cannot be rewritten — refuse with guidance."""
        snippet = "while :; do pgrep -f $PAT && break; sleep 1; done"
        new, warning, refusal = _neutralize_pgrep_selfmatch(snippet)
        assert new == snippet
        assert warning is None
        assert refusal is not None
        assert "REFUSED" in refusal
        assert "bracket-tricked" in refusal
        assert "$PAT" in refusal

    def test_dynamic_double_quoted_refused(self) -> None:
        """`"$PAT"` is still shell-expanded → refuse."""
        snippet = 'pgrep -f "${SENTINEL}-worker"'
        new, warning, refusal = _neutralize_pgrep_selfmatch(snippet)
        assert new == snippet
        assert warning is None
        assert refusal is not None
        assert "pgrep" in refusal

    def test_dynamic_pkill_refused(self) -> None:
        """pkill with a variable pattern would kill the judge shell — refuse."""
        snippet = "pkill -f $TARGET-process"
        new, warning, refusal = _neutralize_pgrep_selfmatch(snippet)
        assert new == snippet
        assert refusal is not None
        assert "pkill" in refusal

    def test_single_quoted_dollar_is_literal_rewritten(self) -> None:
        """A `$` inside single quotes is literal text — safe to rewrite."""
        new, warning, refusal = _neutralize_pgrep_selfmatch("pgrep -f 'my-$LIT'")
        assert refusal is None
        assert warning is not None
        assert new == "pgrep -f '[m]y-$LIT'"

    def test_snippet_without_pgrep_untouched(self) -> None:
        """No pgrep/pkill in the snippet — nothing changes."""
        snippet = "pytest tests/ -q && echo done"
        new, warning, refusal = _neutralize_pgrep_selfmatch(snippet)
        assert new == snippet
        assert warning is None
        assert refusal is None

    def test_pgrep_without_f_untouched(self) -> None:
        """pgrep without -f matches process names only — no wrapper self-match."""
        snippet = "pgrep -l pytest-runner-name"
        new, warning, refusal = _neutralize_pgrep_selfmatch(snippet)
        assert new == snippet
        assert warning is None
        assert refusal is None

    def test_two_probes_one_warning(self) -> None:
        """Multiple probes in one snippet → one aggregated warning."""
        snippet = "pgrep -f first-proc || pkill -f second-proc"
        new, warning, refusal = _neutralize_pgrep_selfmatch(snippet)
        assert refusal is None
        assert warning is not None
        assert "first-proc" in warning
        assert "second-proc" in warning
        assert new == 'pgrep -f "[f]irst-proc" || pkill -f "[s]econd-proc"'


# ── Real-process semantics tests (self-match red-proof + preserved function) ─


def _spawn_sentinel(tag: str) -> subprocess.Popen:
    """Spawn `sh -c 'sleep 61 # <tag>'` in its own session.

    The sh stays alive (no exec), so its cmdline carries *tag* verbatim —
    a perfect wait-probe target with a stable pid.
    """
    proc = subprocess.Popen(
        ["sh", "-c", f"sleep 61 # {tag}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    time.sleep(0.3)
    assert proc.poll() is None, "sentinel process failed to start"
    return proc


def _kill_group(proc: subprocess.Popen) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=5)


def _pid_lines(output: str) -> list[int]:
    pids = []
    for line in output.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


class TestSelfMatchSemantics:
    """Live-process proof: raw form self-matches, rewritten form still works."""

    def test_raw_probe_self_matches_wrapper(self) -> None:
        """REGRESSION PROOF: the un-rewritten probe matches its own wrapper.

        This is the DF-031 defect: `sh -c "pgrep -f <tag>"` — the wrapper's
        argv contains <tag> verbatim, so a RAW `pgrep -f <tag>` reports at
        least two pids (the real target AND the wrapper shell), which is what
        made wait-probes poll to the iteration cap.
        """
        tag = "df031-wait-9x7q-a-sentinel"
        sentinel = _spawn_sentinel(tag)
        try:
            result = command_hygiene.run_bounded(f"pgrep -f {tag}", timeout=10)
            pids = _pid_lines(result.get("output", ""))
            # self-match: wrapper shell + real target → ≥2 DISTINCT pids
            assert len(pids) >= 2, f"raw probe did not self-match: {pids}"
            assert sentinel.pid in pids
            assert len(set(pids)) >= 2
        finally:
            _kill_group(sentinel)

    def test_rewritten_probe_still_finds_real_process(self, evaluator: AgenticEvaluator) -> None:
        """(a) The rewrite preserves function: exactly the real target found."""
        tag = "df031-wait-9x7q-b-sentinel"
        sentinel = _spawn_sentinel(tag)
        try:
            result = evaluator._tool_run_command(f"pgrep -f {tag}")
            # The tool rewrote the snippet before running it.
            assert result.get("cmd") == 'pgrep -f "[d]f031-wait-9x7q-b-sentinel"'
            assert "error" not in result
            pids = _pid_lines(result.get("output", ""))
            # No self-match: the wrapper no longer contains the plain literal.
            assert pids == [sentinel.pid]
        finally:
            _kill_group(sentinel)

    def test_rewritten_pkill_kills_only_real_target(self, evaluator: AgenticEvaluator) -> None:
        """pkill rewrite preserves function and does not kill the judge shell."""
        tag = "df031-pkill-9x7q-c-sentinel"
        sentinel = _spawn_sentinel(tag)
        try:
            result = evaluator._tool_run_command(f"pkill -f {tag}")
            assert "error" not in result
            assert result.get("exit_code") == 0
            assert 'pkill -f "[d]f031-pkill-9x7q-c-sentinel"' in result.get("cmd", "")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and sentinel.poll() is None:
                time.sleep(0.1)
            assert sentinel.poll() is not None, "rewritten pkill did not kill the target"
        finally:
            _kill_group(sentinel)

    def test_dynamic_refused_via_run_command(self, evaluator: AgenticEvaluator) -> None:
        """(c) Wiring: a variable-built pattern is refused at the tool seam."""
        result = evaluator._tool_run_command("pgrep -f $MY_SENTINEL_PROC")
        assert result.get("refused") is True
        error = result.get("error", "")
        assert "REFUSED" in error
        assert "bracket-tricked" in error
        assert "$MY_SENTINEL_PROC" in error
        assert result.get("cmd") == "pgrep -f $MY_SENTINEL_PROC"

    def test_snippet_without_pgrep_untouched_via_run_command(
        self, evaluator: AgenticEvaluator
    ) -> None:
        """(d) Wiring: snippets without pgrep/pkill run verbatim."""
        result = evaluator._tool_run_command("echo df031-noop-check")
        assert "error" not in result
        assert result.get("cmd") == "echo df031-noop-check"
        assert "df031-noop-check" in result.get("output", "")
