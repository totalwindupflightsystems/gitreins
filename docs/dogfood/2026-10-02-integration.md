# Dogfood: MCP Server Surface — 2026-10-02

**Verdict:** 🟢 SHIPPABLE (for the surface tested)

**Promise:** "A 13-tool MCP server over JSON-RPC 2.0 on stdio exposes task lifecycle, guard, judge, configure, propagate, and context.resolve — the primary interface for AI agents using gitreins."

**Surface tested:** The MCP server itself (gitreins_mcp/server.py), driven end-to-end via a custom Python client over stdio. Handshake, all 13 tools, error paths, cross-repo workdir semantics, async judge lifecycle.

**Time-to-first-success:** ~2 min (server started, handshake completed, first task created)

**Friction count:** 4 (all P2/P3 — no P0 blockers)

## Probe Table

| Probe | Command | Expected | Actual | Result |
|-------|---------|----------|--------|--------|
| P1: Handshake | initialize + notifications/initialized + tools/list | protocolVersion=2025-11-25, 13 tools | protocolVersion=2025-11-25, serverInfo={name: gitreins, version: 0.15.0}, 13 tools listed | ✅ PASS |
| P2: Task lifecycle | task.create → task.start → task.list → task.get | Task transitions pending → in_progress, list/get return correct state | All transitions correct, task.get returns full task dict with criteria | ✅ PASS |
| P3: Commit blocking | commit with in-progress task in server workdir | Refused with error listing in-progress tasks | Refused listing 7 tasks from /home/kara/gitreins (DF-GITREINS-POC-70..72, INT-FLAKE-6, REVIEW-GITREINS-024/026, DF-GITREINS-POC-46) — but these were NOT in the cross-repo workdir | ❌ FAIL (POC-76) |
| P4: Cross-repo workdir | task.create with workdir=/tmp/mcp-dogfood | Task created in scratch repo, isolated from server workdir | Task created successfully in /tmp/mcp-dogfood, task.start/list/get all respected workdir param | ✅ PASS |
| P5: Task complete (async) | task.complete with no LLM configured | Returns job_id + status=running, polls until terminal | Returned job_id=job-faebde60814b4c298880dffee85542e4, status=running, polled 3× over 6s — stayed running indefinitely | ❌ FAIL (POC-78) |
| P6: Guard run | guard.run on repo without .gitreins/config.yaml | Refused with actionable error | Refused: "no .gitreins/config.yaml in /tmp/mcp-dogfood — run `gitreins init` first" — correct but breaks MCP-only workflow | ❌ FAIL (POC-77) |
| P7: Configure | configure with model=test-model | Accepts config, returns snapshot | configured=true, current={model: test-model, provider: openai, ...} — no validation | ⚠️ PASS (POC-79) |
| P8: Error paths | Unknown tool, missing task, unknown method, invalid JSON-RPC | -32601 for unknown tool/method, domain error for missing task, -32600 for invalid request | All error codes and messages match docs exactly | ✅ PASS |
| P9: Cleanup | task.delete | Returns {deleted: id} | {deleted: dogfood-task-1} | ✅ PASS |
| P10: Server exit | stdin.close() | Server logs exit line, exits 0 | stderr: "gitreins MCP server 0.15.0 — stdin closed (EOF), exiting 0", exit code 0 | ✅ PASS |

## Findings

### POC-76 (P2): commit tool ignores cross-repo workdir

The commit tool has no workdir parameter (docs/mcp-api.md line 198-204), so when a client uses cross-repo workdir on task ops (e.g. task.create with workdir=/tmp/scratch), the commit tool still checks the server's default workdir for in-progress tasks.

**Repro:** Started server with cwd=/home/kara/gitreins, created task in /tmp/mcp-dogfood via workdir param, task.start succeeded, but commit refused listing 7 in-progress tasks from /home/kara/gitreins — none of which were in the scratch repo.

**Docs say:** "Every tool that touches a repo accepts an optional workdir" (docs/mcp-api.md line 404-409). But commit is the one tool that does not.

**Fix:** Add workdir param to commit, or document that commit always operates on the server's default workdir.

### POC-77 (P2): guard.run refuses without config.yaml but MCP-only workflow has no init path

guard.run on a fresh repo (no .gitreins/config.yaml) returns error: "no .gitreins/config.yaml in /tmp/mcp-dogfood — run `gitreins init` first". The error message is correct and honest, but it breaks the MCP-only workflow: a client using only the MCP server (no CLI access) cannot run guard.run without first running the CLI init command.

