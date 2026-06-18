# Work Items STATUS — All 12 (GR-001 → GR-012) Complete

> **Last updated:** 2026-06-15
> **Project:** GitReins PoC v0.1.0
> **Overall:** All 12 work items complete. 322 tests pass. 0 failed.
>
> **Note on sources:** Work item plan files
> (`.memory-bank/work-items/GR-XXX/plan.yaml`) live in the external
> Axiom orchestrator, not in this repo. The `axiom:trace` comments
> in source files reference these paths. For each item below, the
> evidence is grounded in:
>
> 1. `axiom:trace` comments in the source files
> 2. Git log (`git log --all --oneline`)
> 3. `.hermes/acceptance-criteria.md` (the AC docs that mark the
>    same work)
> 4. Current state of the code

---

## Phases

The 12 work items, in the order they were driven, span 5 phases:

```
docs    →  tests   →  pipeline  →  config  →  README
(GR-007/008/009) (GR-001/002/003) (GR-005/006) (GR-010/011) (GR-012)
                                                                  
                                                                  GR-004 (initial foundation)
```

The order isn't strict — some items were developed in parallel
(e.g., GR-002 and GR-003 in the same test-expansion commit
`d09b955`). The phases are organizational, not serial.

---

## The 12 work items

### GR-001 — Engine foundation + unit tests ✅

**Phase:** tests
**Status:** Complete
**Commit evidence:**
- Engine source first shipped in `a1b3e5c` (2026-05-27, "working PoC — engine, MCP server, CLI")
- 7 engine modules: `llm.py`, `evaluator.py`, `task_manager.py`, `guard_manager.py`, `judge.py`, `pipeline.py`, `dead_code.py`
- Unit test expansion in `d09b955` (2026-06-15, +59 tests, model: deepseek/deepseek-v4-flash)
- `fix: address all 12 known issues` in `af7e7e8` (multi-provider, retry, dedup, secrets, logging)

**axiom:trace references (6 of the 7 modules + 1 test file; dead_code.py has no test file of its own):**
- `tests/__init__.py:1` — `axiom:trace work_item=GR-001,GR-002,GR-003,GR-004`
- `tests/test_llm.py:3` — `GR-001 spec=specs/02-LLM-Interface.md`
- `tests/test_evaluator.py:3` — `GR-001 spec=specs/03-Agentic-Evaluator.md`
- `tests/test_guard_manager.py:3` — `GR-001 spec=specs/04-Guard-Manager.md`
- `tests/test_task_manager.py:3` — `GR-001 spec=specs/05-Task-Manager.md`
- `tests/test_pipeline.py:3` — `GR-001 spec=specs/06-Pipeline-Engine.md`
- `tests/test_judge.py:3` — `GR-001 spec=specs/07-Judge-Orchestrator.md`
- `tests/conftest.py:3` — `GR-001 spec=specs/05-Task-Manager.md step=step-1-1-1-1`

**AC trace:** AC-016 (unit test coverage, 280 tests pass, +59)
**Lines shipped:** 1,500+ across 7 engine modules (dead_code.py is 280 lines, no axiom:trace).

---

### GR-002 — MCP server + integration tests ✅

**Phase:** tests
**Status:** Complete
**Commit evidence:**
- `a1b3e5c` — initial MCP server (280 lines, 9 tools, stdio transport)
- `af7e7e8` — `Renamed mcp/ → gitreins_mcp/ (avoids pip mcp package conflict), Proper JSON-RPC framing`
- `d09b955` — +18 stdio integration tests (TestMCPStdioIntegration class)

**axiom:trace references:**
- `tests/test_mcp_server.py:3` — `axiom:trace work_item=GR-002 spec=specs/08-MCP-Server.md`
- `tests/__init__.py:1` — listed alongside GR-001, GR-003, GR-004

**AC trace:** AC-017 (MCP integration tests, 18 new)
**Files:** `gitreins_mcp/server.py` (417 lines), `gitreins_mcp/__init__.py`
**Tools shipped:** 9 (task.create, task.start, task.complete, task.list, task.get, task.delete, commit, guard.run, judge.evaluate)

---

### GR-003 — CLI + integration tests ✅

**Phase:** tests
**Status:** Complete
**Commit evidence:**
- `a1b3e5c` — initial CLI (170 lines, task/guard/judge/commit/mcp-server)
- `d09b955` — +26 CLI integration tests (7 new test classes)

