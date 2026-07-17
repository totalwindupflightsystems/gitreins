"""
Unit tests for engine/static_analysis.py — static analysis guard runner.
"""

import json
import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from engine.static_analysis import (
    StaticDiag,
    _build_command,
    _parse_mypy,
    _parse_pyright_json,
    _parse_sorbet,
    _parse_sqlfluff_json,
    _parse_phpstan_json,
    _parse_cppcheck,
    find_tool,
    list_available_tools,
    run_static_check,
)

# Ensure tools installed to /home/opencode/.local/bin are findable
_local_bin = "/home/opencode/.local/bin"
if _local_bin not in os.environ.get("PATH", ""):
    os.environ["PATH"] = f"{_local_bin}:{os.environ['PATH']}"


# ── Sample output fixtures ──────────────────────────────────────────────


@pytest.fixture
def mypy_output_errors() -> str:
    return (
        "main.py:9: error: Argument 1 to \"get_user\" has incompatible "
        "type \"str\"; expected \"int\"  [arg-type]\n"
        "main.py:15: warning: Returning Any from function declared "
        'to return "str"  [no-any-return]\n'
        "main.py:22: note: By default the bodies of untyped functions "
        "are not checked  [untyped-def]\n"
        "Found 3 errors in 1 file (checked 1 source file)\n"
        "Success: no issues found\n"
        "note: See https://mypy.rtfd.io/en/stable/ for more info\n"
    )


@pytest.fixture
def pyright_json_output() -> dict:
    return {
        "version": "1.0",
        "generalDiagnostics": [
            {
                "file": "/tmp/workdir/main.py",
                "severity": "error",
                "message": 'Argument of type "str" cannot be assigned '
                    'to parameter of type "int"',
                "range": {
                    "start": {"line": 8, "character": 12},
                    "end": {"line": 8, "character": 20},
                },
                "rule": "reportGeneralTypeIssues",
            },
            {
                "file": "/tmp/workdir/main.py",
                "severity": "warning",
                "message": '"x" is not accessed',
                "range": {
                    "start": {"line": 15, "character": 5},
                    "end": {"line": 15, "character": 6},
                },
                "rule": "reportUnusedVariable",
            },
        ],
    }


@pytest.fixture
def sorbet_output_errors() -> str:
    return (
        "main.rb:5: Expected Integer but found String for "
        "argument user_id https://srb.help/7002\n"
        "main.rb:12: Method `foo` does not exist on Integer "
        "https://srb.help/7003\n"
        "No errors! Everything is great.\n"
        "Errors: 2\n"
    )


@pytest.fixture
def sqlfluff_output() -> list:
    return [
        {
            "filepath": "query.sql",
            "violations": [
                {
                    "line_no": 5,
                    "description": "Line has unexpected trailing whitespace",
                    "code": "W291",
                    "severity": "warning",
                },
                {
                    "line_no": 10,
                    "description": "Unquoted identifiers must be lower case",
                    "code": "L014",
                    "severity": "error",
                },
            ],
        }
    ]


@pytest.fixture
def phpstan_output() -> dict:
    return {
        "totals": {"errors": 1, "file_errors": 1},
        "files": {
            "src/User.php": {
                "errors": 1,
                "messages": [
                    {
                        "line": 27,
                        "message": "Parameter #1 $id of class App\\User "
                            "constructor expects int, string given.",
                    }
                ],
            }
        },
    }


@pytest.fixture
def cppcheck_output_errors() -> str:
    """Simulated cppcheck output with --template format (mypy-compatible)."""
    return (
        "Checking test.cpp ...\n"
        "test.cpp:3: error: Uninitialized variable: x [uninitvar]\n"
        "test.cpp:5: warning: Possible null pointer dereference: ptr "
        "[nullPointer]\n"
        "test.cpp:10: style: Variable 'y' is assigned a value that is never "
        "used. [unreadVariable]\n"
        "test.cpp:15: performance: Prefer prefix ++/-- operators for "
        "non-primitive types. [postfixOperator]\n"
        "test.cpp:20: portability: scanf without field width limits can crash "
        "with huge input data. [invalidscanf]\n"
        "test.cpp:25: information: Cppcheck cannot find all the include files. "
        "Cppcheck can check the code without the include files, but the "
        "results will be more accurate if the headers are available. "
        "[missingInclude]\n"
        "nofile:0: information: Active checkers: 173/975 "
        "[checkersReport]\n"
    )


