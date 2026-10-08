"""Version identity tests — GR-GAP-030.

Asserts the single source of truth: gitreins.__version__ and the CLI --version
output both match the version declared in pyproject.toml (so the test does not
rot on the next version bump).
"""

import os
import re
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    if sys.version_info >= (3, 11):
        import tomllib
    else:
        import tomli as tomllib  # type: ignore[import-not-found]
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as f:
        return tomllib.load(f)["project"]["version"]


def test_gitreins_module_reexports_version():
    """gitreins.__version__ matches the pyproject.toml version."""
    import gitreins

    assert gitreins.__version__ == _pyproject_version()


def test_cli_version_flag_matches_pyproject():
    """`gitreins --version` prints the pyproject.toml version."""
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", "")
    if str(PROJECT_ROOT) not in env["PYTHONPATH"]:
        env["PYTHONPATH"] = str(PROJECT_ROOT) + (
            ":" + env["PYTHONPATH"] if env["PYTHONPATH"] else ""
        )
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "gitreins" / "cli.py"), "--version"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"gitreins {_pyproject_version()}"


def test_mcp_server_identity_matches_package_version(tmp_path):
    """DF-GITREINS-POC-5 — the MCP handshake reports the installed release.

    The three surfaces a user reads used to disagree (CLI 0.12.1 / README
    0.12.0 / MCP 0.1.0); the MCP half was a frozen literal. This pins the
    handshake to the same source of truth as `gitreins --version`.
    """
    from engine.version import __version__
    from gitreins_mcp.server import GitReinsMCPServer

    server = GitReinsMCPServer(str(tmp_path))
    response = server.handle_request({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    assert response["result"]["serverInfo"]["version"] == __version__
    assert __version__ == _pyproject_version()


def test_docs_do_not_pin_a_frozen_mcp_server_version():
    """docs/mcp-api.md names the live version (or none) — never a stale literal.

    A doc that hardcodes `"version": "0.1.0"` is how the surfaces drifted in
    the first place; the version a client should trust is the handshake's.
    """
    version = _pyproject_version()
    text = (PROJECT_ROOT / "docs" / "mcp-api.md").read_text(encoding="utf-8")
    pinned = re.findall(r'"version":\s*"(\d+\.\d+\.\d+)"', text)
    assert all(v == version for v in pinned), f"docs/mcp-api.md pins {pinned}; live is {version}"
    # The doc must name a version-FREE way to read the identity.
    assert "gitreins_mcp.server --version" in text
