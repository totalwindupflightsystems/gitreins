#!/usr/bin/env python3
"""Dogfood probe battery #6 — refined: artifact pollution of diff-scope, judge leg clean."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

ROOT = Path("/home/kara/.hermes/stand-in/dogfood/gitreins/qm-scratch-20261020")
CFG = ROOT / ".gitreins" / "config.yaml"
FIX = ROOT / "quality" / "artifact.json"
ENV = {"PATH": "/home/kara/gitreins/.venv/bin:/usr/bin:/bin", "HOME": "/home/kara"}


def write_quality(cmd: str, modes: dict, enabled: bool = True) -> None:
    text = CFG.read_text().splitlines()
    out, i, done = [], 0, False
    while i < len(text):
        if text[i].startswith("quality:"):
            out += [
                "quality:",
                f"  enabled: {str(enabled).lower()}",
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


COPY = "cp quality/artifact.json .gitreins/quality.json"
TS = "python3 scripts/quality_metrics_ts.py"
SURF = {
    "class_floor": ["unit"],
    "metrics": {"type_hint_pct": {"value": 72.3, "target": 80, "stage": "current"}},
    "surfaces": {
        "COV-1": {
            "files": ["src/calc.py"],
            "test_links": ["tests/test_calc.py"],
            "classes": ["unit"],
        }
    },
}
# make the scratch lint-clean so the judge's tier1 is not failed by my own probe files
ts = ROOT / "scripts" / "quality_metrics_ts.py"
ts.write_text(
    "import datetime\n"
    "import json\n"
    "from pathlib import Path\n"
    "\n"
    "ladder = json.loads(Path('quality/ladder.json').read_text())\n"
    "artifact = {\n"
    "    'metrics': {'type_hint_pct': {'value': 72.3, 'target': ladder['type_hint_pct'], 'stage': 'current'}},\n"  # noqa: E501
    "    'produced_at': datetime.datetime.now().isoformat(),\n"
    "}\n"
    "Path('.gitreins/quality.json').write_text(json.dumps(artifact, indent=2) + '\\n')\n"
)

print("=" * 78)
print("R1 — A/B the diff-scope CHANGED-FILE COUNT with the artifact clean vs the artifact")
print("     rewritten by a producer that stamps produced_at (both with one real staged change)")
FIX.write_text(json.dumps(SURF, indent=2) + "\n")
write_quality(COPY, {"type_hint_pct": "warn"})
run("guard")
commit("probe6: baseline (deterministic artifact)")
(ROOT / "src" / "calc.py").write_text(
    "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a * b\n"
)
sh("git", "add", "src/calc.py")
rc, out, w = run("guard")
print(f"  A) deterministic producer, 1 real staged file  (exit {rc}, {w}s)")
for ln in out.splitlines():
    if "diff-scope" in ln or "surface" in ln:
        print("     >", ln.rstrip())
sh("git", "reset", "-q")

print()
write_quality(TS, {"type_hint_pct": "warn"})
run("guard")  # rewrite artifact with a fresh timestamp
commit("probe6: timestamped artifact")
(ROOT / "src" / "calc.py").write_text(
    "def add(a, b):\n    return a + b\n\n\ndef div(a, b):\n    return a / b\n"
)
sh("git", "add", "src/calc.py")
rc, out, w = run("guard")
print(f"  B) timestamped producer, 1 real staged file  (exit {rc}, {w}s)")
for ln in out.splitlines():
    if "diff-scope" in ln or "surface" in ln:
        print("     >", ln.rstrip())
st = sh("git", "status", "--porcelain")
print("     git status --porcelain:", st.stdout.splitlines())
rc2, out2, _ = run("guard", "--json")
try:
    doc = json.loads([ln for ln in out2.splitlines() if ln.strip().startswith("{")][0])
    for chk in doc.get("checks", []):
        if chk["id"] == "quality":
            print("     JSON quality summary:", repr(chk["summary"])[:300])
except Exception as exc:  # noqa: BLE001
    print("     json parse failed:", exc)
sh("git", "reset", "-q")
sh("git", "checkout", "--", "src/calc.py")

print("=" * 78)
print("R2 — judge leg, lint-clean scratch: block mode below target")
FIX.write_text(json.dumps(SURF, indent=2) + "\n")
write_quality(COPY, {"type_hint_pct": "block"})
commit("probe6: block mode config")
rc, out, w = run("judge", "QM-1", "--skip-tier2")
print(f"### judge block  exit={rc} wall={w}s")
for ln in out.splitlines():
    print("   >", ln.rstrip())

print("=" * 78)
print("R3 — judge leg, warn mode below target: the doc says quality cannot fail the judge")
write_quality(COPY, {"type_hint_pct": "warn"})
rc, out, w = run("judge", "QM-1", "--skip-tier2")
print(f"### judge warn   exit={rc} wall={w}s")
for ln in out.splitlines():
    print("   >", ln.rstrip())

print("=" * 78)
print("R4 — judge --json: where does a machine client find the quality snapshot?")
rc, out, w = run("judge", "QM-1", "--skip-tier2", "--json")
print(f"### judge --json exit={rc}")
try:
    doc = json.loads(out[out.index("{") : out.rindex("}") + 1])
    print("    keys:", sorted(doc.keys()))
    print("    summary contains 'type_hint_pct'?", "type_hint_pct" in (doc.get("summary") or ""))
    print("    checks ids:", [c.get("id") for c in doc.get("checks", [])])
    for chk in doc.get("checks", []):
        if chk.get("id") == "quality":
            print("    quality check:", json.dumps(chk)[:300])
    print("    'quality' anywhere in the JSON text?", "type_hint_pct" in out)
except Exception as exc:  # noqa: BLE001
    print("    parse failed:", exc)
    print(out[:1200])
