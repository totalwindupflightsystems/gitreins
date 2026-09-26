# Worktree fleet quickstart: a lane that survives its own judge phase

The [README's fleet section](../README.md#parallel-worktree-fleet) compresses
this into a copy-paste block. This page walks the full flow on a real consumer
repo and shows what each step looks like when it works — every transcript below
is from an actual run. The destination: a lane whose `command` phase commits
work in its own tree, whose `guard` and `judge` phases run there, and whose
`--merge` fast-forwards canonical main.

## 0. The consumer repo

Any repo that passes its own gate. The walk uses a two-file Python app:

```bash
git init -b main
# app.py, test_app.py, pytest.ini committed — `gitreins guard` is green here.
```

## 1. `gitreins init` — and commit ALL of it

`gitreins init` writes the guard config, the pre-commit hook, `.gitleaks.toml`
and the `.gitignore` entries. Everything it writes must be committed in the
canonical checkout before the first fleet run, because a lane tree is a fresh
checkout of the repo's *committed* files — whatever is untracked in canonical
main never reaches a lane.

The split matters:

| Path | Committed? | Why |
|---|---|---|
| `.gitreins/config.yaml` | **Yes — required** | The one init output that is NOT gitignored, and the one every lane's guard/judge phases need. An uncommitted config is the #1 "fleet cannot merge" cause (see README Troubleshooting F1). |
| `.gitignore`, `.gitleaks.toml` | Yes | Part of the repo's own gate; lanes inherit them with the branch. |
| `.gitreins/tasks.yaml` | Never | Per-checkout task store; gitignored by init. The fleet seeds lane copies itself. |
| `.gitreins/worktrees.json`, `usage.jsonl`, `qa-ledger.jsonl`, `logs/`, `verdicts/` | Never | Runtime artifacts; gitignored by init, and exempt from the merge gate's clean-tree check while untracked. |

```bash
gitreins init
git add -A && git commit -m "gitreins: init"
```

The commit runs the freshly installed pre-commit hook; on a brand-new repo
that is a DEGRADED pass naming what it could not grade (`allow_skips: true` is
what init writes), and it lands.

## 2. Create the task in the canonical checkout

```bash
gitreins task create API-1 "Serve GET /status" \
  "GET /status returns 200" \
  "A test covers the endpoint"
```

Tasks live in the repository's canonical store, not in one lane — but the
store itself is per-checkout (`.gitreins/tasks.yaml`, never committed), so a
freshly created worktree starts with an EMPTY store. The fleet closes that gap
itself: before a lane's judge phase runs, it copies the lane's task verbatim
(title + criteria) from the canonical store into the lane tree. That seeding
only works when the task exists in canonical main, which is why step 2 comes
before step 3.

## 3. The manifest

`worktree fleet` takes one JSON or YAML document with a `lanes` list. Every
field of a lane:

| Field | Required | Meaning |
|---|---|---|
| `task_id` (or `id`) | yes | Names the lane, its worktree (`../<repo>-wt/<task_id>`), its branch (`gitreins/task/<task_id>`) and the task it judges. |
| `command` | yes | The lane's work, as an argv array — run in the lane tree. |
| `guard` | no | Run after `command` in the same tree (typically `["gitreins", "guard"]`). |
| `judge` | no | Run after `guard`; typically names the task from step 2. A lane with no judge phase merges only with `--force-merge`. |
| `priority` | no | Higher merges first (ties broken by task id). Default 0. |
| `brief_path` | no | Path to the lane's worker brief, recorded in the worktree registry. |
| `timeout_seconds` | no | Wall-clock cap applied to each phase command. |

Keep the manifest OUTSIDE the checkout (as here) or commit it: an untracked
file in canonical main is dirt, and the merge gate refuses a dirty canonical
main.

```bash
cat > ../lanes.json <<'JSON'
{
  "lanes": [
    {
      "task_id": "API-1",
      "priority": 10,
      "command": ["bash", "-c", "echo ok > status.txt && git add status.txt && (git diff --cached --quiet || git commit -qm 'API-1 status endpoint')"],
      "guard": ["gitreins", "guard"],
      "judge": ["gitreins", "judge", "API-1", "--skip-tier2"]
    }
  ]
}
JSON
```

### The lane command must commit, and must be idempotent

Two properties the example command has, and a naive `git commit` does not:

- **It commits its work in the lane tree.** The merge gate refuses a dirty
  tree on BOTH sides — canonical main and the lane. Work left uncommitted in
  the lane tree can never merge.
- **It is idempotent across retries.** The same manifest gets re-run — after a
  refused merge, after a failure elsewhere in the fleet, by a scheduler retry.
  A plain `git commit` fails the SECOND time with `nothing to commit, working
  tree clean`, and the re-run reports a failed phase even though the first run
  merged the work. The `(git diff --cached --quiet || git commit ...)` idiom
  skips the commit when the staged diff is empty, so re-running cannot fail on
  the lane's own leftovers.

## 4. Run the fleet with `--merge`

```bash
gitreins worktree fleet ../lanes.json --merge
```

Each lane runs up to three phases in its own tree — `running` (the command),
`guarding`, `judging` — with the judge task seeded just before the judge phase.
The tick report (JSON on stdout) is the source of truth; the command exits 0
even when lanes fail, so always read the report. Trimmed real output:

```json
{
  "cap": 2,
  "lanes": [
    {
      "task_id": "API-1",
      "state": "merged",
      "passed": true,
      "merge": {
        "mode": "fast-forward",
        "branch": "gitreins/task/API-1",
        "source_commit": "72fa83e11dbee7603254002a68eb76a68dfa64df",
        "destination_commit": "72fa83e11dbee7603254002a68eb76a68dfa64df"
      },
      "stages": [
        {"phase": "running",  "exit_code": 0, "output": "..."},
        {"phase": "guarding", "exit_code": 0, "output": "..."},
        {"phase": "judging",  "exit_code": 0, "output": "... Overall: PASS"}
      ]
    }
  ],
  "merge_errors": {},
  "merge_order": ["API-1"]
}
```

`state: "merged"` means the fast-forward landed on canonical main. Anything
else appears in `merge_errors` with the gate's reason.

### The verdict gate — what `--merge` actually checks

For each lane, before touching canonical main:

1. Both trees are clean (runtime artifacts exempt).
2. A verdict exists for the lane's EXACT branch tip: the gate compares the
   verdict's own `worktree`/`branch`/`commit` stamps, so a verdict that graded
   a different commit is not a verdict.
3. The verdict is a PASS — a matching FAIL holds the worktree and branch
   in place, with the verdict reference in the error.
4. The PASS carries no skipped Tier 1 steps — a gate that never ran cannot
   vouch for the commit.
5. The lane branch is fast-forwardable (or gets rebased; after a rebase the
   guard AND judge re-run, because the graded commit changed).

### Tier 2 and the key

`gitreins judge <id>` runs Tier 1 guards, then the Tier 2 LLM evaluator.
Tier 2 needs `GITREINS_LLM_API_KEY` (plus optional `GITREINS_LLM_BASE_URL` /
`GITREINS_LLM_MODEL`) in the environment the fleet runs in. Without a key,
`--skip-tier2` grades Tier 1 alone — an explicit, honest evaluation whose PASS
merges exactly like a full one. A lane that must not write task or verdict
state can judge with `judge --ephemeral --persist-verdict`: no history entry
and no task store, but the one document the gate reads
(`.gitreins/verdicts/verdict.json`) makes its PASS usable.

## 5. Retries and recovery

Three re-run shapes, all real:

- **Re-run after a successful merge** (idempotent command): the lane re-runs,
  its command commits nothing new, and the report says so honestly —
  `"error": "merge refused: task branch has no commits beyond its registered
  branch point"`. Nothing left to merge; that line is the success signal, not
  a defect.
- **Re-run with a non-idempotent command**: the reused tree already contains
  the work, the command exits non-zero (`nothing to commit`), the lane is
  reported failed. Write idempotent lane commands (step 3).
- **Re-run after a FAILED lane**: the failed tree sits at the failed run's
  HEAD and is never silently reused — the fleet refuses with
  `lane '<id>' has a FAILED worktree at a stale HEAD: run 'gitreins worktree
  clean' ... to start fresh`. Reap and re-run:

```bash
gitreins worktree clean          # reaps failed/merged trees
gitreins worktree fleet ../lanes.json --merge
```

A lane that dies BEFORE committing work (F1 below) leaves an empty branch
behind that `clean` does not delete — the next run then refuses with
`branch 'gitreins/task/<id>' already exists but is not registered to task
'<id>'`. Delete that branch and re-run:

```bash
git branch -D gitreins/task/<id>
```

### Recovery after a refused merge — without re-running the work

A dirty canonical main refuses the merge but the lane's work and verdict stay
in the lane tree. Fix the dirt, then merge the already-judged lane directly:

```bash
gitreins worktree merge API-1
# Merged API-1 (fast-forward) into canonical main at 72fa83e4b153
#   branch: gitreins/task/API-1
#   worktree reaped: /tmp/demo-wt/API-1
```

Or re-run the whole manifest with `--merge`: idempotent lane commands make
that safe.

## 6. The two classic failure modes

Condensed here; the README's [Troubleshooting](../README.md#troubleshooting)
has the full entries.

- **`no .gitreins/config.yaml — run 'gitreins init' first'` inside every lane**
  — the config was never committed to the consumer repo. `gitreins init`
  inside the lane tree cannot fix the next lane; commit the config in
  canonical main.
- **`canonical main has uncommitted changes; refusing merge`** — the merge
  gate counts any uncommitted file in canonical main, including an untracked
  manifest. Move the manifest outside the repo or commit it.

## 7. Knobs

```yaml
worktree_fleet:
  max_concurrent_worktrees: 2   # default; per-run override: --max-concurrent-worktrees
  venv:
    source: .venv               # symlinked into each new lane tree when present
    name: .venv
  disk_ceiling_mb: 4096         # written by init; refuse lane creation past it
```

When the configured venv source exists, every new task tree symlinks it rather
than installing dependencies. The shared environment is intentionally not
mutated by GitReins, and concurrent dependency installs are not lane-safe.

`--force-merge` (with a required `--actor`) bypasses the verdict gates for a
run; every bypass is recorded in the fleet board events with the actor and
reason. Successful `--merge` lanes are applied in priority/task-id order under
an advisory lock.
