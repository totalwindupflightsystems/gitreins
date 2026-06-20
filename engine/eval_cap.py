"""
EvalCap — flexible evaluator caps: iteration count, wall-clock time, token budgets.

Supports multiple cap formats that can be combined:

  Numeric (iteration cap):
    "100"   → max 100 iterations
    "-1"    → unlimited
    "0"     → unlimited

  Time-based:
    "30s"   → 30 seconds
    "5m"    → 5 minutes
    "2h"    → 2 hours

  Token budget:
    "200k"      → 200,000 total output tokens
    "100k/50k"  → 100k input + 50k output token budget
    "50000"     → 50,000 output tokens (raw numbers)

  Combined (slash-separated):
    "100/30m"           → 100 iterations, 30 minute cap
    "200k/50k"          → 200k input / 50k output tokens
    "100/30m/200k/50k"  → iteration + time + input tokens + output tokens
    "-1/30m"            → unlimited iterations, 30 minute cap
"""

import logging
import re
import time
from dataclasses import dataclass, field

logger = logging.getLogger("gitreins.eval_cap")


@dataclass
class EvalCap:
    """Parsed evaluation cap configuration."""

    max_iterations: int = -1          # -1 = unlimited
    max_seconds: int = -1             # -1 = unlimited
    max_input_tokens: int = -1        # -1 = unlimited
    max_output_tokens: int = -1       # -1 = unlimited

    # Runtime tracking (populated during evaluation)
    iterations: int = 0
    start_time: float = 0.0
    cumulative_input_tokens: int = 0
    cumulative_output_tokens: int = 0

    # Original parse string for display
    source: str = ""

    # ── Limit checking ────────────────────────────────────────

    @property
    def is_unlimited(self) -> bool:
        """True when ALL caps are disabled."""
        return (
            self.max_iterations == -1
            and self.max_seconds == -1
            and self.max_input_tokens == -1
            and self.max_output_tokens == -1
        )

    def check(self) -> str | None:
        """Check all caps. Returns None if within limits, or an error string if exceeded."""
        # Iteration cap
        if self.max_iterations > 0 and self.iterations >= self.max_iterations:
            return (
                f"Iteration cap ({self.max_iterations}) reached. "
                "Raise max_iterations or split criteria into focused single-criterion tasks."
            )

        # Time cap
        if self.max_seconds > 0 and self.start_time > 0:
            elapsed = int(time.time() - self.start_time)
            if elapsed >= self.max_seconds:
                return (
                    f"Time cap ({_format_seconds(self.max_seconds)}) exceeded "
                    f"({_format_seconds(elapsed)} elapsed). "
                    "Increase time cap or simplify criteria."
                )

        # Input token cap
        if self.max_input_tokens > 0 and self.cumulative_input_tokens >= self.max_input_tokens:
            return (
                f"Input token budget ({_format_tokens(self.max_input_tokens)}) exceeded "
                f"({_format_tokens(self.cumulative_input_tokens)} used). "
                "Increase token budget or reduce message context."
            )

        # Output token cap
        if self.max_output_tokens > 0 and self.cumulative_output_tokens >= self.max_output_tokens:
            return (
                f"Output token budget ({_format_tokens(self.max_output_tokens)}) exceeded "
                f"({_format_tokens(self.cumulative_output_tokens)} used). "
                "Increase token budget or simplify criteria."
            )

        return None

    def record_iteration(self, prompt_tokens: int = 0, completion_tokens: int = 0) -> str | None:
        """Record one iteration's usage. Returns error string if cap exceeded."""
        self.iterations += 1
        self.cumulative_input_tokens += prompt_tokens
        self.cumulative_output_tokens += completion_tokens
        return self.check()

    def start(self) -> None:
        """Start the wall-clock timer."""
        self.start_time = time.time()

    def summary(self) -> str:
        """Human-readable summary of caps and current state."""
        parts = []
        if self.max_iterations > 0:
            parts.append(f"iterations: {self.iterations}/{self.max_iterations}")
        elif self.max_iterations == -1:
            parts.append("iterations: unlimited")
        if self.max_seconds > 0:
            elapsed = int(time.time() - self.start_time) if self.start_time > 0 else 0
            parts.append(f"time: {_format_seconds(elapsed)}/{_format_seconds(self.max_seconds)}")
        if self.max_input_tokens > 0:
            parts.append(f"input tokens: {_format_tokens(self.cumulative_input_tokens)}/{_format_tokens(self.max_input_tokens)}")
        if self.max_output_tokens > 0:
            parts.append(f"output tokens: {_format_tokens(self.cumulative_output_tokens)}/{_format_tokens(self.max_output_tokens)}")
        if not parts:
            return "no caps (unlimited)"
        return ", ".join(parts)


