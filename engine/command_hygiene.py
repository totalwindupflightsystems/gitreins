"""command_hygiene — run judge commands bounded, and refuse CPU busy-waits.

Why this module exists (2026-09-18 host incident, measured live):

    The tier-2 evaluator's ``run_command`` tool executed the judge's shell with
    ``subprocess.run(cmd, shell=True, timeout=...)``. That is unsafe in the
    specific way that produced 278 orphaned CPU burners on the fleet host:

    * ``timeout=`` kills only the DIRECT child. A command that backgrounds
      work — e.g. the load-repro idiom agents invent
      ``for i in $(seq 1 64); do timeout 300 nice -n 0 sh -c 'while :; do :; done' & done``
      — leaves those children running; they are reparented to ``systemd --user``
      and outlive the evaluator entirely (measured: loadavg 220-346, ~2000 procs
      on the box that also serves the gateway, scheduler and DuckBrain).
    * Nothing stopped the judge from *manufacturing* load in the first place.

Two guarantees fix both:

1. ``run_bounded`` spawns the command in its OWN session
   (``start_new_session=True``) and kills the whole PROCESS GROUP when the call
   returns or times out, so backgrounded children cannot escape. Kill signals are
   validated (never PID 1, group must still exist in ``/proc``) — the repo's own
   rule after the ``os.killpg`` incident.
2. ``busy_wait_reason`` refuses busy-wait / unbounded CPU-burn / fork-bomb
   commands outright, pointing at the bounded alternative
   (``scripts/loadgen.py``) or ``sleep`` for waiting.

Output bounding (QA-GITREINS-POC-11, 2026-09-19): the captured output is bounded
by the SHARED line-bounded implementation in ``engine.evidence_bounds`` —
head + omission marker + TAIL — exactly like every other surface that persists
command output (``engine.pipeline``, ``engine.worktree_fleet``,
``engine.worktree_disposable``). The first cut of this module bounded the
output HEAD-ONLY (``output[:max_output]``), which threw away everything past
the cap: for a pipeline step whose pytest output sits after 4000+ chars of
earlier output, pytest's short test summary ("FAILED tests/...::test_x"), the
maxfail banner and xdist's ``Interrupted`` marker were all discarded, so
``engine.types.pytest_outcome`` honestly reported ``interrupted-unclassified``
for a run that had really failed a test. That is the defect class
DF-GITREINS-POC-8 / DF-GITREINS-POC-19 already fixed for the other evidence
surfaces; this module now uses the same bounder so it cannot drift again.
"""

from __future__ import annotations

import os
import re
import shlex
import signal
import subprocess
import time
from pathlib import Path

# The shared head+tail, line-bounded evidence bounder. Importing it here is
# safe — engine.evidence_bounds imports only engine.types (no cycle back into
# engine.command_hygiene).
from engine.evidence_bounds import MAX_EVIDENCE_CHARS, _bound_step_evidence

# ── refusal policy ────────────────────────────────────────────────────────────

_SPIN_LOOP = re.compile(r"while\s+(?::|true)\s*;\s*do\s+(?:(?::|true|continue)\s*;?\s*)+done")
_FORK_BOMB = re.compile(r":\s*\(\s*\)\s*\{[^}]*:\s*\|\s*:[^}]*\}")
_YES_PIPED = re.compile(r"(?:^|[;&|]\s*)yes\s*\|")
_BOUNDING_CONSUMER = re.compile(
    r"\|\s*(?:head|tail|grep\s+-m|sed\s+-n|timeout\s+\d+|awk[^|]*(?:exit|NR))"
)
_YES_BARE = re.compile(r"(?:^|[;&|]\s*)yes\s*(?:>\s*/dev/null|>>\s*/dev/null|$|[;&])")
_CAT_ZERO_REDIRECT = re.compile(r"cat\s+/dev/zero\s*(?:>|>>)")
_DD_UNBOUNDED = re.compile(r"dd\s+[^;&|]*if=/dev/zero[^;&|]*of=/dev/null")


def busy_wait_reason(cmd: str) -> str | None:
    """Return a human reason when ``cmd`` is a CPU busy-wait/burn, else None.

    Only UNBOUNDED forms are refused: ``yes | head -100`` and
    ``dd ... count=10`` terminate on their own and stay allowed.
    """
    if not cmd:
        return None
    normalised = re.sub(r"\s+", " ", cmd)
    if _SPIN_LOOP.search(normalised):
        return "a CPU busy-wait loop (`while :; do :; done`-class)"
    if _FORK_BOMB.search(normalised):
        return "a shell fork bomb"
    if _YES_PIPED.search(normalised) and not _BOUNDING_CONSUMER.search(normalised):
        return "an unbounded `yes` burn"
    if _YES_BARE.search(normalised):
        return "an unbounded `yes` burn"
    if _CAT_ZERO_REDIRECT.search(normalised):
        return "an unbounded `cat /dev/zero` burn"
    if _DD_UNBOUNDED.search(normalised) and "count=" not in normalised:
        return "an unbounded `dd if=/dev/zero of=/dev/null` burn"
    return None


