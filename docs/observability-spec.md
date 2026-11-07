# Observability spec — `gitreins`

Status: **audit complete; §4b/§5b CLI completion records IMPLEMENTED.** The CLI half of
this catalogue ships as `engine/cli_metrics.py` (one completion record per subcommand
invocation, pushed from the single dispatcher `gitreins/cli.py` `_run_subcommand_handler`
in a `try/finally`; transport and best-effort contract per §5b). The resident-server
catalogue (§4a/§5a) is **still not implemented**. Sections 2/2a/2b below still record the
audited pre-instrumentation state that motivated this document.

Audited at version `0.17.1` (`pyproject.toml:7` at audit time; the package now declares `0.19.0`), 2026-11-03.

---

## 1. Role of the product, and what "healthy" means

GitReins is a git-native AI agent co-harness. It ships **two shapes of runtime**, and they
must not be conflated — the correct observability answer is different for each.

### 1a. Resident service: `gitreins serve`

`gitreins serve` starts a long-lived, read-only HTTP server (the judgment viewer) on
`127.0.0.1:8616` by default (`gitreins/cli.py:5013-5014`,
`gitreins/serve.py:822-828`). It is a `ThreadingHTTPServer` (`gitreins/serve.py:837`)
that re-reads the repo's judgment stores on every request.

Healthy means, in order of what an operator asks at 3am:

1. **Process up and bound** — the socket accepts.
2. **Requests are served, not erroring** — 5xx rate near zero; the handler swallows
   exceptions into a 500 body (`gitreins/serve.py:810-814`), so a wedged store shows up
   as 500s and nothing else.
3. **The data it serves is fresh and non-degenerate** — `/api/stats` counts grow as
   judges run. A viewer that is up but reporting a frozen verdict count is broken and,
   today, indistinguishable from a healthy idle one.
4. **Request latency is bounded** — `/api/stats` does a full aggregation over every
   verdict (`gitreins/serve.py:762-775`), so its latency is a direct function of history
   size; it degrades silently as `.gitreins/history` grows (48M, 457 verdicts today).

### 1b. Ephemeral CLI: everything else

`gitreins judge`, `guard`, `task`, `resolve`, `preflight`, `report`, `qa`, `dogfood` are
short-lived processes. They have **no HTTP surface and never should**. For these the
right answer is a **completion record**, not a scrape endpoint: a durable line pushed on
exit to a place a collector can read (pushgateway / textfile collector). A process that
lives for 400ms cannot be scraped at a 15s interval, and a `/metrics` endpoint on it
would be dead weight.

The MCP server (`gitreins mcp-server`, `gitreins_mcp/server.py:1576`) is also resident
but talks line-delimited JSON over **stdio** (`gitreins_mcp/server.py:2-7`,
`pyproject.toml:62` is the only console script). It cannot host an HTTP `/metrics`
without a second listener, so its work is captured by the same completion-record path as
the CLI plus a scrape of `gitreins serve` if one is co-hosted. See §7 (not instrumented).

---

## 2. Current state — verified, with citations

**Verdict: BLIND.** No metric of any kind is emitted today. Every claim below was
verified by command, not assumed.