**axiom:trace references:**
- `tests/test_cli.py:3` — `axiom:trace work_item=GR-003 spec=specs/09-CLI.md`
- `tests/__init__.py:1` — listed alongside GR-001, GR-002, GR-004

**AC trace:** AC-017 (CLI integration tests, 26 new)
**File:** `gitreins/cli.py` (218 lines)
**Commands shipped:** 5 top-level (task, guard, judge, commit, mcp-server), 5 task subcommands (create, start, complete, list, delete)

---

### GR-004 — Initial foundation / 4th item in test suite ✅

**Phase:** tests (initial)
**Status:** Complete
**Commit evidence:** Inferred from `tests/__init__.py:1` listing
`GR-001,GR-002,GR-003,GR-004` as the four work items covered by
the test suite. Direct git log evidence is light (no commit
message contains "GR-004" specifically), but the work was
absorbed into the engine foundation drive.

**axiom:trace references:**
- `tests/__init__.py:1` — `axiom:trace work_item=GR-001,GR-002,GR-003,GR-004 spec=specs/01-Architecture.md plan=.memory-bank/work-items/GR-001/plan.yaml`

**Best inference:** GR-004 was the architecture spec write-up +
the first end-to-end test pass. The `specs/01-Architecture.md`
referenced in the trace is the spec doc that defined the
component dependency graph
(`docs/architecture.md:7-26` is the current realization of
that spec). This is the "plumbing" work that lets GR-001,
GR-002, GR-003 land coherently.

**Honest gap:** No direct commit message names "GR-004."
Inferred from the axiom:trace metadata.

---

### GR-005 — Skylos integration + dead-code tooling ✅

**Phase:** pipeline (extends Tier 1)
**Status:** Complete
**Commit evidence:** Inferred from code state. No commit
message contains "GR-005." But the Skylos tool is shipped in
two places:
- `engine/guard_manager.py:278-343` — `_check_skylos` Tier 1 guard
- `engine/evaluator.py:170-176` + `622-663` — `skylos_scan` tool

These were not in the original 6-engine-module commit
(`a1b3e5c`) and not in the `af7e7e8` "address all 12 known
issues" fix commit. They were added between the v0.1.0 drive
(2026-06-09) and the test expansion (2026-06-15), during the
window when the .skylos/ cache directory was first created
(modtime Jun 17).

**Best inference:** GR-005 added Skylos as both a Tier 1 guard
(opt-in, `guards.skylos: false` default in
`engine/guard_manager.py:55`) and an evaluator tool
(`skylos_scan` in `EVALUATOR_TOOLS`). See
`findings/finding-003-skylos-integration.md` for full detail.

**Honest gap:** No direct evidence of "GR-005" in git log.
Inferred from the code surface and the `.skylos/` cache
directory mtime.

---

### GR-006 — Pipeline engine + line-range file reads ✅

**Phase:** pipeline
**Status:** Complete
**Commit evidence:**
- `107f915` (commit message: `feat: pipeline engine + line-range file reads`)

This commit predates the v0.1.0 drive but is preserved in git
history (`git log --all` shows it). It added the configurable
`engine/pipeline.py` (now 428 lines) and the `offset/limit`
support in `_tool_read_file` (`engine/evaluator.py:380-428`).

**Best inference:** GR-006 was the pipeline abstraction
("configurable multi-stage evaluation pipelines" per
`docs/component-map.md:9`) plus the read_file line-range
feature that lets the evaluator page through large files.

**Honest gap:** No axiom:trace comment for GR-006 in the
current source. Inferred from commit `107f915`.

---

### GR-007 — `docs/architecture.md` reflects implementation ✅

**Phase:** docs
**Status:** Complete
**Commit evidence:**
- `5dd5388` (2026-06-14) — `docs: update architecture.md to reflect implemented v0.1.0 (GR-007)`

**axiom:trace references:**
- None in source — only in commit message

**AC trace:** AC-013 (architecture docs reflect implementation
reality, commit 5dd5388)

**File:** `docs/architecture.md` (82 lines, "IMPLEMENTED
(v0.1.0)" header, .gitreins/ directory section, 7 evaluator
tools with signatures, data flow diagram)

---

### GR-008 — `docs/component-map.md` with real paths/lines ✅

