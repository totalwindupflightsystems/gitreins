"""
LSP Guard Runner — Invoke LSP servers and collect diagnostics.

Supports multiple LSP backends that all produce the same normalized output.
Each LSP tool is started, receives textDocument/didOpen for each staged file,
and returns any diagnostics.
"""

import json
import logging
import os
import select
import shutil
import subprocess
import urllib.parse
from dataclasses import dataclass, asdict
from pathlib import Path

logger = logging.getLogger("gitreins.lsp")

_TOOL_BINARIES = {
    "pylsp": ["pylsp"],
    "ruff-lsp": ["ruff-lsp"],
    "pyright": ["pyright-langserver", "pyright"],
    "lua-lsp": ["lua-lsp"],
    "ts-lsp": ["typescript-language-server"],
    "rust-analyzer": ["rust-analyzer"],
    "gopls": ["gopls"],
    "jdtls": ["jdtls"],
    "kotlin-language-server": ["kotlin-language-server"],
    "csharp-ls": ["csharp-ls", "omnisharp"],
    "sourcekit-lsp": ["sourcekit-lsp"],
    "dart": ["dart"],
    "elixir-ls": ["elixir-ls"],
    "metals": ["metals"],
    "ruby-lsp": ["ruby-lsp"],
    "solargraph": ["solargraph"],
}

_LANGUAGE_MAP: dict[str, str] = {
    ".py": "python",
    ".pyw": "python",
    ".lua": "lua",
    ".rs": "rust",
    ".ts": "typescript",
    ".tsx": "typescriptreact",
    ".js": "javascript",
    ".jsx": "javascriptreact",
    ".go": "go",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".cs": "csharp",
    ".swift": "swift",
    ".dart": "dart",
    ".ex": "elixir",
    ".exs": "elixir",
    ".scala": "scala",
    ".sc": "scala",
    ".rb": "ruby",
}

_TOOL_LANGUAGES: dict[str, list[str]] = {
    "pylsp": ["python"],
    "ruff-lsp": ["python"],
    "pyright": ["python"],
    "lua-lsp": ["lua"],
    "ts-lsp": ["typescript", "typescriptreact", "javascript", "javascriptreact"],
    "rust-analyzer": ["rust"],
    "gopls": ["go"],
    "jdtls": ["java"],
    "kotlin-language-server": ["kotlin"],
    "csharp-ls": ["csharp"],
    "sourcekit-lsp": ["swift"],
    "dart": ["dart"],
    "elixir-ls": ["elixir"],
    "metals": ["scala"],
    "ruby-lsp": ["ruby"],
    "solargraph": ["ruby"],
}


@dataclass
class LspDiag:
    file: str
    line: int
    severity: str
    message: str
    code: str = ""
    tool: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


# ── Readiness probe (INT-FLAKE-4) ───────────────────────────────────────────
#
# ``run_lsp_check`` answers ``[]`` both for "the server is healthy and found
# nothing" and for "the spawn went quiescent and never published anything", so
# a caller cannot tell a clean tree from a stalled server.  A real
# request/response round trip is the discriminator: every LSP server in
# ``_TOOL_BINARIES`` implements ``workspace/symbol``, and any answer (a result
# *or* an error response) proves the server is consuming input and servicing
# requests.  A quiescent gopls under load answers nothing.
READY_PROBE_METHOD = "workspace/symbol"
READY_PROBE_ID = 9001
PROBE_PARAMS: dict = {"query": ""}
READY_PROBE_TIMEOUT_S = 15.0

# How long to wait for the server's first `publishDiagnostics` for a file
# before re-sending the same content as a change.  A healthy server publishes
# in ~0.06-1.5 s even under load, so this window is far above the observed
# latency, while a spawn whose didOpen landed too early never publishes at all.
RECHECK_AFTER_S = 5.0


@dataclass
class LspCheckStatus:
    """Outcome of one LSP check, including whether the server was responsive."""

    tool: str
    diagnostics: list[dict]
    files: list[str]
    server_ready: bool | None = None  # None = not probed
    ready_seconds: float | None = None
    stalled: bool = False
    stall_reason: str | None = None
    probe_method: str | None = None
    # True only when the server sent a `publishDiagnostics` notification for
    # every requested file (an empty list counts: that is a real "found
    # nothing").  False means it never reported on the file at all.
    published: bool = True
    rechecks: int = 0
    duration_s: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