BUSY_WAIT_MESSAGE = (
    "refused: {reason}.\n"
    "These loops burn a core for nothing and — when backgrounded — OUTLIVE the evaluator "
    "(reparented to systemd --user). On 2026-09-18 this left 278 orphaned burners, loadavg "
    "220-346, on the host that also serves the gateway, scheduler and DuckBrain.\n"
    "Use instead:\n"
    "  * to WAIT → `sleep <seconds>`;\n"
    "  * for bounded load in a flake repro → `python3 scripts/loadgen.py --workers 4 --seconds 60` "
    "(caps workers at 8, caps duration, arms PR_SET_PDEATHSIG, refuses shared hosts, fails if any "
    "child survives);\n"
    "  * a bounded burn → `timeout <s> stress-ng --cpu 2 --timeout <s>` when "
    "stress-ng is installed."
)

# ── process-group hygiene ─────────────────────────────────────────────────────


def pids_in_group(pgid: int) -> list[int]:
    """PIDs whose process group is ``pgid`` (validated; scans /proc)."""
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 1:
        return []
    found: list[int] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return found
    for entry in os.listdir(proc):
        if not entry.isdigit():
            continue
        try:
            stat = (proc / entry / "stat").read_text()
            # field 5 = pgrp (index 2 after the comm field, which may contain spaces)
            fields = stat[stat.rindex(")") + 2 :].split()
            pgrp = int(fields[2])
        except (OSError, ValueError, IndexError):
            continue
        if pgrp == pgid:
            found.append(int(entry))
    return found


def kill_group(pgid: int, grace: float = 3.0) -> list[int]:
    """SIGTERM then SIGKILL every process in ``pgid``'s group; return survivors.

    Refuses PID 1 and any group that no longer exists (never signal a reused
    PID): the group is re-checked in /proc between signals.
    """
    if not isinstance(pgid, int) or isinstance(pgid, bool) or pgid <= 1:
        return []
    if not pids_in_group(pgid):
        return []
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            return pids_in_group(pgid)
        time.sleep(grace)
        if not pids_in_group(pgid):
            return []
    return pids_in_group(pgid)


def run_bounded(
    cmd: str | list[str],
    *,
    cwd: str | None = None,
    timeout: float = 30.0,
    max_output: int = MAX_EVIDENCE_CHARS,
    env: dict | None = None,
) -> dict:
    """Run ``cmd`` in its own session; always reap leftover group members.

    ``cmd`` is a shell string (``shell=True``) or an argv LIST (``shell=False``,
    no intermediate shell — DF-CRIER-258). Both forms get the identical
    discipline: own session, whole-group reap on return/timeout, busy-wait
    refusal scan (a list is scanned via ``shlex.join(cmd)``).

    Returns ``{"cmd", "exit_code", "output", "timed_out", "pgid", ...}`` or
    ``{"cmd", "refused", "reason"}`` for a refused busy-wait. ``pgid`` is the
    spawned process's PID (== its process-group leader under
    ``start_new_session``); it lets a caller reap the group later
    (``engine.evaluator`` keeps it as a belt-and-braces reap target). Absent
    when the command was refused or spawn failed.

    ``output`` is bounded to ``max_output`` chars by the shared line-bounded
    head+TAIL bounder (``engine.evidence_bounds``) — never a head-only slice, so
    a summary written at the END of a run (pytest's short test summary, a
    trailing error banner) survives the bound, and the omission marker reports
    how many chars/lines went.
    """
    scan_target = shlex.join(cmd) if isinstance(cmd, list) else cmd
    reason = busy_wait_reason(scan_target)
    if reason:
        return {
            "cmd": cmd,
            "refused": True,
            "reason": BUSY_WAIT_MESSAGE.format(reason=reason),
        }

    try:
        proc = subprocess.Popen(
            cmd,
            shell=not isinstance(cmd, list),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=cwd,
            env=env,
            start_new_session=True,  # own process group → backgrounded children cannot escape
        )
    except OSError as exc:
        return {"cmd": cmd, "error": str(exc)}

    timed_out = False
    output = ""
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_group(proc.pid)
        try:
            output, _ = proc.communicate(timeout=5)
        except (subprocess.TimeoutExpired, ValueError, OSError):
            output = ""
    finally:
        # Even on clean exit the group may hold backgrounded children (`... &`):
        # this is the leak that produced the orphan fleet.
        leftovers = kill_group(proc.pid)

    output = output or ""
    # QA-GITREINS-POC-11: head+TAIL, line-bounded — the previously head-only
    # `output[:max_output]` slice threw away pytest's short test summary (and
    # the maxfail/xdist markers that live there), which made an honest failed
    # run read as `interrupted-unclassified`. Same shared bounder as every
    # other evidence surface, so this cannot drift again.
    output = _bound_step_evidence(output, max_output)

    result = {
        "cmd": cmd,
        "exit_code": proc.returncode,
        "output": output,
        "timed_out": timed_out,
        # DF-CRIER-258: the group leader's pid. Under start_new_session the
        # child is its own group leader, so callers can reap the group later
        # (kill_group is a validated no-op once the group is gone).
        "pgid": proc.pid,
    }
    if timed_out:
        result["error"] = f"Command timed out after {timeout}s"
    if leftovers:
        result["leftover_pids"] = leftovers
        result["warning"] = (
            f"process-group reap left survivors (reported, never hidden): {leftovers}"
        )
    return result
