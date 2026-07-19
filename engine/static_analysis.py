"""
Static Analysis Runner — Invoke type checkers and parse structured output.

Supports: mypy, pyright, sorbet, sqlfluff, phpstan, cppcheck, staticcheck
All backends produce the same normalized diagnostic shape:
    {file, line, severity, message, code, tool}

Mypy text output example:
    #    main.py:9: error: Argument 1 to "get_user" has incompatible
    #        type "str"; expected "int"  [arg-type]

Pyright JSON output example:
    {"version": "...", "generalDiagnostics": [
        {"file": "main.py", "severity": "error", "message": "...", "range": {...}}
    ]}

Sorbet text output example:
    main.rb:5: Expected Integer but found String for argument user_id https://srb.help/7002

Sqlfluff JSON output example:
    [{"filepath": "...", "violations": [{"line_no": 5, "description": "..."}]}]

Phpstan JSON output example:
    {"totals": {...}, "files": {"file.php": {"errors": 1, "messages": [
        {"line": 27, "message": "Parameter #1 ... expects int, string given."}
    ]}}}
"""

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass

logger = logging.getLogger("gitreins.static_analysis")


@dataclass
class StaticDiag:
    """Normalized diagnostic from any static analysis tool."""
    file: str
    line: int
    severity: str       # "error" | "warning" | "note"
    message: str
    code: str = ""      # mypy error code, pyright rule, etc.
    tool: str = ""      # "mypy", "pyright", "sorbet", etc.

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "line": self.line,
            "severity": self.severity,
            "message": self.message,
            "code": self.code,
            "tool": self.tool,
        }


# ── Tool discovery ──────────────────────────────────────────────────────

_TOOL_BINARIES = {
    "mypy": ["mypy"],
    "pyright": ["pyright", "npx pyright"],
    "sorbet": ["srb", "bundle exec srb"],
    "sqlfluff": ["sqlfluff"],
    "phpstan": ["phpstan", "vendor/bin/phpstan"],
    "cppcheck": ["cppcheck"],
    "staticcheck": ["staticcheck"],
    "clippy": ["cargo"],
    "eslint": ["eslint", "npx eslint"],
}

_TOOL_INSTALL_GUIDE = {
    "mypy": "pip install mypy",
    "pyright": "pip install pyright  (or: npm install -g pyright)",
    "sorbet": "gem install sorbet && srb init",
    "sqlfluff": "pip install sqlfluff",
    "phpstan": "composer require --dev phpstan/phpstan",
    "cppcheck": "sudo apt install cppcheck  (or: brew install cppcheck)",
    "staticcheck": "go install honnef.co/go/tools/cmd/staticcheck@latest  (add ~/go/bin to PATH)",
    "clippy": "rustup component add clippy",
    "eslint": "npm install -g eslint  (or: npx eslint --init)",
}


def _install_help(tool: str) -> str:
    """Return the install instruction for a tool, or a generic fallback."""
    return _TOOL_INSTALL_GUIDE.get(tool, f"Install {tool} from your package manager")


def find_tool(tool_name: str) -> str | None:
    """Return the path to a tool binary, or None if not found."""
    candidates = _TOOL_BINARIES.get(tool_name, [tool_name])
    for candidate in candidates:
        # Try as-is first (handles paths and npx wrappers)
        if "/" in candidate or " " in candidate:
            parts = candidate.split()
            if shutil.which(parts[0]):
                return candidate  # Return the full command string
            continue
        path = shutil.which(candidate)
        if path:
            return path
    return None


def list_available_tools(language: str) -> list[str]:
    """Return list of installed static analysis tools for a language.

    Only returns tools actually found on PATH. This is what gitreins init
    uses to decide which tools to enable in config.
    """
    language_tools = {
        "python": ["mypy", "pyright"],
        "ruby": ["sorbet"],
        "sql": ["sqlfluff"],
        "php": ["phpstan"],
        "cpp": ["cppcheck"],
        "go": ["staticcheck"],
        "rust": ["clippy"],
        "javascript": ["eslint"],
        "typescript": ["eslint"],
    }
    available = []
    for tool in language_tools.get(language, []):
        if find_tool(tool):
            available.append(tool)
    return available


# ── Text output parsers ─────────────────────────────────────────────────

