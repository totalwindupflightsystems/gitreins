# Diagnostic trail — how to use the resolution gate and push-check correctly (and why each current failure looks the way it does)

*(Run 24, 2026-10-23, tick gitreins-dogfood-2026-10-23-10-02-55. Read-only
dogfood: nothing below changed code; findings went to the board as
JEVRES-007, DF-025, DF-026, DF-027, DOC-25, PERF-2.)*

## Run 24 (2026-10-23) — the resolution gate's evidence floor, the empty-bundle hint, and the two scanners behind push-check/guard-secrets

### The evidence floor: why preflight can say REVIEW at probability 0.97

`gitreins resolve` and `gitreins preflight` consume the SAME verdict object, but
preflight re-bands it through `effective_band_for` before mapping the decision:

```
engine/preflight.py:84      effective_band = effective_band_for(...)
engine/resolution.py:1530   def effective_band_for(...)
engine/resolution.py:182    MIN_RESOLVED_EVIDENCE_QUALITY = 3
engine/resolution.py:1554   sufficient = (evidence_quality >= 3
                                          and manifest has an ast_exact/bounded-read
                                          entry with score >= 0.80 ...)
```

The Jev rubric (`evidence_quality_legend` persisted in every verdict.json) is
0=bundle-only, 1=adjacent, 2=exact path, 3=exact path PLUS its test. A repo with
no test on the code in question tops out at 2, so the production pre-dispatch
band caps at REVIEW there — by design (spec: "Weak evidence is REVIEW, so
callers dispatch with the right note path"). The defect is not the floor; it is
that the CLI surface never says it happened: the record's `band`/`reason` were
computed FROM the demotion, so the reason reads "the evidence partially resolves
the row" even when nothing was partial (evidence_quality exactly 2, probability
0.97), and §16's table still says "RESOLVED (probability ≥ 0.85) → skip-dispatch".

The right way to use the surface as shipped: treat a preflight REVIEW as
"resolved-but-no-test" and check the nested `verdict.evidence_quality` field
(it IS in the JSON today — `/tmp/df-pf2.json` carries it two levels deep)
before wondering why you did not get skip-dispatch. JEVRES-007 asks for the
demotion to be named in the record/reason so no reader has to re-derive it.

### The empty-bundle ABSTAIN: not a missing binary, a missing project

`resolve` on a fresh repo abstains `empty-bundle` and its hint says "check that
hilo is installed". hilo WAS installed; the actual sequence was:

```
hilo init        # creates manifest.yaml + hilo's own post-commit hooks
hilo graph warm  # builds the graph .vfs cache the trace step reads
```

Only after both does `resolve` return real bands (0.56–1.1 s). The engine is
honest about WHAT failed (empty candidate set) but guesses wrong about WHY, and
its claimed "abstain detail names the exact path that was searched" is not in
the record. DF-025 asks for a `hilo-not-initialized` reason naming those two
commands.

### push-check's line scanner: the two shapes it can and cannot see

`engine/push_secrets.py` scans ADDED LINES from `git log -p` over the push
range against six classes:

| Class | Regex | Example that matches | Example that defeats it |
|---|---|---|---|
| token prefixes | `\bghp_[A-Za-z0-9]{28,}` etc. | `TOKEN = "ghp_aaa…"` (contiguous) | `TOKEN = "ghp_" + "aaa…"` — the body sits after a quote+plus+quote, so the contiguous-body requirement fails |
| API key | `\bsk-[A-Za-z0-9_-]{20,}` | `sk-…` | `sk_live_…` (underscore form is out of the class entirely; the assignment class only catches it when the VALUE is one 12+ char non-space run) |
| credential assignment | `[A-Z0-9_]*(PASSWORD\|TOKEN\|SECRET\|API_KEY)…=[^\s#]{12,}` | `API_TOKEN = "XXXX…40"` | `API_TOKEN = "ghp_" + "XXXX…"` — the value's first non-space run is `"ghp_"` = 6 chars, too short; the spaces around `+` end the run |
| private keys / Slack | verbatim markers | — | split lines |

The measured A/B (single-commit ranges so no range contamination):

```
branch ab-concat (bfd6298): "sk_live_" + "9f8e…" / "ghp_" + "XXXX…" → clean, push PUBLISHES it
branch ab-joined (65c73b9): same literals joined                     → refused (GitHub PAT class)
```

So one concatenation — the form a user writes to keep a literal out of grep
history, or a linter enforces for line length — defeats all six classes at
once, on the gate whose entire job is that publish. DF-026's fix direction
(second pass after eliding quoted-chunk glue) is in the row.

### The guard's secrets scope: three scopes, one shown `whole tree`

The secrets lane's scanner choice is scope-driven, and `--full` does not change it:

| Command | What secrets actually scans | On a committed-secret tree |
|---|---|---|
| `guard` (staged) | `gitleaks protect --staged` = index-vs-HEAD + builtin on the staged diff | clean (0 commits scanned) |
| `guard --full` | the SAME staged secrets scope (only lint/tests get whole-tree) | clean — while the banner says "whole tree" |
| `guard --scope working-tree` | builtin on the union (staged+unstaged+untracked); gitleaks named skipped-for-scope (TRUST-003) | 3 findings, FAIL |

That is why a leaked-commit user who runs the banner's advertised whole-tree
check sees "✓ secrets — clean (gitleaks + builtin cross-check)" and moves on.
`protect --staged` is the right scope for the pre-commit hook (it grades the
commit being made); the gap is `--full` reusing it while asserting whole-tree
grading (DF-027), and no doc naming `--scope working-tree` as the "is the
history/tree clean?" check (it appears once, README.md:491, in a Go context).

### Measured cost (why no profiling was run)

See the perf table in `docs/dogfood/2026-10-23-resolution-gate-integration.md`.
The only wall component above 300 ms is the Jev round-trip itself; the hilo
trace/assemble stage is <0.2 s at repo scale. A cache-free tree abstains in
0.195 s (fail-fast, not a hang). Nothing a user feels; PERF-2 records the
numbers so the next run can measure drift instead of re-deriving.

### Scratch footprint (for the residue check)

- `/home/kara/scratch/gitreins-df-20261023/` — consumer repo + upstream bare +
  coldtree (all disposable; kept for row-verification until the foreman picks
  the rows, then delete).
- `/tmp/df-*` — probe scripts and captures (copied into
  `docs/dogfood/evidence/2026-10-23-resolution-gate/` before commit).
- `/tmp/df-ab/upstream` — the A/B clone (contains a concat-form test credential,
  16-hex chars 'X' filler, not a real secret; keep until rows close, then rm -rf).
- Bunker agent df-gitreins-1023: SPAWNED, USED, DESTROYED (verified gone, key removed).
- The real gitreins repo itself: resolve calls appended 5 usage rows +
  5 resolution records to ITS `.gitreins/` (its own store, expected residue of
  running the tool in-repo; `git status` clean except `.vfs` cache the hilo
  post-commit hook rewrites — same class as the pre-existing `.vfs` dirt at HEAD).