_SEVERITY_MAP = {
    1: "error",
    2: "warning",
    3: "info",
    4: "hint",
}


def normalize_severity(severity: int) -> str:
    return _SEVERITY_MAP.get(severity, "warning")


def find_lsp_tool(tool_name: str) -> str | None:
    binaries = _TOOL_BINARIES.get(tool_name, [tool_name])
    for binary in binaries:
        resolved = shutil.which(binary)
        path = os.path.abspath(resolved) if resolved else None
        if path:
            return path
    return None


def _lsp_encode_message(msg: dict) -> bytes:
    payload = json.dumps(msg)
    body = payload.encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n"
    return header.encode("utf-8") + body


def _lsp_send_request(
    proc: subprocess.Popen, request_id: int, method: str, params: dict | None = None
) -> bool:
    """Send a JSON-RPC request; False when the server's stdin is gone."""
    message = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params or {},
    }
    try:
        if proc.stdin is None:
            return False
        proc.stdin.write(_lsp_encode_message(message))
        proc.stdin.flush()
        return True
    except Exception:
        return False


def _lsp_read_response(proc: subprocess.Popen, timeout: float = 60.0) -> dict | None:
    """Read one JSON-RPC response from an LSP server process.

    Uses os.read on the raw file descriptor to bypass Python's
    BufferedReader buffering, which interferes with select().
    Falls back to proc.stdout.read() for in-memory streams (BytesIO).

    Returns None immediately when the server's stdout is exhausted
    (EOF — the process died) instead of spinning until the deadline
    (GR-138): a dead server must never hold the init/diagnostics
    channel for the full timeout.
    """
    import time as _time
    import os as _os

    deadline = _time.monotonic() + timeout

    # Determine fd — use fileno() for real pipes, None for BytesIO
    try:
        fd = proc.stdout.fileno() if proc.stdout else None
    except Exception:
        fd = None

    # Accumulated raw data buffer
    buffer = b""
    eof = False

    def _read_more() -> int:
        """Read more data into buffer. Returns bytes read (0 = timeout/EOF)."""
        nonlocal buffer, eof
        if fd is None:
            # BytesIO / mock — use normal read
            stdout = proc.stdout
            chunk = stdout.read(4096) if stdout and hasattr(stdout, "read") else b""
            if not chunk:
                eof = True
            buffer += chunk
            return len(chunk)
        # Real pipe — select with deadline, then os.read
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            return 0
        r, _, _ = select.select([fd], [], [], min(remaining, 1.0))
        if not r:
            return 0
        try:
            chunk = _os.read(fd, 4096)
        except Exception:
            return 0
        if not chunk:
            eof = True
            return 0
        buffer += chunk
        return len(chunk)

    # ----------------------------------------------------------------
    # Read header
    header_end = -1
    while header_end == -1:
        idx = buffer.find(b"\r\n\r\n")
        if idx >= 0:
            header_end = idx + 4
            break
        if _read_more() == 0:
            if _time.monotonic() >= deadline:
                return None  # global deadline exceeded
            if eof:
                return None  # server closed stdout — no more messages
            continue  # select() timed out but deadline not reached — retry

    header_bytes = buffer[:header_end]
    buffer = buffer[header_end:]

    header_text = header_bytes.decode("utf-8").strip()
    content_length = 0
    for line in header_text.split("\r\n"):
        if line.lower().startswith("content-length:"):
            content_length = int(line.split(":")[1].strip())

    if content_length == 0:
        return None

    # ----------------------------------------------------------------
    # Read body
    while len(buffer) < content_length:
        if _read_more() == 0:
            if _time.monotonic() >= deadline:
                break  # global deadline exceeded
            if eof:
                break  # server closed stdout mid-message
            continue  # select() timed out but deadline not reached — retry

    if len(buffer) < content_length:
        return None

    body = buffer[:content_length]
    buffer = buffer[content_length:]

    try:
        return json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _collect_diagnostics(
    proc: subprocess.Popen,
    filepath: str,
    timeout: float,
    tool: str,
    probe_method: str | None = None,
    ready_timeout: float = READY_PROBE_TIMEOUT_S,
) -> tuple[list[dict], float | None, bool]:
    """Collect diagnostics for ``filepath``; optionally probe server liveness.

    Returns ``(diagnostics, probe_seconds, published)``.  ``published`` says
    whether the server ever sent a ``publishDiagnostics`` notification for this
    file — an empty list is a *published* "found nothing", which is different
    from the server never reporting on the file at all (INT-FLAKE-4's stall).
    When ``probe_method`` is set a real request is sent as the wait starts:
    ``probe_seconds`` is the latency of its response, or ``None`` when the
    server never answered it.
    """
    import time as _time

    diags: list[dict] = []
    published = False
    started = _time.monotonic()
    deadline = started + timeout
    probe_seconds: float | None = None
    effective_deadline = deadline
    if probe_method:
        _lsp_send_request(proc, READY_PROBE_ID, probe_method, PROBE_PARAMS)
        effective_deadline = min(deadline, started + ready_timeout)

    try:
        while True:
            remaining = effective_deadline - _time.monotonic()
            if remaining <= 0:
                break
            msg = _lsp_read_response(proc, remaining)
            if msg is None:
                if proc.poll() is not None:
                    break  # server exited — no more diagnostics (GR-138)
                continue
            if msg.get("id") == READY_PROBE_ID:
                # The server answered a real request: it is responsive, so a
                # missing diagnostics notification is a real "found nothing"
                # rather than a stall.  Keep waiting for the diagnostics.
                probe_seconds = round(_time.monotonic() - started, 6)
                effective_deadline = deadline
                continue
            if msg.get("method") == "textDocument/publishDiagnostics":
                uri = msg.get("params", {}).get("uri", "")
                file_uri = urllib.parse.urlparse(uri).path if uri else filepath
                # Only collect diagnostics for the requested file
                if file_uri != filepath:
                    continue
                published = True
                for d in msg.get("params", {}).get("diagnostics", []):
                    range_start = d.get("range", {}).get("start", {})
                    line_0based = range_start.get("line", 0)
                    diags.append(
                        {
                            "file": file_uri,
                            "line": line_0based + 1,
                            "severity": normalize_severity(d.get("severity", 1)),
                            "message": d.get("message", ""),
                            "code": str(d.get("code", "")),
                            "tool": tool,
                        }
                    )
                break  # got the file's diagnostics — done
    except Exception:
        pass

    return diags, probe_seconds, published


