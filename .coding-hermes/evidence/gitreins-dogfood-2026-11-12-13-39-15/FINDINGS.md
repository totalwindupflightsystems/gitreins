# gitreins dogfood — tick gitreins-dogfood-2026-11-12-13-39-15

Date: 2026-11-12 (UTC). Lane: gitreins-dogfood. Workdir: /home/kara/.hermes/stand-in/dogfood/gitreins.
Canonical target: /home/kara/gitreins @ dba0349a (branch `main`, tree clean, remote = `github`).
Duplicate-fire check: `grep -c '^#+ .*gitreins-dogfood-2026-11-12-13-39-15'` on the committed
dogfood log = 0, on-disk = 0; last committed entry 2026-11-09d. This fire is the delivering fire.

## What was actually used

Published release `gitreins==0.21.0` installed from PyPI into a fresh venv
(`scratch/df-gitreins-2026-11-12-133915/venv`) — i.e. the consumer's experience, not the dev tree.
Consumer project `slugkit` 0.1.0 (4-file Python package, 4 pytest cases) in the same scratch dir.

Commands exercised: `--version`, `install`, `init`, `doctor`, `guard`, `guard --staged-only`
(network-hostile npm), `task create|start|complete --skip-tier2|list`, guarded `git commit`
through the installed pre-commit hook, `npx pyright`.

## Findings

### Live reproduction of DF-028 (owned row — no new filing)
Static analysis reports clean without executing pyright. Guard log, network-hostile run
(`npm_config_registry=http://127.0.0.1:9`, isolated npm cache, no npx on PATH):

    42:[PASS] static_analysis  passed=true  exit_code=n/a
    45:  mypy — clean
    46:  pyright — clean

`exit_code=n/a` + "clean" with no tool invocation evidence. Still live at 0.21.0.
DF-028 and DF-030 were both still `pending` on the board at tick start; both already own this
class, so nothing new was filed for it.

### Positive behaviours (verified, not assumed)
- Thin criterion (SLUG-3, "alpha does a thing") is accepted but warned, and the warning names the
  missing `failure_condition`/`proof` and audits the existing board; the first two task creations
  left it silently on tasks the CLI had already made. Advisory, not blocking — recorded as a
  positive, not a defect.
- The commit gate genuinely blocks: a staged comment-only edit failed `lint`
  (`ruff format --check: 1 file(s) would be reformatted`), commit refused (exit 1), tree unchanged.
- `gitleaks: clean` on the secrets lane; guard artifacts land under `.gitreins/logs/` and are
  ignored by the `.gitignore` that `init` writes.

### Candidate findings withdrawn after verification (recorded for honesty)
1. *"Ancestor pytest.ini hijacks the consumer's test scope."* The guard's tests lane runs with
   `cwd=workdir` and `python3 -m pytest -x --tb=short` (engine/guard_manager.py:3095, :3181;
   gitreins/cli.py:1240). The guard log shows `rootdir: /home/kara`, `configfile: pytest.ini` —
   the fleet ini at $HOME *is* read — but a bare run in the consumer collects the consumer's own
   4 tests (`barepytest.txt`), because `python -m pytest` supplies an argument, so the ini's
   `testpaths` never narrows collection. No false green. Not filed.
2. *"`git add -A` commits runtime artifacts."* `.gitreins/history/` is deliberately not ignored:
   it is the durable record (gitreins/cli.py:104-108, "the durable record lives in .gitreins/history/").
   Committing it is by design. Not filed.

## Performance (Step 2b)

Headline operation for a CLI consumer = the guarded commit. Warm guarded commit, 4-test Python
consumer, ambient 42-entry PATH, loadavg 9.2 on 16 cores (`warm3.txt`): 2.00 / 1.78 / 1.70 s,
exit 1 (lint refuses — by design, so the work is re-staged each run).

PATH A/B (`pathperf.txt`), n=3 each: 45-entry PATH 2.17 / 1.24 / 1.22 s; 5-entry PATH
1.47 / 1.28 / 1.15 s. No PATH scaling.

Compare PERF-3-R2 (pending, 0.20.0, same 4-file Python shape): 6.16-10.84 s, median 7.55 s,
"remainder tracks PATH length (4.20 s at 5 PATH entries vs 10.07 s at 45)". The PATH-driven
lane-setup cost it names does not reproduce on 0.21.0 at these PATH lengths. Load differs
(9.2 here vs 33 there), so this is not proof of a fix — it is a re-measurement that makes the
row's premise need a controlled re-run before more spend. Filed as PERF-3-R2-R2.

Nothing else was user-noticeably slow: install + init + doctor + a task lifecycle ran inside the
few-second band a user does not feel; no profile was warranted.

## Install leg (ephemeral bunker)

`~/.hermes/scripts/bunker-qa.sh status` -> `server bunker-mvp`, `bunkerd=active docker=yes
act=no uidmap=yes`, rc=0 — the host was reachable, so no SKIP was permissible.
`bunker-qa.sh run /home/kara/gitreins --ttl 2h` spawned agent `c2367fbe` (spawn bundle captured;
credentials redacted, not reproduced here). Battery evidence:
`/tmp/bunker-qa-evidence-20261112T140225Z-160441.jsonl`. Result recorded in the tick log below.

## State left behind

- Board: PERF-3-R2-R2 (this tick's only row). No existing row edited.
- Evidence: `.coding-hermes/evidence/gitreins-dogfood-2026-11-12-13-39-15/` (this file + 9 raw captures).
- Ledger: `.coding-hermes/dogfood-log.md`.
- No code change, no config change, no visibility/permission change.
