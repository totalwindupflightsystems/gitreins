# GitReins MCP server — real-consumer integration report (dogfood run22, 2026-10-15)

What a real integrator does: point an MCP client at `python -m gitreins_mcp.server`
(the `gitreins mcp-server` console entry is equivalent), talk line-delimited
JSON-RPC 2.0 over stdio, and drive the 15-tool surface to manage tasks and gate
commits. This run drove it with a ~100-line stdio client (no MCP SDK) from a
scratch demo repo; full transcripts in
`docs/dogfood/evidence/mcp-surface-2026-10-15/`.

## The working recipe (what actually worked first-try after two gotchas)

1. Launch: `python -m gitreins_mcp.server <REPO>` — workdir is **positional
   argv[1]** and defaults to CWD. Gotcha 1: there is no `--workdir` flag; on
   HEAD that string is treated as a path and crashes server startup (POC-91),
   on released 0.16.0 it silently mis-scopes state.
2. Handshake: `initialize` (protocolVersion 2025-11-25) → read the answer →
   `notifications/initialized` (fire-and-forget: never wait for a reply —
   you will hang; gotcha 2) → `tools/list` (15 tools).
3. Real workflow that round-trips cleanly:
   - `repo.init` writes `.gitreins/config.yaml` (or run `gitreins init` first —
     note `guard.run` REFUSES to run without a config, fail-closed by design).
   - `task.create` → `task.start` → `task.list`/`task.get` → `commit` (blocked
     while a task is in_progress, with a human message) → `task.complete`
     (dispatches an async judge job when an LLM credential resolves; returns a
     skip note without one) → `judge.status` (poll; jobs survive process
     restarts) → `task.delete`.
   - `guard.run` before every commit; `commit` runs Tier 1 itself and returns
     the guard summary in its output (guard ≈0.3-0.7s clean, ~4s incl. git).
4. EOF on stdin = clean exit 0; the startup/exit acknowledgement lives on
   stderr, stdout is protocol-pure. Read ONE line per request.

## Errors hit and what they mean

| Symptom | Meaning | Fix |
|---|---|---|
| server dies on startup, `ValueError: scan root is not a directory: <repo>--workdir` | you passed `--workdir` (POC-91) | pass the repo positionally |
| client hangs forever | you waited on `notifications/initialized` | notifications never answer |
| `-32601 Unknown method/tool` | unknown method OR unknown tool (correct JSON-RPC shape) | check tools/list names |
| `-32000 ..._task_create() missing 2 required positional arguments` | missing required args (POC-92: raw Python leak) | send title+criteria |
| `task.list` on a typo'd workdir → `{"tasks": []}` | silent (POC-93) | validate the path yourself |
| guard.run → `"no .gitreins/config.yaml ... false green"` | fail-closed refusal, correct | run `gitreins init` |
| judge.status → `"task ... no longer exists — job could not be resumed"` | task deleted while its eval job ran; job fail-fasts (correct) | don't delete mid-eval |

## Verdict for integrators

The wire contract is unusually honest (notifications, protocol negotiation,
fail-closed guard, job registry across restarts all behave exactly as
docs/mcp-api.md says). The three filed rough edges are all at the entry-point /
argument-validation boundary, not in the protocol core.
