# GitReins Dogfood Integration — 2026-09-15

**Verdict: 🟡 PROMISING-BUT-ROUGH** (fourth consecutive run; the rough edges moved —
the core loop is now solid and the remaining traps are environmental/release-process.)

**Promise under test:** "A developer or AI agent can `pip install gitreins`, run
`install`/`init` in any repo, manage criteria-based tasks, get per-criterion agentic
LLM judging, and commit through Tier-1 guards that block secrets."

**Run shape:** two independent real-use legs.
- **Leg A — fresh machine:** ephemeral bunker agent (las-bunker-04, bare Debian 13:
  python3.13, no pip/pipx/uv/sudo) + fresh clone from the documented GitHub origin.
- **Leg B — repo HEAD as a consumer:** clone of HEAD `5f90b99` into `/tmp`, own venv,
  full task → guard → judge → MCP-client → worktree workflow.

---

## Leg A — fresh install on a virgin machine (bunker)

**The documented path is impossible as written.** README says `pip install gitreins`;
the machine has no `pip`, no `pipx`, and `python3 -m venv` fails (no ensurepip,
`python3-venv` not installed, and no sudo to install it). A fresh user following the
README verbatim is dead at step 1.

**Working no-root path (5 seconds to a working CLI):**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh      # the only bootstrap needed
uv tool install gitreins
gitreins --version        # → gitreins 0.12.1 (correct — DF-015 drift fix verified)
```

**What the wheel does right (regression checks against 08-27 findings):**
- `install` → `init` in a scratch repo: clean output, config written, hook installed.
- Guard exit codes correct: FAIL→1 (missing pytest), PASS→0 (after `uv tool install pytest`).
- **Secrets block works**: `sk-…` + `ghp_…` in one staged file → pre-commit hook
  rejected the commit (git log stayed clean), reported **both** findings with
  file:line (DF-016 first-finding-only bug is fixed in the wheel).
- Hook pins the installing binary's absolute path (DF-011 fix — no PATH shadowing).
- gitleaks-absent degrade is a clear one-line warning + built-in scanner fallback.

**What the wheel is missing — it is a generation behind HEAD (~70 commits):**
- init announced `uv run pytest -x --tb=short` but persisted `pytest -x --tb=short`
  (the exact DF-GITREINS-POC-3 bug the foreman fixed on 09-11) and printed
  "Static analysis: enabled (no tools detected)" while writing `static_analysis: true`.
- No `worktree` subcommand at all (WORKTREE-006 shipped 09-14, unreleased).
- Payload-completeness verification (DF-GITREINS-POC-2 fix) absent.
→ Filed as **DF-GITREINS-POC-7**. No release pipeline exists (DF-010, open since 08-14).

Also exercised: `gitreins worktree dogfood --skip-judge` on the wheel clone → clean
"invalid choice" error (expected; wheel predates the feature).

Agent destroyed after the leg (contract kept). **Install leg deviation, declared:**
las-bunker-03 was down (ssh connect timeout to 100.69.3.13), so the ephemeral agent
ran on las-bunker-04 — same rootless-Docker bunker pattern, fresh state.

## Leg B — real consumer workflow at HEAD

Setup: `git clone <local HEAD> /tmp/dogfood-gitreins-scratch && uv venv .venv &&
uv pip install -e . pytest pytest-xdist` — 2 s install, `gitreins --version` → 0.12.1,
`--help` now lists `worktree` and `serve` (HEAD-only).

**The documented happy path works… mostly:**

```bash
gitreins task create dogfood-0915 "…" "tests/test_version.py passes under .venv/bin/python -m pytest" "gitreins guard exits 0 on the repo"
gitreins task start dogfood-0915
.venv/bin/python -m pytest tests/test_version.py -x -q     # 2 passed in 0.65s
gitreins task complete dogfood-0915                         # ← FAILED
```

`task complete` output: **tier1 FAIL** (`✗ tests:` …400 chars of pytest banner…),
**tier2 PASS** with excellent per-criterion evidence (the judge re-ran both criteria
itself and cited real command output — 32 s, deepseek-v4-flash), **Overall: FAIL**,
exit 1. A green task judged PASS by the agentic evaluator, failed by the harness.

**Root cause (reproduced independently, `pytest -n 4 --maxfail=1`: 1 failed, 824
passed, 12 skipped):**

1. `init` edited `.gitreins/config.yaml` (dirty file) → the tier1 tests step inside
   `task complete` ran the **full 1461-item suite**, not diff mode.
2. The suite ran under the consumer venv where **`pylsp` is not installed**.
3. `tests/test_lsp.py::TestLspJudgeIntegration::test_lsp_roundtrip_format_parse`
   **FAILS (not skips)** without pylsp — while the runtime itself degrades gracefully
   (`WARNING: LSP tool 'pylsp' not found on PATH — skipping`). CI never sees this
   because its matrix installs dev extras.
4. The failure evidence is truncated to 500 head-chars of pytest banner — the failing
   test name (which appears at the END of pytest output) is never shown. Filed as
   **DF-GITREINS-POC-8** (same lesson as DF-018, different code path).

→ Filed as **DF-GITREINS-POC-6**. Workarounds until fixed: `pip install
python-lsp-server` in any env that will run `task complete`, or use a clean-config
tree (diff mode), or `gitreins task complete --skip-tier2` when you only want Tier 1
explicitly (that flag is the POC-4 credential-aware completion fix — shipped).

**Everything else at HEAD verified working:**
- init consistency: announced = persisted (`uv run pytest -x --tb=short`), static
  analysis stays off unless asked (POC-3 fix confirmed at HEAD).
- MCP server: line-delimited JSON-RPC 2.0 per `docs/mcp-api.md` — `initialize` →
  `tools/list` (12 tools, names match docs exactly) → `guard.run` (returned full
  structured guard results incl. per-guard pass/output) → `task.create` /
  `task.start` / `task.list` (full lifecycle). One client-side lesson: the schema
  param is **`id`**, not `task_id`; wrong kwargs produce a loud `-32000` TypeError.
- `worktree fresh --cmd "…pytest tests/test_version.py -q"` → clean tree, exit 0,
  1.1 s, JSON run record.
- `worktree repro -k 3 --concurrency 2` → 3/3 pass, JSON record matches the
  documented shape field-for field (`passes`, `pass_rate`, `head`, per-run index…).
- `worktree dogfood --skip-judge` at HEAD → 3/4 steps passed, judge cleanly skipped,
  exit 0 (the throwaway tree has a clean config → diff mode → does NOT hit POC-6).

---

## Judgement

| Question | Answer |
|---|---|
| Does it work? | Core loop yes — guards, secrets blocking, MCP, judge, worktree fleet all verified on real data. `task complete` in a fresh consumer env is trapped by the pylsp test (POC-6). |
| Is it useful? | Yes — this is the harness gating this very repo's foreman fleet; the secrets block demonstrably stops real leaks. |
| Is it usable? | Head-line friction: the README install path fails on modern minimal systems (no pip/venv); wheel users get 18-day-old behavior. |
| Is it trustworthy? | Yes on the security path (both findings reported, hook pinned, exit codes honest). Weaker on diagnostics (500-char evidence). |

**Time-to-first-success:** Leg A (fresh machine, uv path): ~5 min incl. clone.
Leg B (HEAD consumer): ~10 min to a green guard; full judged loop ~35 min because of
the POC-6 diagnosis (which is itself the run's most valuable output).

**Friction count this run:** 5 (README install path impossible; pylsp test trap;
evidence truncation; wheel staleness ×2 behaviors; MCP `id` param naming).

**Prior-run regression checks:** DF-015 ✓fixed, DF-011 ✓fixed, DF-016 ✓fixed,
DF-017 (uv/pytest import) not re-hit at HEAD, POC-1/2/3 fixes present at HEAD but
absent from the wheel (POC-7), DF-018 lesson still open in a new path (POC-8),
DF-010 (release pipeline) still open.

*Companion records: board rows DF-GITREINS-POC-6/7/8,
`docs/dogfood/diagnostics.md` (09-15 section), `skills/gitreins-usage/SKILL.md` v1.1.0.*
