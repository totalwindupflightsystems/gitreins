"""DOC-14: the deployment doc's MCP claims must match the live tool surface.

specs/10-Deployment.md section 4 (MCP Server Deployment) makes three claims that
can silently drift when ``gitreins_mcp/server.py`` changes:

1. the deployment tool list names every tool the server actually serves;
2. the workdir set it describes matches the schemas that actually carry a
   ``workdir`` property (the workdir-capable tools);
3. the tool names it lists are aligned with ``docs/mcp-api.md`` (the canonical
   MCP tool catalog).

The doc's numeric claims (counts) live in a machine-readable block at the end of
section 4.4, marked ``<!-- deployment-mcp-contract: ... -->`` so this test can
parse them without scraping prose. Prose itself is checked for the load-bearing
phrases: that ``GITREINS_WORKDIR`` is documented as wrapper-local and that the
"every tool accepts workdir" over-claim is not present.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from gitreins_mcp.server import GitReinsMCPServer  # noqa: E402

SPEC_PATH = REPO_ROOT / "specs" / "10-Deployment.md"
API_DOC_PATH = REPO_ROOT / "docs" / "mcp-api.md"

CONTRACT_RE = re.compile(r"<!--\s*deployment-mcp-contract:\s*(?P<body>.*?)-->", re.DOTALL)


def _live_schemas() -> list[dict]:
    """Return the server's tool schemas (the same list tools/list serves)."""
    server = GitReinsMCPServer.__new__(GitReinsMCPServer)
    return GitReinsMCPServer._tool_schemas(server)


def _live_names() -> list[str]:
    return [t["name"] for t in _live_schemas()]


def _live_workdir_names() -> list[str]:
    return [
        t["name"]
        for t in _live_schemas()
        if "workdir" in t.get("inputSchema", {}).get("properties", {})
    ]


def _doc_text() -> str:
    return SPEC_PATH.read_text(encoding="utf-8")


def _deployment_section() -> str:
    text = _doc_text()
    start = text.index("## 4. MCP Server Deployment")
    return text[start:]


def _parse_contract() -> dict[str, object]:
    text = _doc_text()
    m = CONTRACT_RE.search(text)
    if not m:
        raise AssertionError(
            "specs/10-Deployment.md is missing the "
            "<!-- deployment-mcp-contract: ... --> machine-readable block; "
            "the parity test cannot verify counts against it."
        )
    body = m.group("body")
    pairs = dict(part.split("=", 1) for part in body.replace("\n", " ").split(";") if part.strip())
    return {k.strip(): v.strip() for k, v in pairs.items()}


def test_contract_block_matches_live_surface() -> None:
    contract = _parse_contract()
    live = _live_names()
    wd = _live_workdir_names()
    assert int(contract["tool_count"]) == len(live), (
        f"contract tool_count={contract['tool_count']} but live server has {len(live)} tools: {sorted(live)}"
    )
    assert int(contract["workdir_tool_count"]) == len(wd), (
        f"contract workdir_tool_count={contract['workdir_tool_count']} but {len(wd)} live schemas carry workdir: {sorted(wd)}"
    )
    assert sorted(contract["no_workdir_tools"].split(",")) == sorted(set(live) - set(wd)), (
        "contract no_workdir_tools does not match the live no-workdir set"
    )


def test_deployment_tool_list_names_every_live_tool() -> None:
    """Every live tool name must appear in the deployment section's tool list."""
    section = _deployment_section()
    for name in _live_names():
        assert f"`{name}`" in section, (
            f"deployment tool list is missing live tool `{name}` — "
            "specs/10-Deployment.md §4 has drifted from gitreins_mcp/server.py"
        )


def test_deployment_tool_list_names_match_mcp_api_doc() -> None:
    """Every name the deployment list shares the canonical tool catalog's names."""
    api_text = API_DOC_PATH.read_text(encoding="utf-8")
    api_names = set(re.findall(r"^### \d+\. `([a-z.]+)`", api_text, re.M))
    assert api_names == set(_live_names()), (
        f"docs/mcp-api.md tool catalog {sorted(api_names)} != live server {sorted(_live_names())}"
    )
    for name in _live_names():
        assert f"`{name}`" in _deployment_section(), f"`{name}` missing from deployment tool list"


def test_workdir_claim_is_exact_not_every_tool() -> None:
    section = _deployment_section()
    assert "Every MCP tool accepts an optional `workdir` parameter" not in section, (
        "the 'every MCP tool accepts workdir' over-claim is back in §4.4 — "
        f"only {len(_live_workdir_names())}/{len(_live_names())} live schemas carry workdir"
    )
    no_wd = sorted(set(_live_names()) - set(_live_workdir_names()))
    for name in no_wd:
        assert name in section, f"§4.4 must explain why `{name}` has no workdir property"


def test_gitreins_workdir_documented_as_wrapper_local() -> None:
    text = _doc_text()
    assert "wrapper-local" in text, (
        "specs/10-Deployment.md must state explicitly that GITREINS_WORKDIR is "
        "wrapper-local: the server itself reads no env var — the workdir comes "
        "from the constructor/positional argument (server.py __init__)."
    )
    # The over-claim of a native server variable must not return.
    assert "Default working directory for the MCP server. All tool calls" not in text, (
        "the pre-DOC-14 env-table row describing GITREINS_WORKDIR as a native "
        "server variable is back in §4.3"
    )
