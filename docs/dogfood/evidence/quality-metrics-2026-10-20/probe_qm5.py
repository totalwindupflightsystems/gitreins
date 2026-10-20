#!/usr/bin/env python3
"""Dogfood probe battery #5 — judge leg, staged-vs-unstaged diff scope, artifact hygiene."""

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


def sh(*args, check=False):
    return subprocess.run(args, cwd=ROOT, env=ENV, capture_output=True, text=True, check=check)


def run(*args):
    t0 = time.time()
    p = sh("gitreins", *args)
    return p.returncode, p.stdout + p.stderr, round(time.time() - t0, 3)


COPY = "cp quality/artifact.json .gitreins/quality.json"
SIMPLE = {"metrics": {"type_hint_pct": {"value": 72.3, "target": 80, "stage": "current"}}}
ABOVE = {"metrics": {"type_hint_pct": {"value": 95, "target": 80, "stage": "current"}}}

print("=" * 78)
print("J1 — judge (tier1 only, no LLM) with mode=block and 72.3 < 80: does the judge FAIL,")
print("     and does the failure text name the metric?")
FIX.write_text(json.dumps(SIMPLE, indent=2) + "\n")
write_quality(COPY, {"type_hint_pct": "block"})
sh("git", "add", "-A")
sh("git", "commit", "-q", "-m", "probe: block mode", "--no-verify")
rc, out, w = run("judge", "QM-1", "--skip-tier2")
print(f"### judge human  exit={rc} wall={w}s")
for ln in out.splitlines():
    print("   >", ln.rstrip())

print("=" * 78)
print("J2 — same run as --json: is tier1.extra.quality_snapshot present?")
rc, out, w = run("judge", "QM-1", "--skip-tier2", "--json")
print(f"### judge --json exit={rc}")
try:
    doc = json.loads(out[out.index("{") : out.rindex("}") + 1])
    print("    top keys:", sorted(doc.keys()))
    for key in ("passed", "status", "outcome", "score"):
        if key in doc:
            print(f"    {key} = {doc[key]!r}")
    extra = (doc.get("tier1") or {}).get("extra") or {}
    print("    tier1.extra keys:", sorted(extra.keys()))
    qs = extra.get("quality_snapshot")
    print("    tier1.extra.quality_snapshot:", json.dumps(qs)[:400] if qs else qs)
except Exception as exc:  # noqa: BLE001
    print("    parse failed:", exc)
    print(out[:1500])

print("=" * 78)
print("J3 — persisted verdict document: does verdict.json carry quality_snapshot?")
hist = ROOT / ".gitreins" / "history"
found = sorted(hist.rglob("verdict.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:2]
for vf in found:
    try:
        v = json.loads(vf.read_text())
    except Exception as exc:  # noqa: BLE001
        print(f"    {vf}: unreadable ({exc})")
        continue
    print(f"    {vf.relative_to(ROOT)}")
    print("      top keys:", sorted(v.keys())[:14])
    qs = v.get("quality_snapshot") or ((v.get("tier1") or {}).get("extra") or {}).get(
        "quality_snapshot"
    )
    print("      quality_snapshot:", json.dumps(qs)[:300] if qs else qs)

print("=" * 78)
print("J4 — judge with mode=warn and the SAME below-target value: PASS expected")
write_quality(COPY, {"type_hint_pct": "warn"})
rc, out, w = run("judge", "QM-1", "--skip-tier2")
print(f"### judge warn  exit={rc} wall={w}s")
for ln in out.splitlines():
    if "quality" in ln.lower() or "Tier 1" in ln or "PASS" in ln or "FAIL" in ln:
        print("   >", ln.rstrip())

print("=" * 78)
print("J5 — genuine NO-DIFF state (worktree == HEAD, committed artifact, deterministic producer):")
print("     doc says a diff that matches no surface is reported as 'no_diff'")
write_quality(COPY, {"type_hint_pct": "warn"})
sh("git", "add", "-A")
sh("git", "commit", "-q", "-m", "probe: committed state for no-diff test", "--no-verify")
st = sh("git", "status", "--porcelain")
print("    status before guard:", st.stdout.splitlines() or "(clean)")
FIX.write_text(
    json.dumps(
        {
            "class_floor": ["unit"],
            "metrics": SIMPLE["metrics"],
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
sh("git", "add", "-A")
sh("git", "commit", "-q", "-m", "probe: surfaces artifact", "--no-verify")
st = sh("git", "status", "--porcelain")
print("    status now:", st.stdout.splitlines() or "(clean)")
rc, out, w = run("guard")
print(f"### guard no-diff  exit={rc} wall={w}s")
for ln in out.splitlines():
    if "quality" in ln.lower() or "diff-scope" in ln.lower() or "surface" in ln.lower():
        print("   >", ln.rstrip())
st = sh("git", "status", "--porcelain")
print("    status after guard:", st.stdout.splitlines() or "(clean)")

print("=" * 78)
print("J6 — ARTIFACT HYGIENE: the doc's producer stamps produced_at; is the artifact ignored,")
print("     and does the tree stay clean after a run?")
ts_producer = ROOT / "scripts" / "quality_metrics_ts.py"
write_quality("python3 scripts/quality_metrics_ts.py", {"type_hint_pct": "warn"})
rc, out, w = run("guard")
sh("git", "add", "-A")
sh("git", "commit", "-q", "-m", "probe: timestamped artifact", "--no-verify")
print("    after committing the artifact, run guard again and look at the tree:")
rc, out, w = run("guard")
st = sh("git", "status", "--porcelain")
print("    git status --porcelain:", st.stdout.splitlines() or "(clean)")
ig = sh("git", "check-ignore", "-v", ".gitreins/quality.json")
print("    check-ignore rc:", ig.returncode, ig.stdout.strip() or "(not ignored)")
print("    install's .gitignore entries mentioning .gitreins:")
gi = (ROOT / ".gitignore").read_text().splitlines()
for ln in gi:
    if ".gitreins" in ln:
        print("      ", ln)
print(
    "    tracked?", sh("git", "ls-files", ".gitreins/quality.json").stdout.strip() or "(untracked)"
)
