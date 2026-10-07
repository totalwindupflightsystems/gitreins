"""scanner_nice — one ``nice(1)`` policy for every external scanner GitReins spawns.

DF-GITREINS-POC-55 (Bane directive 2026-09-24): *"when gitreins is running and
it's gitleaks it should run with nice by default and configurable by
environment or as a setting"*.

The guard and the judge's tier 1 spawn external scanners — gitleaks,
golangci-lint, ``go build`` / ``go test``, ruff, pytest, the LSP servers. Scans
are background verification, not interactive work: on a busy box they must be
the least urgent CPU consumer, so a scan can never starve the human's editor or
another agent. Being nice is also provably verdict-neutral: ``nice(2)`` changes
scheduling priority only, never a result.

One policy, every spawn site (:mod:`engine.pipeline` for the judge's Tier 1
shell steps, :mod:`engine.guard_manager` + :mod:`engine.guards` +
:mod:`engine.lsp` for the guard lanes):

* **default ON at level 10**;
* ``GITREINS_SCANNER_NICE`` (env) overrides ``guards.scanner_nice`` (config),
  which overrides the default — precedence **env > config > default**;
* ``0`` disables it completely: the built command is byte-identical to the
  pre-change one (no prefix, no probe, no note line);
* **FAIL-OPEN**: a missing or non-runnable ``nice`` never fails a scan. The
  spawn runs unprefixed and one honest note line says so — a scheduling nicety
  may never become a scan failure;
* the applied prefix (or its unavailability) is **named in the evidence**,
  following the TRUST-003 precedent for scanner attribution.

Two spawn shapes, one policy:

* **shell steps** (:func:`shell_prologue` / :func:`shell_wrap`) — the probe runs
  INSIDE the shell, so it sees the PATH the scan will actually run with, and the
  prefix travels in a variable (``$_gr_nice``) that the fail-open branch clears;
* **argv spawns** (:func:`argv_prefix`) — a Python-side probe of
  ``nice -n <level> true``, cached per (level, PATH).

The prefix is withheld whenever the target program itself does not resolve on
PATH (:func:`argv_prefix`): ``nice`` EXECS its argv, so a prefixed argv whose
program is missing exits 127 with stderr text instead of raising
``FileNotFoundError`` — and every argv caller here keys on that exception to
report the tool as unavailable rather than as a failure.

Out of scope (phase 2 / host-level, per the same directive): ``ionice -c3``,
load-aware escalation, cgroups/cpuset/CPUQuota. GitReins' own interpreter and LLM
calls are never re-niced — only spawned scans.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from dataclasses import dataclass, replace
from typing import Mapping

logger = logging.getLogger("gitreins.scanner_nice")

#: Default level: a modest, always-deprioritised band (``nice -n 10``).
DEFAULT_LEVEL = 10
#: Highest usable level (``nice(2)``'s own ceiling).
MAX_LEVEL = 19
#: Environment override (``0`` = off).
ENV_VAR = "GITREINS_SCANNER_NICE"
#: Config location of the same knob.
CONFIG_SECTION = "guards"
CONFIG_KEY = "scanner_nice"

# `nice -n <level> true` is a one-shot fork; the timeout only bounds a
# pathological shim on PATH hanging the caller.
_PROBE_TIMEOUT_S = 15.0

# (level, PATH) -> (available, reason). Keyed on PATH because availability IS a
# PATH property (a shim can appear or disappear) — a caller that swaps PATH must
# never read another PATH's verdict.
_availability: dict[tuple[int, str], tuple[bool, str]] = {}


# ── level resolution ─────────────────────────────────────────────────────────


def parse_level(value: object) -> int | None:
    """``0..19`` from an int or numeric string; ``None`` when unusable.

    Rejects ``bool`` explicitly (``True`` is an ``int`` in Python and would
    otherwise enable level 1) and every out-of-range or non-numeric value — an
    unusable knobs value is *ignored*, never passed through to ``nice``, which
    would fail the scan (``nice -n 25: invalid number``).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        level = value
    elif isinstance(value, str):
        text = value.strip()
        if not text.isdigit():
            return None
        level = int(text)
    else:
        return None
    return level if 0 <= level <= MAX_LEVEL else None


