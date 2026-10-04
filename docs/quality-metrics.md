# Repo-produced quality metrics

GitReins can run one repository-owned measurement command per guard, judge, or
`doctor` invocation and consume the JSON artifact it writes. GitReins does not
calculate coverage, type hints, wiring, or other quality measurements. Keep one
producer in the repository and make it read the repository's own target ladder.

## Minimal `type_hint_pct` example

From the repository root, create the producer below and run it once to verify
that it writes a valid artifact:

```sh
mkdir -p scripts .gitreins quality
cat > scripts/quality_metrics.py <<'PY'
import json
from pathlib import Path

# Replace this sample value with the repository's actual measurement.
# The target comes from this repository's ladder, not from GitReins.
ladder = json.loads(Path("quality/ladder.json").read_text())
value = 72.3
artifact = {
    "metrics": {
        "type_hint_pct": {
            "value": value,
            "target": ladder["type_hint_pct"],
            "stage": "current",
        }
    },
    "produced_at": "local",
    "command": "python3 scripts/quality_metrics.py",
}
Path(".gitreins/quality.json").write_text(json.dumps(artifact, indent=2) + "\n")
PY
cat > quality/ladder.json <<'JSON'
{"type_hint_pct": 80}
JSON
python3 scripts/quality_metrics.py
```

Then add the following block to `.gitreins/config.yaml`:

```yaml
quality:
  enabled: true
  command: "python3 scripts/quality_metrics.py"
  artifact_path: ".gitreins/quality.json"
  targets_source: "quality/ladder.json"
  per_metric_mode:
    type_hint_pct: warn
  timeout: 300
```

Run `gitreins guard`. The guard will show a line such as:

```text
✓ quality — quality: type_hint_pct=72.3% (target 80%, warn)
```

Change that metric's mode to `block` to fail the guard when its artifact value
is below its artifact target. `stage` is descriptive only; stage-specific
criteria must not independently block a run. Unlisted metrics default to
`warn`. Targets must be numeric values from the repository's ladder. The
example's value is deliberately a sample: replace it with the repo's actual
measurement before relying on it.

## Artifact contract

The command runs with the repository root as its working directory. The
artifact path is resolved relative to that root. It must be JSON with a
`metrics` object; each metric entry has a numeric `value`, optional numeric
`target`, and optional descriptive `stage`. `produced_at` and `command` are
optional provenance fields:

```json
{
  "metrics": {
    "type_hint_pct": {"value": 72.3, "target": 80, "stage": "current"}
  },
  "produced_at": "2026-10-03T12:00:00Z",
  "command": "python3 scripts/quality_metrics.py"
}
```

If the command fails, times out, writes malformed JSON, or does not produce the
artifact, GitReins reports `quality: unavailable` with a reason. It never
substitutes zero. Unavailable metrics are visible but do not fail a guard;
blocking applies only to a present numeric metric with an unmet target and
explicit `block` mode.

`quality.timeout` is in seconds (default 300). There are no quality-specific
environment variables. The same snapshot is shown in guard output, judge
results, `gitreins doctor`, and the MCP `guard.run` response.