| Check | Command | Result |
|---|---|---|
| `/metrics` route | `grep -n '"/metrics"' gitreins/serve.py` | **absent** — routes are `/`, `/health`, `/api/*` only (`gitreins/serve.py:748-796`, `gitreins/serve.py:799-807`) |
| Prometheus client | `grep -rln "prometheus_client\|promhttp\|prom-client" --include=*.py --include=*.toml .` (excl. `node_modules`) | **zero files.** Not in `dependencies` (`pyproject.toml:24-33`) |
| OpenTelemetry in first-party code | `grep -rn "otel\|opentelemetry" engine/ gitreins/ gitreins_mcp/ tests/` | **zero hits.** The 61 corpus-wide `otel` mentions are `.venv*/site-packages` only (`mcp/server/_otel.py`, `rich`, `pygments`, `execnet`) — dependency noise, confirmed |
| StatsD / Sentry | `grep -rn "statsd\|sentry" engine/ gitreins/ gitreins_mcp/` | **zero hits** |
| "own_counter_or_gauge=33" hypothesis | `grep -rEic "(counter\|gauge\|histogram\|metric_)" engine/ gitreins/ gitreins_mcp/` | **18 case-insensitive hits, and every one is a COMMENT or an unrelated word** — `engine/eval_cap.py:75` "iteration counter", `engine/usage.py` docstring prose, `engine/config.py` config-key prose. **No instrumented counter exists.** The 33 was comment noise |
| What it logs | `gitreins/serve.py:816-819` | `log_message` is an explicit **no-op** unless `GITREINS_SERVE_VERBOSE` is set. The resident server is silent by design |
| | `gitreins_mcp/server.py:57-59` | `logging.basicConfig(level=WARNING, stream=sys.stderr)` — INFO task lifecycle lines (`:647`, `:656`, `:665`) are **dropped at the default level** |
| Existing liveness probe | `gitreins/serve.py:748-761` | `/health` returns `{status, version, repo, path}` — a real JSON liveness probe, cheap (no verdict scan). This is the only operational surface that exists |

### 2a. Durable state that exists but is never emitted

This is the crux of the audit. The numbers are already measured and written to disk;
nothing exports them.

| State | Lives at | Citation | What it already knows |
|---|---|---|---|
| Judge job records | `~/.local/share/gitreins/jobs/*.json` (`GITREINS_JOB_DIR`) | `engine/job_store.py:22-40`, `:48-50` | `status`, `started_at`, `finished_at`, `pid`, `error` — a complete job lifecycle. **4075 records / 17M on this host** |
| Per-run token usage | `<repo>/.gitreins/usage.jsonl` | `engine/usage.py:2-10`, `:111-173`; writer inline at `engine/pipeline.py:1160-1167`; resolution gate at `engine/persist.py:1216` | `ts`, `tokens_in`, `tokens_out`, `cache_read`, `cache_write`, `step`, `model`, credential *sources*. **356 rows on this host** |
| Verdict history | `<repo>/.gitreins/history/<date>/<hash>/verdict.json` | `engine/persist.py:225-300`, `HISTORY_REF` at `:154` | `passed`, `items`, `stages`, `evaluated_at`, `supersedes`. **457 verdicts / 48M** |
| Verdict aggregate (already computed!) | `GET /api/stats` | `gitreins/serve.py:416-432` | `total`, `passed`, `failed`, `pass_rate`, `resolution_records`, plus a `usage` summary — **the exact numbers a metric needs, already serialized to JSON** |
| QA ledger | `<repo>/.gitreins/qa-ledger.jsonl` | `engine/qa_ledger.py:28-32`, `:95-119`, `:441` | `status`, `verdict`, `cells`, `findings`, `exit_code`. 25 rows (13 pass / 11 fail / 1 findings) |
| Guard run logs | `<repo>/.gitreins/logs/guard-<stamp>.log` | `engine/guard_manager.py:1215-1223` | Bounded: `GUARD_LOG_KEEP = 20`, `GUARD_LOG_MAX_BYTES = 2MiB` (`:1221`, `:1223`). Retention is handled; **no counts are emitted** |
| Eval caps per run | `engine/eval_cap.py:47-75` | in-memory only | `max_iterations/max_seconds/max_input_tokens/max_output_tokens` and `cumulative_*`. **A cap trip is never recorded anywhere** — `EvalCap.check()` returns a string (`:163-165`) that dies with the process |
| LLM retry / error | `engine/llm.py:237-279` | `logger.warning("HTTP error (attempt %d/%d)")` at `:263`, network at `:265-268` | Retries are logged at WARNING; **failures are not counted**, and the logger's records go nowhere persistent |

