# JEV Resolution Gate — spec

**Status:** proposed (design authority for JEVRES-001..006) · **Author:** ops session, 2026-09-20
**One line:** take a question, assemble the code that could answer it with Hilo inside a measured
token budget, and ask Jev for a *calibrated probability that the evidence resolves the question*.

---

## 1. The question this answers

Not "does this code work" — that is what the tier-2 judge already does, expensively and
open-endedly. This answers a narrower, cheaper question:

> Given a question (a task's acceptance criteria, a judge criterion, or a free-form agent
> question), **is the code we can point at sufficient to resolve it — and if not, what kind of
> piece is missing?**

Today gitreins answers that by *hoping*: a worker reads files until it feels done, and a judge
reads a diff and decides. Both burn a general-purpose LLM over an unbounded context. This
component replaces the open-ended part with a **typed, calibrated, ~$0.0003 decision over a
bounded context**.

## 2. Measured facts (live, 2026-09-20 — not documentation claims)

All numbers below were measured on this host on 2026-09-20; commands are reproducible.

| Fact | Measured value | Why it matters |
|---|---|---|
| Jev transport | `POST https://openrouter.ai/api/alpha/decisions`, model `typesafe/jev-1.13` | Jev is a **decisions** model: it returns typed answers, never text. `/chat/completions` returns 400. |
| Answer shape | `answers.<q>.noul` (0–1) / `.choice` (label + probs) / `.score` (0..N + legend) | Three primitives: probability, classification, rubric position. |
| Cost, measured | 315 in-tokens → `$0.0000132`; 30,008 in-tokens → `$0.001260336` | **$0.042/M input, $0 output.** The 28k-token bundle below costs ≈ $0.0012. |
| Batch property | several questions answered in **one** request, one flat cost | Ask resolution + missing-kind + evidence-quality together; pay once. |
| **Input ceiling** | **32,778 in-tokens accepted** (65,000 chars); 30,008 accepted (105,160 chars); rejected above ≈33k | This is the "32k context" in practice. **Usable budget ≈ 30k; spec caps the bundle at 28k.** |
| Chars per token | **content-dependent: measured 1.98 and 3.5 on the same filler family** | A fixed divisor is unsafe — see the budget law in §3.3. |
| Discriminating power | real snippet ("does this handle negatives") → `noul 0.87`; 30k tokens of filler → `0.09` | It scores **resolution**, not bulk. More code is not more answer. |
| Key availability | `GITREINS_OPENROUTER_KEY` **live**; `OPENROUTER_API_KEY` **401 expired** | Failover across all `sk-or-v1-*` candidates is mandatory, not defensive polish. |
| Hilo bundle | `hilo graph understand "<task>" --budget N` → `## MAP` / `## SIGNATURES` / `## DETAIL` with *whitespace-minified real source* + per-file `provenance` + `score` | This is the assembly primitive. |
| Hilo budget behaviour | `--budget` 2000/4000/8000 all return the **same** 8,889 chars; 16000–30000 return 41,242 chars | `--budget` is a **coarse resolution tier, not a hard clip.** The wrapper must measure and enforce its own ceiling. |
| Hilo bundle ceiling (this repo) | ~41k chars ≈ **11.8k Jev tokens** | One bundle alone cannot fill a 28k window — multi-bundle assembly is required for broad questions. |

## 3. Architecture

```
question ──► TRACE (hilo)  ──► ASSEMBLE (hilo) ──► BUDGET (measure+clip) ──► JEV (1 call) ──► BANDS (code)
             graph search        graph understand      ≤28k tokens,            noul + choice      ≥.85 RESOLVED
             graph related       per seed, tiered      line-aligned,           + score            ≥.50 REVIEW
             (path, not reads)   falls back to reads   truncation disclosed                       <.50 UNRESOLVED
                                                                                               error → ABSTAIN
```

### 3.1 Trace — the path, not the haystack

1. `hilo graph search "<question>" --limit 12` → seed files with scores and `symbols`
   (deterministic TF-IDF + BM25 — no embeddings, no API, microseconds).
