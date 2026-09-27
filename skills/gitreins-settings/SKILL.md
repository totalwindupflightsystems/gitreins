---
name: gitreins-settings
description: >-
  The complete, verified settings reference for the GitReins quality harness —
  every key under guards:, evaluator:, history:, resolution:, defaults:,
  qa_ledger:, worktree_fleet: and commit_audit:, every GITREINS_* environment
  variable, the precedence chain between them, what `gitreins init` actually
  writes, and the failure mode of each knob. Load this whenever you are editing
  .gitreins/config.yaml, wiring GitReins in CI or a container, debugging a judge
  run that exhausts a cap or dies with HTTP 400, or trying to read back the
  EFFECTIVE configuration of a repo instead of guessing from the file.
version: 1.0.0
category: software-development
---

# GitReins Settings & Configuration

> **Reading the ids.** References like `DF-010`, `GR-GAP-055`, `POC-12` or `INT-CI-8` are rows on this project's own
> internal work board. They are kept so a claim can be traced to the incident that produced it;
> nothing in this skill requires knowing what they contain.

Every value below was read out of the code at **v0.15.0 (HEAD `abc7f7c`)** — the
`path:line` next to each claim is the proving reference. Do not trust a value in
this file over the code if they disagree; re-grep and fix the file.

Companion skills: `gitreins-usage` (daily task/judge/guard workflow),
`gitreins-workflow` (install/consumer paths). This file is the *settings* layer
only.

---

## 1. Precedence — the one chain that matters

Nothing is "merged": caps and toggles are resolved by three different readers,
and each stops at the first layer that supplies the value.

```
Caps (evaluator):    code defaults → defaults: block → guards.eval_cap (legacy, replaces all)
                     → evaluator: individual keys → GITREINS_MAX_* env  (last wins)
Other settings:      code defaults → defaults: block → (explicit constructor arg)
Guards:              guards: block, raw, with code defaults per key
Resolution:          resolution: block, top-level only (never under defaults:)
qa_ledger:           GITREINS_QA_LEDGER env → qa_ledger.path → <repo>/.gitreins/qa-ledger.jsonl
LLM client:          constructor arg → GITREINS_LLM_* env → code default
```

- `engine/config.py:7-10` documents the base chain: hardcoded defaults →
  `.gitreins/config.yaml` → explicit constructor parameters.
- `engine/eval_cap.py:482-502` — the `GITREINS_MAX_*` env overrides are the
  **highest** priority for caps and are applied last, after `evaluator:` keys.
- `engine/eval_cap.py:456-462` — `evaluator.max_iterations` is applied *after*
  the `defaults:` overlay, so when both blocks set it, **`evaluator:` wins**.
  Proven: this repo's `.gitreins/config.yaml` has `defaults.model` only, and
  `eval_cap.max_iterations` reads 200 (from `evaluator:`) while
  `load_defaults().max_iterations` reads 100.
- An invalid value does **not** fall back silently: `load_defaults()` re-raises
  `ValueError` from the coercion helpers (`engine/config.py:507-514`). An
  *unreadable* config (bad YAML, wrong encoding) logs a warning and degrades to
  built-in defaults instead (`engine/config.py:495-506`).

### Reading back the EFFECTIVE config (do this, don't guess)

There is **no `gitreins config` subcommand** — the CLI verbs are `install, init,
task, worktree, guard, judge, commit, commit-audit, resolve, preflight,
mcp-server, security-scan, setup-tools, qa, report, serve` (verified with
`gitreins --help`). Read it through the engine, from a checkout or any
environment where `engine` is importable:

```bash
# Caps the evaluator will actually use (defaults + evaluator: + env, resolved):
python -c "from engine.config import load_raw_config; from engine.eval_cap import eval_cap_from_config; \
print(eval_cap_from_config(load_raw_config('.')))"

# The defaults dataclass as overlaid by the defaults: block:
python -c "from engine.config import load_defaults; print(load_defaults('.'))"

# What the raw file actually says (no resolution):
python -c "from engine.config import load_raw_config; import yaml; print(yaml.safe_dump(load_raw_config('.')))"
```

