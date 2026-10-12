# Disposable Verification

Disposable verification runs use detached Git worktrees under
`../<repo>-wt/.disposable/`. They share Git objects with the main checkout,
but commands run in a clean working directory and cannot change the main
checkout. Trees and registry records are reaped after each run unless an
inspection flag retains them.

## QA battery

Run a battery command without a bunker or remote service:

```bash
gitreins worktree fresh --cmd "pytest -q" --json /tmp/fresh.json
```

`--cmd` is a shell command string executed as `sh -c "<command>"` from the
throwaway tree. `--timeout` limits execution in seconds. `--keep` retains the
tree for inspection; otherwise reaping happens in a `finally` block even when
the command fails or times out.

The command's exit code is returned. GitReins infrastructure failures (tree
creation, Git removal, or evidence writing) return exit code 2. A command
failure is returned unchanged. The optional evidence object contains the
command, exit code, duration, tree path, keep status, timestamps, and bounded
command output.

## Dogfood

Exercise GitReins itself in an isolated checkout:

```bash
gitreins worktree dogfood --skip-judge --test-command "true" \
  --json /tmp/dogfood.json
```

The flow runs the source checkout's own entry point (the repository's
`gitreins/cli.py` invoked with `sys.executable`) for
`init`, `task create` plus `task start`, and `guard`. The judge step runs the
real `task complete` path when an LLM key is configured. Without an LLM key it
is recorded as `{"status": "skipped", "reason": "no LLM configured"}`; it is
never represented as a fake pass. `--skip-judge` gives a deterministic skip
for offline tests. `--test-command` changes the generated guard config only
inside the disposable tree. `--keep` preserves the tree, and `--timeout`
limits each child CLI step.

Dogfood returns 0 when the executed steps pass or the judge is skipped, 1 when
a step fails, and 2 for GitReins infrastructure failures. Evidence includes
a `steps` array with status, exit code, duration, and output, plus a `judge`
object and tree lifecycle fields.

## Repro farm

Run the same command from one captured `HEAD` in multiple independent trees:

```bash
gitreins worktree repro --cmd "pytest tests/test_flaky.py -q" -k 10 \
  --concurrency 3 --keep-failures --json /tmp/repro.json
```

`-k` is the number of runs. `--concurrency` defaults to
`max_concurrent_worktrees`; `--timeout` is per run. Successful trees are
always reaped. `--keep-failures` retains only failed trees and prints their
paths for inspection. Exit 0 means every run passed, exit 1 means at least
one command failed, and exit 2 means GitReins could not create, execute, or
reap the farm.

The JSON shape is:

```json
{
  "command": "pytest tests/test_flaky.py -q",
  "k": 10,
  "concurrency": 3,
  "head": "<commit>",
  "passes": 9,
  "failures": 1,
  "pass_rate": 0.9,
  "runs": [
    {"index": 1, "exit_code": 0, "duration_s": 0.31,
     "tree": "../repo-wt/.disposable/run-...", "kept": false}
  ],
  "started_at": 0.0,
  "finished_at": 0.0
}
```

A worked flake example: ten runs with nine exit codes of 0 and one exit code
of 1 report `repro: 9/10 passed (pass rate 0.90)`. With `--keep-failures`,
the failed tree remains available for reproducing the failing state; the nine
successful trees are removed before the command returns.

## QA run ledger

Every QA surface records its outcome. `worktree fresh`, `worktree repro`, and
`worktree dogfood` append one row to the QA ledger, so a verdict survives the
reaped tree, the ceiling reaper, and the gitignored registry:

```bash
gitreins qa list                 # newest runs: verdict, cells, exit code, commit
gitreins qa list --json          # the rows themselves
```

A run produced outside the harness — a fleet QA lane, a bunker battery, a
manual audit — is recorded with `gitreins qa record`:

```bash
gitreins qa record --project my-repo --kind bunker --exit-code 0 \
  --cell launch=OK --cell collect=OK --evidence /tmp/evidence.jsonl \
  --note "fresh-system battery"
```

Rows carry the fleet QA-ledger keys (`ts`, `project`, `status`, `cells`,
`findings`, `evidence`, `note`) plus harness extras (`kind`, `verdict`,
`run_id`, `exit_code`, `commit`, `harness_version`, `detail`), so a consumer
that already reads the fleet schema can read a harness-written ledger.

`GITREINS_QA_LEDGER` overrides the ledger location (a file, or a directory that
receives `qa-ledger.jsonl`); otherwise `qa_ledger.path` in
`.gitreins/config.yaml` applies, defaulting to
`<repo>/.gitreins/qa-ledger.jsonl`. `qa_ledger.enabled: false` stops recording
and `qa_ledger.max_entries` (default 1000) keeps the newest rows. Recording
never fails a QA run: a write failure is reported on stderr and the run's own
exit code is unchanged.

## Disk ceiling

Set the cap in `.gitreins/config.yaml`:

```yaml
worktree_fleet:
  disk_ceiling_mb: 4096
```

The default is 4096 MB. Zero or a negative value means unlimited. Values must
be integers; booleans, fractions, and other malformed values fail clearly.
Before every task or disposable worktree creation, GitReins sums recursive
regular-file sizes for registered managed trees. Symlinks (including shared
virtual environments) and Git object storage are ignored. If the new tree
would exceed the cap, GitReins reaps oldest disposable runs first, then oldest
completed or merged task trees. Running or heartbeat-fresh task trees and the
main checkout are never reaped. If the cap still cannot be satisfied, creation
fails with the configured ceiling and measured usage, without leaving a new
partial worktree.

Retained runs can be collected with:

```bash
gitreins worktree clean
```

That pass reports **every** registry entry, not only the ones it reaped:

```text
Reaped 3 disposable run(s): run-a, run-b, run-c
  interrupted run(s) reaped: run-c
Forgot 1 disposable run(s) whose tree was already gone: run-d
Kept 1 disposable run(s):
  run-e [running] — run is still active (pid 4242)
```

- `Reaped` — kept and kept-failure trees removed now. A subset is named as
  `interrupted run(s) reaped`: a run that was killed before it could finalize
  itself, whose recorded process is gone.
- `Forgot` — the entry's tree directory no longer exists (a manual `rm -rf`, a
  host cleanup). Git metadata is pruned and the entry is dropped; an
  unreapable ghost is never left registered, and a missing directory is no
  longer reported as an infrastructure failure.
- `Kept` — left registered on purpose, one line per entry as
  `<run_id> [<status>] — <reason>`. A live run is protected because its tree
  may be in use, but it is always named, so an entry can no longer sit in the
  registry while clean prints only `Nothing to reap.`

Every entry carries a status:

| Status | Meaning |
|--------|---------|
| `running` | Registered, not finalized yet. |
| `kept` | Finished with exit code 0; the tree was retained. |
| `kept-failure` | Finished non-zero; the tree was retained for inspection. `exit_code` holds the command's exit status. |

A `running` entry whose process is gone is reaped as an interrupted run. One
that has not recorded a pid yet is protected for 300 s — the window between
registering the entry and starting its child — so a `worktree clean` racing a
`worktree repro` can never rip the tree out from under the run about to use
it. Entries written before the status field existed infer one: `keep: true`
reads as `kept`, anything else as `running`.
