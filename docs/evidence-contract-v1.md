# GitReins evidence contract v1

**Status (2026-09-22):** the v1 JSON Schema is published at
[`schemas/evidence-v1.schema.json`](../schemas/evidence-v1.schema.json) and the
CLI emitters are LANDED (EVID-002). `guard`, `judge` and `report` each accept
`--json` and emit the document described below; `guard` and `judge` accept
`--scope staged|working-tree`. `judge --ephemeral` is still pending (EVID-003),
so the `--ephemeral` invocation below is the contract that work implements — it
is not yet a runnable command (`scripts/check_cli_examples.py` grades only
examples that must parse today).

Emitted documents are graded by `tests/test_evidence_contract.py`: each
command's output is validated against the schema, held under the 32 KiB cap,
and checked for the redaction flags; the `--scope working-tree` collection is
tested for read-only git use (the index is byte-identical after a run).

## The v1 automation surface

The contract specifies a stable automation surface for `guard`, `judge`, and `report`:

- `gitreins guard --scope working-tree --json`
- `gitreins judge rorca-run-42-US-001 --ephemeral --title "Story gate" --criterion "Acceptance criteria are satisfied" --scope working-tree --json`
- `gitreins report -n 20 --json`

Each command in the surface writes exactly one UTF-8 JSON document to stdout. The normative JSON Schema is [`schemas/evidence-v1.schema.json`](../schemas/evidence-v1.schema.json), identified by `https://gitreins.dev/schemas/evidence/v1.json` and `schemaVersion: "1.0"`.

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