### 2b. Blind spots that actually hurt (measured on this host)

1. **Orphaned judge jobs are invisible.** 4075 job records exist; **35 are still
   `running`, and all 35 have a dead or absent pid** (verified with an `os.kill(pid, 0)`
   probe). 34 of them are older than 24h. The only thing that repairs them is the next
   `judge --status` poll (`gitreins/cli.py:3481-3511`, "flips dead-pid orphans to error"),
   so **an orphan blocks its `(task_id, workdir)` single-flight key
   (`engine/job_store.py:142-166`) until a human happens to poll it.** An operator has no
   gauge for "how many running jobs are zombies right now".
2. **Nothing records a unit of work's duration as a measurement.** `started_at` and
   `finished_at` exist per job (`engine/job_store.py:31-32`) but no aggregation is
   emitted, and the verdict itself carries no duration field (`verdict.json` keys verified:
   `task_id, task_title, task_criteria, passed, worktree, branch, commit, items, stages,
   summary, evaluated_at, supersedes, superseded_by, evidence` — **no elapsed**).
3. **Token/cost is per-run only.** `usage.jsonl` is append-only and read on demand by the
   viewer, but Prometheus never sees a token count. A judge that silently starts burning
   10× tokens looks exactly like one that is healthy.
4. **Cap exhaustion is a silent failure mode.** When `EvalCap` trips, the run degrades;
   the reason string is returned to a caller and discarded. No counter, no log line at
   INFO, no ledger entry.
5. **The job store grows without bound.** `delete_job` exists (`engine/job_store.py:133`)
   but there is no retention sweep; 17M / 4075 files and climbing. Same for
   `.gitreins/history` (48M / 457 verdicts) — `max_verdicts` pruning is referenced in a
   config comment (`gitreins/cli.py:187`) but the growth itself is unobserved.
6. **`gitreins serve` is dead-silent.** `log_message` is a no-op (`serve.py:816-819`), so
   there are no request counts, no error counts, no latency. If it 500s on every request,
   the only symptom is a broken SPA.

---

## 3. The decision: endpoint vs record

| Runtime | Lifetime | Right answer | Reasoning |
|---|---|---|---|
| `gitreins serve` | resident | **`/metrics` endpoint on the existing ThreadingHTTPServer** (`prometheus_client`, `generate_latest`) | Already has an HTTP surface; a scrape is free |
| `gitreins mcp-server` | resident, stdio | **No endpoint.** Completion records + optional exposure via a co-hosted `serve` | A stdio server has no port to scrape; adding a listener changes its security posture for no gain |
| `gitreins judge` / `guard` / `resolve` / `preflight` / `qa` / `dogfood` / `report` | seconds to minutes, then exit | **Completion record pushed on exit** (pushgateway or `textfile` collector) | Cannot be scraped; a record survives process death |

The one rule: **do not put `/metrics` on a CLI.** It would be scraped zero times and the
series would vanish before the collector's first poke.

---

## 4. Metric catalogue

Naming: `<product>_<subsystem>_<thing>_<unit>`. Every label is low-cardinality — no task
id, no worktree path, no verdict hash, no prompt, no timestamp. `product="gitreins"` and
`host` are attached by the scrape job / push, not by the process (§5).

### 4a. Resident server — `gitreins serve` (`/metrics`)

