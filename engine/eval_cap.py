"""
EvalCap — flexible evaluator caps with fractional tool-call weighting.

Caps are set individually. Each cap type is optional — omit to disable.

  Config (v0.3.0+):

    evaluator:
      max_iterations: 100          # -1 = unlimited
      max_time: "30m"              # 30s, 5m, 2h — wall clock
      max_input_tokens: "200k"     # token budget (supports 0.1M, 1.5M, etc.)
      max_output_tokens: "50k"     # output tokens only
      tool_call_weight: 0.1        # how much a tool call costs (default 0.1)

  Backward compat (v0.2.x):

    evaluator:
      cap: "100/30m/200k/50k"     # combined string

  MCP (individual params):

    judge.evaluate(id="task", max_iterations=50, max_time="10m",
                   max_input_tokens="200k", max_output_tokens="50k")

Tool calls cost a fraction of an iteration. Default 0.1 means:
  - LLM reasons (cost 1.0) → at 1.0/100
  - LLM calls read_file (cost 0.1) → at 1.1/100
  - LLM calls search_pattern (cost 0.1) → at 1.2/100
  - LLM reasons again (cost 1.0) → at 2.2/100

TRBL-135 (AC2): a tier-2 run whose every time source is ABSENT gets the
DEFAULT_MAX_SECONDS wall clock below, so a ceiling that still writes a verdict
always exists. An operator who wrote a max_time anywhere (evaluator, defaults,
legacy cap string, GITREINS_MAX_TIME) keeps full authority — including an
explicit unlimited value (`max_time: "0s"` or `-1`).

At 99.9/100, a full LLM call (1.0) is still allowed — the cap
is checked BEFORE the call, not after. This means the final count
may drift slightly above the cap.
"""

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("gitreins.eval_cap")


# ── Cap-trip note for the CLI completion record (OBS-GITREINS-2) ──────────
# A cap trip dies with the process today (spec §2a: the reason string is
# returned to a caller and discarded). This module-level note is the smallest
# in-process seam the CLI completion record can read at dispatch time: the
# trip-producing returns below record their message through :func:`_trip`
# UNCHANGED. The dispatcher resets before a run and reads after, so a
# long-lived process (the MCP server judging in-process) can never attribute
# an older run's trip to a newer one.
_last_cap_trip: str | None = None

# TRBL-135 (AC2): the wall clock a tier-2 run gets when NEITHER the repo
# config nor the environment names a max_time. RELEASE-014's tier-2 had no
# ceiling at all: it ran >34 minutes, was killed, and left no verdict.json and
# no history directory — "judging" was indistinguishable from "dead". 45
# minutes is well above the 10-18m healthy grades measured on chimera-v2
# (engine/closeout_reuse.py) and below the killed run that pathology measured;
# lanes that need more set their own evaluator.max_time. An operator sets ANY
# max_time (including explicit `"0s"` = unlimited) to override it; see
# eval_cap_from_config for the "was a time config ever present" test.
DEFAULT_MAX_SECONDS: float = 45 * 60


def _trip(message: str) -> str:
    """Record a cap trip for the completion record; return the message unchanged."""
    global _last_cap_trip
    _last_cap_trip = message
    return message


def reset_cap_trip() -> None:
    """Forget any earlier run's trip — the CLI dispatcher calls this per run."""
    global _last_cap_trip
    _last_cap_trip = None


def last_cap_trip() -> str | None:
    """The most recent cap-trip message in this process, if any."""
    return _last_cap_trip


