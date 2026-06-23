"""
Tests for evaluator's read_static_analysis tool (AC-160, sa-eval-tests).
"""
import os
import yaml
from unittest.mock import patch, MagicMock

from engine.evaluator import EVALUATOR_TOOLS


class TestReadStaticAnalysis:
    """Test _tool_read_static_analysis — config gating, diagnostics, error handling."""

    def _write_config(self, workdir, config_dict):
        gitreins_dir = os.path.join(workdir, ".gitreins")
        os.makedirs(gitreins_dir)
        with open(os.path.join(gitreins_dir, "config.yaml"), "w") as f:
            yaml.dump(config_dict, f)

    # Test 1: error when disabled (default)
    def test_disabled_by_default(self, evaluator, tmp_workdir):
        self._write_config(tmp_workdir, {"evaluator": {"static_analysis_diagnostics": False}})
        result = evaluator._tool_read_static_analysis()
        assert "error" in result

    # Test 2: empty diagnostics when no tools configured
    def test_empty_tools(self, evaluator, tmp_workdir):
        self._write_config(tmp_workdir, {"evaluator": {"static_analysis_diagnostics": True}})
        result = evaluator._tool_read_static_analysis()
        assert result == {"diagnostics": [], "note": "No static analysis tools configured"}

    # Test 3: mypy configured with diagnostics
    @patch("engine.static_analysis.run_static_check")
    def test_mypy_configured(self, mock_run, evaluator, tmp_workdir):
        self._write_config(tmp_workdir, {
            "evaluator": {"static_analysis_diagnostics": True},
            "guards": {"static_analysis_tools": {"python": ["mypy"]}},
        })
        mock_run.return_value = [
            {"file": "main.py", "line": 10, "severity": "error",
             "message": "Incompatible types", "code": "", "tool": "mypy"},
        ]
        result = evaluator._tool_read_static_analysis()
        assert result["count"] == 1
        assert len(result["diagnostics"]) == 1
        assert result["diagnostics"][0]["tool"] == "mypy"
        assert result["tools_used"] == ["mypy"]
        mock_run.assert_called_once_with("mypy", evaluator.workdir)

    # Test 4: path arg scopes to specific directory
    @patch("engine.static_analysis.run_static_check")
    def test_with_path_arg(self, mock_run, evaluator, tmp_workdir):
        self._write_config(tmp_workdir, {
            "evaluator": {"static_analysis_diagnostics": True},
            "guards": {"static_analysis_tools": {"python": ["mypy"]}},
        })
        subdir = os.path.join(tmp_workdir, "src")
        os.makedirs(subdir)
        mock_run.return_value = []
        result = evaluator._tool_read_static_analysis(path="src")
        assert result["diagnostics"] == []
        mock_run.assert_called_once_with("mypy", subdir)

    # Test 5: tool failure handled gracefully
    @patch("engine.static_analysis.run_static_check")
    def test_tool_failure(self, mock_run, evaluator, tmp_workdir):
        self._write_config(tmp_workdir, {
            "evaluator": {"static_analysis_diagnostics": True},
            "guards": {"static_analysis_tools": {"python": ["mypy"]}},
        })
        mock_run.side_effect = RuntimeError("mypy crashed")
        result = evaluator._tool_read_static_analysis()
        assert result["count"] == 1
        diag = result["diagnostics"][0]
        assert diag["tool"] == "mypy"
        assert "error" in diag
        assert "mypy crashed" in diag["error"]

    # Test 6: tool definition in EVALUATOR_TOOLS
    def test_tool_definition(self):
        tool_names = [t["function"]["name"] for t in EVALUATOR_TOOLS]
        assert "read_static_analysis" in tool_names
        func = [t["function"] for t in EVALUATOR_TOOLS
                if t["function"]["name"] == "read_static_analysis"][0]
        assert func["name"] == "read_static_analysis"
        assert "path" in func["parameters"]["properties"]
        assert func["parameters"]["properties"]["path"]["type"] == "string"

    # Test 7: tool excluded when static_analysis_diagnostics is false
    def test_tool_excluded_when_disabled(self, evaluator, tmp_workdir):
        self._write_config(tmp_workdir, {"evaluator": {"static_analysis_diagnostics": False}})

        mock_response = MagicMock()
        mock_response.content = '{"verdict":"COMPLETE","items":[],"summary":"done"}'
        mock_response.tool_calls = None
        mock_response.usage = MagicMock(prompt_tokens=10, completion_tokens=10)

        captured_tools = []

        def capture_chat(messages, **kwargs):
            captured_tools.append(kwargs.get("tools", []))
            return mock_response

        with patch.object(evaluator.llm, "chat", capture_chat):
            evaluator.evaluate({
                "id": "test-1",
                "title": "Test",
                "criteria": ["criterion 1"],
            })

        assert len(captured_tools) >= 1
        tool_names = [t["function"]["name"] for t in captured_tools[0]]
        assert "read_static_analysis" not in tool_names

    # Test 8: pyright configured via mock
    @patch("engine.static_analysis.run_static_check")
    def test_pyright_configured(self, mock_run, evaluator, tmp_workdir):
        self._write_config(tmp_workdir, {
            "evaluator": {"static_analysis_diagnostics": True},
            "guards": {"static_analysis_tools": {"python": ["pyright"]}},
        })
        mock_run.return_value = [
            {"file": "app.py", "line": 5, "severity": "warning",
             "message": "Type not declared", "code": "", "tool": "pyright"},
        ]
        result = evaluator._tool_read_static_analysis()
        assert result["count"] == 1
        assert result["diagnostics"][0]["tool"] == "pyright"
        assert result["tools_used"] == ["pyright"]
        mock_run.assert_called_once_with("pyright", evaluator.workdir)

    # Test 9: return structure has correct keys
    @patch("engine.static_analysis.run_static_check")
    def test_return_structure(self, mock_run, evaluator, tmp_workdir):
        self._write_config(tmp_workdir, {
            "evaluator": {"static_analysis_diagnostics": True},
            "guards": {"static_analysis_tools": {"python": ["mypy"]}},
        })
        mock_run.return_value = []
        result = evaluator._tool_read_static_analysis()
        assert isinstance(result, dict)
        assert "diagnostics" in result
        assert "count" in result
        assert "tools_used" in result
        assert isinstance(result["diagnostics"], list)
        assert isinstance(result["count"], int)
        assert isinstance(result["tools_used"], list)

    # Test 10: multiple tools configured
    @patch("engine.static_analysis.run_static_check")
    def test_multiple_tools(self, mock_run, evaluator, tmp_workdir):
        self._write_config(tmp_workdir, {
            "evaluator": {"static_analysis_diagnostics": True},
            "guards": {"static_analysis_tools": {"python": ["mypy", "pyright"]}},
        })
        mock_run.side_effect = [
            [{"file": "a.py", "line": 1, "severity": "error",
              "message": "err1", "code": "", "tool": "mypy"}],
            [{"file": "b.py", "line": 2, "severity": "warning",
              "message": "warn1", "code": "", "tool": "pyright"}],
        ]
        result = evaluator._tool_read_static_analysis()
        assert result["count"] == 2
        assert len(result["diagnostics"]) == 2
        assert result["tools_used"] == ["mypy", "pyright"]
        assert mock_run.call_count == 2
        mock_run.assert_any_call("mypy", evaluator.workdir)
        mock_run.assert_any_call("pyright", evaluator.workdir)