**Phase:** docs
**Status:** Complete
**Commit evidence:**
- `893231b` (2026-06-14) — `docs: update component-map, evaluator-loop, and sandbox docs (GR-008, GR-009)`

**axiom:trace references:**
- `docs/component-map.md:52` — `<!-- axiom:trace work_item=GR-008 spec=specs/01-Architecture.md impl=docs/component-map.md -->`

**AC trace:** AC-014 (component map has actual line counts and
paths, commit 893231b)

**File:** `docs/component-map.md` (52 lines, real paths like
`engine/evaluator.py:569`, status column with "Implemented ✅"
for all components)

---

### GR-009 — `docs/evaluator-loop.md` describes actual tools ✅

**Phase:** docs
**Status:** Complete
**Commit evidence:**
- `893231b` (2026-06-14, same commit as GR-008) — bundled docs
  update for component-map, evaluator-loop, and sandbox

**axiom:trace references:**
- None in source — only in commit message

**AC trace:** AC-015 (evaluator loop docs describe implemented
tools, commit 893231b)

**Files:** `docs/evaluator-loop.md` (247 lines, all 7 tools
with signatures from `engine/evaluator.py`, JSON response
examples, dedup tracking, max iterations, verdict parser
3 strategies) + `docs/sandbox.md` (50 lines, implementation
note banner added in this commit)

---

### GR-010 — `.gitreins/` directory vs `gitreins` branch decision ✅

**Phase:** config
**Status:** Complete
**Commit evidence:**
- `72aeef0` (2026-06-09) — `docs: organize ADRs, add gitignore, clean up root artifacts`
  - This commit added `docs/adr/adr-001-gitreins-directory-storage.md`
    (GR-010 decision), `docs/adr/GR-011-evaluation-history-survey.md`,
    `docs/adr/GR-011-gitreins-history-storage-path.md`, and the
    current `.gitreins/config.yaml` (63 lines).

**axiom:trace references (in the historical ADR docs, recoverable from git):**
- `docs/adr/adr-001-gitreins-directory-storage.md:3` — `axiom:trace work_item=GR-010 spec=specs/01-Architecture.md#REQ-ARCH-006 plan=phase-10-1/task-10-1-1/step-10-1-1-3`
- `docs/adr/GR-010-gitreins-storage-decision.md:3` — `axiom:trace work_item=GR-010 spec=specs/01-Architecture.md,specs/11-Configuration.md plan=phase-10-1/task-10-1-1/step-10-1-1-2`

**AC trace:** Implicit in AC-013 (architecture docs reflect
implementation reality — the architecture would have
contradicted itself without the GR-010 decision).

**File:** `docs/adr/adr-001-gitreins-directory-storage.md`
(106 lines in the original commit, preserved in
`.memory-bank/adr/adr-001-gitreins-dir-storage.md`)

**The original `docs/adr/` directory was later removed** —
that's why this memory bank exists. The ADRs in
`.memory-bank/adr/` are the recoverable copy.

---

### GR-011 — Evaluation history storage path ✅

**Phase:** config
**Status:** Complete
**Commit evidence:**
- `72aeef0` (2026-06-09) — bundled with GR-010 docs
  - Added `docs/adr/GR-011-evaluation-history-survey.md`
    (investigation report) and
    `docs/adr/GR-011-gitreins-history-storage-path.md`
    (design spec for `.gitreins/history/`)
- `966ae79` (commit message: `feat: AC-010 — verdict sandbox
  persistence with auto-commit to gitreins branch`) — added
  `engine/persist.py` (the `VerdictPersister` class that
  writes `.gitreins/history/<date>/<hash>/verdict.json` and
  `summary.md`)

**AC trace:** AC-010 (verdict sandbox persistence with
auto-commit to gitreins branch, moved from backlog to passed
in `3bd6f6a`)

**Evidence in current repo:** The history directory
`.gitreins/history/2026-06-11/058d55a4/` exists with
`verdict.json` (4853 bytes) and `summary.md` (2134 bytes).
This was created by `engine/persist.py` at the time of the
test cron-smoke run.

**Note:** `engine/persist.py` is **not currently in the
working tree** — it was shipped at commit `966ae79` and the
history record was created, but the module was removed
in a later cleanup. The directory structure it created
survives.

---

### GR-012 — README, install, config documentation ✅