def _lsp_initialize(proc: subprocess.Popen, workdir: str, timeout: float = 60.0) -> bool:
    root_uri = Path(workdir).as_uri()
    init_msg = {
        "jsonrpc": "2.0",
        "id": 0,
        "method": "initialize",
        "params": {
            "processId": None,
            "rootUri": root_uri,
            "capabilities": {},
        },
    }
    assert proc.stdin is not None
    proc.stdin.write(_lsp_encode_message(init_msg))
    proc.stdin.flush()

    response = _lsp_read_response(proc, timeout=timeout)
    if response is None:
        return False

    initialized_msg = {
        "jsonrpc": "2.0",
        "method": "initialized",
        "params": {},
    }
    assert proc.stdin is not None
    proc.stdin.write(_lsp_encode_message(initialized_msg))
    proc.stdin.flush()
    return True


def _lsp_did_open(proc: subprocess.Popen[bytes], filepath: str, language_id: str) -> None:
    file_uri = Path(filepath).as_uri()
    try:
        with open(filepath, "r", errors="replace") as f:
            text = f.read()
    except Exception:
        text = ""

    did_open_msg = {
        "jsonrpc": "2.0",
        "method": "textDocument/didOpen",
        "params": {
            "textDocument": {
                "uri": file_uri,
                "languageId": language_id,
                "version": 1,
                "text": text,
            },
        },
    }
    assert proc.stdin is not None
    proc.stdin.write(_lsp_encode_message(did_open_msg))
    proc.stdin.flush()