@dataclass
class EvalCap:
    """Caps that limit how much the evaluator can consume.

    All caps default to -1 (unlimited). Set individually — only
    the caps you configure are enforced.

    Tool calls are discounted: by default each tool call costs
    only 0.1 iterations. The LLM's reasoning turn costs 1.0.
    """

    max_iterations: float = -1.0  # -1 = unlimited, supports fractional
    max_seconds: float = -1.0  # -1 = unlimited
    max_input_tokens: int = -1  # -1 = unlimited
    max_output_tokens: int = -1  # -1 = unlimited
    tool_call_weight: float = 0.1  # fraction of an iteration per tool call

    # Runtime tracking
    iteration_credit: float = 0.0
    start_time: float = 0.0
    cumulative_input_tokens: int = 0  # total input (regular + cache)
    cumulative_output_tokens: int = 0  # output tokens
    cumulative_cache_read: int = 0  # tokens served from cache
    cumulative_cache_write: int = 0  # tokens written to cache

    # Display
    source: str = ""

    # TRBL-135 (AC2): the budget that produced the most recent trip —
    # "time" | "iterations" | "input_tokens" | "output_tokens", "" while no
    # cap has tripped. The evaluator turns "time" into the first-class
    # verdict outcome "timeout"; "" or any other value leaves the outcome
    # unset, so a non-timeout cap verdict keeps the historical shape.
    trip_kind: str = ""

    # ── API ─────────────────────────────────────────────────

    @property
    def is_unlimited(self) -> bool:
        return (
            self.max_iterations == -1.0
            and self.max_seconds == -1.0
            and self.max_input_tokens == -1
            and self.max_output_tokens == -1
        )

    def start(self) -> None:
        """Begin the wall-clock timer."""
        self.start_time = time.time()

    def reset_context_tracking(self) -> None:
        """Reset input/output token counters for a fresh context window.

        Called after compaction — the conversation is rebuilt from scratch
        so accumulated token counts from the old conversation are irrelevant.
        Iteration and time tracking are NOT reset (they span the full evaluation).
        """
        self.cumulative_input_tokens = 0
        self.cumulative_output_tokens = 0
        self.cumulative_cache_read = 0
        self.cumulative_cache_write = 0

    def record_llm_call(
        self,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> str | None:
        """Record a full LLM reasoning call (costs 1.0 iterations).

        The cap is checked BEFORE the call — so at 99.9/100, a 1.0
        call bringing you to 100.9 is allowed.
        """
        # Check iterations BEFORE adding (allows one final full call)
        if self.max_iterations > 0 and self.iteration_credit >= self.max_iterations:
            self.trip_kind = "iterations"
            return _trip(
                f"Iteration cap ({_fmt_num(self.max_iterations)}) reached "
                f"({_fmt_num(self.iteration_credit)} used). "
                "Increase max_iterations or split criteria."
            )

        self.iteration_credit += 1.0
        all_input = prompt_tokens + cache_read_tokens + cache_write_tokens
        self.cumulative_input_tokens += all_input
        self.cumulative_output_tokens += completion_tokens
        self.cumulative_cache_read += cache_read_tokens
        self.cumulative_cache_write += cache_write_tokens

        # Check time and token caps (these are hard limits)
        return self._check_hard_caps()

    def record_tool_call(self) -> str | None:
        """Record a tool execution (costs tool_call_weight iterations by default).

        Same lenient check as record_llm_call — allows going slightly over.
        """
        if self.max_iterations > 0 and self.iteration_credit >= self.max_iterations:
            self.trip_kind = "iterations"
            return _trip(
                f"Iteration cap ({_fmt_num(self.max_iterations)}) reached "
                f"({_fmt_num(self.iteration_credit)} used). "
                "Increase max_iterations or split criteria."
            )

        self.iteration_credit += self.tool_call_weight

        # Also check time/token caps (tool calls consume real time)
        return self._check_hard_caps()

    def remaining_seconds(self) -> float:
        """Return remaining time budget in seconds.

        States:
          - max_seconds <= 0          → -1 (unlimited, time is not tracked)
          - start_time == 0           → max_seconds (timer not started yet)
          - mid-evaluation            → max_seconds - (time.time() - start_time)
          - over budget               → negative float
        """
        if self.max_seconds <= 0:
            return -1.0
        if self.start_time == 0:
            return float(self.max_seconds)
        return self.max_seconds - (time.time() - self.start_time)

    def check(self) -> str | None:
        """Check all caps. Hard stop — used before starting a new LLM call."""
        return self._check_hard_caps()

    def _check_hard_caps(self) -> str | None:
        """Check time and token caps. These are hard limits regardless of leniency."""
        if self.max_seconds > 0 and self.start_time > 0:
            elapsed = time.time() - self.start_time
            if elapsed >= self.max_seconds:
                self.trip_kind = "time"
                return _trip(
                    f"Time cap ({_fmt_seconds(self.max_seconds)}) exceeded "
                    f"({_fmt_seconds(elapsed)} elapsed). "
                    "Increase max_time or simplify criteria."
                )

        if self.max_input_tokens > 0 and self.cumulative_input_tokens >= self.max_input_tokens:
            self.trip_kind = "input_tokens"
            return _trip(
                f"Input token budget ({_fmt_tokens(self.max_input_tokens)}) exceeded "
                f"({_fmt_tokens(self.cumulative_input_tokens)} used). "
                "Increase max_input_tokens or reduce message context."
            )

        if self.max_output_tokens > 0 and self.cumulative_output_tokens >= self.max_output_tokens:
            self.trip_kind = "output_tokens"
            return _trip(
                f"Output token budget ({_fmt_tokens(self.max_output_tokens)}) exceeded "
                f"({_fmt_tokens(self.cumulative_output_tokens)} used). "
                "Increase max_output_tokens or simplify criteria."
            )

        return None

    def summary(self) -> str:
        """Human-readable summary of caps and current usage."""
        parts = []
        if self.max_iterations > 0:
            parts.append(
                f"iterations: {_fmt_num(self.iteration_credit)}/{_fmt_num(self.max_iterations)}"
            )
        elif self.max_iterations == -1:
            parts.append("iterations: unlimited")
        if self.max_seconds > 0:
            elapsed = int(time.time() - self.start_time) if self.start_time > 0 else 0
            parts.append(f"time: {_fmt_seconds(elapsed)}/{_fmt_seconds(self.max_seconds)}")
        if self.max_input_tokens > 0 or self.max_output_tokens > 0:
            in_str = f"in: {_fmt_tokens(self.cumulative_input_tokens)}"
            if self.max_input_tokens > 0:
                in_str += f"/{_fmt_tokens(self.max_input_tokens)}"
            if self.cumulative_cache_read > 0 or self.cumulative_cache_write > 0:
                cache_parts = []
                if self.cumulative_cache_read > 0:
                    cache_parts.append(f"cache-hit {_fmt_tokens(self.cumulative_cache_read)}")
                if self.cumulative_cache_write > 0:
                    cache_parts.append(f"cache-write {_fmt_tokens(self.cumulative_cache_write)}")
                in_str += f" ({', '.join(cache_parts)})"
            parts.append(in_str)
            parts.append(
                f"out: {_fmt_tokens(self.cumulative_output_tokens)}"
                + (f"/{_fmt_tokens(self.max_output_tokens)}" if self.max_output_tokens > 0 else "")
            )
        if not parts:
            return "no caps (unlimited)"
        return ", ".join(parts)

    # Backward compat — used by AgenticEvaluator when it needs an integer
    @property
    def max_iterations_int(self) -> int:
        if self.max_iterations <= 0:
            return 10_000  # safety maximum for unlimited
        return int(self.max_iterations)


# ── Parsing helpers ────────────────────────────────────────

_TIME_RE = re.compile(
    r"^(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours)$"
)  # noqa: E501
_TOKEN_RE = re.compile(r"^(\d+\.?\d*)(k|m|K|M)?$")
_SLASH_TOKEN_RE = re.compile(r"^(\d+\.?\d*)(k|m|K|M)?\s*/\s*(\d+\.?\d*)(k|m|K|M)?$")


def _parse_time(raw: str) -> int | None:
    if raw.strip() in ("M", "H"):
        return None
    m = _TIME_RE.match(raw.strip())
    if not m:
        return None
    value = int(m.group(1))
    unit = m.group(2).lower()
    if unit in ("s", "sec", "secs", "second", "seconds"):
        return value
    elif unit in ("m", "min", "mins", "minute", "minutes"):
        return value * 60
    elif unit in ("h", "hr", "hrs", "hour", "hours"):
        return value * 3600
    return None


def _parse_tokens(raw: str) -> int | None:
    m = _TOKEN_RE.match(raw.strip())
    if not m:
        return None
    value = float(m.group(1))
    suffix = (m.group(2) or "").lower()
    if suffix == "k":
        value *= 1_000
    elif suffix == "m":
        value *= 1_000_000
    return int(value)


def _parse_slash_tokens(raw: str) -> tuple[int | None, int | None]:
    m = _SLASH_TOKEN_RE.match(raw.strip())
    if not m:
        return None, None
    left = m.group(1) + (m.group(2) or "")
    right = m.group(3) + (m.group(4) or "")
    if _parse_time(left) is not None or _parse_time(right) is not None:
        return None, None
    input_val = float(m.group(1))
    input_suffix = (m.group(2) or "").lower()
    if input_suffix == "k":
        input_val *= 1_000
    elif input_suffix == "m":
        input_val *= 1_000_000
    output_val = float(m.group(3))
    output_suffix = (m.group(4) or "").lower()
    if output_suffix == "k":
        output_val *= 1_000
    elif output_suffix == "m":
        output_val *= 1_000_000
    return int(input_val), int(output_val)


def _fmt_seconds(secs: float) -> str:
    s = int(secs)
    if s < 60:
        return f"{s}s"
    elif s < 3600:
        return f"{s // 60}m{s % 60}s" if s % 60 else f"{s // 60}m"
    else:
        h = s // 3600
        m = (s % 3600) // 60
        return f"{h}h{m}m" if m else f"{h}h"


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    elif n >= 1_000:
        return f"{n // 1_000}k"
    return str(n)


def _fmt_num(n: float) -> str:
    """Format a float, dropping trailing .0 for whole numbers."""
    if n == int(n):
        return str(int(n))
    return f"{n:.1f}"


# ── Public constructors ─────────────────────────────────────


def parse_eval_cap(raw: str) -> EvalCap:
    """Parse a legacy combined cap string like '100/30m/200k/50k'.

    For new code, prefer eval_cap_from_config() with individual keys.
    """
    raw = raw.strip()
    if not raw:
        return EvalCap(max_iterations=100, source="(default)")

    cap = EvalCap(source=raw)

    if raw in ("-1", "0", "unlimited", "none"):
        cap.max_iterations = -1.0
        return cap

    if "/" in raw:
        in_tok, out_tok = _parse_slash_tokens(raw)
        if in_tok is not None and out_tok is not None:
            cap.max_input_tokens = in_tok
            cap.max_output_tokens = out_tok
            return cap

        parts = [p.strip() for p in raw.split("/")]
        parsed_iter: float | None = None
        parsed_time: int | None = None
        parsed_in: int | None = None
        parsed_out: int | None = None
        token_parts: list[int] = []

        for i, part in enumerate(parts):
            if not part:
                continue
            t = _parse_time(part)
            if t is not None:
                if parsed_time is None:
                    parsed_time = t
                continue
            if i == 0 and part.lstrip("-").isdigit():
                val = int(part)
                parsed_iter = -1.0 if val <= 0 else float(val)
                continue
            if i == 0 and part.lower() in ("unlimited", "none"):
                parsed_iter = -1.0
                continue
            tok = _parse_tokens(part)
            if tok is not None:
                token_parts.append(tok)
                continue

        if len(token_parts) >= 2:
            parsed_in = token_parts[0]
            parsed_out = token_parts[1]
        elif len(token_parts) == 1:
            parsed_out = token_parts[0]

        if parsed_iter is not None:
            cap.max_iterations = parsed_iter
        if parsed_time is not None:
            cap.max_seconds = float(parsed_time)
        if parsed_in is not None:
            cap.max_input_tokens = parsed_in
        if parsed_out is not None:
            cap.max_output_tokens = parsed_out
        return cap

    # No slash — single value
    if raw.lstrip("-").isdigit():
        val = int(raw)
        cap.max_iterations = -1.0 if val <= 0 else float(val)
        return cap

    t = _parse_time(raw)
    if t is not None:
        cap.max_seconds = float(t)
        return cap

    tok = _parse_tokens(raw)
    if tok is not None:
        cap.max_output_tokens = tok
        return cap

    logger.warning("Unrecognized eval cap string '%s' — using default 100 iterations", raw)
    cap.max_iterations = 100.0
    cap.source = "(default — unrecognized input)"
    return cap


def eval_cap_from_config(config: dict[str, Any]) -> EvalCap:
    """Build an EvalCap from .gitreins/config.yaml.

    Defaults come from engine.config.GitReinsDefaults (single source of truth),
    overridden by config.yaml's evaluator: and defaults: sections.

    Supports new individual keys (v0.3.0+) and legacy combined string (v0.2.x).

    New format (individual):
        evaluator:
          max_iterations: 100
          max_time: "30m"
          max_input_tokens: "200k"
          max_output_tokens: "50k"
          tool_call_weight: 0.1

    Legacy format (combined):
        evaluator:
          cap: "100/30m/200k/50k"

    Individual keys take priority over the combined string.
    Config defaults: section is the global fallback.
    """
    from engine.config import GitReinsDefaults

    # Start with global defaults, overlaid by config's defaults: section
    gd = GitReinsDefaults().overlay(config)
    cap = EvalCap(
        max_iterations=gd.max_iterations,
        max_seconds=gd.max_seconds,
        max_input_tokens=gd.max_input_tokens,
        max_output_tokens=gd.max_output_tokens,
        tool_call_weight=gd.tool_call_weight,
        source=gd._source,
    )

    ev = config.get("evaluator", {}) or {}

    # Try legacy combined string first
    cap_str = ev.get("cap", "") or config.get("guards", {}).get("eval_cap", "")
    if cap_str:
        cap = parse_eval_cap(str(cap_str))

    # Override with individual keys (v0.3.0+)
    if "max_iterations" in ev:
        val = ev["max_iterations"]
        if isinstance(val, (int, float)):
            cap.max_iterations = -1.0 if val <= 0 else float(val)
        elif isinstance(val, str):
            parsed = parse_eval_cap(val)
            cap.max_iterations = parsed.max_iterations

    if "max_time" in ev:
        # TRBL-135: an explicit NON-POSITIVE numeric max_time means "unlimited"
        # — recorded as -1 so the default wall clock below cannot override an
        # operator who turned it off. A parseable duration, or a positive
        # plain number (`max_time: 90` seconds), is honoured as the value.
        raw_max_time = ev["max_time"]
        t = _parse_time(str(raw_max_time))
        if t is not None:
            cap.max_seconds = float(t)
        elif raw_max_time is not None and str(raw_max_time).lstrip("+-").isdigit():
            # TRBL-135: a plain number with no unit — <= 0 means "unlimited",
            # > 0 means seconds. Values that parse neither way stay ignored
            # (the historical behaviour) and fall through to the default
            # wall clock below.
            val = int(str(raw_max_time))
            cap.max_seconds = -1.0 if val <= 0 else float(val)

    if "max_input_tokens" in ev:
        tok = _parse_tokens(str(ev["max_input_tokens"]))
        if tok is not None:
            cap.max_input_tokens = tok

    if "max_output_tokens" in ev:
        tok = _parse_tokens(str(ev["max_output_tokens"]))
        if tok is not None:
            cap.max_output_tokens = tok

    if "tool_call_weight" in ev:
        cap.tool_call_weight = float(ev["tool_call_weight"])

    # ── Env var overrides (v0.10.2) — highest priority, always win ──
    # For CI/docker/one-off runs where editing config.yaml is impractical.
    if os.getenv("GITREINS_MAX_ITERATIONS"):
        val = float(os.environ["GITREINS_MAX_ITERATIONS"])
        cap.max_iterations = val if val > 0 else -1.0
        cap.source = f"{cap.source} + GITREINS_MAX_ITERATIONS={int(val)}"
    if os.getenv("GITREINS_MAX_TIME"):
        t = _parse_time(os.environ["GITREINS_MAX_TIME"])
        if t is not None:
            cap.max_seconds = float(t)
            cap.source = f"{cap.source} + GITREINS_MAX_TIME"
    if os.getenv("GITREINS_MAX_INPUT_TOKENS"):
        tok = _parse_tokens(os.environ["GITREINS_MAX_INPUT_TOKENS"])
        if tok is not None:
            cap.max_input_tokens = tok
            cap.source = f"{cap.source} + GITREINS_MAX_INPUT_TOKENS"
    if os.getenv("GITREINS_MAX_OUTPUT_TOKENS"):
        tok = _parse_tokens(os.environ["GITREINS_MAX_OUTPUT_TOKENS"])
        if tok is not None:
            cap.max_output_tokens = tok
            cap.source = f"{cap.source} + GITREINS_MAX_OUTPUT_TOKENS"

    # ── TRBL-135 (AC2): the default wall clock ────────────────────────────
    # A cap whose max_seconds is still -1 has NO wall clock: RELEASE-014's
    # tier-2 ran >34 minutes, was killed, and persisted nothing — a tick could
    # not tell "judging" from "dead". Apply DEFAULT_MAX_SECONDS ONLY when no
    # time source was EVER present, so explicit configuration (including an
    # explicit unlimited value) keeps full authority. The sources are read by
    # PRESENCE, not by the resolved value, because an unparseable -1 key is
    # currently dropped rather than resolved below.
    defaults_section = config.get("defaults", {}) or {}
    if not isinstance(defaults_section, dict):
        defaults_section = {}
    time_configured = (
        "max_time" in ev
        or "max_time" in defaults_section
        or os.getenv("GITREINS_MAX_TIME") is not None
        # A legacy combined cap string whose time component parsed leaves
        # max_seconds > 0 already, so the cap.max_seconds test below is
        # the legacy-string guard as well.
    )
    if not time_configured and cap.max_seconds <= 0:
        cap.max_seconds = DEFAULT_MAX_SECONDS
        cap.source = f"{cap.source} + default wall clock ({_fmt_seconds(DEFAULT_MAX_SECONDS)})"

    return cap