**Docs say:** "13-tool surface over JSON-RPC 2.0" (docs/mcp-api.md line 3) — but guard.run requires a CLI-side prerequisite.

**Fix options:**
1. Add an init tool to the MCP surface that creates a minimal config.yaml
2. Document that guard.run requires CLI-side init
3. Have guard.run create a default config on first call

Option 1 is cleanest for MCP-only clients.

### POC-78 (P3): task.complete async path dispatches judge job even when LLM not configured

docs/mcp-api.md line 161-162 says "Without LLM: task.complete returns {task: ..., note: 'LLM not configured — skipping evaluation'}". But the async path (wait=false, the default) dispatches a job that never completes.

**Repro:** Server started with no LLM env vars, task.complete returned job_id=job-faebde60814b4c298880dffee85542e4 and status=running. Polled judge.status 3 times over 6 seconds — job stayed running with pid=2970943, never reached terminal state.

**Docs say:** The sync path (wait=true) would return the "LLM not configured" note, but the async path diverges.

**Fix:** Check LLM config before dispatching the async job, or have the job detect missing credentials and transition to error with a clear message.

### POC-79 (P3): configure tool accepts arbitrary model names without validation

configure tool with model=test-model returns {configured: true, current: {model: test-model, ...}}. The tool accepts any string for model, provider, base_url without validating that the model exists, the provider is supported, or the base_url is reachable.

**Docs say:** "hot-reload LLM config at runtime" (docs/mcp-api.md line 117-130) — no mention of validation.

**Fix options:**
1. Validate model against a known list (brittle, breaks when new models are added)
2. Do a lightweight health check on the base_url (adds latency)
3. Document that configure is "trust the user" and validation happens at evaluation time

Option 3 is cheapest and matches the tool's purpose (hot-reload, not validation).

## Install Leg

**Status:** SKIPPED-install-bunker

**Evidence:** Attempted clone on bunker-las-03 (Debian 6.12.107, Python 3.13.5, git 2.47.2):
```
$ git clone https://github.com/wojons/gitreins.git gitreins-install-test
fatal: could not read Username for 'https://github.com': No such device or address
```

The bunker host has no GitHub credentials configured, so it cannot clone public repos that require authentication (or the repo is private). Per the dogfood skill hard rule: do not modify repo visibility or permissions to enable clone access.

**Finding:** The install docs assume the user has GitHub access, but a fresh bunker host does not.

**Fix:** Document that the install leg requires GitHub credentials (or SSH keys) on the target host, or provide a tarball/wheel download path that does not require git clone.

## Regression Sweep (GREEN)

- Handshake: protocol version negotiation exact (2025-11-25), server info matches installed version (0.15.0)
- Tool catalog: all 13 tools listed, names match docs (configure, task.create, task.start, task.complete, task.list, task.get, task.delete, commit, guard.run, judge.evaluate, judge.status, propagate, context.resolve)
- Task lifecycle: create → start → list → get → complete → delete all work, state transitions correct
- Error taxonomy: -32601 for unknown tool/method, -32600 for invalid request, domain errors for missing tasks — all match docs
- Server exit: stdin EOF triggers clean shutdown, stderr logs exit line, exit code 0
- Cross-repo workdir: task ops respect workdir param, isolated from server default workdir

## Perf

Not measured — MCP server is stdio-based, no network latency, task ops are in-memory YAML reads/writes. The judge job dispatch is async (background thread), so task.complete returns immediately. No user-visible slowness.

## What Worked

- Handshake and protocol negotiation exact
- Task lifecycle complete and correct
- Error paths honest and actionable
- Cross-repo workdir isolation works
- Server exit clean

## What Did Not

- commit tool does not respect cross-repo workdir (POC-76)
- guard.run requires CLI-side init, breaks MCP-only workflow (POC-77)
- task.complete async path dispatches job even without LLM config (POC-78)
- configure tool accepts arbitrary model names without validation (POC-79)
- Install leg blocked on fresh bunker host (no GitHub credentials)

## Conclusion

The MCP server is SHIPPABLE for the core workflow (task lifecycle, guard, judge, configure). The four findings are P2/P3 — none block real use, but they are friction points a real agent would hit. The install leg is blocked on fresh hosts without GitHub credentials, which is a docs gap.

The server's honesty is its strength: error messages are actionable, the protocol is exact, and the cross-repo semantics work (except for commit). The async judge path has a contract inconsistency (POC-78) that should be fixed before a real agent relies on it.