# ── Parsing ────────────────────────────────────────────────

_TIME_RE = re.compile(r"^(\d+)\s*(s|sec|secs|second|seconds|m|min|mins|minute|minutes|h|hr|hrs|hour|hours)$")
_TOKEN_RE = re.compile(r"^(\d+)(k|m|K|M)?$")
_SLASH_TOKEN_RE = re.compile(r"^(\d+)(k|m|K|M)?\s*/\s*(\d+)(k|m|K|M)?$")


def _parse_time(raw: str) -> int | None:
    """Parse a time string to seconds. Returns None if not a time string.

    Case-insensitive, but avoids single uppercase M (which is 1 million tokens, not 1 minute).
    """
    # Single uppercase M or H with no digit prefix → not time
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
    """Parse a token count string like '200k' or '50000'. Returns None if not a token string."""
    m = _TOKEN_RE.match(raw.strip())
    if not m:
        return None
    value = int(m.group(1))
    suffix = (m.group(2) or "").lower()
    if suffix == "k":
        value *= 1_000
    elif suffix == "m":
        value *= 1_000_000
    return value


def _parse_slash_tokens(raw: str) -> tuple[int | None, int | None]:
    """Parse '200k/50k' → (200000, 50000). Returns (None, None) if not a slash-token string.

    Rejects matches where either part looks like a time string (e.g. '30m').
    """
    m = _SLASH_TOKEN_RE.match(raw.strip())
    if not m:
        return None, None

    # Reject if either part could be a time string
    left = m.group(1) + (m.group(2) or "")
    right = m.group(3) + (m.group(4) or "")
    if _parse_time(left) is not None or _parse_time(right) is not None:
        return None, None

    input_val = int(m.group(1))
    input_suffix = (m.group(2) or "").lower()
    if input_suffix == "k":
        input_val *= 1_000
    elif input_suffix == "m":
        input_val *= 1_000_000

    output_val = int(m.group(3))
    output_suffix = (m.group(4) or "").lower()
    if output_suffix == "k":
        output_val *= 1_000
    elif output_suffix == "m":
        output_val *= 1_000_000

    return input_val, output_val


def _format_seconds(secs: int) -> str:
    """Format seconds to human-readable string."""
    if secs < 60:
        return f"{secs}s"
    elif secs < 3600:
        return f"{secs // 60}m{secs % 60}s" if secs % 60 else f"{secs // 60}m"
    else:
        h = secs // 3600
        m = (secs % 3600) // 60
        return f"{h}h{m}m" if m else f"{h}h"


