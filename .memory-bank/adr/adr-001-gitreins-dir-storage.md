# ADR-001 — `.gitreins/` Directory as Canonical Storage

> **Decision:** Use a `.gitreins/` directory in the working tree, not
> a dedicated `gitreins` git branch, for all configuration, tasks,
> prompts, and evaluation history.
>
> **Status:** Accepted (2026-06-09). Supersedes the original "gitreins
> branch" design found in the pre-implementation docs.
>
> **Source:** Codebase survey (GR-010). Originally written as
> `docs/adr/adr-001-gitreins-directory-storage.md` in commit
> `72aeef0`. The original `docs/adr/` directory was later removed; this
> memory-bank copy preserves the rationale.

---

## Context

The original GitReins design (in `docs/architecture.md`,
`docs/sandbox.md`, `docs/component-map.md`, `docs/technology-choices.md`,
and the README) proposed a dedicated `gitreins` branch for storing
config, task definitions, prompts, and evaluation history. The
README still had the line "Config: YAML on `gitreins` branch."

When the engine was actually implemented, it chose a different path:
`.gitreins/` directory in the working tree. During the GR-010
codebase survey, we confirmed:

- **Zero code** creates, checks out, or references a `gitreins`
  branch. Grep of `*.py` and `*.sh` for `gitreins branch`,
  `checkout gitreins`, `git worktree gitreins`, etc. → no matches
  outside `.md` files.
- **The directory is used everywhere.** `TaskManager` reads
  `os.path.join(workdir, ".gitreins", "tasks.yaml")` —
  `engine/task_manager.py:42-43`. `Pipeline` reads
  `os.path.join(workdir, ".gitreins", "config.yaml")` —
  `engine/pipeline.py:383-386`. `gitreins/install:17` does
  `mkdir -p "$REPO_ROOT/.gitreins"`.

| Approach | Storage Location | Status |
|----------|-----------------|--------|
| Original design: `gitreins` branch | Parallel branch in repo | **Not implemented** |
| Actual implementation: `.gitreins/` | Directory in working tree | **Implemented, tested** |

## Decision

**Use `.gitreins/` directory in the working tree as the canonical
storage for all GitReins configuration, tasks, and data.** The
`gitreins` branch concept is **superseded**.

The actual layout in the current repo (verified):

```
.gitreins/
├── config.yaml         # 63 lines — pipeline, guards, evaluator settings
├── tasks.yaml          # 26 lines — pending + in_progress tasks
└── history/
    └── 2026-06-11/
        └── 058d55a4/   # short SHA prefix
            ├── verdict.json
            └── summary.md
```

## Rationale

Six factors drove the implementation choice, each verified by the
codebase survey:

1. **Implementation reality.** The code already uses the directory.
   `TaskManager`, `Pipeline`, `install`, and the verifier all read
   from `.gitreins/`. Changing to a branch would require re-architecting
   storage across multiple modules.

2. **One branch, one clone.** `git clone` gives you code + config +
   tasks in one operation. No need to fetch or check out a separate
   branch. (This was the killer argument — it removed a whole class
   of "you forgot to fetch the gitreins branch" bugs.)

3. **Natural history coupling.** When a code change also changes
   task definitions (e.g., adding new criteria), both changes commit
   together on the same branch. The history stays coherent.

4. **Hook simplicity.** Git hooks operate in the current working
   tree. Branch-based storage would require hooks to manage
   branch checkout, merge, and commit on a secondary ref — adding
   significant complexity for marginal benefit. The current
   `gitreins/install:40-64` writes a single self-contained
   `pre-commit` script; no git operations on a separate ref.

5. **MCP server consistency.** The MCP server operates on a working
   directory. Directory-based storage means no secondary branch
   management in the server lifecycle — the server reads/writes
   the same working tree the agent sees.

6. **Ecosystem convention.** `.github/workflows/`, `.circleci/`,
   `.pre-commit-config.yaml` — placing config in a dot-directory at
   repo root is standard practice across the git ecosystem. New
   contributors find it immediately.

## What does the directory contain?

| Path | Written by | Read by |
|------|-----------|---------|
| `.gitreins/config.yaml` | `gitreins/install:20-36` (default) | `engine/pipeline.py:381-428` |
| `.gitreins/tasks.yaml` | `engine/task_manager.py:66-82` | `engine/task_manager.py:46-64` |
| `.gitreins/history/<date>/<hash>/verdict.json` | `engine/persist.py` (commit 966ae79, AC-010) | (read by future analysis tools) |
| `.gitreins/history/<date>/<hash>/summary.md` | same | (human-readable audit trail) |

`.gitreins/tasks.yaml` is **gitignored** (see `.gitignore`) — it's
runtime state, not part of the project's permanent record. Config and
history are committed.

## Consequences

- ✅ Onboarding: clone → install → everything is there.
- ✅ Tooling: the `read_file` tool in the evaluator (`engine/evaluator.py:380`)
  reads `.gitreins/` files naturally without any special-casing.
- ✅ Hooks: a single bash script handles pre-commit; no need to
  stage onto a separate ref.
- ❌ `git diff` between branches doesn't isolate config changes.
  (Mitigation: `tasks.yaml` is gitignored, so `config.yaml` changes
  show up directly in normal diffs.)
- ❌ No automatic "gitreins branch" audit trail for evaluation
  history. The `history/` directory captures this instead.

## Related

- GR-010 (this decision)
- GR-011 (follow-up: history storage path inside `.gitreins/history/`)
- `docs/sandbox.md` — implementation note at top acknowledges the
  in-memory sandbox differs from the original `gitreins` branch design.
- `.gitignore` — `.gitreins/tasks.yaml` is ignored; everything else in
  `.gitreins/` is committed.