# ══════════════════════════════════════════════════════════════════
# StaticDiag
# ══════════════════════════════════════════════════════════════════


class TestStaticDiag:
    """Test StaticDiag dataclass creation and serialization."""

    def test_static_diag_creation(self):
        d = StaticDiag(
            file="test.py", line=5, severity="error",
            message="Undefined variable", code="E001", tool="mypy",
        )
        assert d.file == "test.py"
        assert d.line == 5
        assert d.severity == "error"
        assert d.message == "Undefined variable"
        assert d.code == "E001"
        assert d.tool == "mypy"

    def test_static_diag_defaults(self):
        d = StaticDiag(file="f.py", line=1, severity="warning", message="msg")
        assert d.code == ""
        assert d.tool == ""

    def test_static_diag_to_dict(self):
        d = StaticDiag(
            file="f.py", line=3, severity="warning",
            message="unused import", code="F401", tool="pyright",
        )
        result = d.to_dict()
        assert result["file"] == "f.py"
        assert result["line"] == 3
        assert result["severity"] == "warning"
        assert result["message"] == "unused import"
        assert result["code"] == "F401"
        assert result["tool"] == "pyright"

    def test_static_diag_serialization_roundtrip(self):
        d1 = StaticDiag(
            file="a.py", line=10, severity="error",
            message="bad", code="F401", tool="mypy",
        )
        d2 = StaticDiag(**d1.to_dict())
        assert d1 == d2


# ══════════════════════════════════════════════════════════════════
# find_tool
# ══════════════════════════════════════════════════════════════════


class TestFindTool:
    """Test tool discovery via find_tool()."""

    def test_find_tool_mypy_installed(self):
        path = find_tool("mypy")
        assert path is not None
        assert "mypy" in path
        assert os.path.isabs(path)

    def test_find_tool_pyright_installed(self):
        path = find_tool("pyright")
        assert path is not None
        assert "pyright" in path
        # pyright may be found as 'npx pyright' (compound command) or
        # an absolute path — both are valid returns from find_tool.

    def test_find_tool_missing(self):
        result = find_tool("nonexistent-tool-xyz")
        assert result is None

    def test_find_tool_not_registered(self):
        """Unknown tool name falls through to using the name itself as
        the single candidate."""
        result = find_tool("python3")
        assert result is not None
        assert "python3" in result


# ══════════════════════════════════════════════════════════════════
# list_available_tools
# ══════════════════════════════════════════════════════════════════


class TestListAvailableTools:
    """Test list_available_tools returns correct tool lists."""

    def test_list_python_tools(self):
        tools = list_available_tools("python")
        assert "mypy" in tools
        assert "pyright" in tools

    def test_list_nonexistent_language(self):
        tools = list_available_tools("nonexistent")
        assert tools == []

    def test_list_empty_language(self):
        tools = list_available_tools("")
        assert tools == []

    def test_list_cpp_tools(self):
        tools = list_available_tools("cpp")
        assert "cppcheck" in tools


# ══════════════════════════════════════════════════════════════════
# _parse_mypy
# ══════════════════════════════════════════════════════════════════


