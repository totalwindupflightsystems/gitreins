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
}

_TOOL_LANGUAGES: dict[str, list[str]] = {
    "pylsp": ["python"],
    "ruff-lsp": ["python"],
    "pyright": ["python"],
    "lua-lsp": ["lua"],
    "ts-lsp": ["typescript", "typescriptreact", "javascript", "javascriptreact"],
    "rust-analyzer": ["rust"],
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
        path = shutil.which(binary)
        if path:
            return path
    return None


def _lsp_encode_message(msg: dict) -> bytes:
    payload = json.dumps(msg)
    body = payload.encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n"
    return header.encode("utf-8") + body


def _lsp_read_response(proc: subprocess.Popen, timeout: float = 10.0) -> dict | None:
    """Read one JSON-RPC response from an LSP server process.

    Uses os.read on the raw file descriptor to bypass Python's
    BufferedReader buffering, which interferes with select().
    Falls back to proc.stdout.read() for in-memory streams (BytesIO).
    """
    import time as _time
    import os as _os

    deadline = _time.monotonic() + timeout

    # Determine fd — use fileno() for real pipes, None for BytesIO
    try:
        fd = proc.stdout.fileno()
    except Exception:
        fd = None

    # Accumulated raw data buffer
    buffer = b""

    def _read_more() -> int:
        """Read more data into buffer. Returns bytes read (0 = EOF/timeout)."""
        nonlocal buffer
        if fd is None:
            # BytesIO / mock — use normal read
            chunk = proc.stdout.read(4096) if hasattr(proc.stdout, "read") else b""
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
            return None  # timeout / EOF before complete header

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
            break

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
) -> list[dict]:
    diags: list[dict] = []
    deadline = timeout

    try:
        while True:
            msg = _lsp_read_response(proc, deadline)
            if msg is None:
                break
            if msg.get("method") == "textDocument/publishDiagnostics":
                uri = msg.get("params", {}).get("uri", "")
                file_uri = urllib.parse.urlparse(uri).path if uri else filepath
                for d in msg.get("params", {}).get("diagnostics", []):
                    range_start = d.get("range", {}).get("start", {})
                    line_0based = range_start.get("line", 0)
                    diags.append({
                        "file": file_uri,
                        "line": line_0based + 1,
                        "severity": normalize_severity(d.get("severity", 1)),
                        "message": d.get("message", ""),
                        "code": str(d.get("code", "")),
                        "tool": tool,
                    })
    except Exception:
        pass

    return diags


def _lsp_initialize(proc: subprocess.Popen, workdir: str) -> bool:
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
    proc.stdin.write(_lsp_encode_message(init_msg))
    proc.stdin.flush()

    response = _lsp_read_response(proc, timeout=10.0)
    if response is None:
        return False

    initialized_msg = {
        "jsonrpc": "2.0",
        "method": "initialized",
        "params": {},
    }
    proc.stdin.write(_lsp_encode_message(initialized_msg))
    proc.stdin.flush()
    return True


def _lsp_did_open(proc: subprocess.Popen, filepath: str, language_id: str) -> None:
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
    proc.stdin.write(_lsp_encode_message(did_open_msg))
    proc.stdin.flush()


def _lsp_shutdown(proc: subprocess.Popen) -> None:
    shutdown_msg = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "shutdown",
        "params": {},
    }
    try:
        proc.stdin.write(_lsp_encode_message(shutdown_msg))
        proc.stdin.flush()
        _lsp_read_response(proc, timeout=5.0)
    except Exception:
        pass

    exit_msg = {
        "jsonrpc": "2.0",
        "method": "exit",
        "params": {},
    }
    try:
        proc.stdin.write(_lsp_encode_message(exit_msg))
        proc.stdin.flush()
    except Exception:
        pass


def _get_staged_files(workdir: str) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
            capture_output=True, text=True, timeout=10,
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


def _tool_supports_language(tool: str, lang: str) -> bool:
    supported = _TOOL_LANGUAGES.get(tool, [])
    return lang in supported


def run_lsp_check(
    tool: str,
    workdir: str,
    files: list[str] | None = None,
    timeout_per_file: float = 30.0,
) -> list[dict]:
    tool_path = find_lsp_tool(tool)
    if not tool_path:
        logger.warning("LSP tool '%s' not found on PATH — skipping", tool)
        return []

    if files is not None:
        staged_files = files
    else:
        files_by_lang = _staged_files_by_language(workdir)
        staged_files = []
        for lang, lang_files in files_by_lang.items():
            if _tool_supports_language(tool, lang):
                staged_files.extend(lang_files)

    if not staged_files:
        logger.debug("No staged files for LSP tool '%s'", tool)
        return []

    all_diagnostics: list[dict] = []

    try:
        proc = subprocess.Popen(
            [tool_path],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=workdir,
        )
    except Exception as exc:
        logger.warning("Failed to start LSP tool '%s': %s", tool, exc)
        return []

    try:
        if not _lsp_initialize(proc, workdir):
            logger.warning("LSP tool '%s' failed to initialize", tool)
            _lsp_shutdown(proc)
            return []

        for filepath in staged_files:
            ext = os.path.splitext(filepath)[1].lower()
            language_id = _LANGUAGE_MAP.get(ext, "python")

            _lsp_did_open(proc, filepath, language_id)

            diags = _collect_diagnostics(proc, filepath, timeout_per_file, tool)
            all_diagnostics.extend(diags)

        _lsp_shutdown(proc)

    except subprocess.TimeoutExpired:
        logger.warning("LSP tool '%s' timed out", tool)
    except Exception as exc:
        logger.warning("LSP tool '%s' error: %s", tool, exc)
    finally:
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    return all_diagnostics
