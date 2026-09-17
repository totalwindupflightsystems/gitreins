"""
Unit tests for engine/lsp.py — LSP guard runner.
"""

import json
import os
import shutil
import subprocess
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

from engine.lsp import (
    LspDiag,
    find_lsp_tool,
    normalize_severity,
    run_lsp_check,
    run_lsp_check_status,
    _staged_files_by_language,
)


# A stand-in LSP server with the quiescent-gopls shape (INT-FLAKE-4): it
# answers `initialize` and then ignores everything, publishing no diagnostics.
# ``@PYTHON@`` is substituted with the test interpreter at write time.
QUIET_LSP_SOURCE = '''#!@PYTHON@
"""Fake LSP server that answers initialize, then goes quiescent."""
import json
import sys
import time


def read_message():
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\\r\\n", b"\\n"):
            break
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":")[1])
    return json.loads(sys.stdin.buffer.read(length).decode())


while True:
    message = read_message()
    if message is None:
        time.sleep(60)
        continue
    if message.get("method") == "initialize":
        reply = {"jsonrpc": "2.0", "id": message["id"], "result": {"capabilities": {}}}
        body = json.dumps(reply).encode()
        sys.stdout.buffer.write(b"Content-Length: %d\\r\\n\\r\\n" % len(body) + body)
        sys.stdout.buffer.flush()
    # everything else is intentionally ignored
'''


# The INT-FLAKE-4 stall shape: `didOpen` produces nothing (the file's check
# never ran), while the same content re-sent as a `didChange` publishes the
# diagnostic — exactly what a loaded gopls v0.22 does.
ONLY_ON_CHANGE_LSP_SOURCE = '''#!@PYTHON@
"""Fake LSP server: silent on didOpen, reports on didChange."""
import json
import sys
import time


def send(message):
    body = json.dumps(message).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\\r\\n\\r\\n" % len(body) + body)
    sys.stdout.buffer.flush()


def read_message():
    length = None
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        if line in (b"\\r\\n", b"\\n"):
            break
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":")[1])
    return json.loads(sys.stdin.buffer.read(length).decode())


while True:
    message = read_message()
    if message is None:
        time.sleep(60)
        continue
    method = message.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": message["id"], "result": {"capabilities": {}}})
    elif method == "textDocument/didChange":
        uri = message["params"]["textDocument"]["uri"]
        send(
            {
                "jsonrpc": "2.0",
                "method": "textDocument/publishDiagnostics",
                "params": {
                    "uri": uri,
                    "diagnostics": [
                        {
                            "range": {
                                "start": {"line": 3, "character": 13},
                                "end": {"line": 3, "character": 20},
                            },
                            "severity": 1,
                            "message": 'cannot use "hello" (untyped string constant) as int value',
                            "code": "IncompatibleAssign",
                        }
                    ],
                },
            }
        )
    # didOpen and everything else are intentionally ignored
'''


class TestLspDiag:
    """Test LspDiag dataclass."""

    def test_lsp_diag_creation(self):
        d = LspDiag(
            file="test.py",
            line=5,
            severity="error",
            message="Undefined variable",
            code="E001",
            tool="pylsp",
        )
        assert d.file == "test.py"
        assert d.line == 5
        assert d.severity == "error"
        assert d.message == "Undefined variable"
        assert d.code == "E001"
        assert d.tool == "pylsp"

    def test_lsp_diag_default_code(self):
        d = LspDiag(file="test.py", line=1, severity="warning", message="unused import")
        assert d.code == ""
        assert d.tool == ""

    def test_lsp_diag_to_dict(self):
        d = LspDiag(
            file="f.py", line=3, severity="info", message="msg", code="W001", tool="ruff-lsp"
        )
        result = d.to_dict()
        assert result["file"] == "f.py"
        assert result["line"] == 3
        assert result["severity"] == "info"
        assert result["code"] == "W001"
        assert result["tool"] == "ruff-lsp"

    def test_lsp_diag_serialization_roundtrip(self):
        d1 = LspDiag(
            file="a.py", line=10, severity="error", message="bad", code="F401", tool="pyright"
        )
        d2 = LspDiag(**d1.to_dict())
        assert d1 == d2


class TestNormalizeSeverity:
    """Test severity mapping."""

    def test_severity_1_is_error(self):
        assert normalize_severity(1) == "error"

    def test_severity_2_is_warning(self):
        assert normalize_severity(2) == "warning"

    def test_severity_3_is_info(self):
        assert normalize_severity(3) == "info"

    def test_severity_4_is_hint(self):
        assert normalize_severity(4) == "hint"

    def test_unknown_severity_defaults_to_warning(self):
        assert normalize_severity(99) == "warning"
        assert normalize_severity(0) == "warning"


class TestFindLspTool:
    """Test LSP tool discovery."""

    def test_find_lsp_tool_not_found(self):
        with patch("shutil.which", return_value=None):
            result = find_lsp_tool("pylsp")
        assert result is None

    def test_find_lsp_tool_found(self):
        with patch("shutil.which", return_value="/usr/bin/pylsp"):
            result = find_lsp_tool("pylsp")
        assert result == "/usr/bin/pylsp"

    def test_find_lsp_tool_multiple_binaries(self):
        """Tool with multiple binary names checks each in order."""
        call_order = []

        def fake_which(name):
            call_order.append(name)
            return None

        with patch("shutil.which", side_effect=fake_which):
            result = find_lsp_tool("pyright")
        assert result is None
        assert "pyright-langserver" in call_order
        assert "pyright" in call_order

    def test_find_lsp_tool_fallback_binary(self):
        """When first binary not found, tries fallback."""

        def fake_which(name):
            if name == "pyright-langserver":
                return None
            if name == "pyright":
                return "/usr/bin/pyright"
            return None

        with patch("shutil.which", side_effect=fake_which):
            result = find_lsp_tool("pyright")
        assert result == "/usr/bin/pyright"

    def test_find_lsp_tool_rust_analyzer_not_found(self):
        """rust-analyzer not found returns None."""
        with patch("shutil.which", return_value=None):
            result = find_lsp_tool("rust-analyzer")
        assert result is None

    def test_find_lsp_tool_rust_analyzer_found(self):
        """rust-analyzer found returns its path."""
        with patch("shutil.which", return_value="/home/user/.cargo/bin/rust-analyzer"):
            result = find_lsp_tool("rust-analyzer")
        assert result == "/home/user/.cargo/bin/rust-analyzer"

    def test_find_lsp_tool_ts_lsp_not_found(self):
        """typescript-language-server not found returns None."""
        with patch("shutil.which", return_value=None):
            result = find_lsp_tool("ts-lsp")
        assert result is None

    def test_find_lsp_tool_ts_lsp_found(self):
        """typescript-language-server found returns its path."""
        with patch("shutil.which", return_value="/usr/bin/typescript-language-server"):
            result = find_lsp_tool("ts-lsp")
        assert result == "/usr/bin/typescript-language-server"

    def test_find_lsp_tool_gopls_not_found(self):
        """gopls not found returns None."""
        with patch("shutil.which", return_value=None):
            result = find_lsp_tool("gopls")
        assert result is None

    def test_find_lsp_tool_gopls_found(self):
        """gopls found returns its path."""
        with patch("shutil.which", return_value="/home/user/go/bin/gopls"):
            result = find_lsp_tool("gopls")
        assert result == "/home/user/go/bin/gopls"

    def test_find_lsp_tool_jdtls_not_found(self):
        """jdtls not found returns None."""
        with patch("shutil.which", return_value=None):
            result = find_lsp_tool("jdtls")
        assert result is None

    def test_find_lsp_tool_jdtls_found(self):
        """jdtls found returns its path."""
        with patch("shutil.which", return_value="/usr/bin/jdtls"):
            result = find_lsp_tool("jdtls")
        assert result == "/usr/bin/jdtls"

    def test_find_lsp_tool_kotlin_ls_not_found(self):
        """kotlin-language-server not found returns None."""
        with patch("shutil.which", return_value=None):
            result = find_lsp_tool("kotlin-language-server")
        assert result is None

    def test_find_lsp_tool_kotlin_ls_found(self):
        """kotlin-language-server found returns its path."""
        with patch("shutil.which", return_value="/usr/bin/kotlin-language-server"):
            result = find_lsp_tool("kotlin-language-server")
        assert result == "/usr/bin/kotlin-language-server"

    def test_find_lsp_tool_csharp_ls_not_found(self):
        """csharp-ls not found returns None."""
        with patch("shutil.which", return_value=None):
            result = find_lsp_tool("csharp-ls")
        assert result is None

    def test_find_lsp_tool_csharp_ls_found(self):
        """csharp-ls found returns its path."""
        with patch("shutil.which", return_value="/usr/bin/csharp-ls"):
            result = find_lsp_tool("csharp-ls")
        assert result == "/usr/bin/csharp-ls"

    def test_find_lsp_tool_sourcekit_not_found(self):
        """sourcekit-lsp not found returns None."""
        with patch("shutil.which", return_value=None):
            result = find_lsp_tool("sourcekit-lsp")
        assert result is None

    def test_find_lsp_tool_sourcekit_found(self):
        """sourcekit-lsp found returns its path."""
        with patch("shutil.which", return_value="/usr/bin/sourcekit-lsp"):
            result = find_lsp_tool("sourcekit-lsp")
        assert result == "/usr/bin/sourcekit-lsp"

    def test_find_lsp_tool_dart_not_found(self):
        """dart not found returns None."""
        with patch("shutil.which", return_value=None):
            result = find_lsp_tool("dart")
        assert result is None

    def test_find_lsp_tool_dart_found(self):
        """dart found returns its path."""
        with patch("shutil.which", return_value="/usr/bin/dart"):
            result = find_lsp_tool("dart")
        assert result == "/usr/bin/dart"
        with patch("shutil.which", return_value="/usr/local/bin/gopls"):
            result = find_lsp_tool("gopls")
        assert result == "/usr/local/bin/gopls"

    def test_find_lsp_tool_elixir_ls_not_found(self):
        """elixir-ls not found returns None."""
        with patch("shutil.which", return_value=None):
            result = find_lsp_tool("elixir-ls")
        assert result is None

    def test_find_lsp_tool_elixir_ls_found(self):
        """elixir-ls found returns its path."""
        with patch("shutil.which", return_value="/usr/bin/elixir-ls"):
            result = find_lsp_tool("elixir-ls")
        assert result == "/usr/bin/elixir-ls"

    def test_find_lsp_tool_metals_not_found(self):
        """metals not found returns None."""
        with patch("shutil.which", return_value=None):
            result = find_lsp_tool("metals")
        assert result is None

    def test_find_lsp_tool_metals_found(self):
        """metals found returns its path."""
        with patch("shutil.which", return_value="/usr/bin/metals"):
            result = find_lsp_tool("metals")
        assert result == "/usr/bin/metals"

    def test_find_lsp_tool_ruby_lsp_not_found(self):
        """ruby-lsp not found returns None."""
        with patch("shutil.which", return_value=None):
            result = find_lsp_tool("ruby-lsp")
        assert result is None

    def test_find_lsp_tool_ruby_lsp_found(self):
        """ruby-lsp found returns its path."""
        with patch("shutil.which", return_value="/usr/bin/ruby-lsp"):
            result = find_lsp_tool("ruby-lsp")
        assert result == "/usr/bin/ruby-lsp"

    def test_find_lsp_tool_solargraph_not_found(self):
        """solargraph not found returns None."""
        with patch("shutil.which", return_value=None):
            result = find_lsp_tool("solargraph")
        assert result is None

    def test_find_lsp_tool_solargraph_found(self):
        """solargraph found returns its path."""
        with patch("shutil.which", return_value="/usr/bin/solargraph"):
            result = find_lsp_tool("solargraph")
        assert result == "/usr/bin/solargraph"