def coerce_level(value: object, *, fallback: int = DEFAULT_LEVEL, what: str = CONFIG_KEY) -> int:
    """*value* as a level, or *fallback* (with a loud warning) when unusable."""
    level = parse_level(value)
    if level is None:
        if value is not None:
            logger.warning(
                "%s=%r is not a level 0..%d — using %d", what, value, MAX_LEVEL, fallback
            )
        return fallback
    return level


def _config_level(config: object) -> object:
    """The raw ``guards.scanner_nice`` value out of any config shape we accept.

    Accepts the raw config dict (the guard's ``self.config``), a
    :class:`engine.config.GitReinsDefaults` instance, or a bare level — so every
    surface can hand over whatever it holds without a conversion step.
    """
    if config is None:
        return None
    if isinstance(config, (int, str)):
        return config
    if isinstance(config, Mapping):
        guards = config.get(CONFIG_SECTION, {})
        if not isinstance(guards, Mapping):
            return None
        return guards.get(CONFIG_KEY)
    return getattr(config, CONFIG_KEY, None)


def resolve_level(config: object = None, env: Mapping[str, str] | None = None) -> tuple[int, str]:
    """Resolve ``(level, source)`` with precedence **env > config > default**.

    An unusable value at either layer is ignored (and warned about) so the next
    layer decides — the fail-open posture the whole module follows: a bad
    niceness value must never be able to fail a scan.
    """
    env_map = os.environ if env is None else env
    raw_env = env_map.get(ENV_VAR)
    if raw_env is not None:
        level = parse_level(raw_env)
        if level is not None:
            return level, "env"
        if str(raw_env).strip():
            logger.warning("%s=%r is not a level 0..%d — ignored", ENV_VAR, raw_env, MAX_LEVEL)

    raw_config = _config_level(config)
    if raw_config is not None:
        level = parse_level(raw_config)
        if level is not None:
            return level, "config"
        logger.warning(
            "%s.%s=%r is not a level 0..%d — ignored",
            CONFIG_SECTION,
            CONFIG_KEY,
            raw_config,
            MAX_LEVEL,
        )

    return DEFAULT_LEVEL, "default"


# ── evidence lines ──────────────────────────────────────────────────────────


def nice_exe(level: int) -> str:
    """The prefix as it is written on a command line (``nice -n 10``)."""
    return f"nice -n {level}"


def note_applied(level: int) -> str:
    """Evidence line naming the applied prefix."""
    return f"scanners: nice={nice_exe(level)}"


def note_unavailable(reason: str) -> str:
    """Evidence line for the fail-open branch — the scan still ran."""
    return f"scanners: nice unavailable ({reason}); running at default priority"


# ── availability ────────────────────────────────────────────────────────────


