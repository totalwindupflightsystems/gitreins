# GitReins dogfood integration — 2026-10-26

Tick: `gitreins-dogfood-2026-10-26-11-24-50`
Tested HEAD: `05dccc349ae5d20b2ef220c39464abf88bb1482d`

## Promise

A fresh user can install GitReins on an ephemeral machine, run its documented CLI and QA workflow, and receive an honest installed-artifact and suite verdict. A local operator can use the CLI to inspect configuration, scan outgoing commits, run quality production, resolve a premise, and browse persisted evidence.

## Local real use

The detached HEAD worktree `/tmp/gitreins-dogfood-20261026-112450` exercised:

- `gitreins --version` → `gitreins 0.17.1`.
- `gitreins doctor` → valid gitleaks configuration.
- `gitreins setup-tools` → 3 tools available, `sqlfluff` missing, with an install hint.
- `gitreins report --json` → one bounded JSON document with schema, checks, outcome, producer, scope, and summary fields.
- `gitreins push-check HEAD HEAD` → `PUSH SECRET CHECK: clean`, rc 0.
- `gitreins quality-ensure` → rc 0 and no misleading stdout.
- `gitreins preflight --surface cli --json "Does engine/evidence_bounds.py truncate text?"` → rc 0, top-level `verdict` object, band `REVIEW`, decision `dispatch-with-note`; the result disclosed clipping and omitted files.
- `gitreins worktree dogfood --skip-judge` → `3 passed, 0 failed, 1 skipped`.

The live CLI exposes 19 top-level commands. README's Commands block omits `quality-ensure`, `push-check`, `resolve`, and `preflight`; this is corroborating evidence for existing row `README-008`, not a new duplicate finding.

## Install and clean-machine battery

`bunker-qa.sh launch /home/kara/gitreins` ran on `bunker-mvp`, agent `339cb0f1`, and tested the exact HEAD above. Agent evidence was read before teardown. The fresh-install cell reported:

`install rc=0 but no artifact found (toolsd not in PATH, PREFIX/bin/toolsd missing, or zero-size)`

The same cell's install transcript reported `Successfully installed ... gitreins-0.17.1`. This is a harness detector/template identity failure, not proof that GitReins failed to install. It is filed as `DF-GITREINS-POC-103`.

The native `ci-pass` cell reported rc 120. The agent log recorded `OSError: [Errno 28] No space left on device` while writing output. This is environment/storage evidence, not a clean product-suite verdict. The dependent `chaos-disconnect` cell correctly remained `UNVERIFIED` because `ci-pass` failed first. It is filed as `DF-GITREINS-POC-104`.

The controller collect remained running after the agent completed and was later unreachable after teardown. The run is therefore partial for controller-side collection; the agent-side 19-row evidence is retained. The install leg was RUN, not silently skipped.

## Performance

Exact command:

`hyperfine --warmup 2 --runs 8 'gitreins push-check HEAD HEAD'`

Result: mean `252.9 ms ± 20.8 ms`, range `220.0–285.5 ms`. This is comfortably below a noticeable wait. No PERF row was filed.

## Artifacts

- `docs/dogfood/evidence/2026-10-26-gitreins-cli-battery/agent-evidence.jsonl`
- `docs/dogfood/evidence/2026-10-26-gitreins-cli-battery/push-check-hyperfine.txt`
- `docs/dogfood/evidence/2026-10-26-gitreins-cli-battery/preflight.json`
- `docs/dogfood/evidence/2026-10-26-gitreins-cli-battery/collect.txt`
- `docs/dogfood/diagnostics.md` — appended 2026-10-26 section
- `skills/gitreins-usage/SKILL.md` — appended current battery/detector guidance
- `.coding-hermes/dogfood-log.md` — appended tick verdict

No product code, scheduler state, repository visibility, or permissions were changed. The foreman was not woken.