def _format_tokens(n: int) -> str:
    """Format token count to human-readable string."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    elif n >= 1_000:
        return f"{n // 1_000}k"
    return str(n)


def parse_eval_cap(raw: str) -> EvalCap:
    """Parse a cap string into an EvalCap.

    Supported formats:
      "100"            → 100 iterations
      "-1" / "0"       → unlimited
      "30s" / "5m" / "2h" → time-based
      "200k"           → 200k output tokens
      "100k/50k"       → 100k input / 50k output tokens
      "100/30m"        → iterations + time
      "100/30m/200k/50k" → all four caps

    Slash-separated components are parsed positionally:
      Position 1 → numeric → max_iterations
                  → time → max_seconds
                  → slash-token (e.g. 100k/50k) → max_input_tokens + max_output_tokens
                  → token (e.g. 200k) → max_output_tokens
      Position 2 → time → max_seconds
                  → token → max_output_tokens
      Position 3 → token → max_input_tokens
      Position 4 → token → max_output_tokens
    """
    raw = raw.strip()
    if not raw:
        return EvalCap(max_iterations=100, source="(default)")

    cap = EvalCap(source=raw)

    # Single-value shortcuts
    if raw in ("-1", "0", "unlimited", "none"):
        cap.max_iterations = -1
        return cap

    if "/" in raw:
        # Try slash-token first (100k/50k) — only when the format is EXACTLY num/num
        in_tok, out_tok = _parse_slash_tokens(raw)
        if in_tok is not None and out_tok is not None:
            cap.max_input_tokens = in_tok
            cap.max_output_tokens = out_tok
            return cap

        # Multi-component: split by /
        parts = [p.strip() for p in raw.split("/")]

        # Phase 1: classify each part
        # Position 1 (parts[0]): iter | time
        # Position 2 (parts[1]): time | token
        # Remaining positions: tokens (first → input, second → output)
        parsed_iter: int | None = None
        parsed_time: int | None = None
        parsed_in: int | None = None
        parsed_out: int | None = None
        token_parts: list[int] = []  # collected token values in order

        for i, part in enumerate(parts):
            if not part:
                continue

            # Try time first — avoids "30m" being parsed as 30 million tokens
            t = _parse_time(part)
            if t is not None:
                if parsed_time is None:
                    parsed_time = t
                continue

            # Numeric (iteration cap) — only position 0
            if i == 0 and part.lstrip("-").isdigit():
                val = int(part)
                parsed_iter = -1 if val <= 0 else val
                continue

            # "unlimited" / "none" keywords — only position 0
            if i == 0 and part.lower() in ("unlimited", "none"):
                parsed_iter = -1
                continue

            # Token
            tok = _parse_tokens(part)
            if tok is not None:
                token_parts.append(tok)
                continue

        # Assign token parts: first → input, second → output
        if len(token_parts) >= 2:
            parsed_in = token_parts[0]
            parsed_out = token_parts[1]
        elif len(token_parts) == 1:
            parsed_out = token_parts[0]

        if parsed_iter is not None:
            cap.max_iterations = parsed_iter
        if parsed_time is not None:
            cap.max_seconds = parsed_time
        if parsed_in is not None:
            cap.max_input_tokens = parsed_in
        if parsed_out is not None:
            cap.max_output_tokens = parsed_out

        return cap

    # No slash — single value
    # Numeric
    if raw.lstrip("-").isdigit():
        val = int(raw)
        if val <= 0:
            cap.max_iterations = -1
        else:
            cap.max_iterations = val
        return cap

    # Time
    t = _parse_time(raw)
    if t is not None:
        cap.max_seconds = t
        return cap

    # Token
    tok = _parse_tokens(raw)
    if tok is not None:
        cap.max_output_tokens = tok
        return cap

    # Unknown — fall back to default
    logger.warning("Unrecognized eval cap string '%s' — using default 100 iterations", raw)
    cap.max_iterations = 100
    cap.source = "(default — unrecognized input)"
    return cap


def eval_cap_from_config(config: dict) -> EvalCap:
    """Extract eval cap from .gitreins/config.yaml.

    Reads evaluator.cap (string) from the config dict. Falls back to default 100 iterations.
    """
    cap_str = (
        config.get("evaluator", {}).get("cap", "")
        or config.get("guards", {}).get("eval_cap", "")
    )
    if cap_str:
        return parse_eval_cap(str(cap_str))
    # Default: 100 iterations, no other caps
    return EvalCap(max_iterations=100, source="(config default)")