def _lsp_did_change(proc: subprocess.Popen[bytes], filepath: str, version: int = 2) -> bool:
    """Re-send the file's current content, as a real change, to force a check.

    INT-FLAKE-4: when a ``didOpen`` lands before gopls has a snapshot for the
    file, gopls loads it (symbols and definitions resolve) but never publishes
    diagnostics for that open — a real client's next edit re-triggers the
    check.  Sending the same text with a bumped version is that edit: measured
    at 0.01 s to publish under load, where waiting produced nothing for 42 s.
    """
    file_uri = Path(filepath).as_uri()
    try:
        with open(filepath, "r", errors="replace") as f:
            text = f.read()
    except Exception:
        text = ""

    message = {
        "jsonrpc": "2.0",
        "method": "textDocument/didChange",
        "params": {
            "textDocument": {"uri": file_uri, "version": version},
            "contentChanges": [{"text": text}],
        },
    }
    try:
        if proc.stdin is None:
            return False
        proc.stdin.write(_lsp_encode_message(message))
        proc.stdin.flush()
        return True
    except Exception:
        return False


def _lsp_shutdown(proc: subprocess.Popen[bytes], timeout: float = 30.0) -> None:
    shutdown_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "shutdown",
        "params": {},
    }
    try:
        assert proc.stdin is not None
        proc.stdin.write(_lsp_encode_message(shutdown_msg))
        proc.stdin.flush()
        _lsp_read_response(proc, timeout=timeout)
    except Exception:
        pass

    exit_msg = {
        "jsonrpc": "2.0",
        "method": "exit",
        "params": {},
    }
    try:
        assert proc.stdin is not None
        proc.stdin.write(_lsp_encode_message(exit_msg))
        proc.stdin.flush()
    except Exception:
        pass


def _get_staged_files(workdir: str) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=workdir,
        )
        return [f.strip() for f in result.stdout.split("\n") if f.strip()]
    except Exception:
        return []


def _staged_files_by_language(workdir: str) -> dict[str, list[str]]:
    staged = _get_staged_files(workdir)
    by_lang: dict[str, list[str]] = {}
    for fpath in staged:
        full = os.path.join(workdir, fpath)
        if not os.path.isfile(full):
            continue
        ext = os.path.splitext(fpath)[1].lower()
        lang = _LANGUAGE_MAP.get(ext)
        if lang:
            by_lang.setdefault(lang, []).append(full)
    return by_lang


def select_lsp_files(tool: str, workdir: str, paths: list[str]) -> list[str]:
    """Absolute file paths from ``paths`` that ``tool`` can actually grade.

    The counterpart of :func:`_staged_files_by_language` for a caller that
    already holds its own change set (the guard's ``--scope working-tree``
    scope): the same extension → language map and the same per-tool language
    table decide, so a whole-tree scope cannot send a Markdown file to pylsp
    or a Rust file to clangd. Paths that no longer exist are dropped, and the
    input order is preserved.
    """
    selected: list[str] = []
    for path in paths:
        ext = os.path.splitext(path)[1].lower()
        lang = _LANGUAGE_MAP.get(ext)
        if lang is None or not _tool_supports_language(tool, lang):
            continue
        full = path if os.path.isabs(path) else os.path.join(workdir, path)
        if os.path.isfile(full):
            selected.append(full)
    return selected


def _tool_supports_language(tool: str, lang: str) -> bool:
    supported = _TOOL_LANGUAGES.get(tool, [])
    return lang in supported


