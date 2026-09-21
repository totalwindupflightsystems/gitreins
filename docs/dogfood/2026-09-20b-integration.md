# Dogfood Integration Report — 2026-09-20b — QA ledger + commit-msg audit + a fresh-machine install

**Run:** gitreins-poc dogfood lane, HEAD `0031492`, v0.14.0 (PyPI = HEAD; second run in a row in sync).
**Angle:** runs 1–6 swept the CLI, `install`/`init`, `guard`, `judge`, PyPI wheel and (this morning)
the MCP stdio server + `serve`. This run took the surfaces grep says were never touched:

| Surface | Prior coverage | This run |
|---|---|---|
| `gitreins qa record` / `qa list` (0.14.0 QA ledger) | none | driven as the README's own named consumer — "a run produced outside the harness (a fleet lane, a bunker battery)" |
| `gitreins commit-audit` + a hand-built commit-msg hook | none | hook created from the documented snippet, real messages graded, all four config placements measured |
| `gitreins worktree fresh` / `dogfood` (disposable batteries) | mentioned only | run against a **plain** consumer repo (not a fleet checkout) |
| fresh-machine install | **las-bunker-03 was down in the previous two runs** | RUN: host up, agent `3f4f7cdc` spawned, used, destroyed |

## Promise under test

*"A team (or an agent fleet) can record what its QA runs actually did — harness-run or
outside the harness — into one browsable ledger, gate commits on a message audit, and
self-verify the whole thing in disposable worktrees without a bunker."*

