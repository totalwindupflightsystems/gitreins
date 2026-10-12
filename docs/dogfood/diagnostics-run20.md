
## 2026-10-12 — Run 20: commit-audit deterministic floor (re-proof of POC-85's fix)

**Why the surface was chosen:** commit a4567a5 (2026-10-12) added the deterministic
pre-LLM floor to `engine/commit_audit.py` addressing run 18's POC-85 false-negative,
plus a new docs/cli-reference.md §"Deterministic floor and limits" with a five-run smoke
script. No run had driven it; code HEAD changed after the last dogfood entry, so no
inherited proof applied.

**How it works now:** `audit()` captures the staged diff, and BEFORE any LLM call
`_deterministic_commit_audit()` classifies every changed path (docs suffix vs source
suffix) and the message's conventional-commit prefix. `feat:`-family over an all-docs
diff, or `docs:` over an all-source diff, returns a hard `valid=False` result immediately
— no credential, no network, ~164ms total. Mixed/unknown diffs stay with the LLM path
(deliberately: the floor refuses to turn a classification guess into a rejection).

**The two errors this run found — and why each exists:**

- **POC-88 (P1):** `gitreins/cli.py` consumes the Pipeline result with
  `result["stages"]["commit_audit"]` — a dict keyed by the stage's user-chosen `id`.
  Pipeline itself runs and blocks a custom-id stage correctly (the DEBUG log shows
  "Running stage: ca" and the block exists in the result); only the CLI's read-back misses.
  It then falls through to `commit_audit_skip_message()`, which (per POC-30's design)
  reports any type=commit_audit stage as "armed", so the user sees "skipped (condition not
  met)" — a message that is true about the CLI's view and false about the engine's. The
  fix belongs in the read-back (resolve the stage id from the loaded config, or key
  results by type); the skip-message helper should also be able to distinguish "did not
  run" from "ran but was not read".
- **POC-89 (P1):** `audit()`'s LLM path degrades to `valid=True` when the LLM call fails
  (401 or absent credential). That is fail-open in `mode: block`. The floor's doc section
  honestly scopes its guarantee to recognized contradictions, but nothing tells the user
  that everything outside the floor silently passes when the credential dies — with a
  WARNING the console shows but a scripted consumer never reads. Fail-closed (or a distinct
  BLOCK-UNVERIFIED verdict) in block mode is the fix direction; warn mode can keep the
  current behavior.

**Debugging trail (what actually localized POC-88):** the fresh-box smoke first returned
rc=0 with the skip line. Probes in order: (1) raw yaml.safe_load of the probe config —
`on:` parsed as `True` (YAML 1.1); suspected, but the loader's `_fix_on_key` (added for
exactly this pitfall at pipeline.py:1935) proved the loaded config correct on the box.
(2) DEBUG logging of `Pipeline.run` — "Running stage: ca" proved the stage EXECUTED.
(3) Grepping cli.py's result consumption — the hardcoded literal id. (4) The one-variable
A/B on the control host: id `ca` → false skip rc=0; id `commit_audit` → correct block
rc=1. Lesson: when a skip message and a DEBUG "running" line disagree, the two layers are
reading different keys — check the id contract before the config grammar.

**The right way (for the next agent):**
- Re-proving the floor: run the docs' five-run smoke verbatim; it needs no LLM env —
  unset GITREINS_LLM_*/ANTHROPIC_API_KEY/OPENAI_API_KEY via `env -u` to prove the
  credential-free path.
- Stage ids in .gitreins/config.yaml: until POC-88 closes, use the literal id
  `commit_audit` or your block gate is decorative.
- Bunker legs on this project: las-03/las-04 currently offline; las-02 and bunker-mvp
  healthy (bunkerd active). Agent user has make but no managed pip → venv route, ~17s.