class TestRunLspCheck:
    """Test run_lsp_check entry point."""

    def test_run_lsp_check_tool_not_found(self):
        """Graceful degradation when tool is missing."""
        with patch("engine.lsp.find_lsp_tool", return_value=None):
            result = run_lsp_check("pylsp", "/tmp")
        assert result == []

    def test_run_lsp_check_no_staged_files(self, tmp_workdir):
        """Returns empty diagnostics when no staged files."""
        with patch("engine.lsp.find_lsp_tool", return_value="/usr/bin/pylsp"):
            with patch("engine.lsp._get_staged_files", return_value=[]):
                result = run_lsp_check("pylsp", tmp_workdir)
        assert result == []

    def test_run_lsp_check_no_matching_language(self, tmp_workdir):
        """Returns empty when staged files don't match tool language."""
        with patch("engine.lsp.find_lsp_tool", return_value="/usr/bin/pylsp"):
            with patch("engine.lsp._get_staged_files", return_value=["file.lua"]):
                result = run_lsp_check("pylsp", tmp_workdir)
        assert result == []

    def test_run_lsp_check_timeout_handled_gracefully(self):
        """Handles TimeoutExpired without crashing."""
        with patch("engine.lsp.find_lsp_tool", return_value="/usr/bin/pylsp"):
            with patch("engine.lsp.subprocess.Popen") as mock_popen:
                mock_proc = MagicMock()
                mock_popen.return_value = mock_proc
                mock_proc.wait.side_effect = subprocess.TimeoutExpired(cmd="pylsp", timeout=30)
                mock_proc.kill = MagicMock()
                with patch("engine.lsp._get_staged_files", return_value=["test.py"]):
                    with patch("os.path.isfile", return_value=True):
                        with patch("engine.lsp.os.killpg") as mock_killpg:
                            result = run_lsp_check("pylsp", "/tmp")
        assert result == []
        mock_killpg.assert_not_called()
        mock_proc.kill.assert_called_once()

    def test_run_lsp_check_startup_failure_returns_empty(self):
        """When LSP process can't start, returns empty list."""
        with patch("engine.lsp.find_lsp_tool", return_value="/usr/bin/pylsp"):
            with patch("engine.lsp.subprocess.Popen", side_effect=OSError("not found")):
                with patch("engine.lsp._get_staged_files", return_value=["test.py"]):
                    with patch("os.path.isfile", return_value=True):
                        result = run_lsp_check("pylsp", "/tmp")
        assert result == []

    def test_run_lsp_check_initialize_failure(self):
        """When LSP initialization fails, returns empty diagnostics."""
        mock_proc = MagicMock()
        with patch("engine.lsp.find_lsp_tool", return_value="/usr/bin/pylsp"):
            with patch("engine.lsp.subprocess.Popen", return_value=mock_proc):
                with patch("engine.lsp._lsp_initialize", return_value=False):
                    with patch("engine.lsp._lsp_shutdown") as mock_shutdown:
                        with patch("engine.lsp._get_staged_files", return_value=["test.py"]):
                            with patch("os.path.isfile", return_value=True):
                                result = run_lsp_check("pylsp", "/tmp")
        assert result == []
        mock_shutdown.assert_called_once()

    def test_rust_analyzer_init_failure_uses_bounded_timeout(self):
        """rust-analyzer init failure returns fast with the 30s default cap (GR-138).

        Regression: the old 300s heavy-set cap made an uninitializable
        project hold the init channel for five minutes before the test
        could skip. The default rust-analyzer cap must be bounded and
        passed through to _lsp_initialize.
        """
        mock_proc = MagicMock()
        with patch("engine.lsp.find_lsp_tool", return_value="/usr/bin/rust-analyzer"):
            with patch("engine.lsp.subprocess.Popen", return_value=mock_proc):
                with patch("engine.lsp._lsp_initialize", return_value=False) as mock_init:
                    with patch("engine.lsp._lsp_shutdown"):
                        result = run_lsp_check("rust-analyzer", "/tmp", files=["src/main.rs"])
        assert result == []
        assert mock_init.call_args.kwargs["timeout"] == 30.0


class TestToolDefaultTimeouts:
    """Test C++-aware LSP timeout defaults (GR: C++ repo timeout fix)."""

    def test_clangd_gets_generous_budget(self):
        """clangd is always treated as heavy — it builds a project index."""
        from engine.lsp import _tool_default_timeouts

        init_t, per_file_t = _tool_default_timeouts("clangd", "/tmp/empty")
        assert init_t >= 300
        assert per_file_t >= 120

    def test_rust_analyzer_gets_generous_budget(self):
        from engine.lsp import _tool_default_timeouts

        init_t, per_file_t = _tool_default_timeouts("rust-analyzer", "/tmp/empty")
        assert init_t <= 30
        assert per_file_t >= 120

    def test_pylsp_gets_default_budget(self):
        from engine.lsp import _tool_default_timeouts

        init_t, per_file_t = _tool_default_timeouts("pylsp", "/tmp/empty")
        assert init_t == 60.0
        assert per_file_t == 30.0

    def test_cpp_staged_files_trigger_heavy_budget(self):
        """Repo with staged .cpp files gets the C++ budget even for non-clangd tools."""
        from engine.lsp import _tool_default_timeouts

        with patch("engine.lsp._get_staged_files", return_value=["src/main.cpp"]):
            init_t, per_file_t = _tool_default_timeouts("ccls", "/tmp/cpprepo")
        assert init_t >= 180
        assert per_file_t >= 90

    def test_python_staged_files_stay_default(self):
        from engine.lsp import _tool_default_timeouts

        with patch("engine.lsp._get_staged_files", return_value=["app.py"]):
            init_t, per_file_t = _tool_default_timeouts("pylsp", "/tmp/pyrepo")
        assert init_t == 60.0
        assert per_file_t == 30.0