**Verdict: 🟡 PROMISING-BUT-ROUGH.** Every one of those capabilities is real and, once you
find the right incantation, good: the ledger round-trips into `report`/`serve`; the message
audit is genuinely specific ("Message 'wip' is a generic placeholder… Suggested message:
docs: add f.md…"); the disposable battery ran a command in a throwaway tree in 0.19 s and
self-recorded. What is rough is the **activation path of almost every one of them**: three of
four surfaces silently do nothing (or the wrong thing) in the configuration the docs describe,
and the fresh-machine quickstart's first documented commit is blocked by an environment
we didn't set up. Six findings, four P1.

## The working flow (what a user actually has to type)

Scratch consumer: `/tmp/dogfood-gitreins-qa` (fresh `git init` + README), gitreins from the
repo's `.venv`. Everything below was executed, not inferred.

```bash
GR=/path/to/gitreins                      # or a pip-installed gitreins

# 1. install + init — works, and install is conservative by design
$GR install && $GR init

# 2. THE QA LEDGER — works the moment you pass a verdict
$GR qa record --kind bunker --verdict FAIL --exit-code 1 \
  --cell ci-pass=PASS --cell chaos-resource=FAIL \
  --finding QA-GITREINS-POC-11:"gopls missing on clean image" \
  --note "bunker battery, 2 cells" --agent b59eb20c --server bunker-las-02
#   qa ledger: recorded bunker <repo> FAIL in <repo>/.gitreins/qa-ledger.jsonl   (exit 0)
$GR qa list
#   ═══ GitReins QA Ledger ═══
#   1 QA run(s):
#     ✗ bunker   <repo>   2026-09-20T22:07:34+00:00  cells 1/2 passed  exit 1  commit 25edfa0
$GR report -n 3          # the QA block appears under the verdict history

# 3. DISPOSABLE BATTERY — needs a directory that gitreins' own install never creates
mkdir -p .coding-hermes/board          # ← undocumented prerequisite (POC-27)
$GR worktree fresh --cmd "echo hello-from-throwaway-tree"
#   fresh: exit 0 in 0.191s
#   hello-from-throwaway-tree
#   … and the run self-records: `fresh PASS {'fresh': 'passed'}` in the ledger

# 4. COMMIT-MSG HOOK — the body is the user's job, and the stage is the user's job too
cat > .git/hooks/commit-msg <<'HOOK'
#!/usr/bin/env bash
exec gitreins commit-audit
HOOK
chmod +x .git/hooks/commit-msg

# and the audit does nothing at all until a pipeline stage exists (POC-30):
cat >> .gitreins/config.yaml <<'YAML'
pipeline:
  stages:
    - id: commit_audit
      type: commit_audit
      on: [commit-msg]
commit_audit:            # ← TOP LEVEL. This is the only placement that blocks.
  mode: block
YAML
$GR commit-audit "wip"
#   ⚠ Commit message issues:
#     - Message 'wip' is a generic placeholder that conveys no information about the change.
#   Suggested message: docs: add f.md with initial documentation
#   (Commit BLOCKED — fix message or set commit_audit.mode=warn)      exit 1
```

Row shape written to `<repo>/.gitreins/qa-ledger.jsonl` (verbatim, one row):

```json
{"ts":"2026-09-20T22:07:34+00:00","project":"dogfood-gitreins-qa","status":"fail",
 "verdict":"FAIL","kind":"bunker","cells":{"ci-pass":"PASS","chaos-resource":"FAIL"},
 "findings":[{"id":"QA-GITREINS-POC-11","title":"gopls missing on clean image"}],
 "evidence":"","note":"bunker battery, 2 cells","exit_code":1,
 "commit":"25edfa04fd2ab3a4080f66ed03a0d2a7ff4c0c04","agent":"b59eb20c",
 "server":"bunker-las-02","harness_version":"0.14.0"}
```

That row is the good part: it carries the fleet QA keys (`ts project status cells findings
evidence note`) **plus** harness extras (`kind verdict run_id exit_code commit
harness_version detail`), so a consumer that already reads the fleet schema can read a
harness-written ledger. The advertised interop held up on inspection.

## Errors hit, and their fixes

| # | What I did | What happened | Fix / workaround |
|---|---|---|---|
| 1 | `worktree fresh --cmd "echo hello"` in a plain repo | `WorktreeResolutionError: canonical board directory does not exist: .coding-hermes/board` — traceback, exit 1 | `mkdir -p .coding-hermes/board`; it then passes in 0.19 s (**POC-27** — fixed: the board is optional as of DF-GITREINS-POC-27, no workaround needed) |
| 2 | `qa record --kind lane --note X` (no verdict/exit-code) | `recorded … UNKNOWN`, `"status":"unknown","verdict":"UNKNOWN"`, exit 0 — docs say the default is a *passing* verdict (**POC-29**) | pass `--verdict` explicitly |
| 3 | `qa record --evidence /tmp/does-not-exist.json` | accepted, exit 0, path stored verbatim — dangling audit pointer (**POC-29**) | — (validate or flag) |
| 4 | `qa record` with `qa_ledger.max_entries: 3` already full | 4th record exits 0 "recorded", row count stays 3 — eviction is silent (**POC-32**) | raise the cap |
| 5 | `commit-audit "wip"` on a fresh install | exit 0, **stdout and stderr both empty** — no audit, no skip message (**POC-30**) | declare a `pipeline.stages[]` entry |
| 6 | stage-level `mode: block` (the documented placement) | still `(Warning only — commit will proceed)`, exit 0 (**POC-30**) | set **top-level** `commit_audit.mode: block` |
| 7 | `pip install gitreins` on fresh Debian 13 | PEP-668, exit 1 (`externally-managed-environment`) | `python3 -m venv .venv && .venv/bin/pip install gitreins` — 32 s |
| 8 | `guard` / the first `git commit` from an unactivated venv | `✗ tests (full) — /bin/sh: 1: pytest: not found`, exit 1, commit blocked (**POC-28**) | `source .venv/bin/activate` first — then DEGRADED PASS, exit 0, commit lands |
| 9 | `git add -A && git commit` after install | `.gitreins/qa-ledger.jsonl` **committed** (`create mode 100644`) — install's gitignore omits it (**POC-31**) | add it to `.gitignore` by hand |

Nine friction points, seven of them in the first ten minutes of a documented workflow.

## The fresh-machine leg (installability, proven on a clean box)

Host `bunker-las-03` was **reachable and `bunkerd` active** this time (it was down for the
09-15 run and unusable for 09-20's). Ephemeral agent `3f4f7cdc` on 100.69.3.13, bare Debian 13,
py3.13.5, no toolchains, no `uv`. Spawned → used → **destroyed and verified gone**
(`bunker destroy` exit 0, key removed, `bunker list` → "No agents found").

| Step | Result |
|---|---|
| `pip install gitreins` (README literal) | ✗ exit 1 — PEP 668 |
| `python3 -m venv .venv` + `.venv/bin/pip install gitreins` | ✓ 32 s, `gitreins 0.14.0` |
| `gitreins install` + `gitreins init` | ✓ pre-commit hook pinned to the **absolute venv path** — DF-011's PATH-shadowing fix verified again |
| `gitreins guard` (no pytest in venv) | ✗ exit 1 `pytest: not found` |
| `git add -A && git commit -m test` | ✗ exit 1 — hook re-runs the same FAIL. **The README's documented next step is blocked** |
| `pip install pytest` + a real test, *still unactivated venv* | ✗ exit 1 `pytest: not found` — the guard's `sh -c` does not see the venv |
| `source .venv/bin/activate && gitreins guard` | ✓ `Tier 1: DEGRADED PASS (skips: lint=no linter on PATH)`, `✓ tests (full)` |
| `git commit -m test` (activated) | ✓ exit 0, 7 files |
| `qa record` / `qa list` on the wheel | ✓ recorded + listed; **UNKNOWN** default reproduced here too |
| `worktree fresh` on the wheel | ✗ same board-dir error — all three P1s reproduce on released 0.14.0, not just HEAD |
| `commit-msg` hook installed by `install`/`init`? | ✗ correctly absent (docs say so) — but its body has to come from the hook snippet, and per POC-30 that body is inert without the stage |

## Judgement on the four questions

1. **Does it work?** Yes, each capability, once activated — the ledger round-trips into
   `report`, the battery runs and self-records, the audit grades messages well. The failure
   mode is not "broken" but "silently off": `commit-audit` prints *nothing at all* in the
   default state, and there is no line saying why.
2. **Is it useful?** The QA ledger is the strongest new thing here: one schema for harness-run
   and externally-produced QA, browsable next to verdicts. For this fleet it is directly
   useful — the bunker batteries run on 09-19/20 wrote `df-gitreins qa record …`-shaped data
   by hand. The message audit is useful where a repo commits through the harness.
3. **Is it usable?** That is the blocker. Time-to-first-success for the *ledger* was ~3 min.
   Time to a working *message audit* was ~20 min including reading `engine/pipeline.py` to
   find which `mode` the engine actually reads. Time to a working *battery* was gated on a
   directory the install never creates. Friction count: **9**, seven inside the documented flow.
4. **Is it trustworthy?** Better than last time: no corruption, no data loss on the happy path,
   PyPI == HEAD, DF-011 still fixed. But the ledger has three independent ways to be wrong
   about what happened — an undecided row (POC-29), a dangling evidence pointer (POC-29), and
   silent truncation (POC-32) — and `install` leaves the ledger un-ignored so QA rows land in
   user history (POC-31).

## What would fix the most, in order

1. **POC-28** — make the guard's subprocess see the venv that runs it. One PATH prepend;
   unblocks the documented quickstart's first commit on every fresh machine.
2. **POC-27** — `mkdir -p` the board dir (it is scratch state) or document the requirement and
   exit 2 with the actionable line instead of a traceback.
3. **POC-30** — stop being silent: name the skip when no stage matches, and honour the `mode`
   placement the docs describe.
4. **POC-29 / POC-31 / POC-32** — ledger honesty: default verdict, evidence validation,
   install-time gitignore, eviction announcement.

## Artifacts left behind

- Board rows **DF-GITREINS-POC-27 … -32** (6 rows: 4×P1, 2×P2) on `.coding-hermes/board/tasks.jsonl`.
- `skills/gitreins-usage/SKILL.md` v1.4.0 — new pitfalls 21–25 and the activation-path sections.
- `docs/dogfood/diagnostics.md` — the 09-20b section: how the ledger/pipeline/hook code is
  actually wired, why each silent no-op happens, and the right way to activate each surface.
- This report.
- No foreman wake, no cooldown touched (2026-09-09 fleet law), no code changed.