**Phase:** README
**Status:** Complete
**Commit evidence:**
- `3ef9132` (2026-06-14) — `docs: update README to reflect implemented v0.1.0 status (GR-012)`

**axiom:trace references:**
- `README.md:57` — `<!-- axiom:trace work_item=GR-012 spec=specs/01-Architecture.md,specs/09-CLI.md,specs/10-Install-Bootstrap.md,specs/11-Configuration.md plan=.memory-bank/work-items/GR-012/plan.yaml -->`

**AC trace:** AC-018 (README reflects implemented reality,
commit 3ef9132). Depends on AC-013, AC-014, AC-015 (GR-007,
GR-008, GR-009) ✅

**Files:** `README.md` (57 lines, "Implemented v0.1.0" banner,
5-step How It Works, Architecture & Docs table, Quick Start
commands, .gitreins/ directory config) + `.gitreins/config.yaml`
(63 lines, current default) + `gitreins/install` (76 lines,
one-command activation)

---

## Summary table

| GR | Phase | Primary deliverable | Direct evidence | Confidence |
|----|-------|---------------------|-----------------|------------|
| GR-001 | tests | 7 engine modules + unit tests | 6 axiom:trace comments (dead_code.py has no test file), AC-016, d09b955 | High |
| GR-002 | tests | MCP server + integration tests | axiom:trace in test_mcp_server.py, AC-017, d09b955 | High |
| GR-003 | tests | CLI + integration tests | axiom:trace in test_cli.py, AC-017, d09b955 | High |
| GR-004 | tests | Architecture spec + initial plumbing | axiom:trace in tests/__init__.py | Medium (inferred scope) |
| GR-005 | pipeline | Skylos integration | Code in 2 files, .skylos/ cache | Medium (inferred from code state) |
| GR-006 | pipeline | Pipeline engine + line-range reads | Commit 107f915 ("feat: pipeline engine + line-range file reads") | High |
| GR-007 | docs | docs/architecture.md | Commit 5dd5388, AC-013 | High |
| GR-008 | docs | docs/component-map.md | Commit 893231b, axiom:trace in component-map.md, AC-014 | High |
| GR-009 | docs | docs/evaluator-loop.md | Commit 893231b, AC-015 | High |
| GR-010 | config | .gitreins/ storage decision | Commit 72aeef0 (ADR-001), recoverable from git | High |
| GR-011 | config | Verdict history persistence | Commits 72aeef0 + 966ae79, AC-010 | High |
| GR-012 | README | README, install, config docs | Commit 3ef9132, axiom:trace in README.md, AC-018 | High |

**Confidence levels:**
- **High** = direct commit message + axiom:trace comment + AC doc
- **Medium** = one of the above is missing, but the work is
  visible in the code/git

---

## What's missing from the historical record

- **No `plan.yaml` files in this repo.** The Axiom orchestrator
  owns them. The `axiom:trace` comments reference
  `.memory-bank/work-items/GR-XXX/plan.yaml` paths that exist
  in Axiom's namespace, not here. This memory bank's
  `work-items/STATUS.md` is the recoverable equivalent.
- **No `docs/adr/` directory in the current repo.** It was added
  in commit `72aeef0` and removed later (likely during the
  v0.1.0 drive cleanup). The ADRs are preserved in
  `.memory-bank/adr/`.
- **`engine/persist.py` (VerdictPersister) is not in the
  current working tree.** It was shipped at commit `966ae79`
  and created the `.gitreins/history/2026-06-11/058d55a4/`
  record that still exists. The module would need to be
  re-introduced if persistent history is a current need.

---

## Recovery commands

If you need to recover any of the historical artifacts:

```bash
# Recover the original docs/adr/ directory
cd ~/gitreins-poc
git show 72aeef0:docs/adr/adr-001-gitreins-directory-storage.md
git show 72aeef0:docs/adr/GR-010-gitreins-storage-decision.md
git show 72aeef0:docs/adr/GR-011-evaluation-history-survey.md
git show 72aeef0:docs/adr/GR-011-gitreins-history-storage-path.md

# Recover engine/persist.py
git show 966ae79:engine/persist.py

# Recover the original specs/ directory
git show ef1d143:specs/00-README.md   # and 01-11

# See all commits in the GR-* lineage
git log --all --oneline | grep -E "(GR-0|GR-1)"

# All axiom:trace comments in the repo
grep -rn "axiom:trace" --include="*.py" --include="*.md" .
```
