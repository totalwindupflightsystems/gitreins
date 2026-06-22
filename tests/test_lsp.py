"""
Unit tests for engine/lsp.py — LSP guard runner.
"""

import json
import subprocess
from unittest.mock import MagicMock, patch

from engine.lsp import (
    LspDiag,
    find_lsp_tool,
    normalize_severity,
    run_lsp_check,
)


class TestLspDiag:
    """Test LspDiag dataclass."""

    def test_lsp_diag_creation(self):
        d = LspDiag(file="test.py", line=5, severity="error", message="Undefined variable", code="E001", tool="pylsp")
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
        d = LspDiag(file="f.py", line=3, severity="info", message="msg", code="W001", tool="ruff-lsp")
        result = d.to_dict()
        assert result["file"] == "f.py"
        assert result["line"] == 3
        assert result["severity"] == "info"
        assert result["code"] == "W001"
        assert result["tool"] == "ruff-lsp"

    def test_lsp_diag_serialization_roundtrip(self):
        d1 = LspDiag(file="a.py", line=10, severity="error", message="bad", code="F401", tool="pyright")
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
            with patch("engine.lsp._get_staged_python_files", return_value=[]):
                result = run_lsp_check("pylsp", tmp_workdir)
        assert result == []

    def test_run_lsp_check_no_matching_language(self, tmp_workdir):
        """Returns empty when staged files don't match tool language."""
        with patch("engine.lsp.find_lsp_tool", return_value="/usr/bin/pylsp"):
            with patch("engine.lsp._get_staged_python_files", return_value=["file.lua"]):
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
                with patch("engine.lsp._get_staged_python_files", return_value=["test.py"]):
                    with patch("os.path.isfile", return_value=True):
                        result = run_lsp_check("pylsp", "/tmp")
        assert result == []

    def test_run_lsp_check_startup_failure_returns_empty(self):
        """When LSP process can't start, returns empty list."""
        with patch("engine.lsp.find_lsp_tool", return_value="/usr/bin/pylsp"):
            with patch("engine.lsp.subprocess.Popen", side_effect=OSError("not found")):
                with patch("engine.lsp._get_staged_python_files", return_value=["test.py"]):
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
                        with patch("engine.lsp._get_staged_python_files", return_value=["test.py"]):
                            with patch("os.path.isfile", return_value=True):
                                result = run_lsp_check("pylsp", "/tmp")
        assert result == []
        mock_shutdown.assert_called_once()


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