class TestParseMypy:
    """Test parsing of mypy text output."""

    def test_parse_mypy_errors(self, mypy_output_errors):
        diags = _parse_mypy(mypy_output_errors)
        assert len(diags) == 3

        # First diagnostic: error
        assert diags[0].file == "main.py"
        assert diags[0].line == 9
        assert diags[0].severity == "error"
        assert "incompatible" in diags[0].message
        assert diags[0].code == "arg-type"
        assert diags[0].tool == "mypy"

        # Second diagnostic: warning
        assert diags[1].file == "main.py"
        assert diags[1].line == 15
        assert diags[1].severity == "warning"
        assert diags[1].code == "no-any-return"

        # Third diagnostic: note
        assert diags[2].file == "main.py"
        assert diags[2].line == 22
        assert diags[2].severity == "note"
        assert diags[2].code == "untyped-def"

    def test_parse_mypy_empty(self):
        diags = _parse_mypy("")
        assert diags == []

    def test_parse_mypy_no_errors(self):
        diags = _parse_mypy("Success: no issues found\n")
        assert diags == []

    def test_parse_mypy_with_code(self):
        text = "app.py:5: error: Incompatible return value type (got \"str\", expected \"int\")  [return-value]\n"
        diags = _parse_mypy(text)
        assert len(diags) == 1
        assert diags[0].code == "return-value"

    def test_parse_mypy_no_code(self):
        text = "app.py:5: error: Something went wrong\n"
        diags = _parse_mypy(text)
        assert len(diags) == 1
        assert diags[0].code == ""


# ══════════════════════════════════════════════════════════════════
# _parse_pyright_json
# ══════════════════════════════════════════════════════════════════


class TestParsePyrightJson:
    """Test parsing of pyright JSON output."""

    def test_parse_pyright_json(self, pyright_json_output):
        diags = _parse_pyright_json(pyright_json_output)
        assert len(diags) == 2

        assert diags[0].file == "/tmp/workdir/main.py"
        assert diags[0].line == 9  # 8 (0-indexed) + 1
        assert diags[0].severity == "error"
        assert "cannot be assigned" in diags[0].message
        assert diags[0].code == "reportGeneralTypeIssues"
        assert diags[0].tool == "pyright"

        assert diags[1].file == "/tmp/workdir/main.py"
        assert diags[1].line == 16  # 15 + 1
        assert diags[1].severity == "warning"
        assert diags[1].code == "reportUnusedVariable"

    def test_parse_pyright_json_empty(self):
        diags = _parse_pyright_json({"generalDiagnostics": []})
        assert diags == []

    def test_parse_pyright_json_no_diagnostics_key(self):
        diags = _parse_pyright_json({})
        assert diags == []

    def test_parse_pyright_json_missing_range(self):
        data = {
            "generalDiagnostics": [
                {
                    "file": "test.py",
                    "severity": "error",
                    "message": "missing range",
                }
            ]
        }
        diags = _parse_pyright_json(data)
        assert len(diags) == 1
        assert diags[0].line == 1  # default to 0 + 1


# ══════════════════════════════════════════════════════════════════
# _parse_sorbet
# ══════════════════════════════════════════════════════════════════


class TestParseSorbet:
    """Test parsing of sorbet text output."""

    def test_parse_sorbet_errors(self, sorbet_output_errors):
        diags = _parse_sorbet(sorbet_output_errors)
        assert len(diags) == 2

        assert diags[0].file == "main.rb"
        assert diags[0].line == 5
        assert diags[0].severity == "error"
        assert "Expected Integer" in diags[0].message
        assert diags[0].code == "7002"
        assert diags[0].tool == "sorbet"

        assert diags[1].file == "main.rb"
        assert diags[1].line == 12
        assert diags[1].severity == "error"
        assert diags[1].code == "7003"

    def test_parse_sorbet_warning_severity(self):
        text = "main.rb:5: warning: Method is deprecated https://srb.help/7004\n"
        diags = _parse_sorbet(text)
        assert len(diags) == 1
        assert diags[0].severity == "warning"

    def test_parse_sorbet_empty(self):
        diags = _parse_sorbet("")
        assert diags == []

    def test_parse_sorbet_no_errors(self):
        diags = _parse_sorbet("No errors!\n")
        assert diags == []

    def test_parse_sorbet_no_code(self):
        text = "main.rb:5: Expected Integer but found String\n"
        diags = _parse_sorbet(text)
        assert len(diags) == 1
        assert diags[0].code == ""


