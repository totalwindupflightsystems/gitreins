# Dogfood Integration Report — 2026-09-20 — MCP stdio server as a real client (+ serve)

**Run:** gitreins-poc dogfood lane, HEAD `fbbf32f`, v0.14.0 (PyPI latest = HEAD: first run in
six where the wheel is in sync with the repo).
**Angle:** every prior run (08-03 → 09-16b) exercised the CLI/guard/judge/PyPI surfaces. This
run took the two untouched surfaces: the **MCP stdio server** (12 tools, the surface AI agents
actually use) driven by a raw JSON-RPC client — no MCP SDK — and the **`gitreins serve`**
judgment browser driven over HTTP.

## Promise under test

*"An AI agent can connect `gitreins mcp-server`, manage criteria-based tasks, dispatch
background LLM evaluations, poll them, and commit through the harness — and everything it
produces is browsable later via `gitreins serve`."*

**Verdict: 🟡 PROMISING-BUT-ROUGH.** The 12-tool surface works end-to-end for a real agent
session — task lifecycle, commit gate, background judge with genuine evidence, disk-resume
across server restarts, correct protocol negotiation. Two real gaps: MCP-judge verdicts are
never persisted to the browsable history (DF-GITREINS-POC-23), and `judge.status` gives a
client no terminating field to poll on (DF-GITREINS-POC-24).

## The working client (the actual integration)

A real consumer does not need the `mcp` package. The whole client is line-delimited JSON-RPC
over stdin/stdout (working example at its original path during the run: `/tmp/df-mcp-client.py`;
condensed below):

```python
import json, subprocess, select, time

class McpClient:
    def __init__(self, workdir):
        self.p = subprocess.Popen(
            ["gitreins", "mcp-server"], cwd=workdir,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1)
        self.next_id = 1

    def request(self, method, params=None, timeout=60):
        rid = self.next_id; self.next_id += 1
        req = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None: req["params"] = params
        self.p.stdin.write(json.dumps(req) + "\n"); self.p.stdin.flush()
        deadline = time.time() + timeout
        while True:
            line = self._readline(deadline)          # select() + readline, see pitfalls
            resp = json.loads(line)
            if resp.get("id") == rid:
                return resp                          # ignore unmatched ids

    def call_tool(self, name, args, timeout=90):
        r = self.request("tools/call", {"name": name, "arguments": args}, timeout)
        if "error" in r: return {"JSONRPC_ERROR": r["error"]}
        return json.loads(r["result"]["content"][0]["text"])
```

Session shape that worked, against a scratch consumer repo (`gitreins install && gitreins init`,
then a 6-test `converter.py` module as the "real work"):

1. `initialize` (protocolVersion `2024-11-05`) → negotiated `2024-11-05`, serverInfo
   `gitreins 0.14.0` (the POC-20 fix holds). `notifications/initialized` next.
