"""Criterion validation — every acceptance criterion carries a failure condition and a proof.

GR-172: an acceptance criterion that only states an outcome ("Has tests") cannot
fail a judge run — there is no condition it must satisfy and no command that
would prove it. The detector below recognises the two labelled sections a
criterion must carry:

- ``FAILIF:`` / ``FAILS WHEN:`` / ``FAILURE CONDITION:`` — the failure condition;
- ``PROOF:`` / ``PROOF COMMAND:`` — a command or a locator (``path``,
  ``path:line``, ``tests/x.py::test_y``) that demonstrates the criterion is met.

Markers are case-insensitive and may follow optional leading prose separated by
``;`` / ``.`` / newline / whitespace. A ``PROOF:`` with an empty payload, a
payload shorter than 3 non-space characters, or a payload that is neither
command-shaped nor locator-shaped counts as a missing proof.

Pure functions, no I/O, no imports from other engine modules: this module is
safe to import from anywhere (``task_manager``, the CLI, and the MCP server all
do).
"""

import re

#: Labelled-section marker fragments for the failure condition. Case-insensitive
#: (the detector compiles these with ``re.IGNORECASE``); whitespace inside a
#: fragment is written ``\\s+`` so the spellings may wrap or double-space.
FAILURE_MARKERS: tuple[str, ...] = (
    r"failif",
    r"fails\s+when",
    r"failure\s+condition",
)

#: Labelled-section marker fragments for the proof. ``PROOF COMMAND:`` is
#: accepted as an alias of ``PROOF:``.
PROOF_MARKERS: tuple[str, ...] = (
    r"proof",
    r"proof\s+command",
)

#: Command tokens a proof payload may START with to count as a runnable command
#: rather than bare prose.
RUNNER_TOKENS: tuple[str, ...] = (
    "pytest",
    "python",
    ".venv/",
    "make",
    "npm",
    "pnpm",
    "go",
    "cargo",
    "bash",
    "sh",
    "gitreins",
    "curl",
)

#: A proof payload must carry at least this many non-space characters.
MIN_PROOF_PAYLOAD_CHARS = 3

_FAILURE_RE = re.compile(
    r"(?:^|[\s;.\n])(?:" + "|".join(FAILURE_MARKERS) + r")\s*:",
    re.IGNORECASE,
)
# The payload is everything after the marker's colon; DOTALL lets a multi-line
# criterion keep its proof payload across the newline.
_PROOF_RE = re.compile(
    r"(?:^|[\s;.\n])(?:" + "|".join(PROOF_MARKERS) + r")\s*:(.*)$",
    re.IGNORECASE | re.DOTALL,
)
# `path:line` and friends: a colon immediately followed by a digit.
_LOCATOR_RE = re.compile(r":\d")


def _proof_payload(criterion: str) -> str | None:
    """Return the text after the first ``PROOF:``/``PROOF COMMAND:`` marker, or None."""
    match = _PROOF_RE.search(criterion)
    return None if match is None else match.group(1)


def _is_command_or_locator(payload: str) -> bool:
    """True when *payload* looks like a command or a locator, not bare prose."""
    if "/" in payload or "::" in payload or _LOCATOR_RE.search(payload):
        return True
    return payload.strip().lower().startswith(RUNNER_TOKENS)


def criterion_gaps(criterion: str) -> list[str]:
    """Return the mandatory field names *criterion* is missing, in canonical order.

    The result is a stable-ordered subset of ``["failure_condition", "proof"]``:
    ``[]`` when both labelled sections are present and the proof payload is a
    command or a locator, the missing field names otherwise. Pure and I/O-free.
    """
    gaps: list[str] = []
    if _FAILURE_RE.search(criterion) is None:
        gaps.append("failure_condition")
    payload = _proof_payload(criterion)
    if (
        payload is None
        or len("".join(payload.split())) < MIN_PROOF_PAYLOAD_CHARS
        or not _is_command_or_locator(payload)
    ):
        gaps.append("proof")
    return gaps