# Mypy: "file.py:line: severity: message  [code]"
_MYPY_LINE_RE = re.compile(
    r"^(.+?):(\d+):\s+(error|warning|note):\s+(.+?)(?:\s+\[(.+?)\])?\s*$"
)


def _parse_mypy(text: str, tool: str = "mypy") -> list[StaticDiag]:
    diagnostics: list[StaticDiag] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        # Skip mypy summary lines
        if line.startswith("Found ") and "error" in line:
            continue
        if line.startswith("Success:") or line.startswith("note:"):
            continue
        m = _MYPY_LINE_RE.match(line)
        if m:
            diagnostics.append(StaticDiag(
                file=m.group(1),
                line=int(m.group(2)),
                severity=m.group(3),
                message=m.group(4).strip(),
                code=m.group(5) or "",
                tool=tool,
            ))
        else:
            logger.debug("mypy: unparsed line: %s", line[:120])
    return diagnostics


# Sorbet: "file.rb:line: message https://srb.help/CODE"
_SORBET_LINE_RE = re.compile(
    r"^(.+?):(\d+):\s+(.+?)(?:\s+https://srb\.help/(\d+))?\s*$"
)


def _parse_sorbet(text: str, tool: str = "sorbet") -> list[StaticDiag]:
    diagnostics: list[StaticDiag] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if line.startswith("No errors!"):
            continue
        if "Errors:" in line:
            continue
        m = _SORBET_LINE_RE.match(line)
        if m:
            msg = m.group(3).strip()
            severity = "error"
            if msg.lower().startswith("warning"):
                severity = "warning"
            diagnostics.append(StaticDiag(
                file=m.group(1),
                line=int(m.group(2)),
                severity=severity,
                message=msg,
                code=m.group(4) or "",
                tool=tool,
            ))
        else:
            logger.debug("sorbet: unparsed line: %s", line[:120])
    return diagnostics


# Cppcheck: "file.cpp:line: severity: message [code]" (with --template)
_CPPCHECK_LINE_RE = re.compile(
    r"^(.+?):(\d+):\s+(error|warning|style|performance|portability|information):"
    r"\s+(.+?)(?:\s+\[(.+?)\])?\s*$"
)


def _parse_cppcheck(text: str, tool: str = "cppcheck") -> list[StaticDiag]:
    """Parse cppcheck text output (--template format, mypy-compatible).

    Cppcheck uses a wider severity vocabulary than mypy.  We normalise
    ``error`` / ``warning`` as-is and map everything else (style,
    performance, portability, information) to ``note`` so downstream
    consumers get a consistent three-level scheme.
    """
    diagnostics: list[StaticDiag] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        # Skip header lines
        if line.startswith("Checking ") or line.startswith("nofile:"):
            continue
        m = _CPPCHECK_LINE_RE.match(line)
        if m:
            raw_severity = m.group(3)
            severity = (
                raw_severity if raw_severity in ("error", "warning") else "note"
            )
            diagnostics.append(StaticDiag(
                file=m.group(1),
                line=int(m.group(2)),
                severity=severity,
                message=m.group(4).strip(),
                code=m.group(5) or "",
                tool=tool,
            ))
        else:
            logger.debug("cppcheck: unparsed line: %s", line[:120])
    return diagnostics


# Staticcheck: "file.go:line:col: message (SAxxxx)"
_STATICCHECK_LINE_RE = re.compile(
    r"^(.+?):(\d+):\d+:\s+(.+?)(?:\s+\((.+?)\))?\s*$"
)


def _parse_staticcheck(text: str, tool: str = "staticcheck") -> list[StaticDiag]:
    """Parse staticcheck text output.

    Staticcheck uses its own diagnostic codes (SAxxxx, STxxxx).  We map
    SA codes (static analysis) to ``error`` or ``warning`` and everything
    else to ``note``.
    """
    diagnostics: list[StaticDiag] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        # Skip non-file lines (compile errors from the toolchain, etc.)
        if line.startswith("-:") or line.startswith("# "):
            continue
        m = _STATICCHECK_LINE_RE.match(line)
        if m:
            code = m.group(4) or ""
            severity = "error" if code.startswith("SA") else "warning"
            diagnostics.append(StaticDiag(
                file=m.group(1),
                line=int(m.group(2)),
                severity=severity,
                message=m.group(3).strip(),
                code=code,
                tool=tool,
            ))
        else:
            logger.debug("staticcheck: unparsed line: %s", line[:120])
    return diagnostics


