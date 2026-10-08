"""Reverse parity test: specs/02-MCP-Protocol.md must match the live MCP tool surface.

DOC-12: the spec drifted from the live server (claimed 9 tools while the surface grew
to 15). These tests fail when a live `tools/list` name is missing from the spec's tool
catalogue, or when the spec names a tool the live server no longer exposes — so the
spec cannot silently drift again.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SPEC_PATH = REPO_ROOT / "specs" / "02-MCP-Protocol.md"
SERVER_MODULE = "gitreins_mcp.server"


def _live_tool_names() -> list[str]:
    """Run tools/list against the real stdio server and return the tool names."""
    request = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    proc = subprocess.run(
        [sys.executable, "-m", SERVER_MODULE],
        input=request + "\n",
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, f"server exited {proc.returncode}: {proc.stderr[-500:]}"
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        resp = json.loads(line)
        if resp.get("id") == 2:
            tools = resp["result"]["tools"]
            return [t["name"] for t in tools]
    raise AssertionError(f"no tools/list response for id=2 in stdout:\n{proc.stdout[:500]}")


def _spec_text() -> str:
    return SPEC_PATH.read_text(encoding="utf-8")


def _spec_catalog_names(spec: str) -> set[str]:
    """Tool names from the §8 catalogue table (rows like `| 3 | ``task.create`` | ...`)."""
    names: set[str] = set()
    for match in re.finditer(r"^\|\s*\d+\s*\|\s*`([a-z]+(?:\.[a-z]+)?)`\s*\|", spec, re.M):
        names.add(match.group(1))
    return names


class TestMcpSpecParity:
    def test_spec_covers_every_live_tool(self) -> None:
        live = _live_tool_names()
        spec = _spec_text()
        for name in live:
            assert name in spec, (
                f"live tool {name!r} missing from {SPEC_PATH.name} — refresh the tool "
                "catalogue (§8) from the live tools/list surface"
            )

    def test_spec_names_no_removed_tool(self) -> None:
        live = set(_live_tool_names())
        spec = _spec_text()
        catalogued = _spec_catalog_names(spec)
        stale = catalogued - live
        assert not stale, (
            f"spec catalogue names non-live tools {sorted(stale)} — remove them or the "
            "live surface changed and the spec needs regeneration"
        )

    def test_catalogue_table_lists_exactly_the_live_set(self) -> None:
        live = _live_tool_names()
        spec = _spec_text()
        catalogued = _spec_catalog_names(spec)
        assert catalogued == set(live), (
            f"catalogue table vs live surface mismatch: spec-only={sorted(catalogued - set(live))} "
            f"live-only={sorted(set(live) - catalogued)}"
        )

    def test_every_live_tool_has_a_catalogue_entry(self) -> None:
        live = _live_tool_names()
        spec = _spec_text()
        catalogued = _spec_catalog_names(spec)
        missing = set(live) - catalogued
        assert not missing, f"live tools absent from the §8 catalogue table: {sorted(missing)}"

    def test_each_live_tool_appears_in_scope_or_status_sections(self) -> None:
        live = _live_tool_names()
        spec = _spec_text()
        # The tool catalogue (§8) is the authority; the Implementation Status (§14)
        # table must also name every live tool.
        status_section = spec.split("## 14. Implementation Status", 1)[1]
        for name in live:
            assert f"| {name} |" in status_section, (
                f"live tool {name!r} missing a row in §14 Implementation Status"
            )

    def test_scope_count_tracks_live_surface(self) -> None:
        live = _live_tool_names()
        spec = _spec_text()
        scope = spec.split("## 2. Scope", 1)[1].split("## 3.", 1)[0]
        match = re.search(r"(\d+) exposed tools", scope)
        assert match, "Scope section must state '<N> exposed tools'"
        assert int(match.group(1)) == len(live), (
            f"Scope claims {match.group(1)} exposed tools but live surface is "
            f"{len(live)} ({', '.join(live)})"
        )
