#!/usr/bin/env python3
"""Dogfood probe battery #3 — diff-scoped surfaces (GR-145) + the blocking-line shape."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

ROOT = Path("/home/kara/.hermes/stand-in/dogfood/gitreins/qm-scratch-20261020")
CFG = ROOT / ".gitreins" / "config.yaml"
FIX = ROOT / "quality" / "artifact.json"
ENV = {"PATH": "/home/kara/gitreins/.venv/bin:/usr/bin:/bin", "HOME": "/home/kara"}


def write_quality(cfg: dict) -> None:
    text = CFG.read_text().splitlines()
    out, i, done = [], 0, False
    while i < len(text):
        if text[i].startswith("quality:"):
            out += [
                "quality:",
                f"  enabled: {str(cfg['enabled']).lower()}",
                f'  command: "{cfg["command"]}"',
                f'  artifact_path: "{cfg["artifact_path"]}"',
                f'  targets_source: "{cfg["targets_source"]}"',
            ]
            if cfg["per_metric_mode"]:
                out.append("  per_metric_mode:")
                out += [f"    {k}: {v}" for k, v in cfg["per_metric_mode"].items()]
            else:
                out.append("  per_metric_mode: {}")
            out.append(f"  timeout: {cfg['timeout']}")
            done = True
            i += 1
            while i < len(text) and text[i].startswith(" "):
                i += 1
            continue
        out.append(text[i])
        i += 1
    assert done
    CFG.write_text("\n".join(out) + "\n")


BASE = {
    "enabled": True,
    "command": "cp quality/artifact.json .gitreins/quality.json",
    "artifact_path": ".gitreins/quality.json",
    "targets_source": "quality/ladder.json",
    "per_metric_mode": {"type_hint_pct": "warn"},
    "timeout": 300,
}


def run(*args: str) -> tuple[int, str, float]:
    t0 = time.time()
    p = subprocess.run(["gitreins", *args], cwd=ROOT, env=ENV, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr, round(time.time() - t0, 3)


def show(name, rc, out, wall, raw=True):
    print(f"\n### {name}  exit={rc} wall={wall}s")
    if raw:
        for ln in out.splitlines():
            print("   >", ln.rstrip())


SURFACES = {
    "class_floor": ["unit", "integration"],
    "metrics": {"type_hint_pct": {"value": 72.3, "target": 80, "stage": "current"}},
    "surfaces": {
        "COV-1": {
            "files": ["src/calc.py"],
            "test_links": ["tests/test_calc.py"],
            "classes": ["unit"],
        },
        "COV-2": {"files": ["src/other.py"], "test_links": [], "classes": []},
    },
}

print("=" * 78)
print("G1 — surfaces model + a STAGED change to src/calc.py (warn mode, value below target)")
(ROOT / "src" / "calc.py").write_text(
    "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
)
subprocess.run(["git", "add", "src/calc.py"], cwd=ROOT, check=False)
FIX.write_text(json.dumps(SURFACES, indent=2) + "\n")
write_quality(BASE)
rc, out, w = run("guard")
show("G1 surfaces + staged change", rc, out, w)

print("=" * 78)
print("G2 — SAME staged diff, but mode=block and 72.3 < 80: what does the ✗ line say?")
write_quality({**BASE, "per_metric_mode": {"type_hint_pct": "block"}})
rc, out, w = run("guard")
show("G2 block fail WITH surfaces", rc, out, w)

print("=" * 78)
print("G2b — the same failing run as machine JSON (guard --json): is the metric there?")
rc, out, w = run("guard", "--json")
print(f"### G2b guard --json  exit={rc}")
try:
    doc = json.loads([ln for ln in out.splitlines() if ln.strip().startswith("{")][0])
    for chk in doc.get("checks", []):
        print(f"    check id={chk['id']} outcome={chk['outcome']} passed={chk['passed']}")
        print(f"      summary={chk.get('summary', '')!r}")
    print("    outcome:", doc.get("outcome"), "passed:", doc.get("passed"))
except Exception as exc:  # noqa: BLE001
    print("    parse failed:", exc)
    print(out[:800])

print("=" * 78)
print(
    "G3 — clean tree (nothing staged/unstaged): doc says 'a diff that matches no surface is no_diff'"  # noqa: E501
)
subprocess.run(["git", "checkout", "--", "src/calc.py"], cwd=ROOT, check=False)
subprocess.run(["git", "reset", "-q"], cwd=ROOT, check=False)
st = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True)
print("    git status --porcelain:", st.stdout.splitlines()[:5])
write_quality(BASE)
rc, out, w = run("guard")
show("G3 no diff", rc, out, w)

print("=" * 78)
print("G4 — producer counter: how many times is the producer invoked by one guard run, and")
print("     does a second surface (doctor) reuse or re-run it?")
counter = ROOT / "quality" / "counter.txt"
counter.write_text("0\n")
script = ROOT / "scripts" / "counting_producer.py"
script.write_text(
    "from pathlib import Path\n"
    "c = Path('quality/counter.txt')\n"
    "c.write_text(str(int(c.read_text().strip()) + 1) + '\\n')\n"
    "Path('.gitreins/quality.json').write_text(Path('quality/artifact.json').read_text())\n"
)
FIX.write_text(json.dumps(SURFACES, indent=2) + "\n")
write_quality({**BASE, "command": "python3 scripts/counting_producer.py"})
rc, out, w = run("guard")
print(f"    after guard:    counter = {counter.read_text().strip()}  (guard exit {rc})")
rc, out, w = run("doctor")
print(f"    after doctor:   counter = {counter.read_text().strip()}  (doctor exit {rc})")
rc, out, w = run("guard")
print(f"    after 2nd guard: counter = {counter.read_text().strip()}")