The resolved cap carries a `source` field naming where it came from
(`engine/eval_cap.py:445`, `:487-502`) — e.g. `.gitreins/config.yaml +
GITREINS_MAX_OUTPUT_TOKENS`. Print it; it answers "why is my cap this number".

---

## 2. `defaults:` — global overrides

Overlaid onto `GitReinsDefaults` by `GitReinsDefaults.overlay()`
(`engine/config.py:132-305`). Only keys present in the file replace the default.

| Key | Default | What it changes / bad-value failure mode |
|---|---|---|
| `model` | `deepseek-v4-flash` (`engine/config.py:42`) | Judge model. Wrong id → every Tier 2 call 4xx/5xx and the verdict is an error, not a FAIL. |
| `llm_reasoning` | `disabled` (`:43`) | `enabled` adds DeepSeek `thinking` to the payload (`engine/llm.py:323-328`). Costs output tokens and wall time; burns the output cap faster. |
| `max_iterations` | `100.0`, `-1` = unlimited (`:46`) | Turn cap. Also `evaluator.max_iterations` — that one wins. Too low → `Cap exceeded:` verdicts with work half-graded. |
| `max_time` | `-1.0` = unlimited (`:47`) | Wall clock. Accepts `30s`/`5m`/`2h` (`_coerce_seconds`, `:647-670`). An unparseable string coerces to `-1.0` — i.e. **silently unlimited**, not an error. |
| `max_input_tokens` | `10_000_000` (`:48`) | Cumulative input budget; accepts `10M`/`200k` (`_coerce_tokens`, `:673-690`). Drives compaction (`compaction_threshold` is a share of it). Exhausting it ends the run. |
| `max_output_tokens` | `131_072` (`:49`) | Cumulative **session** output budget — not the per-request `max_tokens`. `gitreins install` writes `"1M"` here; see §9. |
| `max_tokens_per_call` | `16384` (`:50`) | The value actually sent as `max_tokens` per request (`engine/evaluator.py:1159`, `:1225`). **Raise this past the provider's per-response cap and every judge call is HTTP 400.** |
| `tool_call_weight` | `0.1` (`:51`) | Fraction of an iteration each tool call costs. Raise → caps exhaust sooner. |
| `compaction_threshold` | `0.90` (`:54`) | Compact once cumulative input since the last compaction exceeds this share of `max_input_tokens`. Lower = more compactions, more cost/latency, more lost detail. |
| `code_context_budget` | `0.70` (`:55`) | Share of the input budget the pre-loaded code context may take. Too high starves the judge's own reasoning. |
| `file_scope` | `changed` (`:56`) | `changed` (changed files + tests) or `full` (whole codebase). `full` on a large repo = prompt/context explosion. |
| `fast_track` | `auto` (`:57`) | `on`/`off`/`auto`; `auto` turns it ON at ≥20 packages (`engine/evaluator.py:498-506`). Skips full call-graph expansion. |
| `max_file_bytes` | `131_072` (`:58-60`) | Caps `read_file` results to stop context explosion. Also read by the commit auditor (`engine/commit_audit.py`). |
| `pass_on_error` | `False` (`:61`) | `True` = skip Tier 2 when the LLM is unavailable (advisory-only mode). Turns a hard failure into a pass; only correct where the harness is advisory. |
| `hook_timeout` | `300` (`:64`) | Overall pre-commit hook budget. When it expires, remaining guards are **skipped and the commit proceeds (fail-open)** (`engine/guard_manager.py:1456-1462`). |
| `max_concurrent_worktrees` | `2` (`:67`) | Fleet/repro concurrency. Must be a positive int (`_coerce_positive_int`, `:449-461`): `true`, `0`, or `2.5` raise `ValueError`. |
| `worktree_venv_source` | `.venv` (`:68`) | Shared venv copied from the canonical main checkout into each disposable tree. |
| `worktree_venv_name` | `.venv` (`:69`) | Destination name inside each tree. |
| `check_for_updates` | `True` (`:101`) | PyPI check on each run. |
| `update_check_ttl` | `24h` → `24.0` h (`:102`) | Re-check interval. Unparseable → `-1.0` (`_coerce_float`, `:635-644`) which makes every run re-check. |
| `history_enabled` / `history_path` / `history_storage` / `history_max_verdicts` | `True` / `.gitreins/history` / `git` / `1000` (`:105-108`) | Second shape for the same knobs as the `history:` block — **`history:` is the one `engine/persist.py` reads**; these `defaults.*` spellings are the dataclass mirror. |
| `security_scan.{enabled,model,min_confidence,cve_source}` | `False` / `antares-1b` / `0.7` / `nvd` (`:73-76`, `:185-198`) | Read from `defaults.security_scan` — but the **guard lane enable flag is `guards.security_scan.enabled`**; see §6. |
| `static_analysis_diagnostics` | (not a `defaults:` key) | Lives under `evaluator:`; see §4. |