def _tool_default_timeouts(tool: str, workdir: str) -> tuple[float, float]:
    """Return (init_timeout, per_file_timeout) tuned for the tool/project.

    C++ (clangd/ccls) LSP servers build a project-wide index on first
    run and parse full translation units per file — 30-60s caps are far
    too tight for real C++ repos (compile_commands.json, heavy header
    includes). rust-analyzer gets the same generous per-file budget, but
    its *init* cap stays bounded (~30s): a project rust-analyzer cannot
    recognize (missing src/main.rs, broken toolchain shim) must fail the
    init fast so the guard/tests skip instead of holding the init
    channel for minutes (GR-138).
    """
    if tool == "rust-analyzer":
        return (30.0, 120.0)
    if tool in {"clangd", "ccls"}:
        return (300.0, 120.0)
    # Repo-level sniff: any staged or on-disk C/C++/Rust sources?
    try:
        staged = _get_staged_files(workdir)
    except Exception:
        staged = []
    cpp_hint = any(
        os.path.splitext(f)[1].lower() in {".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".rs"}
        for f in staged
    )
    if cpp_hint:
        return (180.0, 90.0)
    return (60.0, 30.0)


def run_lsp_check_status(
    tool: str,
    workdir: str,
    files: list[str] | None = None,
    timeout_per_file: float | None = None,
    init_timeout: float | None = None,
    probe: bool = False,
    ready_timeout: float = READY_PROBE_TIMEOUT_S,
    recheck_after: float = RECHECK_AFTER_S,
) -> LspCheckStatus:
    """Run one LSP check and report *why* it produced no diagnostics.

    Every file is opened once; if the server publishes nothing for it within
    ``recheck_after``, the same content is re-sent as a ``didChange`` — the
    INT-FLAKE-4 root cause is a ``didOpen`` that lands before gopls has a
    snapshot for the file, after which the check simply never runs even though
    the file resolves for symbols and definitions.  ``status.published``
    reports whether the server ever reported on every file, so a caller can
    tell a real "found nothing" from a spawn that never checked anything.

    With ``probe=True`` the server must additionally answer a real request
    (``workspace/symbol``) within ``ready_timeout``; ``stalled`` marks a spawn
    that never reported on the file at all (or never answered), which is a
    load/environment signal, not a verdict.

    ``run_lsp_check`` keeps the historical contract (diagnostics only);
    this entry point is for callers that must discriminate.
    """
    import time as _time

    started = _time.monotonic()
    status = LspCheckStatus(
        tool=tool,
        diagnostics=[],
        files=list(files or []),
        probe_method=READY_PROBE_METHOD if probe else None,
    )

    def finish(diagnostics: list[dict]) -> LspCheckStatus:
        status.diagnostics = diagnostics
        status.duration_s = round(_time.monotonic() - started, 6)
        return status

    init_t, per_file_t = _tool_default_timeouts(tool, workdir)
    if init_timeout is None:
        init_timeout = init_t
    if timeout_per_file is None:
        timeout_per_file = per_file_t
    tool_path = find_lsp_tool(tool)
    if not tool_path:
        logger.warning("LSP tool '%s' not found on PATH — skipping", tool)
        return finish([])

    if files is not None:
        staged_files = files
    else:
        files_by_lang = _staged_files_by_language(workdir)
        staged_files = []
        for lang, lang_files in files_by_lang.items():
            if _tool_supports_language(tool, lang):
                staged_files.extend(lang_files)

    status.files = list(staged_files)
    if not staged_files:
        logger.debug("No staged files for LSP tool '%s'", tool)
        return finish([])

    all_diagnostics: list[dict] = []
    proc = None

    try:
        proc = subprocess.Popen(
            [tool_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=workdir,
            start_new_session=True,  # isolate in own process group for clean kill
        )
    except Exception as exc:
        logger.warning("Failed to start LSP tool '%s': %s", tool, exc)
        return finish([])

    try:
        if not _lsp_initialize(proc, workdir, timeout=init_timeout):
            logger.warning("LSP tool '%s' failed to initialize", tool)
            if probe:
                status.server_ready = False
                status.stalled = True
                status.stall_reason = (
                    f"the server did not answer `initialize` within {init_timeout:.0f}s"
                )
            return finish([])

        for filepath in staged_files:
            ext = os.path.splitext(filepath)[1].lower()
            language_id = _LANGUAGE_MAP.get(ext, "python")

            _lsp_did_open(proc, filepath, language_id)

            first_phase = min(timeout_per_file, recheck_after)
            diags, probe_seconds, published = _collect_diagnostics(
                proc,
                filepath,
                first_phase,
                tool,
                status.probe_method,
                ready_timeout,
            )
            if not published and recheck_after < timeout_per_file:
                # The server never reported on this file.  Re-send the same
                # content as a change to force the check (INT-FLAKE-4: a
                # didOpen that landed before the snapshot existed otherwise
                # never produces diagnostics, while the same content as a
                # didChange publishes in ~0.01 s).
                if _lsp_did_change(proc, filepath):
                    status.rechecks += 1
                    more, probe_seconds_2, published_2 = _collect_diagnostics(
                        proc,
                        filepath,
                        max(1.0, timeout_per_file - first_phase),
                        tool,
                        None,  # the readiness probe was already sent for this file
                        ready_timeout,
                    )
                    diags.extend(more)
                    published = published or published_2
                    if probe_seconds is None:
                        probe_seconds = probe_seconds_2
            all_diagnostics.extend(diags)
            status.published = status.published and published
            if not published:
                # The server gave us nothing to judge this file with: neither a
                # diagnostics notification (an empty list counts as a verdict)
                # nor, when probed, an answer to a real request.
                status.stalled = True
                if not probe:
                    status.stall_reason = (
                        f"the server never published diagnostics for {filepath} "
                        f"(re-requested the file as a change to force the check)"
                    )
                elif probe_seconds is not None:
                    status.stall_reason = (
                        f"the server answered `{status.probe_method}` in {probe_seconds}s but "
                        f"never published diagnostics for {filepath}"
                    )
                elif proc.poll() is not None:
                    status.stall_reason = (
                        f"the server exited before answering `{status.probe_method}` or "
                        f"publishing diagnostics for {filepath}"
                    )
                else:
                    status.stall_reason = (
                        f"the server never answered `{status.probe_method}` and never published "
                        f"diagnostics for {filepath} within {ready_timeout:.0f}s"
                    )
                logger.warning("LSP tool '%s' reported nothing for %s", tool, filepath)
            if not probe:
                continue
            if probe_seconds is None:
                # Unanswered probe — a liveness fact about the spawn, not a
                # verdict gate (a server can publish without answering it).
                status.server_ready = False
                continue
            status.server_ready = True
            if status.ready_seconds is None or probe_seconds < status.ready_seconds:
                status.ready_seconds = probe_seconds

    except subprocess.TimeoutExpired:
        logger.warning("LSP tool '%s' timed out", tool)
    except Exception as exc:
        logger.warning("LSP tool '%s' error: %s", tool, exc)
    finally:
        # Always attempt graceful shutdown first, then force-kill the process group.
        # A stalled server never answers `shutdown`, so do not wait the full
        # grace period for one (INT-FLAKE-4: an attempt must stay cheap so the
        # caller can afford to retry a stalled spawn).
        if proc is not None:
            _lsp_shutdown(proc, timeout=1.0 if status.stalled else 30.0)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                logger.warning(
                    "LSP tool '%s' (pid %d) did not exit — killing process group",
                    tool,
                    proc.pid,
                )
                try:
                    import signal as _signal

                    lsp_pid = proc.pid
                    if not isinstance(lsp_pid, int) or isinstance(lsp_pid, bool) or lsp_pid <= 1:
                        raise ValueError(f"unsafe LSP pid: {lsp_pid!r}")
                    lsp_pgid = os.getpgid(lsp_pid)
                    our_pgid = os.getpgid(os.getpid())
                    if lsp_pgid == lsp_pid and lsp_pgid != our_pgid:
                        os.killpg(lsp_pgid, _signal.SIGKILL)
                    else:
                        logger.error(
                            "LSP tool '%s' has unsafe process group (pid=%d, pgid=%d, ours=%d) — "
                            "falling back to proc.kill()",
                            tool,
                            lsp_pid,
                            lsp_pgid,
                            our_pgid,
                        )
                        proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    try:
                        proc.kill()
                        proc.wait(timeout=2)
                    except Exception:
                        pass

    return finish(all_diagnostics)


def run_lsp_check(
    tool: str,
    workdir: str,
    files: list[str] | None = None,
    timeout_per_file: float | None = None,
    init_timeout: float | None = None,
) -> list[dict]:
    """Return the diagnostics for ``files`` (no readiness probe).

    Behaviour is unchanged from before INT-FLAKE-4: the guard and every
    existing caller keep the diagnostics-only contract, and callers that need
    to tell a stalled spawn from a clean tree use ``run_lsp_check_status``.
    """
    return run_lsp_check_status(
        tool,
        workdir,
        files=files,
        timeout_per_file=timeout_per_file,
        init_timeout=init_timeout,
        probe=False,
    ).diagnostics
