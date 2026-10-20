#!/usr/bin/env python3
"""Dogfood probe battery for docs/quality-metrics.md.

Rewrites the .gitreins/config.yaml quality block, runs the real CLI, and reports
exactly the quality/diff-scope lines plus exit codes. Read-only w.r.t. the product:
it only touches this scratch consumer repo.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/home/kara/.hermes/stand-in/dogfood/gitreins/qm-scratch-20261020")
CFG = ROOT / ".gitreins" / "config.yaml"
ENV = {"PATH": "/home/kara/gitreins/.venv/bin:/usr/bin:/bin", "HOME": "/home/kara"}


def write_quality(cfg: dict | None) -> None:
    text = CFG.read_text()
    lines = text.splitlines()
    out, i, done = [], 0, False
    while i < len(lines):
        if lines[i].startswith("quality:"):
            block = ["quality:"]
            if cfg is not None:
                block.append(f"  enabled: {str(cfg['enabled']).lower()}")
                block.append(f'  command: "{cfg["command"]}"')
                block.append(f'  artifact_path: "{cfg["artifact_path"]}"')
                block.append(f'  targets_source: "{cfg["targets_source"]}"')
                modes = cfg["per_metric_mode"]
                if modes:
                    block.append("  per_metric_mode:")
                    for k, v in modes.items():
                        block.append(f"    {k}: {v}")
                else:
                    block.append("  per_metric_mode: {}")
                block.append(f"  timeout: {cfg['timeout']}")
            else:
                block += [
                    "  enabled: false",
                    '  command: ""',
                    '  artifact_path: ""',
                    '  targets_source: ""',
                    "  per_metric_mode: {}",
                    "  timeout: 300",
                ]
            out += block
            done = True
            i += 1
            while i < len(lines) and (lines[i].startswith(" ") or lines[i].strip() == ""):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    assert done, "quality block not found in config.yaml"
    CFG.write_text("\n".join(out) + "\n")


def write_artifact(payload) -> None:
    p = ROOT / ".gitreins" / "quality.json"
    if payload is None:
        if p.exists():
            p.unlink()
        return
    p.write_text(payload if isinstance(payload, str) else json.dumps(payload, indent=2) + "\n")


DEFAULT_CFG = {
    "enabled": True,
    "command": "python3 scripts/quality_metrics.py",
    "artifact_path": ".gitreins/quality.json",
    "targets_source": "quality/ladder.json",
    "per_metric_mode": {"type_hint_pct": "warn"},
    "timeout": 300,
}


def run(*args: str, json_mode: bool = True) -> tuple[int, str]:
    cmd = ["gitreins", *args]
    if json_mode and args and args[0] in {"guard", "doctor", "judge"}:
        pass
    t0 = time.time()
    p = subprocess.run(cmd, cwd=ROOT, env=ENV, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr, round(time.time() - t0, 3)


def quality_lines(out: str) -> list[str]:
    return [
        ln.rstrip()
        for ln in out.splitlines()
        if "quality" in ln.lower() or "diff-scope" in ln.lower()
    ]


def probe(name: str, out: str, rc: int, wall: float) -> None:
    print(f"\n### {name}  (exit {rc}, wall {wall}s)")
    for ln in quality_lines(out):
        print("   ", ln)


def main() -> int:
    print("=" * 78)
    print("PROBE A — block mode, value BELOW target (72.3 < 80): doc says guard FAILS")
    write_artifact(
        {"metrics": {"type_hint_pct": {"value": 72.3, "target": 80, "stage": "current"}}}
    )
    write_quality({**DEFAULT_CFG, "per_metric_mode": {"type_hint_pct": "block"}})
    rc, out, w = run("guard")
    probe("A block+below", out, rc, w)

    print("=" * 78)
    print("PROBE B — block mode, value ABOVE target (95 >= 80)")
    write_artifact({"metrics": {"type_hint_pct": {"value": 95, "target": 80, "stage": "current"}}})
    rc, out, w = run("guard")
    probe("B block+above", out, rc, w)

    print("=" * 78)
    print("PROBE C — trust: artifact self-declares target 10, repo ladder says 80, mode=block")
    print("           (targets_source: quality/ladder.json contains {'type_hint_pct': 80})")
    (ROOT / "quality" / "ladder.json").write_text('{"type_hint_pct": 80}\n')
    write_artifact(
        {"metrics": {"type_hint_pct": {"value": 72.3, "target": 10, "stage": "current"}}}
    )
    rc, out, w = run("guard")
    probe("C artifact target overrides ladder", out, rc, w)

    print("=" * 78)
    print("PROBE C2 — same, but artifact target 90 (also disagrees with ladder 80)")
    write_artifact(
        {"metrics": {"type_hint_pct": {"value": 72.3, "target": 90, "stage": "current"}}}
    )
    rc, out, w = run("guard")
    probe("C2 artifact target 90 vs ladder 80", out, rc, w)

    print("=" * 78)
    print(
        "PROBE D — producer exits 1 and writes nothing: doc says 'quality: unavailable' with a reason"  # noqa: E501
    )
    write_quality(
        {**DEFAULT_CFG, "command": "sh -c 'echo boom >&2; exit 1'", "per_metric_mode": {}}
    )
    write_artifact(None)
    rc, out, w = run("guard")
    probe("D producer fails", out, rc, w)

    print("=" * 78)
    print("PROBE E — producer writes MALFORMED JSON")
    write_quality(
        {
            **DEFAULT_CFG,
            "command": "sh -c 'echo {not-json > .gitreins/quality.json'",
            "per_metric_mode": {},
        }
    )
    write_artifact(None)
    rc, out, w = run("guard")
    probe("E malformed json", out, rc, w)

    print("=" * 78)
    print("PROBE F — producer succeeds but writes NO artifact")
    write_quality({**DEFAULT_CFG, "command": "true", "per_metric_mode": {}})
    write_artifact(None)
    rc, out, w = run("guard")
    probe("F no artifact", out, rc, w)

    print("=" * 78)
    print("PROBE G — producer hangs 5s, quality.timeout = 1: does the guard bound it?")
    write_quality(
        {
            **DEFAULT_CFG,
            "command": "sh -c 'sleep 5; echo {} > .gitreins/quality.json'",
            "per_metric_mode": {},
            "timeout": 1,
        }
    )
    write_artifact(None)
    rc, out, w = run("guard")
    probe("G timeout=1 vs producer sleep 5", out, rc, w)
    print(f"    guard wall = {w}s  (expect ~1s if the timeout is honoured, ~5s if not)")

    print("=" * 78)
    print("PROBE H — artifact numeric contract: value is the STRING 'n/a'")
    write_quality({**DEFAULT_CFG, "per_metric_mode": {"type_hint_pct": "block"}})
    write_artifact('{"metrics": {"type_hint_pct": {"value": "n/a", "target": 80}}}')
    rc, out, w = run("guard")
    probe("H string value", out, rc, w)

    print("=" * 78)
    print(
        "PROBE I — artifact metric NOT listed in per_metric_mode (doc: unlisted defaults to warn)"
    )
    write_quality({**DEFAULT_CFG, "per_metric_mode": {}})
    write_artifact({"metrics": {"wiring_pct": {"value": 10, "target": 90, "stage": "beta"}}})
    rc, out, w = run("guard")
    probe("I unlisted metric", out, rc, w)

    print("=" * 78)
    print("PROBE J — freshness/dirty-tree: the doc's own producer writes 'produced_at';")
    print("           does a timestamped artifact leave the tree dirty after a run?")
    write_quality(DEFAULT_CFG)
    (ROOT / "scripts" / "quality_metrics_ts.py").write_text(
        "import json, datetime\n"
        "from pathlib import Path\n"
        "m = json.loads(Path('quality/ladder.json').read_text())\n"
        "art = {'metrics': {'type_hint_pct': {'value': 72.3, 'target': m['type_hint_pct'], 'stage': 'current'}},\n"  # noqa: E501
        "       'produced_at': datetime.datetime.now().isoformat(),\n"
        "       'command': 'python3 scripts/quality_metrics_ts.py'}\n"
        "Path('.gitreins/quality.json').write_text(json.dumps(art, indent=2) + '\\n')\n"
    )
    write_quality({**DEFAULT_CFG, "command": "python3 scripts/quality_metrics_ts.py"})
    rc, out, w = run("guard")
    probe("J1 guard with timestamped producer", out, rc, w)
    st = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True)
    print("    git status --porcelain after a guard run:")
    for ln in st.stdout.splitlines():
        print("      ", ln)
    print("    is .gitreins/quality.json ignored?", end=" ")
    ig = subprocess.run(
        ["git", "check-ignore", "-v", ".gitreins/quality.json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    print(f"rc={ig.returncode} {ig.stdout.strip() or '(not ignored)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