### `defaults.commit_audit:` — commit review

| Key | Default | Notes / failure mode |
|---|---|---|
| `enabled` | `True` (`engine/config.py:79`) | Runs the commit-message audit lane. |
| `mode` | `warn` (`:80`) | `warn` \| `block` \| `suggest`. `block` rejects commits the judge considers mislabelled — a false positive stops work. |
| `strictness` | `standard` (`:81`) | `lenient` \| `standard` \| `strict`. |
| `max_iterations` | `3` (`:82`) | LLM exploration rounds; `0` = single call, no tools. |
| `suggest_message` | `True` (`:83`) | Offer a better message on rejection. |
| `review_mode` | `message` (`:86`) | `message` \| `review` \| `agent`. |
| `review_checks.{bugs,security,style,performance,anti_patterns}` | `True`,`True`,`False`,`False`,`True` (`:87-91`) | Each enabled check is another LLM pass: more latency, more cost, more false positives. |
| `review_severity` | `standard` (`:92`) | `critical-only` \| `standard` \| `all`. |
| `review_suggest_fix` | `True` (`:93`) | Emit a suggested patch. |
| `review_max_tokens` | `2048` (`:94`) | Per-review output budget. Above the provider cap → HTTP 400 (same clamp caveat as §9). |
| `review_score_threshold` | `8.0` (`:97`) | CVE-style score at which a finding counts. |
| `review_score_offset` | `1.0` (`:98`) | Score offset subtracted/budgeted per pass (`engine/pipeline.py:975-988`, `engine/commit_audit.py:482-500`). |

---

## 3. `evaluator:` — the cap block (this is where `defaults.max_iterations` loses)

Read by `eval_cap_from_config()` (`engine/eval_cap.py:412-504`). Grammar for the
string forms: `_parse_tokens` accepts `10M`/`200k`/`0.1M`; `_parse_time` accepts
`30s`/`5m`/`2h`.

| Key | Default | Notes |
|---|---|---|
| `max_iterations` | `100` | Numeric `≤ 0` becomes `-1` (unlimited) (`engine/eval_cap.py:456-462`). A string is parsed as a whole cap string. |
| `max_time` | `-1` (unlimited) | `"60m"` etc. |
| `max_input_tokens` | `10M` | |
| `max_output_tokens` | `131072` | Session budget (see §9). |
| `max_tokens_per_call` | `16384` | Per-request `max_tokens` — read directly from the `evaluator:` dict, not via the cap (`engine/evaluator.py:1159`). |
| `tool_call_weight` | `0.1` | |
| `compaction_threshold` | `0.90` | |
| `code_context_budget` | `0.70` | |
| `file_scope`, `fast_track` | `changed`, `auto` | |
| `max_file_bytes`, `pass_on_error` | `131072`, `False` | |
| `static_analysis_diagnostics` | `False` | Off by default (`gitreins/cli.py:1153`). When false the judge does not even advertise `read_static_analysis` (`engine/evaluator.py:1162`, `:1888-1889`), and calling it returns `{"error": "static_analysis_diagnostics is not enabled in .gitreins/config.yaml"}`. |
| `cap` (legacy) | — | `"100/30m/200k/50k"` — **replaces the whole cap object** (`engine/eval_cap.py:451-453`); individual keys then override per field. |