def check_available(level: int, env: Mapping[str, str] | None = None) -> tuple[bool, str]:
    """``(available, reason)`` for ``nice -n <level>`` here — probed at most once.

    A level of ``0`` is "available" by definition: off means nothing is
    prefixed and no probe is warranted.
    """
    if level <= 0:
        return True, ""
    env_map = os.environ if env is None else env
    key = (level, str(env_map.get("PATH", "")))
    cached = _availability.get(key)
    if cached is not None:
        return cached

    exe = shutil.which("nice", path=env_map.get("PATH"))
    if not exe:
        verdict: tuple[bool, str] = (False, "nice is not on PATH")
    else:
        try:
            probe = subprocess.run(
                [exe, "-n", str(level), "true"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                timeout=_PROBE_TIMEOUT_S,
                env=dict(env_map),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            verdict = (False, f"probe failed: {exc}")
        else:
            if probe.returncode == 0:
                verdict = (True, "")
            else:
                verdict = (False, f"`{exe} -n {level}` exited {probe.returncode}")
    _availability[key] = verdict
    return verdict


# ── the policy object ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class NicePolicy:
    """The resolved policy for one spawn site.

    ``prefix`` is empty when the knob is off (level 0) OR when it is on but
    unusable here — ``applied`` tells those two apart, and ``note`` is empty
    only in the first case (nothing to report when nothing was asked for).
    """

    level: int
    source: str = "default"
    prefix: tuple[str, ...] = ()
    note: str = ""
    available: bool = True
    unavailable_reason: str = ""

    @property
    def applied(self) -> bool:
        """True when a ``nice`` prefix will be put on the spawned argv."""
        return bool(self.prefix)

    @property
    def exe(self) -> str:
        """The prefix as a command-line string (``""`` when not applied)."""
        return " ".join(self.prefix)


def policy_for_level(level: int, env: Mapping[str, str] | None = None) -> NicePolicy:
    """Build the policy for an already-resolved *level* (no config lookup)."""
    if level <= 0:
        return NicePolicy(level=0, source="off")
    available, reason = check_available(level, env)
    if not available:
        return NicePolicy(
            level=level,
            available=False,
            unavailable_reason=reason,
            note=note_unavailable(reason),
        )
    return NicePolicy(level=level, prefix=("nice", "-n", str(level)), note=note_applied(level))


def policy(config: object = None, env: Mapping[str, str] | None = None) -> NicePolicy:
    """Resolve the policy from *config* (+ the ambient env); probes availability."""
    level, source = resolve_level(config, env)
    if level <= 0:
        # Off: no prefix, no probe, no note line. Callers that build a command
        # string from this produce today's bytes exactly.
        return NicePolicy(level=level, source=source)
    return replace(policy_for_level(level, env), source=source)


def argv_prefix(
    level: int, program: str, env: Mapping[str, str] | None = None
) -> tuple[tuple[str, ...], str]:
    """``(prefix, note)`` for a subprocess spawn of *program* at *level*.

    The prefix is withheld when *program* does not resolve on PATH: ``nice``
    execs its argv, so ``["nice", "-n", "10", "missing-tool"]`` exits 127 with
    stderr text instead of raising ``FileNotFoundError`` — the signal every
    caller here uses to report a missing tool as *unavailable* rather than as a
    failed check. A withheld prefix carries no note: the caller is about to
    report the tool as unavailable, and claiming a priority that was never set
    would be a lie.
    """
    if level <= 0:
        return (), ""
    env_map = os.environ if env is None else env
    path = env_map.get("PATH")
    if shutil.which(program, path=path) is None:
        logger.debug("scanner_nice: %s not on PATH — no nice prefix", program)
        return (), ""
    resolved = policy_for_level(level, env)
    return resolved.prefix, resolved.note


# ── shell shapes ────────────────────────────────────────────────────────────


def sh_single_quote(text: str) -> str:
    """Single-quote *text* for ``/bin/sh`` (POSIX-safe, embedded quotes escaped)."""
    return "'" + text.replace("'", "'\\''") + "'"


def shell_prologue(level: int) -> tuple[str, str]:
    """``(prologue, invocation_prefix)`` for GitReins' own shell scripts.

    The probe runs INSIDE the shell (so it sees the PATH the scan will run
    with, including a shim directory that only exists at run time) and the
    fail-open branch clears ``$_gr_nice``, leaving the scan to run unprefixed
    with one honest note line. Returns ``("", "")`` when the knob is off, which
    is what keeps the level-0 command byte-identical to the pre-change one.
    """
    if level <= 0:
        return "", ""
    exe = nice_exe(level)
    reason = f"{exe} did not run"
    prologue = (
        f'_gr_nice="{exe}"; '
        f'if {exe} true >/dev/null 2>&1; then echo "{note_applied(level)}"; '
        f'else _gr_nice=""; echo "{note_unavailable(reason)}"; fi; '
    )
    return prologue, "$_gr_nice "


def shell_wrap(cmd: str, level: int) -> str:
    """Wrap an opaque shell *cmd* so it — and everything it chains — runs at *level*.

    Used for commands GitReins does not own the text of (``guards.test_command``).
    The whole command is handed to one ``sh -c`` AFTER the prefix, so a chain
    (``a && b``) is covered end to end: prefixing only the first word of a chain
    would leave every later command at default priority.
    """
    if level <= 0 or not cmd.strip():
        return cmd
    prologue, prefix = shell_prologue(level)
    return f"{prologue}{prefix}sh -c {sh_single_quote(cmd)}"
