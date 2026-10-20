#!/usr/bin/env python3
"""Dogfood probe — MCP `quality.status` contract from docs/mcp-api.md §13.

Documented claims under test:
  1. quality.status on a repo with quality enabled and NOTHING computed yet
     -> {"status": "not-computed", "note": "...call guard.run (or judge.evaluate) first"}
  2. "This tool never triggers the producer."
  3. after guard.run, quality.status returns the SAME snapshot dict.
  4. quality disabled/absent -> {"status": "disabled"}
"""

from __future__ import annotations

import json
import subprocess
import sys
from itertools import count
from pathlib import Path

ROOT = Path("/home/kara/.hermes/stand-in/dogfood/gitreins/qm-scratch-20261020")
CFG = ROOT / ".gitreins" / "config.yaml"
FIX = ROOT / "quality" / "artifact.json"
COUNTER = ROOT / "quality" / "counter.txt"
ENV = {
    "PATH": "/home/kara/gitreins/.venv/bin:/usr/bin:/bin",
    "HOME": "/home/kara",
    # keep the producer deterministic and countable
}


def write_quality(enabled: bool) -> None:
    text = CFG.read_text().splitlines()
    out, i, done = [], 0, False
    while i < len(text):
        if text[i].startswith("quality:"):
            out += [
                "quality:",
                f"  enabled: {str(enabled).lower()}",
                '  command: "python3 scripts/counting_producer.py"',
                '  artifact_path: ".gitreins/quality.json"',
                '  targets_source: "quality/ladder.json"',
                "  per_metric_mode:",
                "    type_hint_pct: warn",
                "  timeout: 300",
            ]
            done = True
            i += 1
            while i < len(text) and text[i].startswith(" "):
                i += 1
            continue
        out.append(text[i])
        i += 1
    assert done
    CFG.write_text("\n".join(out) + "\n")


def counter() -> str:
    return COUNTER.read_text().strip()


class Server:
    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            ["python3", "-m", "gitreins_mcp.server"],
            cwd=ROOT,
            env=ENV,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        self.ids = count(1)

    def call(self, method: str, **params):
        req = {"jsonrpc": "2.0", "id": next(self.ids), "method": method}
        if params:
            req["params"] = params
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        return json.loads(line) if line.strip() else None

    def tool(self, name: str, args: dict):
        resp = self.call("tools/call", name=name, arguments=args)
        if resp is None:
            return None
        if "error" in resp:
            return resp
        text = resp["result"]["content"][0]["text"]
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def close(self):
        self.proc.stdin.close()
        out = self.proc.wait(timeout=30)
        err = self.proc.stderr.read()
        return out, err


def brief(obj, keys=("status", "workdir", "note", "quality_snapshot")):
    if not isinstance(obj, dict):
        return obj
    return {k: obj[k] for k in keys if k in obj}


print("=" * 78)
print("MCP-1 — quality ENABLED, nothing computed yet: does quality.status answer not-computed")
print("        and does it leave the producer UNRUN (counter stays 0)?")
write_quality(True)
FIX.write_text(
    json.dumps(
        {"metrics": {"type_hint_pct": {"value": 72.3, "target": 80, "stage": "current"}}}, indent=2
    )
    + "\n"
)
COUNTER.write_text("0\n")
s = Server()
init = s.call(
    "initialize",
    protocolVersion="2025-11-25",
    capabilities={},
    clientInfo={"name": "dogfood", "version": "1.0"},
)
print("  initialize protocolVersion:", init["result"].get("protocolVersion"))
s.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
s.proc.stdin.flush()
tools = s.call("tools/list")
print("  tools/list count:", len(tools["result"]["tools"]))
qs = s.tool("quality.status", {})
print("  quality.status BEFORE any guard run:", brief(qs))
print("  producer counter after quality.status:", counter())
gr = s.tool("guard.run", {})
print("  guard.run status:", brief(gr, ("status", "outcome", "passed", "quality_snapshot")))
print("  producer counter after guard.run:", counter())
qs2 = s.tool("quality.status", {})
print("  quality.status AFTER guard.run:", brief(qs2))
print("  producer counter after 2nd quality.status:", counter())
same = qs2.get("quality_snapshot") == gr.get("quality_snapshot") if isinstance(qs2, dict) else None
print("  snapshot identical to guard.run's? ", same)
rc, err = s.close()
print("  server exit:", rc)
print("  stderr tail:", [ln for ln in err.splitlines() if ln.strip()][-3:])

print("=" * 78)
print("MCP-2 — quality DISABLED: doc says {'status': 'disabled'}")
write_quality(False)
COUNTER.write_text("0\n")
s = Server()
s.call(
    "initialize",
    protocolVersion="2025-11-25",
    capabilities={},
    clientInfo={"name": "dogfood", "version": "1.0"},
)
s.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
s.proc.stdin.flush()
print("  quality.status (disabled):", brief(s.tool("quality.status", {})))
print("  producer counter:", counter())
rc, err = s.close()
print("  server exit:", rc)

print("=" * 78)
print("MCP-3 — quality.status with a workdir that does not exist (documented refusal)")
write_quality(True)
s = Server()
s.call(
    "initialize",
    protocolVersion="2025-11-25",
    capabilities={},
    clientInfo={"name": "dogfood", "version": "1.0"},
)
s.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}) + "\n")
s.proc.stdin.flush()
print("  ->", s.tool("quality.status", {"workdir": "/tmp/gitreins-typo"}))
rc, err = s.close()
print("  server exit:", rc)
sys.exit(0)