# ── JSON output parsers ─────────────────────────────────────────────────


def _parse_pyright_json(data: dict, tool: str = "pyright") -> list[StaticDiag]:
    diagnostics: list[StaticDiag] = []
    for diag in data.get("generalDiagnostics", []):
        start = diag.get("range", {}).get("start", {})
        diagnostics.append(StaticDiag(
            file=diag.get("file", "unknown"),
            line=start.get("line", 0) + 1,  # pyright uses 0-indexed lines
            severity=diag.get("severity", "error"),
            message=diag.get("message", ""),
            code=diag.get("rule", ""),
            tool=tool,
        ))
    return diagnostics


def _parse_sqlfluff_json(data, tool: str = "sqlfluff") -> list[StaticDiag]:
    diagnostics: list[StaticDiag] = []
    # sqlfluff output is a list of file results
    files = data if isinstance(data, list) else [data]
    for file_result in files:
        filepath = file_result.get("filepath", "unknown")
        for violation in file_result.get("violations", []):
            diagnostics.append(StaticDiag(
                file=filepath,
                line=violation.get("line_no", 0),
                severity="error" if violation.get("severity") in ("error", None) else "warning",
                message=violation.get("description", ""),
                code=violation.get("code", ""),
                tool=tool,
            ))
    return diagnostics


def _parse_clippy_json(text: str, tool: str = "clippy") -> list[StaticDiag]:
    """Parse cargo clippy --message-format=json output.

    Clippy outputs one JSON object per line.  Compiler messages (warnings,
    errors) carry ``reason: "compiler-message"`` with per-span file / line /
    column info.  We extract the primary span from each message and normalise
    clippy severity levels (``error`` / ``warning``) into the standard
    three-level scheme.
    """
    diagnostics: list[StaticDiag] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            logger.debug("clippy: invalid JSON line: %s", line[:120])
            continue

        if obj.get("reason") != "compiler-message":
            continue

        msg = obj.get("message", {})
        level = msg.get("level", "warning")
        severity = level if level in ("error", "warning") else "note"
        code_obj = msg.get("code") or {}
        code = code_obj.get("code", "") if isinstance(code_obj, dict) else str(code_obj)

        # Use the primary span (is_primary: true), or the first span
        primary_span = None
        for span in msg.get("spans", []):
            if span.get("is_primary"):
                primary_span = span
                break
        if primary_span is None and msg.get("spans"):
            primary_span = msg["spans"][0]

        if primary_span:
            diagnostics.append(StaticDiag(
                file=primary_span.get("file_name", "unknown"),
                line=primary_span.get("line_start", 1),
                severity=severity,
                message=msg.get("message", ""),
                code=code,
                tool=tool,
            ))
        else:
            # No span — use rendered message with unknown location
            diagnostics.append(StaticDiag(
                file="unknown",
                line=1,
                severity=severity,
                message=msg.get("rendered", msg.get("message", "")),
                code=code,
                tool=tool,
            ))

    return diagnostics


def _parse_eslint_json(data, tool: str = "eslint") -> list[StaticDiag]:
    """Parse eslint --format=json output.

    ESLint JSON output is a list of file results, each with ``filePath``
    and ``messages``.  Severity 2 = error, 1 = warning, 0 = off (ignored).
    """
    diagnostics: list[StaticDiag] = []
    files = data if isinstance(data, list) else [data]
    for file_result in files:
        filepath = file_result.get("filePath", "unknown")
        for msg in file_result.get("messages", []):
            sev = msg.get("severity", 1)
            severity = "error" if sev >= 2 else "warning"
            diagnostics.append(StaticDiag(
                file=filepath,
                line=msg.get("line", 1),
                severity=severity,
                message=msg.get("message", ""),
                code=msg.get("ruleId", ""),
                tool=tool,
            ))
    return diagnostics


def _parse_phpstan_json(data: dict, tool: str = "phpstan") -> list[StaticDiag]:
    diagnostics: list[StaticDiag] = []
    for filepath, file_data in data.get("files", {}).items():
        for msg in file_data.get("messages", []):
            diagnostics.append(StaticDiag(
                file=filepath,
                line=msg.get("line", 0),
                severity="error",
                message=msg.get("message", ""),
                code="",
                tool=tool,
            ))
    return diagnostics


# ── Main runner ─────────────────────────────────────────────────────────