# ══════════════════════════════════════════════════════════════════
# _parse_sqlfluff_json
# ══════════════════════════════════════════════════════════════════


class TestParseSqlfluffJson:
    """Test parsing of sqlfluff JSON output."""

    def test_parse_sqlfluff_json(self, sqlfluff_output):
        diags = _parse_sqlfluff_json(sqlfluff_output)
        assert len(diags) == 2

        assert diags[0].file == "query.sql"
        assert diags[0].line == 5
        assert diags[0].severity == "warning"
        assert "trailing whitespace" in diags[0].message
        assert diags[0].code == "W291"
        assert diags[0].tool == "sqlfluff"

        assert diags[1].file == "query.sql"
        assert diags[1].line == 10
        assert diags[1].severity == "error"
        assert diags[1].code == "L014"

    def test_parse_sqlfluff_json_empty(self):
        diags = _parse_sqlfluff_json([])
        assert diags == []

    def test_parse_sqlfluff_json_no_violations(self):
        diags = _parse_sqlfluff_json([{"filepath": "q.sql", "violations": []}])
        assert diags == []


# ══════════════════════════════════════════════════════════════════
# _parse_phpstan_json
# ══════════════════════════════════════════════════════════════════


class TestParsePhpstanJson:
    """Test parsing of phpstan JSON output."""

    def test_parse_phpstan_json(self, phpstan_output):
        diags = _parse_phpstan_json(phpstan_output)
        assert len(diags) == 1

        assert diags[0].file == "src/User.php"
        assert diags[0].line == 27
        assert diags[0].severity == "error"
        assert "expects int" in diags[0].message
        assert diags[0].code == ""
        assert diags[0].tool == "phpstan"

    def test_parse_phpstan_json_empty(self):
        diags = _parse_phpstan_json({"files": {}})
        assert diags == []

    def test_parse_phpstan_json_no_files_key(self):
        diags = _parse_phpstan_json({})
        assert diags == []


# ══════════════════════════════════════════════════════════════════
# _parse_cppcheck
# ══════════════════════════════════════════════════════════════════


class TestParseCppcheck:
    """Test parsing of cppcheck text output (--template format)."""

    def test_parse_cppcheck_all_severities(self, cppcheck_output_errors):
        """Cppcheck output with error, warning, style, performance,
        portability, and information — six diagnostics, header lines
        (Checking…, nofile:…) are skipped."""
        diags = _parse_cppcheck(cppcheck_output_errors)
        assert len(diags) == 6

        # error stays error
        assert diags[0].file == "test.cpp"
        assert diags[0].line == 3
        assert diags[0].severity == "error"
        assert diags[0].code == "uninitvar"
        assert diags[0].tool == "cppcheck"

        # warning stays warning
        assert diags[1].file == "test.cpp"
        assert diags[1].line == 5
        assert diags[1].severity == "warning"
        assert diags[1].code == "nullPointer"

        # style → note
        assert diags[2].file == "test.cpp"
        assert diags[2].line == 10
        assert diags[2].severity == "note"
        assert diags[2].code == "unreadVariable"

        # performance → note
        assert diags[3].severity == "note"
        assert diags[3].code == "postfixOperator"

        # portability → note
        assert diags[4].severity == "note"
        assert diags[4].code == "invalidscanf"

        # information → note
        assert diags[5].severity == "note"
        assert diags[5].code == "missingInclude"

    def test_parse_cppcheck_empty(self):
        diags = _parse_cppcheck("")
        assert diags == []

    def test_parse_cppcheck_no_errors(self):
        diags = _parse_cppcheck("Checking test.cpp ...\n")
        assert diags == []

    def test_parse_cppcheck_with_code(self):
        text = "app.cpp:5: error: Incompatible types [typeError]\n"
        diags = _parse_cppcheck(text)
        assert len(diags) == 1
        assert diags[0].code == "typeError"
        assert diags[0].severity == "error"

    def test_parse_cppcheck_no_code(self):
        text = "app.cpp:5: error: Something went wrong\n"
        diags = _parse_cppcheck(text)
        assert len(diags) == 1
        assert diags[0].code == ""

    def test_parse_cppcheck_skips_header(self):
        text = (
            "Checking test.cpp ...\n"
            "nofile:0: information: Active checkers: 173/975 [checkersReport]\n"
        )
        diags = _parse_cppcheck(text)
        assert diags == []


