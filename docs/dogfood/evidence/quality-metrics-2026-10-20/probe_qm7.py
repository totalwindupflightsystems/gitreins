#!/usr/bin/env python3
"""Dogfood probe battery #7 — clean A/B on the judge surface + judge --json shape."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

ROOT = Path("/home/kara/.hermes/stand-in/dogfood/gitreins/qm-scratch-20261020")
CFG = ROOT / ".gitreins" / "config.yaml"
ENV = {"PATH": "/home/kara/gitreins/.venv/bin:/usr/bin:/bin", "HOME": "/home/kara"}
COPY = "cp quality/artifact.json .gitreins/quality.json"


def write_quality(cmd: str, modes: dict) -> None:
    text = CFG.read_text().splitlines()
    out, i, done = [], 0, False
    while i < len(text):
        if text[i].startswith("quality:"):
            out += [
                "quality:",
                "  enabled: true",
                f'  command: "{cmd}"',
                '  artifact_path: ".gitreins/quality.json"',
                '  targets_source: "quality/ladder.json"',
            ]
            if modes:
                out.append("  per_metric_mode:")
                out += [f"    {k}: {v}" for k, v in modes.items()]
            else:
                out.append("  per_metric_mode: {}")
            out.append("  timeout: 300")
            done = True
            i += 1
            while i < len(text) and text[i].startswith(" "):
                i += 1
            continue
        out.append(text[i])
        i += 1
    assert done
    CFG.write_text("\n".join(out) + "\n")


def sh(*args):
    return subprocess.run(args, cwd=ROOT, env=ENV, capture_output=True, text=True)


def run(*args):
    t0 = time.time()
    p = sh("gitreins", *args)
    return p.returncode, p.stdout + p.stderr, round(time.time() - t0, 3)


def commit(msg):
    sh("git", "add", "-A")
    sh("git", "commit", "-q", "-m", msg, "--no-verify")


FIX = ROOT / "quality" / "artifact.json"
FIX.write_text(
    json.dumps(
        {
            "class_floor": ["unit"],
            "metrics": {"type_hint_pct": {"value": 72.3, "target": 80, "stage": "current"}},
            "surfaces": {
                "COV-1": {
                    "files": ["src/calc.py"],
                    "test_links": ["tests/test_calc.py"],
                    "classes": ["unit"],
                }
            },
        },
        indent=2,
    )
    + "\n"
)

print("=" * 78)
print("A/B — lint is clean now (my probe producers are gone). Which lane fails the judge?")
write_quality(COPY, {"type_hint_pct": "warn"})
commit("probe7: warn config")
rc, out, w = run("judge", "QM-1", "--skip-tier2", "--json")
doc = json.loads(out[out.index("{") : out.rindex("}") + 1])
print("  WARN mode: exit", rc, "outcome:", doc.get("outcome"), "passed:", doc.get("passed"))
for c in doc.get("checks", []):
    print(
        "     check",
        c.get("id"),
        c.get("outcome"),
        "| data keys:",
        sorted((c.get("data") or {}).keys()),
    )

write_quality(COPY, {"type_hint_pct": "block"})
commit("probe7: block config")
rc, out, w = run("judge", "QM-1", "--skip-tier2")
print(f"\n  BLOCK mode human output (exit {rc}, {w}s):")
for ln in out.splitlines():
    print("   >", ln.rstrip())

rc, out, w = run("judge", "QM-1", "--skip-tier2", "--json")
doc = json.loads(out[out.index("{") : out.rindex("}") + 1])
print(
    "\n  BLOCK mode --json: exit", rc, "outcome:", doc.get("outcome"), "passed:", doc.get("passed")
)
print("  'quality_snapshot' as a JSON key anywhere in the envelope?", "quality_snapshot" in out)
for c in doc.get("checks", []):
    print(
        "     check",
        c.get("id"),
        c.get("outcome"),
        "| data keys:",
        sorted((c.get("data") or {}).keys()),
    )
    if c.get("id") == "stage-tier1":
        print("     stage-tier1 data:", json.dumps(c.get("data"))[:400])
print("  summary text:")
for ln in str(doc.get("summary", "")).splitlines():
    print("     |", ln)