2. `hilo graph related <seed> --direction reverse` → **who depends on / imports the seed**; the
   reverse edge set is the "path" through which the question's answer is wired.
3. `hilo graph impact <seed>` only when the question is *about* blast radius.
4. Hilo is **JIT** — `related`/`impact` auto-parse on first access, so no warm step is required.
   `graph stats` is **not** JIT (empty on a cold cache); never gate on it.

### 3.2 Assemble — inside the measured ceiling

1. Primary bundle: `hilo graph understand "<question>" --budget 16000` (the tier that yields
   `DETAIL` on this repo).
2. If tokens remain and the question named extra seeds, a second targeted `understand` per seed.
3. Only if still short: bounded, line-aligned reads (`sed -n`) of the top-scoring files, cap per file.
4. **Everything carries provenance.** The bundle manifest (file → provenance, score, bytes,
   truncated?) is part of the output — a verdict with no traceable bundle is not a verdict.

### 3.3 Budget law (the part that must not be improv'd)

- `--budget` **does not** guarantee the ceiling; the wrapper **measures** the assembled text and
  enforces `MAX_BUNDLE_TOKENS = 28_000` (2k margin under the measured ~30k wall).
- Token estimate must be **conservative or real**: this filler family measured **1.98** chars/token
  when repetitive and **3.5** when varied, so `chars // 3.5` can undercount by ~75% and blow the ceiling.
  Use a real tokenizer when one is available; otherwise the conservative floor `chars // 2`.
  Recompute after every bundle, and keep the measurement in the verdict so a low score can be told apart
  from a clipped bundle.
- Truncation is **line-aligned** and **disclosed** (how many bytes/lines were dropped, from where).
  **Reuse `engine/evidence_bounds.py`** — it already implements head/tail budgets with an omission
  marker and hoisted summary lines. Do not write a second truncator.
- Budget arithmetic is logged with the verdict, so a low resolution score can be told apart from
  "we ran out of room" — those are different failures with different fixes.

### 3.4 The Jev call — one request, three typed questions

```jsonc
{
  "model": "typesafe/jev-1.13",           // pin the build when thresholds matter; log the echo
  "state": "<question>\n\n<assembled bundle>",
  "questions": {
    "resolves_question": { "type": "noul",   "instructions": "Probability that the supplied code is sufficient to answer the question." },
    "missing_kind":      { "type": "choice", "instructions": "If the code does not resolve it, what is absent?",
                           "criteria": { "none": "...", "implementation": "...", "test": "...", "wiring": "...", "config": "...", "docs": "..." } },
    "evidence_quality":  { "type": "score",  "instructions": "How directly does the bundle address the question?",
                           "criteria": ["mentions only", "adjacent code", "the exact code path", "the exact path plus its test"] }
  }
}
```

### 3.5 Bands live in CODE, never in the model

| `resolves_question` | Verdict | Action |
|---|---|---|
| ≥ 0.85 | `RESOLVED` | skip the expensive path (pre-dispatch) / confirmatory judge |
| 0.50 – 0.85 | `REVIEW` | a human or the full judge looks; never auto-passed |
| < 0.50 | `UNRESOLVED` | dispatch/continue; `missing_kind` names what to build |
| transport error, 401/402/429 on **every** key, malformed answer | **`ABSTAIN`** | **fail closed** — never silently "resolved" |

Thresholds are config, tunable without touching Jev. Fail-closed is doctrine, not preference: a
silent PASS here would let unresolved work through the cheapest gate in the system.

## 4. Where it plugs into gitreins

