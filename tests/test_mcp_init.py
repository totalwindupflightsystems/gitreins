"""
Tests for the repo.init MCP tool (DF-GITREINS-POC-77).

repo.init closes the MCP-only gap left by GR-GAP-054: guard.run refuses a
repo without .gitreins/config.yaml, and the only writer of that file was the
CLI (`gitreins init`). With repo.init on the tool surface the full workflow
(init → guard.run → judge) works without ever opening a shell.
"""

from __future__ import annotations
from pathlib import Path
from typing import Any

import json
import os

import pytest

from gitreins.cli import DEFAULT_GITREINS_CONFIG
from gitreins_mcp.server import GitReinsMCPServer


@pytest.fixture
def mcp_server(tmp_workdir: str) -> Any:
    """Create an MCP server pointed at a temp git repo (same shape as
    test_mcp_server.py's fixture — repo.init is the tool under test, the
    workdir itself is only where the server instance is anchored)."""
    return GitReinsMCPServer(tmp_workdir)


@pytest.fixture
def bare_git_repo(tmp_path: Path) -> Any:
    """A git repo with NO .gitreins/config.yaml (unlike tmp_workdir)."""
    repo = tmp_path / "fresh-repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    return str(repo)


@pytest.fixture
def plain_dir(tmp_path: Path) -> Any:
    """A directory that is not a git repository at all."""
    d = tmp_path / "plain-dir"
    d.mkdir()
    return str(d)


def _call(server: GitReinsMCPServer, tool: str, arguments: dict) -> dict:
    response = server.handle_request(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments},
        }
    )
    return json.loads(response["result"]["content"][0]["text"])


def _tools_list(server: GitReinsMCPServer) -> dict:
    response = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    return {t["name"]: t for t in response["result"]["tools"]}


class TestRepoInitMCP:
    def test_tools_list_advertises_repo_init(self, mcp_server: Any) -> None:
        """repo.init is on the advertised tool surface with a workdir schema."""
        tools = _tools_list(mcp_server)
        assert "repo.init" in tools
        schema = tools["repo.init"]["inputSchema"]
        assert schema["type"] == "object"
        assert "workdir" in schema["properties"]
        assert schema["properties"]["workdir"]["type"] == "string"
        assert "guard.run" in tools  # the tool it unblocks is still advertised

    def test_repo_init_creates_config(self, mcp_server: Any, bare_git_repo: Any) -> None:
        """Fresh git repo → created True, file exists, carries the defaults block."""
        result = _call(mcp_server, "repo.init", {"workdir": bare_git_repo})
        assert result.get("created") is True, result
        assert result["workdir"] == os.path.abspath(bare_git_repo)
        config_path = result["config_path"]
        assert config_path == os.path.join(
            os.path.abspath(bare_git_repo), ".gitreins", "config.yaml"
        )
        with open(config_path) as f:
            content = f.read()
        assert "defaults:" in content
        assert content == DEFAULT_GITREINS_CONFIG

    def test_repo_init_is_idempotent(self, mcp_server: Any, bare_git_repo: Any) -> None:
        """Second call reports created False and leaves the file untouched."""
        first = _call(mcp_server, "repo.init", {"workdir": bare_git_repo})
        assert first.get("created") is True, first
        config_path = first["config_path"]
        before_mtime = os.stat(config_path).st_mtime_ns
        before_content = open(config_path).read()

        second = _call(mcp_server, "repo.init", {"workdir": bare_git_repo})
        assert second.get("created") is False, second
        assert second["config_path"] == config_path
        assert second["workdir"] == os.path.abspath(bare_git_repo)
        assert second.get("note")
        assert "not overwritten" in second["note"]
        assert os.stat(config_path).st_mtime_ns == before_mtime
        assert open(config_path).read() == before_content

    def test_repo_init_customized_config_not_clobbered(
        self, mcp_server: Any, bare_git_repo: Any
    ) -> None:
        """A user-edited config survives a repo.init call byte-for-byte."""
        cfg_dir = os.path.join(bare_git_repo, ".gitreins")
        os.makedirs(cfg_dir)
        custom = os.path.join(cfg_dir, "config.yaml")
        with open(custom, "w") as f:
            f.write("guards:\n  tests: false\n")
        result = _call(mcp_server, "repo.init", {"workdir": bare_git_repo})
        assert result.get("created") is False, result
        with open(custom) as f:
            assert f.read() == "guards:\n  tests: false\n"

    def test_repo_init_plain_dir_errors_writes_nothing(
        self, mcp_server: Any, plain_dir: Any
    ) -> None:
        """Non-git dir → error dict, and nothing at all was created."""
        result = _call(mcp_server, "repo.init", {"workdir": plain_dir})
        assert "error" in result, result
        assert os.path.abspath(plain_dir) in result["error"]
        assert ".git" in result["error"]
        assert result["workdir"] == os.path.abspath(plain_dir)
        assert not os.path.exists(os.path.join(plain_dir, ".gitreins"))

    def test_repo_init_defaults_to_server_workdir(self, tmp_path: Path) -> None:
        """No workdir argument → the MCP server's own workdir is initialized."""
        repo = tmp_path / "server-repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        server = GitReinsMCPServer(str(repo))
        result = _call(server, "repo.init", {})
        assert result.get("created") is True, result
        assert os.path.isfile(os.path.join(str(repo), ".gitreins", "config.yaml"))

    def test_guard_run_works_after_repo_init(
        self, mcp_server: Any, bare_git_repo: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The end-to-end MCP-only path: repo.init then guard.run — guard.run
        must NOT return the GR-GAP-054 'no .gitreins/config.yaml — run
        `gitreins init` first' refusal anymore (DF-GITREINS-POC-77)."""
        init_result = _call(mcp_server, "repo.init", {"workdir": bare_git_repo})
        assert init_result.get("created") is True, init_result

        guard_result = _call(mcp_server, "guard.run", {"workdir": bare_git_repo})
        assert "error" not in guard_result, guard_result
        assert guard_result["passed"] is True
        assert guard_result["workdir"] == os.path.abspath(bare_git_repo)