# ══════════════════════════════════════════════════════════════════
# _build_command
# ══════════════════════════════════════════════════════════════════


class TestBuildCommand:
    """Test _build_command constructs correct subprocess args."""

    def test_build_command_mypy(self):
        cmd = _build_command("mypy", "/usr/bin/mypy", "/tmp/work")
        assert cmd == ["/usr/bin/mypy", "--strict", "--no-error-summary",
                        "--explicit-package-bases", "."]

    def test_build_command_pyright(self):
        cmd = _build_command("pyright", "/usr/bin/pyright", "/tmp/work")
        assert cmd == ["/usr/bin/pyright", "--outputjson", "/tmp/work"]

    def test_build_command_sorbet(self):
        cmd = _build_command("sorbet", "/usr/bin/srb", "/tmp/work")
        assert cmd == ["/usr/bin/srb", "tc", "--no-error-count"]

    def test_build_command_sqlfluff(self):
        cmd = _build_command("sqlfluff", "/usr/bin/sqlfluff", "/tmp/work")
        assert cmd == ["/usr/bin/sqlfluff", "lint", "--format", "json",
                        "/tmp/work"]

    def test_build_command_phpstan(self):
        cmd = _build_command("phpstan", "/usr/bin/phpstan", "/tmp/work")
        assert cmd == ["/usr/bin/phpstan", "analyse",
                        "--error-format=json", "--no-progress", "/tmp/work"]

    def test_build_command_cppcheck(self):
        cmd = _build_command("cppcheck", "/usr/bin/cppcheck", "/tmp/work")
        assert cmd[0] == "/usr/bin/cppcheck"
        assert "--enable=all" in cmd
        assert "--suppress=missingIncludeSystem" in cmd
        assert any("--template=" in a for a in cmd)
        assert cmd[-1] == "/tmp/work"

    def test_build_command_unknown(self):
        cmd = _build_command("unknown", "/usr/bin/unknown", "/tmp/work")
        assert cmd == ["/usr/bin/unknown", "/tmp/work"]

    def test_build_command_compound(self):
        cmd = _build_command("pyright", "npx pyright", "/tmp/work")
        assert cmd == ["npx", "pyright", "--outputjson", "/tmp/work"]


# ══════════════════════════════════════════════════════════════════
# run_static_check
# ══════════════════════════════════════════════════════════════════