**Corrected stale claim:** an *empty* `evaluator:` block does **not** mean
unlimited. `eval_cap_from_config({})` returns `100.0 / -1.0 / 10000000 / 131072`
from the code defaults (verified by running it). To get unlimited you must set
`max_iterations: -1` (or `0`, which coerces to `-1`).

### Environment overrides for caps (highest priority)

`GITREINS_MAX_ITERATIONS`, `GITREINS_MAX_TIME`, `GITREINS_MAX_INPUT_TOKENS`,
`GITREINS_MAX_OUTPUT_TOKENS` (`engine/eval_cap.py:484-502`). They win over every
file. They are for CI/containers where editing `config.yaml` is impractical.
Note they mutate **cap** fields only — `GITREINS_MAX_OUTPUT_TOKENS` does *not*
touch `max_tokens_per_call`, so it cannot cause (or fix) an HTTP 400 by itself.

---

## 4. `guards:` — Tier 1

Read raw from the file by `GuardManager` (`engine/guard_manager.py:868-890`,
`:1205-1287`). Code-level defaults differ from what `init` writes.

| Key | Code default | What it changes / bad-value failure mode |
|---|---|---|
| `secrets` | `True` (`:1207`) | Gitleaks scan. The one guard that must stay on: it is a BLOCK, not a warning. |
| `lint` | `True` (`:1208`) | Language linter. For Python the lane runs `ruff check --force-exclude` **and** a `ruff format --check` (`engine/guard_manager.py:2005-2020`); fallback list `["ruff", "flake8"]`. With no linter on PATH the step SKIPs and emits the `GITREINS_SKIP:` marker. |
| `tests` | `True` (`:1209`) | Runs `test_command`. |
| `test_mode` | `full` (`:1234`) | `full` \| `diff`. In `diff` mode only packages with staged changes are tested; a clean index then skips the lane entirely. Per-run overrides: `--full`, `--staged-only`, `--scope {staged,working-tree}`. |
| `test_command` | `pytest -x --tb=short` (`:2194`) | Any shell string; chaining `&&` is supported (this repo chains a docs-drift check before pytest). **Pin the interpreter** (`.venv/bin/python -m pytest …`): a bare `pytest` resolves via PATH and on a host where another venv is first you get a *different* pytest and spurious `unrecognized arguments: -n`. Confirmed as a real blocker in this repo's committed config. |
| `test_timeout` | `180` (`:1257-1259`) | Seconds; coerced to int, so `"300s"`-style strings no longer crash `subprocess.run` (they used to: GR-GAP-028). Timeout = lane FAIL. |
| `hook_timeout` | `300` (`:1263-1265`) | Overall budget; on expiry remaining guards are skipped and the commit is allowed (fail-open, warning printed). |
| `test_on_clean` | `False` (`:1248`) | Run `test_command` even with an empty index. `False` + empty index = the tests lane is a SKIP with reason `no staged files`. |
| `allow_skips` | `False` (`:1253`) | When false, a run where a substantive lane (lint/tests/lsp) did no work is a **DEGRADED pass that exits 2**; `true` keeps exit 0. `init` writes `true`; the code default is fail-loud. Keep it `false` in CI so a gate that did no work can never read as a gate that passed. |
| `dead_code` | `False` (`:1210`) | Python AST dead-code check. Noisy on legacy trees. `gitreins guard --dead-code` overrides per run. |
| `skylos` | `False` (`:1211`) | Multi-language dead code / AI-mistake scan; needs the package installed or the lane skips. |
| `static_analysis` | `False` (`:1212`) | Type checkers. **Enabling it on a repo with pre-existing errors blocks every commit** — this repo keeps it off because of 2150 pre-existing `mypy --strict` errors. |
| `static_analysis_tools` | `{}` (`:1218`) | Per-language list, e.g. `{python: [mypy, pyright]}`. `static_analysis: true` with an empty tool list silently no-ops — always pair them. |
| `lsp` | `False` (`:1213`) | LSP diagnostics guard. Needs the server on PATH (`pylsp` etc.) or the lane reports an absent analyser as a named skip, not a clean pass. |
| `lsp_tools` | `["pylsp"]` (`:1219`) | Server list. |
| `lsp_timeouts.{init,per_file}` | unset → language-aware (clangd/C++ get 300s/120s) (`:1220-1224`) | Raise for large C++ trees. |
| `go.{build,lint,tests}` | `True` each (`:1466-1484`) | Only applied when the repo is detected as Go (real ecosystem marker, `engine/lang_detect.py`). |
| `security_scan.enabled` | `False` (`:1214-1216`) | **This** is the guard-lane enable flag. The scanner's own tuning (`model`, `min_confidence`, `cve_source`) is read from `defaults.security_scan` by the CLI (`gitreins/cli.py:3124`) and the CVE feed (`engine/cve_feed.py:156`). Two blocks for one feature — enabling only `defaults.security_scan.enabled` does not turn the guard on. |
| `eval_cap` (legacy) | — | Combined `"<iter>/<time>/<in>/<out>"` string; read as a cap source (`engine/eval_cap.py:451`). Prefer `evaluator:`. |