| Metric | Type | Unit | Labels | The one question it answers |
|---|---|---|---|---|
| `gitreins_serve_http_requests_total` | counter | requests | `method`, `route`, `status_class` | Is it serving, and are responses 2xx or 5xx? |
| `gitreins_serve_http_request_duration_seconds` | histogram | seconds | `route` | Which route is slow — is `/api/stats` degrading as history grows? |
| `gitreins_serve_up` | gauge | 1 \| 0 | — | Is the process alive and past its first request? (use `/health` for liveness; this for "serving") |
| `gitreins_verdicts_total` | gauge | verdicts | `repo` | How many graded verdicts does this checkout hold? |
| `gitreins_verdicts_failed_total` | gauge | verdicts | `repo` | Is the pass rate moving in the wrong direction? |
| `gitreins_history_bytes` | gauge | bytes | `repo` | Is the verdict store growing without bound? |
| `gitreins_usage_tokens_total` | gauge | tokens | `repo`, `kind` (`input`/`output`) | How many judge tokens has this checkout burned in total? |
| `gitreins_jobs_records` | gauge | records | `status` | How many jobs sit in `running` — i.e. how many could be zombies? |
| `gitreins_jobs_orphaned_running` | gauge | jobs | — | **The 3am question:** how many `running` records have a dead pid right now? |

`gitreins_verdicts_*`, `gitreins_history_bytes`, `gitreins_usage_tokens_total`,
`gitreins_jobs_*` are computed at scrape time from disk using the readers that already
exist (`gitreins/serve.py:416-432` `stats()`, `engine/usage.py:174` `load_usage_rows`,
`engine/job_store.py:117` `list_jobs`, `engine/job_store.py:237` `pid_alive`). **Do not
duplicate the computation** — call the same functions the API calls.

### 4b. Completed CLI work — pushed completion records

| Metric | Type | Unit | Labels | The one question it answers |
|---|---|---|---|---|
| `gitreins_cli_run_completed_total` | counter | runs | `command`, `outcome` (`pass`/`fail`/`error`/`cap`) | Is the harness doing work, and how often does it fail? |
| `gitreins_cli_run_duration_seconds` | histogram | seconds | `command` | How long does a judge/guard unit of work take? |
| `gitreins_cli_run_tokens_total` | counter | tokens | `command`, `kind` (`input`/`output`) | How much model spend did that run cost? |
| `gitreins_cli_run_failure_total` | counter | runs | `command`, `reason` (`llm_http`, `llm_network`, `cap_iterations`, `cap_seconds`, `cap_tokens`, `orphaned`) | **Why** did it fail? |
| `gitreins_cli_run_last_success_timestamp_seconds` | gauge | unixtime | `command` | When did this lane last actually work? |

`reason` is a **closed set of ~6 constants**, never a free-text error message — that is
where cardinality explosions come from. The `command` label set is the dispatcher's
static handler tables (`gitreins/cli.py` `_WORKTREE_HANDLERS` / `_QA_HANDLERS` /
`_SIMPLE_HANDLERS` / `_TASK_HANDLERS`) plus the seven bounded `task_<action>` rows —
never argv text, a task id or a path. Cardinality: commands × 6 reasons for the
failure counter, a few series per command for the rest — bounded by those tables
(≈ 38 commands today), so no label can explode with usage.

`gitreins_cli_run_last_success_timestamp_seconds` is the metric that catches the failure
mode nobody else catches: a lane that silently stopped running. Alert on
`time() - gitreins_cli_run_last_success_timestamp_seconds > 86400`.

**Shipped sources (OBS-GITREINS-2 rework).** The recording seam is ONE context manager
(`gitreins/cli.py:_run_completion`) used by BOTH dispatch sites — `_run_subcommand_handler`
(simple/worktree/qa families) and `_dispatch_task_command` (`gitreins task`, labels
`task_create` … `task_worktree` from the resolved action). Token counts come from the
run-scoped accumulator in `engine/usage.py` that `append_usage_row` increments as it
writes its ledger row — the single accounting path the LLM steps already use; the
dispatcher resets it before the handler and passes the taken counts, so a run with no
LLM step still emits no token series. `orphaned` has a real producer: the judge-status
liveness path (`gitreins judge <id> --status`, the QA-GITR-002 dead-pid flip in
`gitreins/cli.py:_judge_status_running`) notes the reap, and that run's ONE completion
record carries `reason="orphaned"` with the command's own typed outcome (`fail`, exit 3).

