# Sandbox: gitreins Branch Persistence

> ⚠️ **Implementation Note:** The current implementation differs from this design doc.
> In practice, sandbox is an in-memory Python dict (`AgenticEvaluator._sandbox`), not a
> filesystem directory. It is cleared at each evaluation start. Auto-commit to a gitreins
> branch is not yet implemented. See `engine/evaluator.py` for actual behavior.

The evaluator's scratch space does not live in `/tmp`. It persists on the **gitreins branch**, making every evaluation version-controlled, auditable, and cloneable.

## Flow

```
DURING EVALUATION                    AFTER VERDICT
┌─────────────────────────┐         ┌─────────────────────────────┐
│ .git/gitreins-sandbox/   │         │ gitreins branch:             │
│  ├── evidence-log.md     │ ──────→ │  history/2026-05-26/         │
│  └── checklist.md        │ verdict │   <commit-hash>/             │
│                          │         │    ├── verdict.json          │
│ gitignored · isolated    │         │    └── logs.md               │
└─────────────────────────┘         │                              │
                                    │ version-controlled · auditable│
                                    └─────────────────────────────┘
```

## Sandbox Properties

| Property | Behavior |
|---|---|
| Location | `.git/gitreins-sandbox/` |
| Isolation | Cannot touch repository working tree |
| Lifespan | Created per evaluation, cleaned after verdict |
| Persistence | Available across all tool calls in one session |
| Use case | Evidence log, checklist, partial findings |
| Post-verdict | Auto-committed to `gitreins` branch |

## Why Not /tmp?

1. **Auditability** — Every evaluation leaves a permanent trace
2. **Cloneable** — Clone the repo, clone the full evaluation history
3. **Learning** — Past evaluations queryable. Agent can learn from common failure patterns
4. **No cleanup** — `/tmp` gets purged on reboot. gitreins branch is permanent

## Sandbox vs MCP

| | Sandbox | MCP Bridge |
|---|---|---|
| **Scope** | Evaluator's own scratch space | External truth sources |
| **Isolation** | Cannot touch working tree | Opt-in per repo |
| **Purpose** | Offload evidence, build checklist | Verify against Jira, docs, APIs |
| **Config** | Always available | `.gitreins.yaml` mcp_allowlist |