There is **no `--tier N` flag**: the guard *is* Tier 1; Tier 2 is `gitreins judge`.

---

## 5. `history:` — verdict persistence

Read by `engine/persist.py` (default dict at `:99`, pruning at `:893-894`).

| Key | Default | Failure mode |
|---|---|---|
| `enabled` | `True` | `false` = verdicts are not saved (`gitreins report` then has nothing to show). |
| `path` | `.gitreins/history` | Relative to the repo root. Moving it orphans existing history. |
| `storage` | `git` | `git` auto-commits verdicts to a dedicated branch; `filesystem` writes files only. Under `git`, a repo with a restrictive pre-commit hook or a dirty index can make verdict persistence fail noisily. |
| `max_verdicts` | `1000` | Oldest entries are pruned past this limit. |

Token telemetry lives separately: `.gitreins/usage.jsonl`, one line per step
(`ts`, `tokens_in`, `tokens_out`, `cache_read`, `cache_write`, `step`). Counters
are cumulative per context window and reset on compaction — sum deltas, never
read the last line as a total.

---

## 6. `resolution:` — the Jev resolution gate

A **top-level** block (never nested under `defaults:`), read by
`resolution_cfg()` and friends (`engine/config.py:287-301`, `:388-420`).
Default for every surface is OFF: the gate sends a bundle to OpenRouter (real
egress) and only a literal `true` opens a surface — absent, `false`, and
wrong-typed all read as OFF (`engine/config.py:400-405`).

| Key | Default | Failure mode / notes |
|---|---|---|
| `enabled.cli` | `false` (`:119`) | `gitreins resolve`. Needs an OpenRouter key: `GITREINS_OPENROUTER_KEY` → `OPENROUTER_API_KEY` → `MYTHOS_OPENROUTER_KEY` (`engine/resolution.py:160`). No key = ABSTAIN (fail closed), never a PASS. |
| `enabled.mcp` | `false` (`:120`) | `context_resolve` MCP tool. |
| `enabled.predispatch` | `false` (`:121`) | `gitreins preflight` — can **skip a dispatch**. Uncalibrated bands must not drive this. On failure it fails OPEN (dispatches, recording `abstain_reason`). |
| `enabled.judge_prescreen` | `false` (`:122`) | Judge pre-screen; can mark criteria resolved without full evaluation. |
| `model` | `typesafe/jev-1.13` (`:123`) | The scoring model. |
| `tokens_max` | `28000` (`:124`) | Bundle ceiling; accepts `"8k"`-style strings; non-positive falls back to the default (`engine/config.py:416-420`). The upstream model rejected ~30.2k+ inputs with HTTP 400 `max_tokens_exceeded` in observed runs, which is why the default sits at 28k. |
| `bands.resolved_at` | `0.85` (`:125`) | ≥ this = RESOLVED → skip-dispatch. |
| `bands.review_at` | `0.50` (`:126`) | ≥ this = REVIEW → dispatch with a note. Below = UNRESOLVED → dispatch. |
| `egress_exclude` | `()` (`:127`) | List of path patterns never sent off-host. A non-list degrades to empty (no crash) — which silently widens egress, so verify the type. |

