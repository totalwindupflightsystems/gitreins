#!/usr/bin/env python3
"""Bounded, self-cleaning CPU load generator for load-dependent tests.

Why this exists (INT-FLAKE-5): reproducing a load-dependent flake used to mean
shell one-liners such as ``setsid sh -c 'while :; do :; done'``.  Those loops
are session leaders detached from the caller, so a killed runner leaves them
spinning forever — a verification script became a host incident (load average
33.8, 26 survivors, on the box that also runs the Hermes gateway, the scheduler
and DuckBrain).  This module is the replacement: load that cannot outlive its
runner.

Safety by construction:

* children are ``multiprocessing`` processes with ``daemon=True`` AND a Linux
  ``PR_SET_PDEATHSIG`` arming the kernel to SIGKILL them when the parent dies —
  including a ``SIGKILL`` of the parent, which no ``atexit`` handler can cover;
* no ``setsid``/detached sessions and no shell busy loops: nothing is left
  behind when the runner is terminated;
* the worker count is hard-capped, the run is bounded by ``--seconds``, and the
  CPU set can be restricted (``--cpus``) so a bounded load stays bounded;
* the runner refuses to start on a host that is running the shared Hermes
  services (gateway / cron scheduler / DuckBrain) unless explicitly allowed —
  synthetic load belongs on a bunker box, not on the host that serves the fleet;
* it verifies every child pid is gone before returning and reports a survivor as
  a failure (exit 1).

Usage::

    python scripts/loadgen.py --workers 4 --seconds 60 --cpus 0-3
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import signal
import sys
import time
from multiprocessing import Process

MAX_WORKERS = 8
DEFAULT_WORKERS = 4
MAX_SECONDS = 600.0
PR_SET_PDEATHSIG = 1
SHARED_HOST_MARKERS = (
    "hermes_cli.main gateway",
    "cron.scheduler",
    "duckbrain.js",
    "schedulerd",
)
ALLOW_SHARED_ENV = "GITREINS_LOADGEN_ALLOW_SHARED_HOST"


def _arm_parent_death_signal(parent_pid: int) -> bool:
    """Ask the kernel to SIGKILL this process when its parent dies (Linux).

    Returns False where the mechanism is unavailable (non-Linux, no libc); the
    daemonic flag and the parent's own cleanup still apply there.
    """
    if os.name != "posix" or not sys.platform.startswith("linux"):
        return False
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        if libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
            return False
    except (OSError, AttributeError):
        return False
    if os.getppid() != parent_pid:
        # The parent died between fork and arming: never spin on.
        os._exit(0)
    return True


def _spin(parent_pid: int) -> None:  # pragma: no cover - burns CPU until killed
    _arm_parent_death_signal(parent_pid)
    while True:
        pass


def shared_host_processes(markers: tuple[str, ...] = SHARED_HOST_MARKERS) -> list[str]:
    """Return cmdlines of the shared Hermes services running on this host."""
    found: list[str] = []
    if not os.path.isdir("/proc"):
        return found
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join("/proc", entry, "cmdline"), "rb") as handle:
                cmdline = handle.read().decode("utf-8", "replace").replace("\x00", " ").strip()
        except OSError:
            continue
        if cmdline and any(marker in cmdline for marker in markers):
            found.append(f"{entry}: {cmdline[:120]}")
    return found


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - other user's process
        return True
    except OSError:  # pragma: no cover
        return False
    return True


def _terminate(children: list[Process]) -> None:
    for child in children:
        if child.is_alive():
            child.terminate()
    for child in children:
        child.join(timeout=5)
    for child in children:
        if child.is_alive():  # pragma: no cover - defensive
            child.kill()
            child.join(timeout=5)


def _parse_cpus(spec: str, allowed: set[int] | None = None) -> set[int]:
    """Parse ``0-3,7`` into a CPU set, intersected with what the host allows."""
    chosen: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, _, end = part.partition("-")
            chosen.update(range(int(start), int(end) + 1))
        else:
            chosen.add(int(part))
    if not chosen:
        raise ValueError("empty CPU set")
    return chosen & allowed if allowed else chosen


def generate(
    workers: int = DEFAULT_WORKERS,
    seconds: float = 30.0,
    *,
    cpus: str | None = None,
    allow_shared_host: bool | None = None,
    markers: tuple[str, ...] = SHARED_HOST_MARKERS,
    on_start=None,
) -> dict:
    """Run ``workers`` CPU burners for ``seconds`` and report the outcome.

    Raises ``ValueError`` for an out-of-range request and ``RuntimeError`` when
    the host runs the shared Hermes services while ``allow_shared_host`` is not
    set (the env var ``GITREINS_LOADGEN_ALLOW_SHARED_HOST`` accepts 1/true/yes).
    ``on_start`` is called with the child pids once they are running.
    """
    if not isinstance(workers, int) or isinstance(workers, bool) or workers < 1:
        raise ValueError(f"workers must be a positive integer, got {workers!r}")
    if workers > MAX_WORKERS:
        raise ValueError(f"workers is capped at {MAX_WORKERS}, got {workers!r}")
    if not isinstance(seconds, (int, float)) or isinstance(seconds, bool) or seconds <= 0:
        raise ValueError(f"seconds must be positive, got {seconds!r}")
    if seconds > MAX_SECONDS:
        raise ValueError(f"seconds is capped at {MAX_SECONDS}, got {seconds!r}")

    if allow_shared_host is None:
        allow_shared_host = os.environ.get(ALLOW_SHARED_ENV, "").strip().lower() in {
            "1",
            "true",
            "yes",
        }
    services = shared_host_processes(markers)
    if services and not allow_shared_host:
        raise RuntimeError(
            "refusing to generate synthetic load on a host running the shared Hermes services "
            f"({'; '.join(services[:3])}); run this on a bunker box, or set {ALLOW_SHARED_ENV}=1 "
            "(or pass --allow-shared-host) while the affected fleet lane is paused"
        )

    affinity_before = None
    if cpus:
        if not hasattr(os, "sched_setaffinity"):  # pragma: no cover - non-Linux
            raise ValueError("--cpus is only supported where os.sched_setaffinity exists")
        try:
            affinity_before = sorted(os.sched_getaffinity(0))
            os.sched_setaffinity(0, _parse_cpus(cpus, os.sched_getaffinity(0)))
        except (OSError, ValueError) as exc:
            raise ValueError(f"could not restrict the CPU set to {cpus!r}: {exc}") from exc

    parent_pid = os.getpid()
    children: list[Process] = []
    summary: dict = {
        "workers": workers,
        "seconds": seconds,
        "cpus": cpus,
        "shared_services": services,
        "allow_shared_host": bool(allow_shared_host),
        "pids": [],
        "survivors": [],
        "clean": True,
        "pdeathsig": sys.platform.startswith("linux"),
    }
    try:
        for _ in range(workers):
            child = Process(target=_spin, args=(parent_pid,), daemon=True)
            children.append(child)
            child.start()
        summary["pids"] = [child.pid for child in children]
        if on_start is not None:
            on_start(list(summary["pids"]))
        deadline = time.monotonic() + float(seconds)
        while time.monotonic() < deadline:
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
    finally:
        _terminate(children)
        if affinity_before is not None:
            try:
                os.sched_setaffinity(0, set(affinity_before))
            except OSError:  # pragma: no cover - defensive
                pass
        survivors = [pid for pid in summary["pids"] if pid and _alive(pid)]
        summary["survivors"] = survivors
        summary["clean"] = not survivors
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--cpus", default=None, help="CPU set for the run, e.g. 0-3")
    parser.add_argument(
        "--allow-shared-host",
        action="store_true",
        help=f"permit load on a host running the shared services ({ALLOW_SHARED_ENV})",
    )
    args = parser.parse_args(argv)

    def _announce(pids):
        # The started line lets a harness see the child pids before it kills the
        # runner — which is exactly how the PDEATHSIG guarantee is verified.
        print(json.dumps({"event": "started", "pids": pids}), file=sys.stderr, flush=True)

    try:
        summary = generate(
            args.workers,
            args.seconds,
            cpus=args.cpus,
            allow_shared_host=args.allow_shared_host,
            on_start=_announce,
        )
    except (ValueError, RuntimeError) as exc:
        print(f"loadgen: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2))
    if not summary["clean"]:
        print(
            f"loadgen: survivors left running: {summary['survivors']}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry
    sys.exit(main())
