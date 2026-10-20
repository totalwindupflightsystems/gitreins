#!/usr/bin/env bash
# Step 2b — MEASURE the cost of the quality-metrics surface on the commit path.
# Headline operation: `gitreins guard` (what the pre-commit hook runs on every commit).
set -u
cd /home/kara/.hermes/stand-in/dogfood/gitreins/qm-scratch-20261020
export PATH="/home/kara/gitreins/.venv/bin:$PATH"
EV=/home/kara/.hermes/stand-in/dogfood/gitreins/evidence
mkdir -p "$EV"

set_quality () {  # $1=command  $2=per_metric_mode value-line count
python3 - "$1" <<'PY'
import sys
from pathlib import Path
p = Path(".gitreins/config.yaml")
lines = p.read_text().splitlines()
out, i, done = [], 0, False
cmd = sys.argv[1]
while i < len(lines):
    if lines[i].startswith("quality:"):
        out += ["quality:", "  enabled: true", f'  command: "{cmd}"',
                '  artifact_path: ".gitreins/quality.json"', '  targets_source: "quality/ladder.json"',
                "  per_metric_mode:", "    type_hint_pct: warn", "  timeout: 300"]
        done = True
        i += 1
        while i < len(lines) and lines[i].startswith(" "):
            i += 1
        continue
    out.append(lines[i]); i += 1
assert done
p.write_text("\n".join(out) + "\n")
PY
}

set_quality_disabled () {
python3 - <<'PY'
from pathlib import Path
p = Path(".gitreins/config.yaml")
lines = p.read_text().splitlines()
out, i, done = [], 0, False
while i < len(lines):
    if lines[i].startswith("quality:"):
        out += ["quality:", "  enabled: false", '  command: ""', '  artifact_path: ""',
                '  targets_source: ""', "  per_metric_mode: {}", "  timeout: 300"]
        done = True
        i += 1
        while i < len(lines) and lines[i].startswith(" "):
            i += 1
        continue
    out.append(lines[i]); i += 1
assert done
p.write_text("\n".join(out) + "\n")
PY
}

echo "### host context"
uptime
echo "### nproc: $(nproc)"

printf 'import json\nimport time\nfrom pathlib import Path\ntime.sleep(2)\nPath(".gitreins/quality.json").write_text(json.dumps({"metrics": {"type_hint_pct": {"value": 72.3, "target": 80, "stage": "current"}}}) + "\\n")\n' > /tmp/slow_producer.py
printf 'import json\nimport time\nfrom pathlib import Path\ntime.sleep(30)\nPath(".gitreins/quality.json").write_text("{}\\n")\n' > /tmp/hang_producer.py

echo
echo "=== P1  guard, quality DISABLED (baseline) — warm, hyperfine ==="
set_quality_disabled
git add -A >/dev/null 2>&1; git commit -q -m "perf: quality disabled" --no-verify
hyperfine --warmup 3 --runs 20 'gitreins guard' 2>&1 | tail -8

echo
echo "=== P2  guard, quality ENABLED with the doc's example producer — warm, hyperfine ==="
set_quality "python3 scripts/quality_metrics.py"
hyperfine --warmup 3 --runs 20 'gitreins guard' 2>&1 | tail -8

echo
echo "=== P3  guard, quality ENABLED with a 2s repo-owned producer (realistic type-check/coverage) ==="
set_quality "python3 /tmp/slow_producer.py"
hyperfine --warmup 1 --runs 5 'gitreins guard' 2>&1 | tail -8

echo
echo "=== P4  cold (page cache dropped once before the run), n=1 each, same command ==="
for variant in "disabled" "docproducer"; do
  if [ "$variant" = "disabled" ]; then set_quality_disabled; else set_quality "python3 scripts/quality_metrics.py"; fi
  sudo sh -c 'sync; echo 3 > /proc/sys/vm/drop_caches'
  /usr/bin/time -f "cold[$variant] wall=%es user=%Us sys=%Ss" gitreins guard 2>&1 | grep -E "cold\[|Tier 1"
done

echo
echo "=== P5  a WEDGED producer: quality.timeout=2 while the producer sleeps 30 ==="
echo "    (the guard must not wait past the budget; default budget is 300s)"
set_quality "python3 /tmp/hang_producer.py"
python3 - <<'PY'
from pathlib import Path
p = Path(".gitreins/config.yaml")
p.write_text(p.read_text().replace("  timeout: 300", "  timeout: 2"))
PY
/usr/bin/time -f "guard[timeout=2] wall=%es user=%Us sys=%Ss" gitreins guard 2>&1 | grep -E "guard\[|quality"
echo "--- the same with the DEFAULT budget (300s) would occupy the commit path for that long ---"
set_quality "cp quality/artifact.json .gitreins/quality.json"
git add -A >/dev/null 2>&1; git commit -q -m "perf: restore deterministic producer" --no-verify
