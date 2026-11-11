# Operations — running GitReins

Audience: the operator who has to keep this thing running, restart it, and know
what is safe to lose. Everything below is measured against this checkout
(`gitreins 0.18.0`) — commands are runnable as written, and every claim about the
codebase names the file it comes from.

Two surfaces exist and they are different animals:

| surface | what it is | lifetime |
|---|---|---|
| `gitreins` CLI | one-shot commands (`guard`, `judge`, `task`, `commit`, `report`) | exits when the command finishes |
| `gitreins serve` | live judgment browser: an HTTP server over `.gitreins/history/` | long-lived, operator-launched |
| `gitreins mcp-server` | stdio MCP bridge for an agent harness (no port, no daemon) | lives as long as the harness that spawned it |

Only `serve` is a resident network service. Nothing in this repository starts it
for you — see *Supervision*.

## Fixture canary ledger

The append-only canary helper records fixture or mock outcomes and reports
must-catch recall. It does not call a provider or make a network request.

```bash
.venv/bin/python scripts/canary.py /path/to/canaries.jsonl
```

Create records through `append_record(path, {"must_catch": True,
"caught": True|False, ...})`; `report(path)` returns `caught`, `missed`, and
`recall`. The ledger is fixture/mock-only: this artifact proves local wiring and
accounting, not provider behavior or live detection performance.

Trace: ch:trace row=GR-209 spec=.coding-hermes/board/tasks.jsonl#GR-209 wave=none:serial test=tests/test_canary.py doc=docs/operations.md evidence=scripts/canary.py witness=none:fixture-or-mock-only verdict=none:pending-judge commit=none:pending memory=none:pending

## Run it: start the judgment viewer

```bash
pipx install gitreins
gitreins serve --port 8616
```

Flags (from `gitreins/cli.py`, the `serve` subparser):

| flag | default | meaning |
|---|---|---|
| `--port` | `8616` | TCP port to bind |
| `--host` | `127.0.0.1` | bind address; anything else exposes judgment data with no auth |
| `--repo` | cwd's repository | browse another checkout's judgments by path |
| `--project` | *(empty)* | scheduler project name for the tick ledger, e.g. `gitreins` |
| `--open` | off | open a browser on start |

The server is a `ThreadingHTTPServer` (`gitreins/serve.py`) that re-reads the
verdict store on each request and holds no state of its own, so there is nothing
to drain before a stop.

Health and metrics, no browser required:

```bash
curl -sS http://127.0.0.1:8616/health | jq -r .status      # -> ok
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8616/metrics
```

`/health` is a pure liveness probe (status, version, repo — it triggers no scan);
`/metrics` serves the Prometheus exposition described in
`docs/observability-spec.md`. Both must answer 200 even when the job store is
empty or unreadable by design.

## Stop and restart it

```bash
# graceful stop: Ctrl-C in the foreground, or by pid
pgrep -af 'gitreins serve'          # identify the exact process first
kill <pid>                          # SIGTERM; the HTTP server exits cleanly
```

Restart is just a re-run — there is no warm state to restore:

```bash
gitreins serve --port 8616 --project gitreins
```

Diagnose a refused start by the port, not by the process name — a second
instance silently loses the bind:

```bash
ss -ltnp | grep ':8616 '
```

Confirm the listener is gone after a stop (`ss -ltn` must print nothing on the
port) instead of trusting `kill`'s exit status: a wrapper shell can exit while
the server child keeps the port.

## Logs

`serve` writes to stdout only. `BaseHTTPRequestHandler.log_message` is
deliberately a no-op (`gitreins/serve.py`), so **no request log is persisted** —
an operator who needs one must redirect at launch:

```bash
gitreins serve --port 8616 > ~/gitreins-serve.log 2>&1 &
```

For an unattended deployment, that redirect (or the supervisor's own journal) is
the entire log surface today. Long-lived MCP servers started by an agent harness
log into the harness's own session, not here.

## State and backup

There is **no owned database**. The durable artifacts are files, and the two
that matter are already under version control:

| path | contents | durability |
|---|---|---|
| `.gitreins/history/<date>/<run>/verdict.json` | every verdict plus its evidence (`commit.patch`, `worktree.patch`, `summary.md`, driver log tail) | auto-committed to the orphan `gitreins` branch (`history.storage: git`) |
| `.gitreins/tasks.yaml` | live task ledger for this checkout | local file, **gitignored** — not backed up by git |
| `.gitreins/qa-ledger.jsonl`, `.gitreins/usage.jsonl` | QA history and judge token accounting | local files, gitignored |
| `.coding-hermes/board/{tasks,events,board}.jsonl` | the foreman board | git-tracked JSONL (the canonical store) |
| `~/.local/share/gitreins/jobs/` | async judge job records and logs (`GITREINS_JOB_DIR` overrides) | local, per-host |

The backup answer is therefore explicit and short:

```bash
git push github main                # board, code, docs
git push github gitreins            # verdict history branch (if not auto-pushed)
```

plus the host backup for the gitignored local ledgers (`tasks.yaml`,
`qa-ledger.jsonl`, `usage.jsonl`, the job store). None of those are reconstructible
from a clone: a restored host loses in-flight task records and job logs, not
verdicts.

**Root-level dev databases are not a datastore.** This checkout used to carry
`memory.db`, `dev.db`, `dagger.db` and `tasks.db` in the repository root. All four
were gitignored (`.gitignore`: `*.db`), referenced by no module in `gitreins/`,
`engine/` or `scripts/`, and dated from unrelated local experiments — dev residue,
not product state. They were deleted (`OPS-GITREINS-1`) so that a future census or
a curious operator does not mistake them for something worth backing up. Deleting
them loses nothing: the product never opened them.

## Upgrade

```bash
pipx upgrade gitreins
gitreins --version
```

For a working checkout, install it editable instead and re-run `init` so the
pre-commit hook is rewritten against the current entry point:

```bash
uv pip install -e '.[dev]'
gitreins init
```

Two traps worth knowing before an upgrade:

- **A pipx copy can be stale while reporting the same version.** Hooks and
  `gitreins task complete` resolve through the pipx console script, so a
  long-lived install can execute code older than the checkout. Prove identity by
  a symbol, not the version string — e.g. grep the pipx site-packages for a
  function your change introduced.
- **A `guard` run on a clean tree is not a test run.** With nothing staged the
  tests lane reports `No files staged — skipped`; the full suite is only
  exercised when files are staged or `guards.test_on_clean` is set.

## Supervision (open decision)

Nothing restarts `serve` today: no systemd unit, no container, no orchestrator is
shipped in this repository, and the fleet measured no supervisor on the host.
Whether it becomes a supervised service (systemd unit + `/health` probe) or stays
an on-demand operator tool is **not decided here** — it is tracked as
`OBS-GITREINS-5`, and until that decision lands the safe reading is:

- `serve` is an operator-facing viewer, started by hand;
- its metrics are live only while a human has it open;
- a deployment that needs it always-on must add supervision deliberately, not by
  copying a command out of this doc.

Related: `docs/judgment-viewer.md` (user-facing usage and API contract),
`docs/observability-spec.md` (metrics, scrape job and the monitoring decision),
`docs/onboarding.md` (§6 walks through `report` and `serve` for a first-time
operator).