2. `tools/list` → exactly 12 tools, names + schemas matching `docs/mcp-api.md` (checked
   tool-by-tool against the doc's section list).
3. `task.create` → `task.start` (status flips to `in_progress`).
4. `commit` tool **correctly refuses** while a task is in_progress, with the documented
   error string naming the task and the rule (README/MCP-docs promise verified live).
5. `task.get`/`task.delete` on a missing id → clean `{"error": "Task not found: ..."}`
   results, not crashes.
6. Do the work, `git add`, then `task.complete` → task complete +
   `{"job_id": "job-…", "status": "running", "note": "evaluation running in background —
   poll judge.status"}`.
7. Poll `judge.status` from a SECOND server instance → disk job store picked the job up,
   `pid` of the dead dispatcher detected, no false resume confusion, result delivered once
   terminal. This is DF-006 working as designed across server restarts — genuinely nice.
8. `commit` tool again → `{"committed": true}`, Tier 1 PASS inside the tool output, and
   `git show --name-only HEAD` lists **all three files** (README, module, tests).

### The judge is real

The verdict for the run (job `job-326f9cbf53f340ec8f3ed776c5850883`): `verdict: COMPLETE`,
`passed: true`, all three criteria PASS with concrete evidence, e.g.:

> `converter.py:5 returns "".join(ch for ch in s if ch.isdigit()). Runtime check:
> to_digits('a1b2c3')=='123', to_digits('abc')==''. pytest test_converter.py::test_digits_basic
> PASSED (6 passed in 0.01s, exit_code 0).`

The judge executed the suite itself and cited per-criterion runtime checks. Tier 2 on
deepseek-v4-flash took ~7 minutes wall clock (dispatch 03:34 → terminal 03:41, 21:42Z vs
21:34Z). Summary: "All three criteria verified by code inspection and a fresh passing pytest
run (6 passed, exit_code 0)."

### Finding DF-GITREINS-POC-23 (P1): the judge's verdict is not persisted anywhere browsable

The job record lives only in `~/.local/share/gitreins/jobs/job-<id>.json`
(`engine/job_store.py`). `gitreins serve`, `gitreins report`, and the static judgment page all
read `<workdir>/.gitreins/history/` (`gitreins/serve.py:_verdict_dir`) — populated only by
`VerdictPersister`, which the MCP job path never calls (grep: only `gitreins/cli.py` and
`engine/worktree_manager.py` use it). Verified: after the judged run, the scratch repo's
`.gitreins/` has no `history/` dir at all. An agent-driven team gets PASS in the tool response
and an empty browser. Full evidence on the board row.

### Finding DF-GITREINS-POC-24 (P2): polling `judge.status` has no termination signal

The payload never carries a `running` boolean — `{"status": "running"}` is a poll-phase
value, the terminal value is `{"status": "complete"}`. A client implementing the natural
"poll until running == false" pattern loops forever (reproduced live; cost this dogfood run
two tool-timeouts before diagnosis). `docs/mcp-api.md` documents the three status strings
correctly but shows no worked poll loop. Fix: add `running: bool` to the record (additive),
or a worked example in the docs.

## `gitreins serve` — the second untouched surface, fully working

`gitreins serve --port 8617 --project gitreins-poc` against the real repo:

- `/api/stats` → 167 verdicts, 119 passed / 48 failed, pass_rate 71%, aggregate judge usage
  (103.2M tokens in / 679k out across 116 judgements on deepseek-v4-flash), `prices_configured:
  false` honestly reported.
- `/api/verdicts` → 167 metadata rows; `/api/verdicts/2026-09-19/973d7dc0` → full record with
  criteria + evidence. `/api/qa` → the QA run ledger (dogfood/repro rows incl. this fleet's
  earlier runs). `/api/ticks` → scheduler tick ledger with per-tick commits/cost.
- SPA root serves the dark hash-route viewer; fetches all five endpoints via `Promise.all`.
- Error paths: unknown evidence name → 404; `../`-style date traversal → 404 (regex-gated at
  `load_verdict`); unknown API path → 404. Binds 127.0.0.1 by default and warns loudly if you
  override the host.

The browser is the strongest trust artifact this project ships — live counts, per-judgment
telemetry, honest pass rates including the failures. Which is exactly why POC-23 matters:
MCP-driven verdicts never reach it.

## Install leg (fresh machine, las-bunker-02 agent, by hand)

`bunker-qa.sh launch` failed three ways in a row, each reported as success (harness finding
DF-GITREINS-POC-25 — full mechanism on the board row; NOT a gitreins defect). The leg was
completed by hand over direct ssh on the already-synced fresh agent, exactly per the skill:

- `python3 -m venv ~/venv && ~/venv/bin/pip install gitreins` → **16 s** to `gitreins 0.14.0`
  (PyPI latest == HEAD — the DF-010/POC-7 release-lag class is closed this run).
- Documented smoke (`install` → `init` → `guard`) on a scratch repo: install and init clean,
  guard FAILs with `✗ tests — pytest: not found` in the fresh venv — the known fresh-venv
  gap (skill pitfall 18), still undocumented in README quickstart. gitleaks-absent fallback
  scanner engaged with a clear warning line.
- MCP handshake on the wheel: identical negotiated initialize response (0.14.0, 2024-11-05).

## Value judgment (brutal, evidence-based)

1. **Does it work?** Yes on both surfaces, end-to-end, with one P1 persistence gap (POC-23).
2. **Is it useful?** The MCP surface is the right product for its stated user (AI agents):
   one process, zero SDK, disk-backed async jobs surviving restarts, a commit gate that
   actually refused me. `serve` turns the run history into the project's best sales pitch.
3. **Is it usable?** Time-to-first-success ~20 min for a from-scratch raw client (schema
   reading included); friction count 6, of which 3 are real findings (POC-23, POC-24,
   fresh-venv pytest — already known). `docs/mcp-api.md` is the best doc in the repo; it
   needs one worked poll example.
4. **Is it trustworthy?** Guard-in-tool-output honest, judge evidence concrete, `serve`
   404s correct, no state corruption after SIGKILL of clients mid-poll; the job store
   picked up every orphaned job exactly once.

**Would I use it again:** yes — wiring the MCP server into an agent session is ~20 lines and
the commit gate + async judge are real workflow value. But an agent-team adopting it today
would lose their audit trail (POC-23) and lose 15 min to the poll loop (POC-24).
