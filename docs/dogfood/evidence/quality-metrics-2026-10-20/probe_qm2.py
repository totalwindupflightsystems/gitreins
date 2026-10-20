#!/usr/bin/env python3
"""Dogfood probe battery #2 — same surface, but the artifact is fully controllable.

Key fix vs probe #1: the doc's producer hardcodes 72.3 and REWRITES the artifact on
every run, so a hand-written artifact never reached the reader. Here the configured
`quality.command` copies a fixture into the artifact path, so the value under test
is the one GitReins actually reads.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/home/kara/.hermes/stand-in/dogfood/gitreins/qm-scratch-20261020")
CFG = ROOT / ".gitreins" / "config.yaml"
ART = ROOT / ".gitreins" / "quality.json"
FIX = ROOT / "quality" / "artifact.json"
ENV = {"PATH": "/home/kara/gitreins/.venv/bin:/usr/bin:/bin", "HOME": "/home/kara"}
COPY_CMD = "cp quality/artifact.json .gitreins/quality.json"


def write_quality(cfg: dict) -> None:
    text = CFG.read_text()
    lines = text.splitlines()
    out, i, done = [], 0, False
    while i < len(lines):
        if lines[i].startswith("quality:"):
            out += [
                "quality:",
                f"  enabled: {str(cfg['enabled']).lower()}",
                f'  command: "{cfg["command"]}"',
                f'  artifact_path: "{cfg["artifact_path"]}"',
                f'  targets_source: "{cfg["targets_source"]}"',
                "  per_metric_mode:",
            ]
            for k, v in (cfg["per_metric_mode"] or {}).items():
                out.append(f"    {k}: {v}")
            if not cfg["per_metric_mode"]:
                out[-1] = "  per_metric_mode: {}"
            out.append(f"  timeout: {cfg['timeout']}")
            done = True
            i += 1
            while i < len(lines) and lines[i].startswith(" "):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    assert done
    CFG.write_text("\n".join(out) + "\n")


BASE = {
    "enabled": True,
    "command": COPY_CMD,
    "artifact_path": ".gitreins/quality.json",
    "targets_source": "quality/ladder.json",
    "per_metric_mode": {"type_hint_pct": "block"},
    "timeout": 300,
}


def fixture(obj) -> None:
    FIX.write_text(obj if isinstance(obj, str) else json.dumps(obj, indent=2) + "\n")


def run(*args: str) -> tuple[int, str, float]:
    t0 = time.time()
    p = subprocess.run(["gitreins", *args], cwd=ROOT, env=ENV, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr, round(time.time() - t0, 3)


def show(name: str, rc: int, out: str, wall: float, raw: bool = False) -> None:
    print(f"\n### {name}  exit={rc} wall={wall}s")
    for ln in out.splitlines():
        if "quality" in ln.lower() or "diff-scope" in ln.lower():
            print("   |", ln.rstrip())
    if raw:
        print("   --- full raw ---")
        for ln in out.splitlines():
            print("   >", ln.rstrip())


def main() -> int:
    (ROOT / "quality" / "ladder.json").write_text('{"type_hint_pct": 80}\n')

    print("=" * 78)
    print("PROBE A — block mode, 72.3 < target 80  (doc: guard FAILS)")
    fixture({"metrics": {"type_hint_pct": {"value": 72.3, "target": 80, "stage": "current"}}})
    write_quality(BASE)
    rc, out, w = run("guard")
    show("A block+below", rc, out, w, raw=True)

    print("=" * 78)
    print("PROBE B — block mode, 95 >= target 80  (doc: guard PASSES)")
    fixture({"metrics": {"type_hint_pct": {"value": 95, "target": 80, "stage": "current"}}})
    rc, out, w = run("guard")
    show("B block+above", rc, out, w, raw=True)

    print("=" * 78)
    print("PROBE C — TRUST: artifact self-declares target 10 while repo ladder says 80, mode=block")
    fixture({"metrics": {"type_hint_pct": {"value": 72.3, "target": 10, "stage": "current"}}})
    rc, out, w = run("guard")
    show("C artifact target 10 vs ladder 80", rc, out, w, raw=True)

    print("=" * 78)
    print("PROBE C2 — artifact target 90, no ladder consult? (ladder says 80)")
    fixture({"metrics": {"type_hint_pct": {"value": 85, "target": 90, "stage": "current"}}})
    rc, out, w = run("guard")
    show("C2 artifact target 90 vs ladder 80", rc, out, w, raw=True)

    print("=" * 78)
    print(
        "PROBE C3 — artifact declares NO target, ladder says 80, mode=block: where does target come from?"  # noqa: E501
    )
    fixture({"metrics": {"type_hint_pct": {"value": 72.3, "stage": "current"}}})
    rc, out, w = run("guard")
    show("C3 no target in artifact", rc, out, w, raw=True)

    print("=" * 78)
    print("PROBE D — non-numeric value ('n/a'), mode=block")
    fixture({"metrics": {"type_hint_pct": {"value": "n/a", "target": 80}}})
    rc, out, w = run("guard")
    show("D string value", rc, out, w, raw=True)

    print("=" * 78)
    print("PROBE E — metric not listed in per_metric_mode (doc: defaults to warn)")
    fixture({"metrics": {"wiring_pct": {"value": 10, "target": 90, "stage": "beta"}}})
    write_quality({**BASE, "per_metric_mode": {}})
    rc, out, w = run("guard")
    show("E unlisted metric", rc, out, w, raw=True)

    print("=" * 78)
    print("PROBE F — judge sees the snapshot? (judge tier1 quality_snapshot + verdict.json)")
    fixture({"metrics": {"type_hint_pct": {"value": 72.3, "target": 80, "stage": "current"}}})
    write_quality({**BASE, "per_metric_mode": {"type_hint_pct": "warn"}})
    rc, out, w = run("guard", "--json")
    show("F1 guard --json", rc, out, w)
    rc, out, w = run("doctor", "--json")
    show("F2 doctor --json", rc, out, w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
