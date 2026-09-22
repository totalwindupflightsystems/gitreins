# GitReins evidence contract v1

GitReins 0.11.0 exposes a stable automation surface for `guard`, `judge`, and `report`:

```bash
gitreins guard --scope working-tree --json
gitreins judge rorca-run-42-US-001 --ephemeral --title "Story gate" \
  --criterion "Acceptance criteria are satisfied" --scope working-tree --json
gitreins report -n 20 --json
```

Each command writes exactly one UTF-8 JSON document to stdout. The normative JSON Schema is [`schemas/evidence-v1.schema.json`](../schemas/evidence-v1.schema.json), identified by `https://gitreins.dev/schemas/evidence/v1.json` and `schemaVersion: "1.0"`.

## Compatibility

- Existing human-readable commands remain supported.
- v1 field meanings will not change incompatibly. Additive metadata fields may be introduced. An incompatible shape requires a new schema URL and major contract version.
- `guard` and `judge` exit `0` only for a passing result and `1` for a non-passing result. CLI usage errors exit `2`.
- JSON is capped at 32 KiB. Text and collections are capped first; `metadata.truncated` reports any truncation.
- All text crosses a secret-redaction boundary before serialization. `metadata.redacted` is always `true`; `redactionsApplied` indicates a detected replacement.

## Scopes

- `staged` (default): files in the Git index.
- `working-tree`: union of staged, unstaged, and non-ignored untracked files. Collection uses read-only Git commands and never calls `git add`, `reset`, `stash`, `checkout`, or another index-mutating operation.
- `history`: report-only verdict history.

## Ephemeral judge

`judge --ephemeral` builds an in-memory task from `--title` and repeatable `--criterion` values. It does not instantiate `TaskManager`, write `.gitreins/tasks.yaml`, call `VerdictPersister`, write `.gitreins/history`, create/switch the `gitreins` branch, or use the stash. This mode is intended for Rorca's per-story execution gate.

Repository pipeline commands configured by the operator still run as configured; callers must use trusted `.gitreins/config.yaml` content.
