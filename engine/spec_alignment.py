"""Optional Auger-backed specification-alignment guard (GR-154).

The guard is deliberately an adapter: Auger owns spec parsing and alignment
judgement. GitReins only supplies the staged patch and roots, validates the
compatibility handshake, and maps the checker result into the configured warn
or block policy.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from engine import command_hygiene


_REDACT_PATTERNS = (
    re.compile(
        r"(?i)(\b(?:api[_-]?key|token|secret|password|authorization|bearer)\b\s*[:=]\s*)\S+"
    ),
    re.compile(r"\b(?:sk|ghp|glpat)-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
)


@dataclass(frozen=True)
class SpecAlignmentResult:
    """Safe, bounded result of one Auger checker invocation."""

    passed: bool
    status: str
    output: str = ""
    error: str = ""
    warning: str = ""
    exit_code: int | None = None
    timed_out: bool = False
    missing_checker: bool = False


def redact_output(text: str) -> str:
    """Remove credential-shaped values before console or evidence persistence."""
    redacted = text or ""
    for pattern in _REDACT_PATTERNS:
        redacted = pattern.sub(
            lambda match: f"{match.group(1)}[REDACTED]" if match.lastindex else "[REDACTED]",
            redacted,
        )
    return redacted


def staged_diff(workdir: str) -> str:
    """Read only the index diff; never inspect or modify the working tree."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--binary", "--no-ext-diff", "--no-renames", "--"],
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
        env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    )
    if result.returncode != 0:
        raise RuntimeError("could not read staged diff")
    return result.stdout