A wrong-typed block (`resolution: true`) is kept verbatim and read as
all-surfaces-off (`engine/config.py:538-541`) — it never crashes config loading.

---

## 7. `qa_ledger:` — QA run records

`engine/qa_ledger.py:110-123`, path resolution `:83-107`.

| Key | Default | Notes |
|---|---|---|
| `enabled` | `True` (`:120`) | `false` = outcomes are not recorded. |
| `path` | `<repo>/.gitreins/qa-ledger.jsonl` (`:107`) | Relative paths resolve against the repo root; absolute paths used as-is. **`GITREINS_QA_LEDGER` overrides this key** — a file path, or a directory (existing, or ending with a separator) that then receives `qa-ledger.jsonl`. |
| `max_entries` | `1000` (`:63`, `:122`) | Newest N rows kept; rotation says so on stderr. A bad value (bool, `0`, garbage) silently reverts to 1000 (`:72-80`). |

---

## 8. `worktree_fleet:` — disposable trees and fleet concurrency

Read by `WorktreeManager`/`WorktreeDisposable`/`WorktreeFleet`
(`engine/worktree_manager.py:391-399`, `engine/worktree_disposable.py:430-434`,
`engine/worktree_fleet.py:184-188`). **There is no `worktree:` block.**

| Key | Default | Notes |
|---|---|---|
| `disk_ceiling_mb` | `4096` (`engine/config.py:70`) | `≤ 0` = unlimited (`_coerce_disk_ceiling`, `:464-472`). A bool raises `ValueError`. Unlimited on a shared disk is how a repro sweep fills the filesystem. |
| `venv.source` (legacy nested) | `.venv` | Legacy shape accepted alongside `defaults.worktree_venv_source` (`engine/config.py:433-445`). |
| `venv.name` (legacy nested) | `.venv` | Same. |

Concurrency is `defaults.max_concurrent_worktrees` (default `2`); `worktree
repro -k N --concurrency M` overrides per run, and an invalid `--concurrency`
raises `WorktreeError` (`engine/worktree_disposable.py:836-838`).

---

## 9. The knob that bites: output tokens and the provider clamp

Three different "max tokens" exist and they are not interchangeable:

1. `max_output_tokens` — the **session** budget metered across the run
   (`engine/eval_cap.py:184-188`).
2. `max_tokens_per_call` (default `16384`) — the value sent as `max_tokens` on
   each request (`engine/evaluator.py:1159`, `:1225`).
3. `review_max_tokens` (default `2048`) — commit-review output budget.

**`gitreins install` writes `max_output_tokens: "1M"`** in its default config
(`gitreins/cli.py:100`), while the code default is `131072` and the code comment
calls 128K a "safe floor below most provider caps" (`engine/config.py:49`).
Sending the *session* budget as a per-request `max_tokens` is exactly what
produced HTTP 400 on every judge call on DeepSeek; v0.10.1 fixed it by switching
the per-request value to `max_tokens_per_call` (`CHANGELOG.md:544-545`).

The per-provider clamp does **not** reliably save you:

- The clamp table is `{deepseek: 393216, openai: 1M, anthropic: 1M, openrouter: 1M}`
  (`engine/llm.py:279-284`).
- The lookup uses `self.provider`, and `self.provider` is only ever `"anthropic"`
  (anthropic-looking base URL) or `"openai"` (everything else)
  (`engine/llm.py:146-152`).
- Proof, run against HEAD with a DeepSeek model on an OpenAI-compatible base URL:
  `_clamp_max_tokens(1_000_000, "openai")` → `1000000`. The `deepseek: 393216`
  entry is unreachable unless a caller passes `provider="deepseek"` explicitly —
  and no in-tree caller does (all call sites are bare `LLMClient()`/`LLMClient(model=…)`).

So: **keep `max_tokens_per_call` ≤ the provider's real per-response cap**
(393216 for DeepSeek). If you raise it above the cap, every Tier 2 call fails and
the verdict is an error, not a FAIL.

---

## 10. Environment variables

Every `GITREINS_*` name that current source actually reads. Anything not in this
table is documentation-era or spec-era, not a knob.