---

## 5. Exposure mechanism and collection path

### 5a. Server — `prometheus_client` in `gitreins/serve.py`

Add `prometheus_client` to `pyproject.toml:24-33` dependencies (it is a pure-Python,
zero-dependency package). In `Handler._dispatch_api` (`gitreins/serve.py:746`), add one
branch ahead of the existing routes:

```python
if path == "/metrics":
    self._send(200, generate_latest(REGISTRY), CONTENT_TYPE_LATEST)
```

Instrument at the `do_GET` boundary (`gitreins/serve.py:799-814`), as a wrapper around
`_dispatch_api`, so every route — including the 500 path — is counted without touching any
handler. Route label must be the **matched route**, never the raw path
(`/api/verdicts/<date>/<hash>` → `"verdict_detail"`), which keeps cardinality at ~8.

### 5b. CLI — completion record on exit

**IMPLEMENTED (OBS-GITREINS-2) as `engine/cli_metrics.py`**, hooked at the dispatcher
below in a `try/finally`; a cap trip is surfaced through the `last_cap_trip` note in
`engine/eval_cap.py` (the reason string that previously died with the process, §2a‑4),
and the judge-status orphan reap is surfaced the same way through the
`note_orphan_reap` note in `engine/cli_metrics.py`. The record function is
best-effort per the contract described here and emits the §4b series verbatim; the
tokens counter is fed from the `engine.usage` run accumulator (§4b shipped sources),
not from a second accounting path.

Every CLI subcommand already returns through a single dispatcher
(`gitreins/cli.py` `_run_subcommand_handler`). Emit the record there, in a
`finally`, so it fires on success, on exception and on cap-stop alike:

- **Primary:** POST to the pushgateway with a `gitreins_<command>` grouping key, shaping
  the payload as text exposition. Gate behind `GITREINS_METRICS_PUSH_URL`.
- **Fallback for a host with no pushgateway:** write the same text to
  `${GITREINS_METRICS_TEXTFILE_DIR}/gitreins-<command>.prom` for the node-exporter
  textfile collector.
- **Never fatal:** exactly the contract `append_usage_row` already documents
  (`engine/usage.py:156-160`) — a metrics failure returns quietly and never fails the run.

### 5c. Collection on fleet-monitor