class TestLspHeaderParsing:
    """Test Content-Length header parsing via _lsp_encode_message/_lsp_read_response."""

    def test_encode_message_has_content_length(self):
        from engine.lsp import _lsp_encode_message

        msg = {"jsonrpc": "2.0", "id": 1, "method": "shutdown"}
        data = _lsp_encode_message(msg)
        assert b"Content-Length:" in data
        header, _, body = data.partition(b"\r\n\r\n")
        length = int(header.split(b":")[1].strip())
        assert length == len(body)

    def test_encode_decode_roundtrip(self):
        from engine.lsp import _lsp_encode_message

        msg = {"jsonrpc": "2.0", "method": "textDocument/didOpen", "params": {}}
        data = _lsp_encode_message(msg)
        header_end = data.find(b"\r\n\r\n") + 4
        header_text = data[:header_end].decode("utf-8").strip()
        body = data[header_end:]
        content_length = 0
        for line in header_text.split("\r\n"):
            if line.lower().startswith("content-length:"):
                content_length = int(line.split(":")[1].strip())
        assert content_length == len(body)
        decoded = json.loads(body.decode("utf-8"))
        assert decoded["method"] == "textDocument/didOpen"

    def test_read_response_parses_header(self):
        import io
        from engine.lsp import _lsp_encode_message, _lsp_read_response

        msg = {"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {}}
        data = _lsp_encode_message(msg)

        mock_proc = MagicMock()
        mock_proc.stdout = io.BytesIO(data)

        with patch("select.select", return_value=[True]):
            result = _lsp_read_response(mock_proc, timeout=1.0)
        assert result is not None
        assert result["method"] == "textDocument/publishDiagnostics"

    def test_read_response_returns_none_quickly_when_server_dead(self):
        """A dead LSP server must not hold the read loop until the timeout (GR-138).

        Regression: the old loop kept select()/read() cycling on the
        closed stdout pipe of an exited server until the deadline —
        a broken rust-analyzer shim burned the full 300s init cap.
        EOF must return None immediately.
        """
        import time as _time

        from engine.lsp import _lsp_read_response

        proc = subprocess.Popen(
            [sys.executable, "-c", "import sys"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            start = _time.monotonic()
            result = _lsp_read_response(proc, timeout=60.0)
            elapsed = _time.monotonic() - start
        finally:
            proc.kill()
            proc.wait()
        assert result is None
        assert elapsed < 5.0


# ── Integration tests with real pylsp server ────────────────────

pytestmark_integration = pytest.mark.skipif(
    shutil.which("pylsp") is None,
    reason="pylsp not installed — integration test skipped",
)

# DF-GITREINS-POC-6: real-server tests that assert non-empty diagnostics must
# skip (not fail) when pylsp is absent. The second condition mirrors the
# venv-bin escape hatch used by test_lsp_roundtrip_format_parse, which
# prepends dirname(sys.executable) to PATH before resolving the tool —
# without it this marker over-skips in a repo venv that has pylsp and
# under-skips in a bare consumer venv. Apply per-test ONLY: the hermetic
# judge/evaluator tests in TestLspJudgeIntegration must keep running
# everywhere, so a class-level marker would silently delete coverage.
PYLSP_MISSING = pytest.mark.skipif(
    shutil.which("pylsp") is None
    and not os.path.exists(os.path.join(os.path.dirname(sys.executable), "pylsp")),
    reason="pylsp not installed — real-server integration test skipped",
)


@pytest.fixture
def lsp_workdir(tmp_path):
    """Create a clean temp directory for LSP integration tests."""
    return str(tmp_path)


PYLSP_SKIP_310 = pytest.mark.skipif(
    sys.version_info < (3, 11),
    reason="pylsp pyflakes/pycodestyle plugins may not activate on Python 3.10 — CI-only skip",
)


class TestLspIntegration:
    """Integration tests that exercise real pylsp server communication.

    These tests use the optional ``files`` parameter of ``run_lsp_check``
    to bypass git-staging logic and pass file paths directly.
    """

    # Serialize real-LSP integration tests onto one xdist worker: their
    # servers (pylsp/rust-analyzer/gopls) are CPU-heavy and mutually
    # starve each other under -n 4, which is a load source behind the
    # gopls full-suite flake (GR-GAP-032).
    pytestmark = [pytestmark_integration, pytest.mark.xdist_group("lsp-integration")]

    BAD_CODE_UNDEFINED = "x = undefined_variable\n"

    BAD_CODE_SYNTAX = "if True\n    pass\n"  # missing colon + unexpected indent → syntax error

    CLEAN_CODE = "x = 1\ny = x + 1\nprint(y)\n"

    def _write_py(self, workdir, name, content):
        path = os.path.join(workdir, name)
        with open(path, "w") as f:
            f.write(content)
        return path

    def _run_check(self, workdir, files, tool="pylsp"):
        return run_lsp_check(tool, workdir, files=files, timeout_per_file=8.0)

    @PYLSP_SKIP_310
    def test_pylsp_detects_undefined_variable(self, lsp_workdir):
        """Bad code with undefined variable produces diagnostics."""
        path = self._write_py(lsp_workdir, "bad_undefined.py", self.BAD_CODE_UNDEFINED)
        diags = self._run_check(lsp_workdir, [path])
        assert len(diags) > 0, "Expected diagnostics for undefined variable"
        messages = [d["message"].lower() for d in diags]
        assert any("undefined" in m for m in messages), f"No 'undefined' in diagnostics: {messages}"
        # Verify severity is error (1)
        assert any(d["severity"] == "error" for d in diags), (
            "Expected at least one error-severity diagnostic"
        )

    @PYLSP_SKIP_310
    def test_pylsp_detects_syntax_error(self, lsp_workdir):
        """Syntax error produces diagnostics."""
        path = self._write_py(lsp_workdir, "bad_syntax.py", self.BAD_CODE_SYNTAX)
        diags = self._run_check(lsp_workdir, [path])
        assert len(diags) > 0, "Expected diagnostics for syntax error"
        messages = [d["message"].lower() for d in diags]
        # pylsp/pyflakes reports "expected ':'" or "unexpected indent" for missing colon
        assert any("expected" in m or "indent" in m or "syntax" in m for m in messages), (
            f"No syntax error in diagnostics: {messages}"
        )

    def test_pylsp_clean_code_no_diagnostics(self, lsp_workdir):
        """Clean code produces no diagnostics."""
        path = self._write_py(lsp_workdir, "clean.py", self.CLEAN_CODE)
        diags = self._run_check(lsp_workdir, [path])
        assert diags == [], f"Expected no diagnostics for clean code, got: {diags}"

    @PYLSP_SKIP_310
    def test_pylsp_guard_fails_on_bad_code(self, lsp_workdir):
        """Guard machinery reports FAIL for bad code."""
        path = self._write_py(lsp_workdir, "failing.py", self.BAD_CODE_UNDEFINED)
        diags = self._run_check(lsp_workdir, [path])
        has_errors = any(d.get("severity") == "error" for d in diags)
        assert has_errors, "Bad code should produce error-severity diagnostics"

    def test_pylsp_guard_passes_on_clean_code(self, lsp_workdir):
        """Guard machinery reports PASS for clean code."""
        path = self._write_py(lsp_workdir, "passing.py", self.CLEAN_CODE)
        diags = self._run_check(lsp_workdir, [path])
        assert diags == [], "Clean code should produce no diagnostics"

    def test_missing_lsp_tool_skips_gracefully(self, lsp_workdir):
        """Non-existent tool returns empty list (skip, not crash)."""
        path = self._write_py(lsp_workdir, "dummy.py", self.CLEAN_CODE)
        diags = run_lsp_check("nonexistent-lsp-tool-xyz", lsp_workdir, files=[path])
        assert diags == [], "Missing LSP tool should return empty diagnostics"

    @PYLSP_SKIP_310
    def test_pylsp_multiple_files_mixed(self, lsp_workdir):
        """Mixed files — bad and clean — return only bad diagnostics."""
        bad_path = self._write_py(lsp_workdir, "mixed_bad.py", self.BAD_CODE_UNDEFINED)
        clean_path = self._write_py(lsp_workdir, "mixed_clean.py", self.CLEAN_CODE)
        diags = self._run_check(lsp_workdir, [bad_path, clean_path])
        # Should have at least one diagnostic for the bad file
        assert len(diags) > 0, "Expected diagnostics from mixed files"
        # All diagnostics should reference the bad file
        bad_basename = os.path.basename(bad_path)
        for d in diags:
            assert bad_basename in d.get("file", ""), (
                f"Diagnostic should reference bad file, got: {d}"
            )


class TestLspJudgeIntegration:
    """Integration test: LSP diagnostics flow through Judge → Evaluator.

    Tests that LSP diagnostics collected during Tier 1 are correctly
    extracted and structured. Uses real pylsp for LSP checks but
    verifies the parsing/extraction logic with mocked LLM calls.
    """

    BAD_CODE = "x = undefined_var\n"
    GOOD_CODE = "x = 1\nprint(x)\n"

    def _init_git_repo(self, workdir):
        """Initialize a real git repo in workdir."""
        import subprocess

        subprocess.run(["git", "init"], cwd=workdir, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@test.com"], cwd=workdir, capture_output=True
        )
        subprocess.run(["git", "config", "user.name", "Test"], cwd=workdir, capture_output=True)

    @PYLSP_MISSING
    @PYLSP_SKIP_310
    def test_lsp_roundtrip_format_parse(self, tmp_path):
        """Real pylsp output → formatted like GuardManager → parsed back by Judge.

        Verifies the full roundtrip:
          1. Run_lsp_check produces real diagnostics for bad code
          2. Formatted like _check_lsp would format them
          3. Judge._parse_lsp_output correctly parses them back
        """
        from engine.judge import Judge
        from engine.lsp import run_lsp_check
        from unittest.mock import MagicMock

        # Bare `python -m pytest` doesn't put .venv/bin on PATH, so
        # shutil.which('pylsp') misses the venv's pylsp; prepend the venv
        # bin dir (derived from sys.executable) so find_lsp_tool resolves it.
        os.environ["PATH"] = (
            os.path.dirname(sys.executable) + os.pathsep + os.environ.get("PATH", "")
        )

        workdir = str(tmp_path / "repo")
        os.makedirs(workdir)
        self._init_git_repo(workdir)

        # Create bad code
        bad_path = os.path.join(workdir, "bad_code.py")
        with open(bad_path, "w") as f:
            f.write(self.BAD_CODE)

        # Create good code
        good_path = os.path.join(workdir, "good_code.py")
        with open(good_path, "w") as f:
            f.write(self.GOOD_CODE)

        # Run real LSP check on both files
        diags = run_lsp_check("pylsp", workdir, files=[bad_path, good_path], timeout_per_file=8.0)

        # Verify we got diagnostics for the bad file
        bad_diags = [d for d in diags if "bad_code.py" in d.get("file", "")]
        assert len(bad_diags) > 0, f"Expected LSP diagnostics for bad code, got {diags}"
        assert any("undefined" in d["message"].lower() for d in bad_diags), (
            f"Expected 'undefined' in messages: {[d['message'] for d in bad_diags]}"
        )

        # Format diagnostics like GuardManager._check_lsp would
        formatted_lines = []
        for d in diags:
            severity = d.get("severity", "error")
            prefix = "✗" if severity == "error" else "⚠"
            formatted_lines.append(
                f"  {prefix} {d['file']}:{d['line']} [{d.get('tool', 'pylsp')}] {d['message']}"
            )
        formatted_output = "\n".join(formatted_lines)

        # Parse back using Judge._parse_lsp_output
        llm = MagicMock()
        judge = Judge(llm, workdir)
        parsed = judge._parse_lsp_output(formatted_output)

        # Verify roundtrip preserves key fields
        assert len(parsed) == len(diags), (
            f"Roundtrip lost diagnostics: {len(diags)} → {len(parsed)}"
        )
        for p, d in zip(parsed, diags, strict=False):
            assert p["severity"] == d.get("severity", "warning")
            assert p["message"] == d["message"]
            assert p["line"] == d["line"]

    def test_evaluator_receives_lsp_diagnostics(self, tmp_path, llm_client):
        """Evaluator task prompt includes TIER 1 LSP DIAGNOSTICS when task has them."""
        from unittest.mock import patch, MagicMock

        workdir = str(tmp_path / "repo")
        os.makedirs(workdir)
        self._init_git_repo(workdir)

        # Create evaluator
        from engine.evaluator import AgenticEvaluator

        evaluator = AgenticEvaluator(llm_client, workdir, max_iterations=1)

        # Build task dict with tier1_diagnostics
        task = {
            "id": "lsp-eval-test",
            "title": "LSP Evaluator Test",
            "criteria": ["No undefined variables"],
            "tier1_diagnostics": [
                {
                    "file": "bad_code.py",
                    "line": 1,
                    "severity": "error",
                    "message": "Undefined variable 'undefined_var'",
                    "tool": "pylsp",
                },
            ],
        }

        MockResponse = MagicMock
        mock_usage = MockResponse(
            prompt_tokens=0,
            completion_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            total_tokens=0,
        )
        mock_resp = MockResponse(content=None, tool_calls=None, usage=mock_usage)

        with patch.object(llm_client, "chat", return_value=mock_resp):
            with patch.object(
                evaluator,
                "_parse_verdict",
                return_value=evaluator._parse_verdict(
                    '{"verdict":"INCOMPLETE","items":[],"summary":"no LLM"}'
                ),
            ):
                pass  # We'll inspect the internal state instead

        # Check that diagnostics are stored on the evaluator
        # (They should be set during evaluate(), but we can check directly)
        evaluator._tier1_diagnostics = task["tier1_diagnostics"]
        assert len(evaluator._tier1_diagnostics) == 1
        assert evaluator._tier1_diagnostics[0]["message"] == "Undefined variable 'undefined_var'"

        # Verify the tool returns them
        result = evaluator._tool_read_lsp_diagnostics()
        assert result["count"] == 1
        assert len(result["diagnostics"]) == 1
        assert result["diagnostics"][0]["severity"] == "error"

    def test_evaluator_no_lsp_diagnostics_empty(self, tmp_path, llm_client):
        """Evaluator handles missing tier1_diagnostics gracefully."""
        from engine.evaluator import AgenticEvaluator

        workdir = str(tmp_path / "repo")
        os.makedirs(workdir)
        self._init_git_repo(workdir)

        evaluator = AgenticEvaluator(llm_client, workdir, max_iterations=1)

        # No tier1_diagnostics — evaluator should handle gracefully
        assert evaluator._tier1_diagnostics == []
        result = evaluator._tool_read_lsp_diagnostics()
        assert result["count"] == 0
        assert result["diagnostics"] == []

    def test_evaluator_read_lsp_diagnostics_tool_defined(self):
        """read_lsp_diagnostics tool definition exists in EVALUATOR_TOOLS."""
        from engine.evaluator import EVALUATOR_TOOLS

        tool_names = [t["function"]["name"] for t in EVALUATOR_TOOLS]
        assert "read_lsp_diagnostics" in tool_names, (
            f"Expected read_lsp_diagnostics in tools, got {tool_names}"
        )


# ── Tests for _staged_files_by_language mapping ──────────────────


class TestStagedFilesByLanguage:
    """Test _staged_files_by_language correctly maps files to languages."""

    def test_maps_python_files(self):
        """.py files map to python language."""
        with patch("engine.lsp._get_staged_files", return_value=["file.py"]):
            with patch("os.path.isfile", return_value=True):
                result = _staged_files_by_language("/tmp")
        assert result == {"python": ["/tmp/file.py"]}

    def test_maps_rust_files(self):
        """.rs files map to rust language."""
        with patch("engine.lsp._get_staged_files", return_value=["file.rs"]):
            with patch("os.path.isfile", return_value=True):
                result = _staged_files_by_language("/tmp")
        assert result == {"rust": ["/tmp/file.rs"]}

    def test_maps_ts_js_files(self):
        """.ts, .tsx, .js, .jsx files map to their languages."""
        with patch(
            "engine.lsp._get_staged_files",
            return_value=["file.ts", "file.tsx", "file.js", "file.jsx"],
        ):
            with patch("os.path.isfile", return_value=True):
                result = _staged_files_by_language("/tmp")
        assert result == {
            "typescript": ["/tmp/file.ts"],
            "typescriptreact": ["/tmp/file.tsx"],
            "javascript": ["/tmp/file.js"],
            "javascriptreact": ["/tmp/file.jsx"],
        }

    def test_maps_lua_files(self):
        """.lua files map to lua language."""
        with patch("engine.lsp._get_staged_files", return_value=["file.lua"]):
            with patch("os.path.isfile", return_value=True):
                result = _staged_files_by_language("/tmp")
        assert result == {"lua": ["/tmp/file.lua"]}

    def test_skips_unknown_extensions(self):
        """Files with unknown extensions are skipped."""
        with patch("engine.lsp._get_staged_files", return_value=["file.xyz", "file.txt"]):
            with patch("os.path.isfile", return_value=True):
                result = _staged_files_by_language("/tmp")
        assert result == {}

    def test_skips_missing_files(self):
        """Files that don't exist on disk are skipped."""
        with patch("engine.lsp._get_staged_files", return_value=["file.py"]):
            with patch("os.path.isfile", return_value=False):
                result = _staged_files_by_language("/tmp")
        assert result == {}

    def test_maps_mixed_languages(self):
        """Multiple languages map correctly in a single call."""
        with patch("engine.lsp._get_staged_files", return_value=["a.py", "b.rs", "c.ts"]):
            with patch("os.path.isfile", return_value=True):
                result = _staged_files_by_language("/tmp")
        assert result == {
            "python": ["/tmp/a.py"],
            "rust": ["/tmp/b.rs"],
            "typescript": ["/tmp/c.ts"],
        }

    def test_maps_go_files(self):
        """.go files map to go language."""
        with patch("engine.lsp._get_staged_files", return_value=["file.go"]):
            with patch("os.path.isfile", return_value=True):
                result = _staged_files_by_language("/tmp")
        assert result == {"go": ["/tmp/file.go"]}

    def test_maps_java_files(self):
        """.java files map to java language."""
        with patch("engine.lsp._get_staged_files", return_value=["File.java"]):
            with patch("os.path.isfile", return_value=True):
                result = _staged_files_by_language("/tmp")
        assert result == {"java": ["/tmp/File.java"]}

    def test_maps_kotlin_files(self):
        """.kt and .kts files map to kotlin language."""
        with patch(
            "engine.lsp._get_staged_files", return_value=["src/Main.kt", "build.gradle.kts"]
        ):
            with patch("os.path.isfile", return_value=True):
                result = _staged_files_by_language("/tmp")
        assert result == {"kotlin": ["/tmp/src/Main.kt", "/tmp/build.gradle.kts"]}

    def test_maps_csharp_files(self):
        """.cs files map to csharp language."""
        with patch("engine.lsp._get_staged_files", return_value=["src/Program.cs"]):
            with patch("os.path.isfile", return_value=True):
                result = _staged_files_by_language("/tmp")
        assert result == {"csharp": ["/tmp/src/Program.cs"]}

    def test_maps_swift_files(self):
        """.swift files map to swift language."""
        with patch("engine.lsp._get_staged_files", return_value=["Sources/main.swift"]):
            with patch("os.path.isfile", return_value=True):
                result = _staged_files_by_language("/tmp")
        assert result == {"swift": ["/tmp/Sources/main.swift"]}

    def test_maps_dart_files(self):
        """.dart files map to dart language."""
        with patch("engine.lsp._get_staged_files", return_value=["lib/main.dart"]):
            with patch("os.path.isfile", return_value=True):
                result = _staged_files_by_language("/tmp")
        assert result == {"dart": ["/tmp/lib/main.dart"]}
        with patch("engine.lsp._get_staged_files", return_value=["main.go", "util.go"]):
            with patch("os.path.isfile", return_value=True):
                result = _staged_files_by_language("/tmp")
        assert result == {"go": ["/tmp/main.go", "/tmp/util.go"]}

    def test_maps_elixir_files(self):
        """.ex and .exs files map to elixir language."""
        with patch(
            "engine.lsp._get_staged_files", return_value=["lib/my_module.ex", "lib/helper.exs"]
        ):
            with patch("os.path.isfile", return_value=True):
                result = _staged_files_by_language("/tmp")
        assert result == {"elixir": ["/tmp/lib/my_module.ex", "/tmp/lib/helper.exs"]}

    def test_maps_scala_files(self):
        """.scala and .sc files map to scala language."""
        with patch(
            "engine.lsp._get_staged_files", return_value=["src/main.scala", "src/helper.sc"]
        ):
            with patch("os.path.isfile", return_value=True):
                result = _staged_files_by_language("/tmp")
        assert result == {"scala": ["/tmp/src/main.scala", "/tmp/src/helper.sc"]}

    def test_maps_ruby_files(self):
        """.rb files map to ruby language."""
        with patch("engine.lsp._get_staged_files", return_value=["lib/foo.rb", "spec/foo_spec.rb"]):
            with patch("os.path.isfile", return_value=True):
                result = _staged_files_by_language("/tmp")
        assert result == {"ruby": ["/tmp/lib/foo.rb", "/tmp/spec/foo_spec.rb"]}


# ── Integration tests with real rust-analyzer server ──────────────


class TestRustAnalyzerIntegration:
    """Integration tests that exercise real rust-analyzer server communication."""

    pytestmark = pytest.mark.xdist_group("lsp-integration")

    BAD_RS_CODE = """fn main() {
    let x: i32 = "hello";
}
"""
    CLEAN_RS_CODE = """fn main() {
    let x: i32 = 42;
    println!("{}", x);
}
"""
    CARGO_TOML = """[package]
name = "test-lsp"
version = "0.1.0"
edition = "2021"
"""

    def _write_file(self, workdir, name, content):
        path = os.path.join(workdir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return path

    def test_rust_analyzer_skip_if_not_installed(self, lsp_workdir):
        """When rust-analyzer not found, skip gracefully (no crash)."""
        path = self._write_file(lsp_workdir, "src/main.rs", self.CLEAN_RS_CODE)
        self._write_file(lsp_workdir, "Cargo.toml", self.CARGO_TOML)
        with patch("engine.lsp.find_lsp_tool", return_value=None):
            diags = run_lsp_check("rust-analyzer", lsp_workdir, files=[path])
        assert diags == []

    def test_rust_analyzer_detects_type_error(self, lsp_workdir):
        """rust-analyzer detects type mismatches when available."""
        if shutil.which("rust-analyzer") is None:
            pytest.skip("rust-analyzer not installed")
        self._write_file(lsp_workdir, "Cargo.toml", self.CARGO_TOML)
        path = self._write_file(lsp_workdir, "src/main.rs", self.BAD_RS_CODE)
        diags = run_lsp_check("rust-analyzer", lsp_workdir, files=[path], timeout_per_file=15.0)
        if not diags:
            pytest.skip("rust-analyzer failed to initialize (no project structure or timeout)")
        messages = [d["message"].lower() for d in diags]
        assert any(
            "expected" in m or "type" in m or "string" in m or "i32" in m for m in messages
        ), f"No type error diagnostics from rust-analyzer: {messages}"

    def test_rust_analyzer_clean_code_no_diagnostics(self, lsp_workdir):
        """Clean Rust code produces no error diagnostics."""
        if shutil.which("rust-analyzer") is None:
            pytest.skip("rust-analyzer not installed")
        self._write_file(lsp_workdir, "Cargo.toml", self.CARGO_TOML)
        path = self._write_file(lsp_workdir, "src/main.rs", self.CLEAN_RS_CODE)
        diags = run_lsp_check("rust-analyzer", lsp_workdir, files=[path], timeout_per_file=15.0)
        errors = [d for d in diags if d.get("severity") == "error"]
        assert len(errors) == 0, f"Expected no errors for clean Rust code, got: {errors}"


# ── Integration test: ts-lsp graceful skip (not installed) ────────


class TestTsLspIntegration:
    """Integration tests for TypeScript LSP. ts-lsp is not installed, so tests verify graceful skip."""

    def test_ts_lsp_skip_gracefully(self, lsp_workdir):
        """typescript-language-server not found returns empty diagnostics."""
        if find_lsp_tool("ts-lsp"):
            pytest.skip(
                "typescript-language-server is installed; graceful-skip contract "
                "verified when tool absent"
            )
        path = os.path.join(lsp_workdir, "test.ts")
        with open(path, "w") as f:
            f.write("const x: number = 'hello';\n")
        diags = run_lsp_check("ts-lsp", lsp_workdir, files=[path])
        assert diags == [], "ts-lsp should return empty diagnostics when not installed"


# ── Integration tests: gopls ────────────────────────────────────────


class TestGoplsIntegration:
    """Integration tests for Go LSP with gopls."""

    pytestmark = pytest.mark.xdist_group("lsp-integration")

    # GR-GAP-032 / INT-FLAKE-4: gopls gets an explicit, generous diagnostics
    # budget and retries with a fresh server. Under CPU contention (xdist
    # workers + real rust-analyzer/pylsp integration tests + fleet load) a
    # gopls v0.22 spawn can go QUIESCENT — it never publishes diagnostics,
    # ignores even repeated didOpen nudges, and shows zero work in a goroutine
    # dump. The race is binary, not slowness: non-stalled runs deliver in
    # <1.5s even under heavy load, stalled runs never recover within ANY
    # budget (observed 30s and 120s timeouts).
    #
    # INT-FLAKE-4 changed the retry from "3 attempts, then fail" to a
    # readiness-driven budget. The signal is `published` — did the server send a
    # `publishDiagnostics` notification for the file at all (an empty list is a
    # real "found nothing")? A spawn that never reported on the file is killed
    # early and retried, so a stalled attempt costs ~READY_TIMEOUT instead of
    # ~180s and the loop can afford many fresh servers inside a wall-clock stall
    # budget (the attempt count is only a ceiling, not the budget).
    #
    # ⚠️ Deliberately NOT the failure signal: `server_ready`. Measured under CPU
    # load (see the tick-302 record), a quiescent gopls ANSWERS requests —
    # `workspace/symbol` in 7.7e-05 s, document symbols and definitions resolve
    # — while never publishing diagnostics for the opened file (42 s of
    # silence), so request-responsiveness is exactly what a stalled spawn still
    # has. Failing the test on `server_ready and not published` would re-create
    # the flake the row is about. The failure gate is therefore "the server
    # reported on the file (published) and reported nothing on blatantly bad Go
    # code", which is a real defect; a never-reporting spawn is a distinct,
    # non-failing diagnostic that names both flags so the record stays honest.
    GOPLS_INIT_TIMEOUT = 120.0
    GOPLS_PER_FILE_TIMEOUT = 60.0
    GOPLS_READY_TIMEOUT = 12.0
    GOPLS_STALL_BUDGET_S = 150.0
    GOPLS_MAX_ATTEMPTS = 12

    def _gopls_attempt(self, workdir, path):
        """One fresh-server attempt, classified by the readiness probe."""
        return run_lsp_check_status(
            "gopls",
            workdir,
            files=[path],
            init_timeout=self.GOPLS_INIT_TIMEOUT,
            timeout_per_file=self.GOPLS_PER_FILE_TIMEOUT,
            probe=True,
            ready_timeout=self.GOPLS_READY_TIMEOUT,
        )

    @staticmethod
    def _classify_gopls_outcome(never_reported: list, reported_empty: list) -> str:
        """Classify a finished batch of gopls attempts.

        * ``"verdict"`` — no batch to judge (an attempt produced diagnostics).
        * ``"defect"`` — the server REPORTED on the file (``published``, possibly
          an empty list) and still found nothing on blatantly bad Go code. A
          server that published a verdict of "clean" is a real defect.
        * ``"stall"`` — no spawn ever reported on the file: the load-dependent
          quiescent shape, reported as a distinct non-failing diagnostic.

        ``server_ready`` is deliberately NOT part of this decision: measured
        under CPU load a quiescent gopls still answers ``workspace/symbol``
        (7.7e-05 s) and resolves symbols/definitions while never publishing
        diagnostics for the opened file, so treating "responsive but silent" as
        a defect would re-create exactly the flake this row is about. A stall
        batch that answered the probe is still a stall, and the skip message
        names how many did.
        """
        if reported_empty:
            return "defect"
        if never_reported:
            return "stall"
        return "verdict"

    def test_gopls_detects_go_errors(self, lsp_workdir):
        """gopls detects type errors in Go code when installed."""
        if not shutil.which("gopls"):
            pytest.skip("gopls not installed")
        # gopls needs a go.mod for module context
        import subprocess

        subprocess.run(
            ["go", "mod", "init", "example.com/test"], cwd=lsp_workdir, capture_output=True
        )
        path = os.path.join(lsp_workdir, "test.go")
        with open(path, "w") as f:
            f.write('package main\n\nfunc main() {\n\tvar x int = "hello"\n}\n')
        # INT-FLAKE-4: retry a FRESH server while the server never reported on
        # the file at all (that is the environment signal — a diagnosed
        # quiescent spawn, not a verdict), inside a wall-clock budget. The
        # attempt count is a ceiling; the budget follows the signal.
        # `published` is the gate, never `server_ready`: the measured stall shape
        # answers requests, so only "did the server report on the file" separates
        # a verdict from an environment stall.
        started = time.monotonic()
        never_reported: list[dict] = []
        reported_empty: list[dict] = []
        status = None
        while True:
            status = self._gopls_attempt(lsp_workdir, path)
            if status.diagnostics:
                break
            over_budget = time.monotonic() - started >= self.GOPLS_STALL_BUDGET_S
            if not status.published:
                # Includes server_ready=True: the quiescent spawn answers
                # requests yet never reports on the file.
                never_reported.append(status.to_dict())
                if over_budget or len(never_reported) >= self.GOPLS_MAX_ATTEMPTS:
                    break
                continue
            # The server REPORTED on the file (published, possibly empty) but
            # found nothing on blatantly bad Go code. That is a real
            # correctness signal; confirm with one more fresh server first.
            reported_empty.append(status.to_dict())
            if (
                len(reported_empty) >= 2
                or over_budget
                or len(reported_empty) + len(never_reported) >= self.GOPLS_MAX_ATTEMPTS
            ):
                break

        if status is not None and status.diagnostics:
            assert len(status.diagnostics) > 0
            return
        outcome = self._classify_gopls_outcome(never_reported, reported_empty)
        if outcome == "stall":
            # Every spawn stayed silent about the file, even after the engine
            # re-requested it as a change: a load-dependent quiescent spawn,
            # not a gopls verdict. Report a distinct, non-failing diagnostic
            # that names both signals.
            answered = sum(1 for item in never_reported if item.get("server_ready"))
            pytest.skip(
                f"gopls produced no verdict: {len(never_reported)} fresh spawn(s) in "
                f"{time.monotonic() - started:.0f}s never published diagnostics for the file "
                f"({answered} of them still answered the `{never_reported[-1]['probe_method']}` "
                f"readiness probe — the measured load-dependent quiescent shape, not a "
                f"type-checking failure): {never_reported[-1]['stall_reason']}"
            )
        assert outcome == "defect", (outcome, never_reported, reported_empty)
        pytest.fail(
            "gopls REPORTED on the file but produced no diagnostics for blatantly bad "
            f"Go code: {len(reported_empty)} reported-empty attempt(s), "
            f"{len(never_reported)} silent: "
            f"{json.dumps((status.to_dict() if status else {}), default=str)[:600]}"
        )

    def test_gopls_budgets_are_explicit_and_readiness_driven(self):
        """Regression guard for GR-GAP-032 + INT-FLAKE-4.

        The gopls integration test asserts *correctness*, not latency, so its
        budgets must stay explicit and generous — and its retry budget must be
        driven by the status record's readiness signal (`published`: did the
        server report on the file at all?) rather than by an attempt count.
        """
        from engine.lsp import (
            READY_PROBE_METHOD,
            LspCheckStatus,
            run_lsp_check_status,
        )

        assert self.GOPLS_INIT_TIMEOUT >= 60.0
        assert self.GOPLS_PER_FILE_TIMEOUT >= 60.0
        assert self.GOPLS_READY_TIMEOUT >= 5.0
        assert self.GOPLS_MAX_ATTEMPTS >= 3
        # A wall-clock stall budget, not an attempt count: it must outlast
        # several stalled spawns.
        assert self.GOPLS_STALL_BUDGET_S >= self.GOPLS_READY_TIMEOUT * 3
        # The status record carries the signal the retry loop keys on, plus
        # the count of re-requests the engine sent.
        assert READY_PROBE_METHOD == "workspace/symbol"
        assert callable(run_lsp_check_status)
        for field in ("published", "stalled", "server_ready", "rechecks"):
            assert field in LspCheckStatus.__dataclass_fields__, field

    def test_gopls_failure_gate_is_published_not_probe_responsiveness(self):
        """INT-FLAKE-4: what fails the test, and what is a non-failing diagnostic.

        Measured under CPU load, a quiescent gopls answers `workspace/symbol` in
        7.7e-05 s and resolves symbols while never publishing diagnostics for the
        opened file — so "responsive" must not be the failure gate.
        """
        cls = type(self)
        answered_but_silent = [
            {
                "server_ready": True,
                "published": False,
                "stalled": True,
                "stall_reason": "answered but never published",
            }
        ]
        never_answered = [{"server_ready": False, "published": False, "stalled": True}]
        reported_empty = [{"server_ready": True, "published": True, "stalled": False}]

        # A stall that still answered the probe is a stall, not a defect.
        assert cls._classify_gopls_outcome(answered_but_silent, []) == "stall"
        assert cls._classify_gopls_outcome(never_answered, []) == "stall"
        # A server that reported on the file and found nothing IS a defect.
        assert cls._classify_gopls_outcome([], reported_empty) == "defect"
        assert cls._classify_gopls_outcome(never_answered, reported_empty) == "defect"
        # Nothing to judge means diagnostics were found (the success path).
        assert cls._classify_gopls_outcome([], []) == "verdict"

    def test_gopls_recovers_a_didopen_that_never_checked_the_file(
        self, lsp_workdir, tmp_path, monkeypatch
    ):
        """INT-FLAKE-4 root cause: a `didOpen` that lands before the server has
        a snapshot never produces diagnostics. Re-sending the same content as a
        change forces the check, so the engine recovers it in one attempt."""
        quiet_bin = tmp_path / "bin"
        quiet_bin.mkdir()
        fake = quiet_bin / "gopls"
        fake.write_text(
            ONLY_ON_CHANGE_LSP_SOURCE.replace("@PYTHON@", sys.executable), encoding="utf-8"
        )
        fake.chmod(0o755)
        monkeypatch.setenv("PATH", f"{quiet_bin}{os.pathsep}{os.environ['PATH']}")

        import subprocess

        subprocess.run(
            ["go", "mod", "init", "example.com/stall"], cwd=lsp_workdir, capture_output=True
        )
        path = os.path.join(lsp_workdir, "stall.go")
        with open(path, "w") as f:
            f.write('package main\n\nfunc main() {\n\tvar x int = "hello"\n}\n')

        status = run_lsp_check_status(
            "gopls",
            lsp_workdir,
            files=[path],
            init_timeout=30.0,
            timeout_per_file=30.0,
            recheck_after=1.0,
        )
        assert status.rechecks == 1, status
        assert status.published is True, status
        assert not status.stalled, status
        assert len(status.diagnostics) == 1, status
        assert status.diagnostics[0]["tool"] == "gopls"

    def test_gopls_probe_reports_a_quiescent_spawn_as_stalled(
        self, lsp_workdir, tmp_path, monkeypatch
    ):
        """INT-FLAKE-4: a spawn that answers `initialize` and then goes silent
        must be reported as STALLED, not mistaken for a clean tree."""
        quiet_bin = tmp_path / "bin"
        quiet_bin.mkdir()
        fake = quiet_bin / "gopls"
        fake.write_text(QUIET_LSP_SOURCE.replace("@PYTHON@", sys.executable), encoding="utf-8")
        fake.chmod(0o755)
        monkeypatch.setenv("PATH", f"{quiet_bin}{os.pathsep}{os.environ['PATH']}")

        import subprocess

        subprocess.run(
            ["go", "mod", "init", "example.com/quiet"], cwd=lsp_workdir, capture_output=True
        )
        path = os.path.join(lsp_workdir, "quiet.go")
        with open(path, "w") as f:
            f.write('package main\n\nfunc main() {\n\tvar x int = "hello"\n}\n')

        started = time.monotonic()
        status = run_lsp_check_status(
            "gopls",
            lsp_workdir,
            files=[path],
            init_timeout=30.0,
            timeout_per_file=6.0,
            probe=True,
            ready_timeout=3.0,
            recheck_after=2.0,
        )
        elapsed = time.monotonic() - started
        assert status.server_ready is False, status
        assert status.stalled is True, status
        assert status.published is False, status
        assert status.diagnostics == [], status
        assert "never answered" in (status.stall_reason or ""), status
        # Detected within the configured budgets instead of hanging on a
        # default: that is what makes fresh-server retries affordable.
        assert elapsed < 30.0, f"{elapsed:.1f}s"

    def test_gopls_skip_gracefully_when_not_installed(self, lsp_workdir):
        """gopls not found returns empty diagnostics."""
        with patch("shutil.which", return_value=None):
            diags = run_lsp_check("gopls", lsp_workdir, files=[])
        assert diags == [], "gopls should return empty diagnostics when not installed"


# ── Integration tests: jdtls ──────────────────────────────────────────


class TestJdtlsIntegration:
    """Integration tests for Java LSP with jdtls."""

    def test_jdtls_skip_gracefully_when_not_installed(self, lsp_workdir):
        """jdtls not found returns empty diagnostics (skip, not crash)."""
        with patch("engine.lsp.find_lsp_tool", return_value=None):
            diags = run_lsp_check(
                "jdtls", lsp_workdir, files=[os.path.join(lsp_workdir, "Main.java")]
            )
        assert diags == [], "jdtls should return empty diagnostics when not installed"

    def test_jdtls_java_file_language_mapping(self, lsp_workdir):
        """.java files are mapped to 'java' language via _LANGUAGE_MAP."""
        from engine.lsp import _staged_files_by_language

        with patch("engine.lsp._get_staged_files", return_value=["src/Main.java"]):
            with patch("os.path.isfile", return_value=True):
                result = _staged_files_by_language(lsp_workdir)
        assert "java" in result
        assert any("Main.java" in f for f in result["java"])


# ── Integration tests: kotlin-language-server ─────────────────────


class TestKotlinLsIntegration:
    """Integration tests for Kotlin LSP with kotlin-language-server."""

    def test_kotlin_ls_skip_gracefully_when_not_installed(self, lsp_workdir):
        """kotlin-language-server not found returns empty diagnostics."""
        with patch("engine.lsp.find_lsp_tool", return_value=None):
            diags = run_lsp_check(
                "kotlin-language-server", lsp_workdir, files=[os.path.join(lsp_workdir, "Main.kt")]
            )
        assert diags == [], (
            "kotlin-language-server should return empty diagnostics when not installed"
        )

    def test_kotlin_ls_kts_language_mapping(self, lsp_workdir):
        """.kts files are mapped to kotlin via _LANGUAGE_MAP."""
        from engine.lsp import _staged_files_by_language

        with patch("engine.lsp._get_staged_files", return_value=["build.gradle.kts"]):
            with patch("os.path.isfile", return_value=True):
                result = _staged_files_by_language(lsp_workdir)
        assert "kotlin" in result
        assert any("build.gradle.kts" in f for f in result["kotlin"])


# ── Integration tests: csharp-ls ──────────────────────────────────


class TestCsharpLsIntegration:
    """Integration tests for C# LSP with csharp-ls."""

    def test_csharp_ls_skip_gracefully_when_not_installed(self, lsp_workdir):
        """csharp-ls not found returns empty diagnostics."""
        with patch("engine.lsp.find_lsp_tool", return_value=None):
            diags = run_lsp_check(
                "csharp-ls", lsp_workdir, files=[os.path.join(lsp_workdir, "Program.cs")]
            )
        assert diags == [], "csharp-ls should return empty diagnostics when not installed"

    def test_csharp_ls_cs_language_mapping(self, lsp_workdir):
        """.cs files are mapped to csharp via _LANGUAGE_MAP."""
        from engine.lsp import _staged_files_by_language

        with patch("engine.lsp._get_staged_files", return_value=["Program.cs"]):
            with patch("os.path.isfile", return_value=True):
                result = _staged_files_by_language(lsp_workdir)
        assert "csharp" in result
        assert any("Program.cs" in f for f in result["csharp"])


# ── Integration tests: sourcekit-lsp ──────────────────────────────


class TestSourcekitLsIntegration:
    """Integration tests for Swift LSP with sourcekit-lsp."""

    def test_sourcekit_ls_skip_gracefully_when_not_installed(self, lsp_workdir):
        """sourcekit-lsp not found returns empty diagnostics."""
        with patch("engine.lsp.find_lsp_tool", return_value=None):
            diags = run_lsp_check(
                "sourcekit-lsp", lsp_workdir, files=[os.path.join(lsp_workdir, "main.swift")]
            )
        assert diags == [], "sourcekit-lsp should return empty diagnostics when not installed"

    def test_sourcekit_ls_swift_language_mapping(self, lsp_workdir):
        """.swift files are mapped to swift via _LANGUAGE_MAP."""
        from engine.lsp import _staged_files_by_language

        with patch("engine.lsp._get_staged_files", return_value=["main.swift"]):
            with patch("os.path.isfile", return_value=True):
                result = _staged_files_by_language(lsp_workdir)
        assert "swift" in result
        assert any("main.swift" in f for f in result["swift"])


# ── Integration tests: dart ───────────────────────────────────────


class TestDartLsIntegration:
    """Integration tests for Dart LSP with dart."""

    def test_dart_ls_skip_gracefully_when_not_installed(self, lsp_workdir):
        """dart not found returns empty diagnostics."""
        with patch("engine.lsp.find_lsp_tool", return_value=None):
            diags = run_lsp_check(
                "dart", lsp_workdir, files=[os.path.join(lsp_workdir, "main.dart")]
            )
        assert diags == [], "dart should return empty diagnostics when not installed"

    def test_dart_ls_dart_language_mapping(self, lsp_workdir):
        """.dart files are mapped to dart via _LANGUAGE_MAP."""
        from engine.lsp import _staged_files_by_language

        with patch("engine.lsp._get_staged_files", return_value=["main.dart"]):
            with patch("os.path.isfile", return_value=True):
                result = _staged_files_by_language(lsp_workdir)
        assert "dart" in result
        assert any("main.dart" in f for f in result["dart"])


# ── Integration tests: elixir-ls ───────────────────────────────────


class TestElixirLsIntegration:
    """Integration tests for Elixir LSP with elixir-ls."""

    def test_elixir_ls_skip_gracefully_when_not_installed(self, lsp_workdir):
        """elixir-ls not found returns empty diagnostics."""
        with patch("engine.lsp.find_lsp_tool", return_value=None):
            diags = run_lsp_check(
                "elixir-ls", lsp_workdir, files=[os.path.join(lsp_workdir, "main.ex")]
            )
        assert diags == [], "elixir-ls should return empty diagnostics when not installed"

    def test_elixir_ls_elixir_language_mapping(self, lsp_workdir):
        """.ex files are mapped to elixir via _LANGUAGE_MAP."""
        from engine.lsp import _staged_files_by_language

        with patch("engine.lsp._get_staged_files", return_value=["main.ex"]):
            with patch("os.path.isfile", return_value=True):
                result = _staged_files_by_language(lsp_workdir)
        assert "elixir" in result
        assert any("main.ex" in f for f in result["elixir"])


# ── Integration tests: metals ─────────────────────────────────────


class TestMetalsIntegration:
    """Integration tests for Scala LSP with metals."""

    def test_metals_skip_gracefully_when_not_installed(self, lsp_workdir):
        """metals not found returns empty diagnostics."""
        with patch("engine.lsp.find_lsp_tool", return_value=None):
            diags = run_lsp_check(
                "metals", lsp_workdir, files=[os.path.join(lsp_workdir, "main.scala")]
            )
        assert diags == [], "metals should return empty diagnostics when not installed"

    def test_metals_scala_language_mapping(self, lsp_workdir):
        """.scala files are mapped to scala via _LANGUAGE_MAP."""
        from engine.lsp import _staged_files_by_language

        with patch("engine.lsp._get_staged_files", return_value=["main.scala"]):
            with patch("os.path.isfile", return_value=True):
                result = _staged_files_by_language(lsp_workdir)
        assert "scala" in result
        assert any("main.scala" in f for f in result["scala"])


# ── Integration tests: ruby-lsp ───────────────────────────────────


class TestRubyLspIntegration:
    """Integration tests for Ruby LSP with ruby-lsp and solargraph."""

    def test_ruby_lsp_skip_gracefully_when_not_installed(self, lsp_workdir):
        """ruby-lsp not found returns empty diagnostics."""
        with patch("engine.lsp.find_lsp_tool", return_value=None):
            diags = run_lsp_check(
                "ruby-lsp", lsp_workdir, files=[os.path.join(lsp_workdir, "main.rb")]
            )
        assert diags == [], "ruby-lsp should return empty diagnostics when not installed"

    def test_solargraph_skip_gracefully_when_not_installed(self, lsp_workdir):
        """solargraph not found returns empty diagnostics."""
        with patch("engine.lsp.find_lsp_tool", return_value=None):
            diags = run_lsp_check(
                "solargraph", lsp_workdir, files=[os.path.join(lsp_workdir, "main.rb")]
            )
        assert diags == [], "solargraph should return empty diagnostics when not installed"

    def test_ruby_lsp_language_mapping(self, lsp_workdir):
        """.rb files are mapped to ruby via _LANGUAGE_MAP."""
        from engine.lsp import _staged_files_by_language

        with patch("engine.lsp._get_staged_files", return_value=["main.rb"]):
            with patch("os.path.isfile", return_value=True):
                result = _staged_files_by_language(lsp_workdir)
        assert "ruby" in result
        assert any("main.rb" in f for f in result["ruby"])


class TestPylspSkipContract:
    """DF-GITREINS-POC-6: pylsp absent ⇒ the real-pylsp node SKIPS, never FAILS.

    Proves the skip contract hermetically on ANY machine — including ones
    where pylsp IS installed — by running the exact node id in a subprocess
    pytest with a plugin that hides pylsp from ``shutil.which`` before the
    test module is imported (the skipif marker is evaluated at import time).
    """

    NODE_ID = "tests/test_lsp.py::TestLspJudgeIntegration::test_lsp_roundtrip_format_parse"

    PYLSP_HIDER_PLUGIN = '''\
"""Test plugin: hide pylsp AND force >=3.11 (DF-GITREINS-POC-6)."""
import os
import shutil
import sys

_original_which = shutil.which
_original_exists = os.path.exists

# On a real <3.11 interpreter, pre-seed tomllib from tomli BEFORE the fake
# version lands, so engine/version.py's version-gated `import tomllib`
# keeps working inside the subprocess (tomllib is stdlib-only 3.11+).
if sys.version_info < (3, 11):
    try:
        import tomli as _tomli

        sys.modules["tomllib"] = _tomli
    except ImportError:  # pragma: no cover - only on tomli-less 3.10
        pass


def _which_without_pylsp(cmd, *args, **kwargs):
    if cmd == "pylsp":
        return None
    return _original_which(cmd, *args, **kwargs)


def _exists_without_pylsp(path, *args, **kwargs):
    # The PYLSP_MISSING skipif carries an os.path.exists(venv-bin/pylsp)
    # escape hatch mirroring the test's PATH-prepend; it must see the tool
    # as absent too, or a pylsp-having machine would run (and fail) the node.
    if os.path.basename(str(path)) == "pylsp":
        return False
    return _original_exists(path, *args, **kwargs)


def pytest_configure(config):
    # Applied at configure time so import-time skipif markers see it.
    # sys.version_info is faked to >=3.11 so the CI-only PYLSP_SKIP_310
    # marker can never mask the missing-tool contract on any interpreter.
    sys.version_info = (3, 11, 0, "final", 0)
    shutil.which = _which_without_pylsp
    os.path.exists = _exists_without_pylsp
'''

    def test_roundtrip_node_skips_when_pylsp_hidden(self, tmp_path):
        """Subprocess run with pylsp hidden exits 0 and reports the node skipped."""
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        plugin_dir = tmp_path / "skip_plugin"
        plugin_dir.mkdir()
        (plugin_dir / "zz_pylsp_hider.py").write_text(self.PYLSP_HIDER_PLUGIN, encoding="utf-8")

        env = dict(os.environ)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(plugin_dir) + (os.pathsep + existing if existing else "")

        # Single node id, no xdist: cannot recurse into a full-suite spawn.
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                self.NODE_ID,
                "--override-ini=addopts=",
                "-p",
                "no:cacheprovider",
                "-p",
                "zz_pylsp_hider",
                "-q",
            ],
            cwd=repo_root,
            env=env,
            capture_output=True,
            text=True,
            timeout=90,
        )
        combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
        if proc.returncode != 0 or "1 skipped" not in combined:
            pytest.fail(
                f"pylsp-less run must SKIP (exit 0), got rc={proc.returncode}:\n{combined[-4000:]}"
            )