_JSON_PARSERS = {
    "pyright": _parse_pyright_json,
    "sqlfluff": _parse_sqlfluff_json,
    "phpstan": _parse_phpstan_json,
    "eslint": _parse_eslint_json,
}

_TEXT_PARSERS = {
    "mypy": _parse_mypy,
    "sorbet": _parse_sorbet,
    "cppcheck": _parse_cppcheck,
    "staticcheck": _parse_staticcheck,
    "clippy": _parse_clippy_json,
}


def run_static_check(tool: str, workdir: str) -> list[dict]:
    """Run a static analysis tool against a directory and return diagnostics.

    Args:
        tool: Tool name (e.g. "mypy", "pyright", "sorbet")
        workdir: Absolute path to the project root or target directory

    Returns:
        List of normalized diagnostic dicts: {file, line, severity, message, code, tool}
        Empty list if the tool binary is not found (graceful fallback).
    """
    # Resolve the binary
    binary = find_tool(tool)
    if not binary:
        logger.warning(
            "Static analysis tool '%s' not found on PATH. "
            "Install with: %s. "
            "Skipping — no diagnostics returned.",
            tool, _install_help(tool),
        )
        return []

    # Build command
    cmd = _build_command(tool, binary, workdir)

    # Run
    logger.debug("Running: %s", " ".join(cmd))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=workdir if tool != "pyright" else workdir,  # pyright uses CWD for root
        )
    except subprocess.TimeoutExpired:
        logger.warning("%s timed out after 120s", tool)
        return []
    except FileNotFoundError:
        logger.warning("%s binary not found: %s", tool, binary)
        return []
    except Exception as exc:
        logger.warning("%s failed: %s", tool, exc)
        return []

    stdout = result.stdout.strip()
    stderr = result.stderr.strip()

    # Mypy outputs to stdout; other tools may use stderr for diagnostics
    # Sorbet outputs errors to stderr, JSON tools to stdout
    if tool == "sorbet":
        output = stderr if stderr else stdout
    elif tool in _JSON_PARSERS:
        output = stdout
    else:
        output = stdout + ("\n" + stderr if stderr else "")

    # Parse
    if tool in _JSON_PARSERS:
        try:
            data = json.loads(output)
            parser = _JSON_PARSERS[tool]
            diags = parser(data, tool=tool)
        except json.JSONDecodeError:
            logger.warning("%s returned invalid JSON", tool)
            return []
    elif tool in _TEXT_PARSERS:
        parser = _TEXT_PARSERS[tool]
        diags = parser(output, tool=tool)
    else:
        logger.warning("No parser available for tool '%s'", tool)
        return []

    # Normalize paths relative to workdir
    for d in diags:
        if d.file and not os.path.isabs(d.file):
            pass  # Already relative
        # Truncate common prefix for cleaner output
        if d.file.startswith(workdir):
            d.file = os.path.relpath(d.file, workdir)

    return [d.to_dict() for d in diags]


def _build_command(tool: str, binary: str, workdir: str) -> list[str]:
    """Build the subprocess command for a given tool."""
    # Handle compound commands like "npx pyright"
    cmd_parts = binary.split()

    if tool == "mypy":
        return cmd_parts + [
            "--no-error-summary",
            "--explicit-package-bases",
            ".",
        ]
    elif tool == "pyright":
        return cmd_parts + [
            "--outputjson",
            workdir,
        ]
    elif tool == "sorbet":
        return cmd_parts + [
            "tc",
            "--no-error-count",
        ]
    elif tool == "sqlfluff":
        return cmd_parts + [
            "lint",
            "--format", "json",
            workdir,
        ]
    elif tool == "phpstan":
        return cmd_parts + [
            "analyse",
            "--error-format=json",
            "--no-progress",
            workdir,
        ]
    elif tool == "cppcheck":
        return cmd_parts + [
            "--enable=all",
            "--suppress=missingIncludeSystem",
            "--template={file}:{line}: {severity}: {message} [{id}]",
            workdir,
        ]
    elif tool == "staticcheck":
        return cmd_parts + [
            "./...",
        ]
    elif tool == "clippy":
        return cmd_parts + [
            "clippy",
            "--message-format=json",
        ]
    elif tool == "eslint":
        return cmd_parts + [
            "--format=json",
            workdir,
        ]
    else:
        # Generic fallback
        return cmd_parts + [workdir]