class TestRunStaticCheck:
    """Test the main run_static_check orchestrator."""

    def test_run_static_check_mypy(self, mypy_output_errors):
        """Mypy with mocked subprocess returns normalized diagnostics."""
        mock_result = MagicMock()
        mock_result.stdout = mypy_output_errors
        mock_result.stderr = ""

        with patch("engine.static_analysis.find_tool",
                   return_value="/usr/bin/mypy"):
            with patch("engine.static_analysis.subprocess.run",
                       return_value=mock_result):
                result = run_static_check("mypy", "/tmp/workdir")

        assert len(result) == 3
        assert result[0]["file"] == "main.py"
        assert result[0]["line"] == 9
        assert result[0]["severity"] == "error"
        assert result[0]["tool"] == "mypy"
        assert result[0]["code"] == "arg-type"

    def test_run_static_check_pyright(self, pyright_json_output):
        """Pyright with mocked subprocess returns normalized diagnostics
        with paths relativized to workdir."""
        mock_result = MagicMock()
        mock_result.stdout = json.dumps(pyright_json_output)
        mock_result.stderr = ""

        with patch("engine.static_analysis.find_tool",
                   return_value="/usr/bin/pyright"):
            with patch("engine.static_analysis.subprocess.run",
                       return_value=mock_result):
                result = run_static_check("pyright", "/tmp/workdir")

        assert len(result) == 2
        # Path is relativized because it starts with workdir
        assert result[0]["file"] == "main.py"
        assert result[0]["line"] == 9
        assert result[0]["severity"] == "error"
        assert result[0]["code"] == "reportGeneralTypeIssues"
        assert result[0]["tool"] == "pyright"

    def test_run_static_check_tool_not_found(self):
        """When tool is not found, returns empty list."""
        with patch("engine.static_analysis.find_tool", return_value=None):
            result = run_static_check("nonexistent-tool", "/tmp/workdir")
        assert result == []

    def test_run_static_check_timeout(self):
        """When subprocess times out, returns empty list."""
        with patch("engine.static_analysis.find_tool",
                   return_value="/usr/bin/mypy"):
            with patch("engine.static_analysis.subprocess.run",
                       side_effect=subprocess.TimeoutExpired(
                           cmd="mypy", timeout=120)):
                result = run_static_check("mypy", "/tmp/workdir")
        assert result == []

    def test_run_static_check_file_not_found(self):
        """When subprocess raises FileNotFoundError, returns empty list."""
        with patch("engine.static_analysis.find_tool",
                   return_value="/usr/bin/mypy"):
            with patch("engine.static_analysis.subprocess.run",
                       side_effect=FileNotFoundError):
                result = run_static_check("mypy", "/tmp/workdir")
        assert result == []

    def test_run_static_check_generic_exception(self):
        """When subprocess raises generic exception, returns empty list."""
        with patch("engine.static_analysis.find_tool",
                   return_value="/usr/bin/mypy"):
            with patch("engine.static_analysis.subprocess.run",
                       side_effect=PermissionError("denied")):
                result = run_static_check("mypy", "/tmp/workdir")
        assert result == []

    def test_run_static_check_invalid_json(self):
        """When tool returns invalid JSON, returns empty list."""
        mock_result = MagicMock()
        mock_result.stdout = "not valid json"
        mock_result.stderr = ""

        with patch("engine.static_analysis.find_tool",
                   return_value="/usr/bin/pyright"):
            with patch("engine.static_analysis.subprocess.run",
                       return_value=mock_result):
                result = run_static_check("pyright", "/tmp/workdir")
        assert result == []

    def test_run_static_check_sorbet_stderr(self, sorbet_output_errors):
        """Sorbet reads diagnostics from stderr."""
        mock_result = MagicMock()
        mock_result.stdout = ""
        mock_result.stderr = sorbet_output_errors

        with patch("engine.static_analysis.find_tool",
                   return_value="/usr/bin/srb"):
            with patch("engine.static_analysis.subprocess.run",
                       return_value=mock_result):
                result = run_static_check("sorbet", "/tmp/workdir")

        assert len(result) == 2
        assert result[0]["file"] == "main.rb"
        assert result[0]["tool"] == "sorbet"

    def test_run_static_check_cppcheck(self, cppcheck_output_errors):
        """Cppcheck text output parsed through the text-parser path."""
        mock_result = MagicMock()
        mock_result.stdout = cppcheck_output_errors
        mock_result.stderr = ""

        with patch("engine.static_analysis.find_tool",
                   return_value="/usr/bin/cppcheck"):
            with patch("engine.static_analysis.subprocess.run",
                       return_value=mock_result):
                result = run_static_check("cppcheck", "/tmp/workdir")

        assert len(result) == 6
        assert result[0]["file"] == "test.cpp"
        assert result[0]["severity"] == "error"
        assert result[0]["tool"] == "cppcheck"
        assert result[0]["code"] == "uninitvar"