| # | Integration | What changes | Value |
|---|---|---|---|
| 1 | **Pre-dispatch premise check** | before a worker is dispatched for a board row, resolve the row's criteria; `RESOLVED` ⇒ do not dispatch, annotate the row with the bundle + probability — **shipped in this repo as `gitreins preflight` (JEVRES-003, `engine/preflight.py`); the foreman-side wiring is external** | kills the duplicate/wasted-worker class directly |
| 2 | **Judge pre-screen (tier 1.5)** | run resolve over `diff + criteria`; feed `missing_kind` and per-criterion probabilities to the judge | cheap triage; the judge stops re-deriving what is absent |
| 3 | **MCP tool + CLI** | `gitreins resolve "<question>"` and MCP `context.resolve` | agents ask the repo instead of reading it — the context-saving primitive |
| 4 | **Per-criterion attribution** | resolution probability per acceptance criterion in the verdict artifact | turns "3/3 criteria PASS" into "3/3 PASS, 0.91/0.88/0.93" with the code path cited |

## 5. Drift surfaces that this work MUST sync (learned the hard way in this repo)

- **MCP tool count is claimed in ≥5 docs** (`architecture.md:37`, `cli-reference.md:334`,
  `evaluator-loop.md:86` and `:119`, `mcp-api.md:4`). Adding `context.resolve` makes all of them
  stale — the existing MCP tool-count drift class (GR-GAP-017) recurs here.
- **README + CONTRIBUTING test counts** must move in the same commit as new tests or CI's drift
  gate goes red (GR-GAP-061 class).
- Docs links must resolve (`docs/` link checks run in the PM sweep).

## 6. Honest limits (decide these explicitly, do not discover them at runtime)

1. **One bundle ≈ 11.8k tokens on this repo.** Broad questions need 2–3 bundles or bounded reads;
   the assembler must be allowed to say "budget exhausted" rather than pretend coverage.
2. **Precision beats volume** (filler scored 0.09). Do not pad to fill the window.
3. **Egress:** the bundle leaves the host for OpenRouter → TypeSafe. Anything assembled is
   third-party-bound; respect the existing secret-scanning posture (never bundle `.env`, keys, or
   anything the secrets guard would block). Add an explicit exclusion filter.
4. **Calibration drifts between Jev builds** — log the returned model id with every verdict.
5. **This is a signal, not a gate** — `RESOLVED` may skip *dispatch*, but it must never be the sole
   authority for a merge/commit. Same doctrine as the injection guard's "routing signal" rule.

## 7. Task decomposition (filed as board rows)

| Row | Scope | Depends on |
|---|---|---|
| `JEVRES-001` | `engine/resolution.py` core: trace → assemble → budget (reusing `evidence_bounds`) → Jev call with key failover → fail-closed bands. Hermetic tests (mock endpoint) + live battery | — |
| `JEVRES-002` | Surface: `gitreins resolve` CLI + MCP `context.resolve` + **the 5 doc count/location syncs** | 001 |
| `JEVRES-003` | Pre-dispatch premise check wiring in the foreman/board path (`RESOLVED` ⇒ annotate, don't dispatch) | 001 |
| `JEVRES-004` | Judge pre-screen (tier 1.5) + per-criterion resolution probabilities in the verdict artifact | 001 |
| `JEVRES-005` | Calibration harness: labeled resolved/not-resolved corpus, threshold sweep, regression battery | 001 |
| `JEVRES-006` | Config + docs: knobs (model, bands, token ceiling, per-surface enable), `docs/jev-resolution-gate.md` cross-links | 002 |

## 8. Acceptance criteria (apply to every row)

- Behaviour proven by **live** output, not a self-report; the live numbers in §2 are the reference.
- Fail-closed is tested: with every key invalid, the verdict is `ABSTAIN`, exit non-zero, and **no**
  surface reads it as success.
- Budget ceiling proven: a synthetic 40k-token bundle is clipped to ≤28k, line-aligned, with the
  omission disclosed in the manifest.
- Full repo suite + ruff clean; README/CONTRIBUTING counts synced in the same commit.
- Every verdict artifact carries: probability, verdict band, `missing_kind`, bundle manifest,
  model build id, token counts, cost — and is **persisted where `gitreins serve` can show it**
  (the DF-GITREINS-POC-23 lesson: an invisible verdict is not an audit trail).