def run_checker(
    workdir: str,
    *,
    command: str = "",
    endpoint: str = "",
    spec_root: str = "specs",
    mode: str = "warn",
    missing_checker: str = "warn",
    timeout: float = 30.0,
    protocol_version: str = "1",
    min_checker_version: str = "1.0.0",
) -> SpecAlignmentResult:
    """Invoke Auger with staged content and map its compatibility contract.

    A command receives ``--repo-root``, ``--spec-root``,
    ``--protocol-version`` and ``--diff-stdin`` and reads the exact staged
    patch from stdin. It must emit one JSON object containing ``protocol``,
    ``version`` and ``status`` (``pass``, ``warn`` or ``fail``; ``block`` is
    accepted as a backwards-compatible alias for ``fail``). An endpoint
    receives the same fields plus ``diff`` in a JSON POST body. No shell is
    used for command invocation.
    """
    mode = mode if mode in {"warn", "block"} else "warn"
    missing_checker = missing_checker if missing_checker in {"warn", "block"} else "warn"
    try:
        diff = staged_diff(workdir)
    except (OSError, RuntimeError) as exc:
        return _unavailable_result(missing_checker, "error", f"spec alignment: {exc}")

    resolved_spec_root = os.path.realpath(
        spec_root if os.path.isabs(spec_root) else os.path.join(workdir, spec_root)
    )
    if endpoint:
        return _run_endpoint(
            endpoint,
            workdir=workdir,
            spec_root=resolved_spec_root,
            diff=diff,
            mode=mode,
            unavailable_policy=missing_checker,
            timeout=timeout,
            protocol_version=protocol_version,
            min_checker_version=min_checker_version,
        )

    argv = _command_argv(command)
    if not argv or not _checker_available(argv[0]):
        return _missing_result(missing_checker, command or endpoint)
    argv.extend(
        [
            "--repo-root",
            os.path.realpath(workdir),
            "--spec-root",
            resolved_spec_root,
            "--protocol-version",
            protocol_version,
            "--diff-stdin",
        ]
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    result = command_hygiene.run_bounded(
        argv,
        cwd=workdir,
        timeout=timeout,
        env=env,
        input_text=diff,
    )
    if result.get("refused"):
        return _unavailable_result(missing_checker, "error", redact_output(str(result["reason"])))
    if result.get("error") and not result.get("timed_out"):
        return _unavailable_result(missing_checker, "error", redact_output(str(result["error"])))
    output = redact_output(str(result.get("output", "")))
    if result.get("timed_out"):
        return _unavailable_result(
            missing_checker,
            "timeout",
            f"spec alignment: checker timed out after {timeout:g}s",
            output=output,
            exit_code=result.get("exit_code"),
            timed_out=True,
        )
    payload = _parse_payload(result.get("output", ""))
    handshake_error = _validate_handshake(payload, protocol_version, min_checker_version)
    if handshake_error:
        return _unavailable_result(
            missing_checker,
            "error",
            f"spec alignment: {handshake_error}",
            output=output,
            exit_code=result.get("exit_code"),
        )
    status = _payload_status(payload)
    if status is None:
        return _unavailable_result(
            missing_checker,
            "error",
            "spec alignment: checker returned an invalid status",
            output=output,
            exit_code=result.get("exit_code"),
        )
    return _policy_result(
        mode,
        status,
        output or f"spec alignment: {status}",
        output=output,
        exit_code=result.get("exit_code"),
    )


def _run_endpoint(
    endpoint: str,
    *,
    workdir: str,
    spec_root: str,
    diff: str,
    mode: str,
    unavailable_policy: str,
    timeout: float,
    protocol_version: str,
    min_checker_version: str,
) -> SpecAlignmentResult:
    body = json.dumps(
        {
            "protocol": protocol_version,
            "version": protocol_version,
            "repo_root": os.path.realpath(workdir),
            "spec_root": spec_root,
            "diff": diff,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(command_hygiene.MAX_EVIDENCE_CHARS).decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        failure_status = "timeout" if isinstance(exc, TimeoutError) else "error"
        return _unavailable_result(
            unavailable_policy,
            failure_status,
            f"spec alignment: endpoint unavailable ({type(exc).__name__})",
            timed_out=failure_status == "timeout",
        )
    payload = _parse_payload(raw)
    handshake_error = _validate_handshake(payload, protocol_version, min_checker_version)
    safe = redact_output(raw)
    if handshake_error:
        return _unavailable_result(
            unavailable_policy,
            "error",
            f"spec alignment: {handshake_error}",
            output=safe,
        )
    status = _payload_status(payload)
    if status is None:
        return _unavailable_result(
            unavailable_policy,
            "error",
            "spec alignment: checker returned an invalid status",
            output=safe,
        )
    return _policy_result(mode, status, safe or f"spec alignment: {status}")


def _command_argv(command: str) -> list[str]:
    try:
        return shlex.split(command)
    except ValueError:
        return []


def _checker_available(program: str) -> bool:
    return (
        os.path.isfile(program) and os.access(program, os.X_OK)
        if os.path.sep in program
        else shutil.which(program) is not None
    )


def _parse_payload(output: str) -> dict[str, Any]:
    for line in reversed((output or "").splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    return {}


def _validate_handshake(payload: dict[str, Any], protocol_version: str, minimum: str) -> str:
    if str(payload.get("protocol", "")) != protocol_version:
        return f"incompatible checker protocol (expected {protocol_version})"
    version = str(payload.get("version", ""))
    if _version_tuple(version) < _version_tuple(minimum):
        return f"checker version {version or 'missing'} is below minimum {minimum}"
    return ""


def _version_tuple(value: str) -> tuple[int, ...]:
    match = re.match(r"^(\d+(?:\.\d+)*)", value)
    return tuple(int(part) for part in match.group(1).split(".")) if match else (0,)


def _payload_status(payload: dict[str, Any]) -> str | None:
    status = str(payload.get("status", "")).lower()
    if status in {"pass", "warn"}:
        return status
    if status in {"fail", "block"}:
        return "fail"
    return None


def _missing_result(policy: str, command: str) -> SpecAlignmentResult:
    detail = "spec alignment: checker missing"
    if command:
        detail += f" ({redact_output(command.split()[0])})"
    return _unavailable_result(policy, "missing", detail, missing_checker=True)


def _unavailable_result(
    policy: str,
    status: str,
    detail: str,
    *,
    output: str = "",
    exit_code: int | None = None,
    timed_out: bool = False,
    missing_checker: bool = False,
) -> SpecAlignmentResult:
    """Apply the explicit policy for checker-unavailable or invalid outcomes."""
    safe_detail = redact_output(detail)
    blocked = policy == "block"
    return SpecAlignmentResult(
        passed=not blocked,
        status=status,
        output=output or safe_detail,
        error=safe_detail if blocked else "",
        warning=safe_detail if not blocked else "",
        exit_code=exit_code,
        timed_out=timed_out,
        missing_checker=missing_checker,
    )


def _policy_result(
    mode: str,
    status: str,
    detail: str,
    *,
    output: str = "",
    exit_code: int | None = None,
    timed_out: bool = False,
) -> SpecAlignmentResult:
    safe_detail = redact_output(detail)
    if status == "pass":
        return SpecAlignmentResult(True, status, output=output or safe_detail, exit_code=exit_code)
    if status == "warn":
        return SpecAlignmentResult(
            True,
            status,
            output=output or safe_detail,
            warning=safe_detail,
            exit_code=exit_code,
            timed_out=timed_out,
        )
    if status == "fail" and mode == "block":
        return SpecAlignmentResult(
            False,
            status,
            output=output or safe_detail,
            error=safe_detail,
            exit_code=exit_code,
            timed_out=timed_out,
        )
    return SpecAlignmentResult(
        True,
        status,
        output=output or safe_detail,
        warning=safe_detail,
        exit_code=exit_code,
        timed_out=timed_out,
    )