| Variable | Read at | Effect |
|---|---|---|
| `GITREINS_LLM_API_KEY` | `engine/llm.py:120-121` | Primary LLM credential. Falls back in order to `NEURALWATT_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, `KIMI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY` (`:124-136`). The source of the key is recorded so a rejected key names its variable — never the value. |
| `GITREINS_LLM_BASE_URL` | `engine/llm.py:97-103` | API base URL (default `https://api.openai.com/v1`). |
| `GITREINS_LLM_MODEL` | `engine/llm.py:137` | Model id; wins over the code default. |
| `GITREINS_LLM_REASONING` | `engine/llm.py:144` | `disabled` (default) or `enabled` — DeepSeek thinking mode. |
| `GITREINS_ANTHROPIC_VERSION` | `engine/llm.py:158` | Anthropic API version header (default `2023-06-01`). |
| `GITREINS_MOCK_LLM_RESPONSE` | `engine/llm.py:186-189` | A JSON string that replaces the real call — for tests/subprocess work only. Never set it in a real run. |
| `GITREINS_MAX_ITERATIONS` / `MAX_TIME` / `MAX_INPUT_TOKENS` / `MAX_OUTPUT_TOKENS` | `engine/eval_cap.py:484-502` | Highest-priority cap overrides (§3). |
| `GITREINS_QA_LEDGER` | `engine/qa_ledger.py:61`, `:85-90` | Ledger file **or** directory; beats `qa_ledger.path`. |
| `GITREINS_OPENROUTER_KEY` | `engine/resolution.py:160` | Credential for the Jev resolution gate (fallbacks `OPENROUTER_API_KEY`, `MYTHOS_OPENROUTER_KEY`, all tried in turn). |
| `GITREINS_WORKER_BRIEF` | `engine/evidence.py:50` | Path to the worker brief attached to the evidence record. |
| `GITREINS_DRIVER_LOG` | `engine/evidence.py:51` | Path to the driver log; only the tail is read. |
| `GITREINS_JOB_DIR` | `engine/job_store.py:52` | Where async judge jobs live (default `~/.local/share/gitreins/jobs/`). |
| `GITREINS_CHILD_PYTHON` | `engine/worktree_disposable.py:156` | Interpreter for child steps in disposable trees. |
| `GITREINS_SERVE_VERBOSE` | `gitreins/serve.py:726` | Verbose logging for the local judgment browser. |
| `GITREINS_LOADGEN_ALLOW_SHARED_HOST` | `scripts/loadgen.py:53`, `:154` | Required (`1`/`true`/`yes`) for the load generator to run on a shared host — a deliberate guard, do not set it casually. |
| `GITREINS_TIER1` | `engine/pipeline.py:111` | Stamped by the pipeline into its **test step's child environment**; `tests/conftest.py` reads it to skip `live`-marked egress tests so a host-global smoke test can never grade a judge run. Not an operator knob, and deliberately not set by `guards`. |

Not environment variables, despite looking like them:
`GITREINS_SKIP:` is an **output marker** for skipped steps (`engine/pipeline.py:102`),
`GITREINS_GITIGNORE_ENTRIES` is a Python constant (`gitreins/cli.py:51`), and
`DEFAULT_GITREINS_CONFIG` (`gitreins/cli.py:91`) is the literal config text —
only its *name* looks like an env var.

**Documented but inert — verified:** `engine/llm.py:12` and `docs/mcp-api.md:127`
document `GITREINS_LLM_PROVIDER` as forcing `openai`/`anthropic`, and the MCP
`configure` tool sets it (`gitreins_mcp/server.py:445`). `LLMClient.__init__`
never reads it: with `GITREINS_LLM_PROVIDER=anthropic` exported and an
OpenAI base URL, `LLMClient().provider` is still `"openai"` (ran it). Only the
constructor argument `provider=` works. `tests/test_llm.py:64-69` passes
`provider=` explicitly, so the test does not catch this.

---

## 11. What `init` writes vs what `install` writes vs what the docs claim

Verified by running `gitreins init` in an empty scratch repo at HEAD:

- `gitreins install` (`gitreins/cli.py:402-409`, text at `:91-135`) writes
  `DEFAULT_GITREINS_CONFIG` if `config.yaml` is missing. That text includes a
  `defaults:` block with **`max_output_tokens: "1M"`** — the value that trips the
  provider cap story in §9 — plus `max_iterations: 100`, `max_input_tokens: 10M`,
  `guards.test_mode: full`, `guards.test_command: pytest -x --tb=short`,
  `allow_skips: true`, `worktree_fleet.disk_ceiling_mb: 4096`, `history.*`.
- `gitreins init` (`gitreins/cli.py:444-556`) is **different**: it writes no
  `defaults:` block at all. It writes a language-shaped `guards:` section
  (`gitreins/cli.py:947-1010`; Python gets `lint`/`tests`/`static_analysis:
  true` with `static_analysis_tools: {python: [mypy, pyright]}`), an `evaluator:`
  block with a **size-graduated** `max_iterations` (15 ≤3 packages, 25 ≤10,
  50 ≤25, else 100 — `:930-944`) plus `static_analysis_diagnostics: false`,
  `history`, `worktree_fleet.disk_ceiling_mb: 4096`, and the full `resolution:`
  block with every surface `false`. It is re-runnable and never overwrites a
  user value; `--reset` does.
- So a repo initialized with `init` runs on the **code** defaults for
  `max_output_tokens` (131072), while a repo initialized with `install`
  inherits `"1M"`. Do not assume they match — read the file.
- Claims to distrust in older notes and doc blocks: `max_output_tokens: 1M` as a
  *default*, `compaction_threshold: 0.70`, `code_context_budget: 0.30`,
  `max_time: 5m` as a default, and "an empty `evaluator:` block means unlimited".
  All four are wrong at HEAD (`engine/config.py:47-55`, and the §3 run).

---

## 12. Safe to change vs knobs that bite

| Safe (local effect, easy to undo) | Bites |
|---|---|
| `guards.{secrets,lint,tests}` toggles | `guards.allow_skips: true` in CI — a gate that did no work reads as exit 0 |
| `guards.test_command`, `test_mode`, `test_timeout` | `guards.static_analysis: true` on a repo with existing errors — blocks every commit |
| `evaluator.max_iterations`, `max_time`, `max_input_tokens` | `max_tokens_per_call` above the provider per-response cap — HTTP 400 on every judge call |
| `defaults.model`, `llm_reasoning` | `resolution.enabled.predispatch` / `judge_prescreen` — real egress + can skip a dispatch |
| `check_for_updates`, `update_check_ttl` | `pass_on_error: true` where the harness is the gate |
| `qa_ledger.*`, `history.*`, `worktree_fleet.disk_ceiling_mb` | `file_scope: full` on a large repo; `max_concurrent_worktrees` high; `disk_ceiling_mb ≤ 0` on a shared disk |
| `guards.hook_timeout`, `lsp_timeouts.*` | `commit_audit.mode: block` with a noisy reviewer |

Per-run overrides beat the file and are the right first move when a config value
is suspect: `gitreins guard --full|--staged-only|--scope working-tree|--dead-code`,
`gitreins judge --skip-tier2`, `gitreins worktree repro --concurrency N`.
The `GITREINS_MAX_*` env vars are the override for caps.

---

## 13. Checklist when a config change misbehaves

1. Print the **resolved** cap and its `source` (§1) — never reason from the file.
2. Compare `init`-written vs `install`-written expectation (§11).
3. For HTTP 400 on judge calls, check `max_tokens_per_call` first, then the
   model's real per-response cap; remember the `deepseek` clamp entry is
   unreachable without `provider=` (§9).
4. For "the gate passed but nothing ran", check `guards.test_mode`,
   `test_on_clean`, `allow_skips`, and whether the lanes printed
   `GITREINS_SKIP:`.
5. For "the config change did nothing", check the block: `security_scan`'s enable
   flag lives under `guards:` while its tuning lives under `defaults:` (§4), and
   caps live under `evaluator:` while `defaults.max_iterations` is only a
   fallback (§1).