- **Scrape job** `gitreins`, `metrics_path: /metrics`, `static_configs` target
  `127.0.0.1:8616` (or the host's `serve` port), with labels
  `host: <hostname>`, `product: gitreins`, `service: serve`. `serve` binds 127.0.0.1 by
  default (`gitreins/cli.py:5014`), so the scrape must run on the same host as the agent,
  or `serve` must be started with `--host` on a trusted interface (which it warns about:
  `gitreins/serve.py:868-875`).
- **Caveat, stated plainly:** the fleet agent PUSHES to Prometheus (remote_write), so a
  scrape-only endpoint needs the new scrape job to exist in
  `/root/fleet-monitoring/prometheus/prometheus.yml`. That file is **not GitReins' to
  edit** and was not touched by this audit. Row `OBS-GITREINS-4` owns it.
- **Push records** arrive via the pushgateway at `<pushgateway-url>/metrics`, scraped by
  the same or an adjacent job. Same three labels.
- `gitreins serve` is **not currently running on this host** (`pgrep -af "gitreins serve"`
  → nothing; `ss -ltn` → nothing on 8616; verified). So there is nothing to scrape today.
  The supervisor that is supposed to keep it up is an open question — see §8.

---

## 6. Verification — how a reviewer proves each metric is live

Run in order. **Done** means every command below returns a non-empty, non-zero series.

**Server (after 5a lands):**
```bash
curl -sS http://127.0.0.1:8616/metrics | grep -E '^gitreins_'
curl -sS http://127.0.0.1:8616/metrics | grep -c '^gitreins_serve_http_requests_total'
# the series must MOVE when traffic arrives:
before=$(curl -sS http://127.0.0.1:8616/metrics | awk '/^gitreins_serve_http_requests_total/{s+=$2}END{print s+0}')
curl -sS http://127.0.0.1:8616/api/stats >/dev/null
after=$(curl -sS http://127.0.0.1:8616/metrics | awk '/^gitreins_serve_http_requests_total/{s+=$2}END{print s+0}')
test "$after" -gt "$before" && echo LIVE
```
PromQL that proves the same through the stack:
```promql
rate(gitreins_serve_http_requests_total{product="gitreins"}[5m])      # > 0 under traffic
histogram_quantile(0.99, rate(gitreins_serve_http_request_duration_seconds_bucket[5m]))
gitreins_jobs_orphaned_running{product="gitreins"}                     # expected ~35 today
```

**CLI records (after 5b lands):**
```bash
gitreins judge --status <task_id>; # any real subcommand, then:
curl -sS <pushgateway-url>/metrics | grep -E '^gitreins_cli_run_'
```
```promql
increase(gitreins_cli_run_completed_total[1h])
time() - gitreins_cli_run_last_success_timestamp_seconds{command="judge"}   # < 86400
```

**Cross-check against the source of truth** (the metric must equal the disk):
```bash
# verdict count must equal the served gauge
find .gitreins/history -name verdict.json | wc -l
# orphaned-running must equal the dead-pid probe result
python3 -c "import json,glob,os;print(sum(1 for f in glob.glob(os.path.expanduser('~/.local/share/gitreins/jobs/*.json')) if (lambda d: d.get('status')=='running' and not (d.get('pid') and (lambda p:(os.kill(p,0) or True))(d['pid']) if d.get('pid') else False))(json.load(open(f))))"
```
A gauge that disagrees with its filesystem source is a wiring bug, not a rounding error.

---

## 7. Deliberately NOT instrumented, and why

- **Per-task / per-verdict labels.** `task_id`, verdict `hash`, `worktree` path. Cardinality
  is unbounded (457 verdicts and climbing) and the dashboard cannot use them. Look them up
  in `serve`'s UI.
- **`repo` on CLI records.** The CLI runs on many checkouts; `repo` there would multiply
  series. It is kept on the server metrics only, where one process serves one tree.
- **Prompt / criterion text.** Contains user code and prompts; never a label, never a log
  field.
- **Credential values.** `usage.jsonl` already stores only *sources*
  (`engine/usage.py:75-91`, values never). Metrics follow the same rule: `model` may be a
  label (bounded by the configured fleet), key/URL may not.
- **The MCP stdio server's tool calls.** No port to scrape; its work is captured by the
  completion record of the judge it dispatches. Instrumenting `mcp-server` directly would
  mean a second HTTP listener on a stdio-only server for no operator benefit.
- **Guard progress counters mid-run.** `guard` already bounds its own logs
  (`engine/guard_manager.py:1221-1223`); a live counter would only matter for runs long
  enough to scrape, and none are.
- **OpenTelemetry tracing.** Out of scope for this spec; the 61 `otel` hits in the corpus
  are all vendored dependency code, not a tracing setup.

---

## 8. Open questions this audit could NOT answer

Stated plainly rather than guessed:

1. **Who supervises `gitreins serve`?** Nothing on this host runs it, and no cron/systemd
   unit was found referencing it. Until something does, the server metrics have no process
   to scrape. This is prerequisite work, not an observability task.
2. **Is `gitreins serve` deployed anywhere as a service today?** Not verifiable from this
   host; `docs/judgment-viewer.md` documents it as an operator-launched local viewer.
3. **Does the fleet already have a pushgateway?** The brief names Prometheus with
   remote_write, a Loki stack and a blackbox-exporter, but not a pushgateway. If there is
   none, §5b's textfile fallback is the only path and requires node-exporter's
   `--collector.textfile.directory` to be pointed at a shared dir.
