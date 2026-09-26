# Dogfood Run 14 — `gitreins report` / `gitreins commit` / `gitreins setup-tools` (2026-09-25)

**Verdict: 🟢 SHIPPABLE (for the surfaces tested) — with one P1 on the
commit door and a dead fresh-clone fallback that three runs have now
danced around.**

**Promise under test:** "After judging, a user browses verdict history via
`gitreins report` (human report, `--json` evidence document, `--interactive`
TUI, with the documented fresh-clone branch fallback), commits finished work
with `gitreins commit <message>` (guards wired in, MCP-style refusal while a
task is in_progress), and `gitreins setup-tools` tells them which static
analysis tools exist and how to install the missing ones."

Runs 1–13 never promised any of these three commands. They are the
maintenance loop every user lives in after the first judged commit, and the
door the AGENTS.md quickstart teaches.

## What was done (real use, consumer clone)

Scratch consumer clone of HEAD 5e9d38d at /tmp/dogfood-gitreins/consumer14
(fresh clone, so the fresh-clone surfaces are the real ones), plus a
no-config empty repo for zero-state probes and a second cold clone for
cold-path timing.

| Probe | Result | Notes |
|---|---|---|
| `report` on fresh clone | ✅ | serves 2 verdicts from 2026-08-17/18 — the stale TRACKED dirs win precedence (F3) |
| `report --json` | ✅ | bounded evidence v1 doc, `redacted: true`, `checkCount` matches, exit 0 |
| `report --interactive` | ✅ (honest) | textual is not a wheel dependency → fallback banner + text report, exit 0 |
| `report -n 1` | ✅ | bound respected |
| `report` in empty repo | ✅ | "No verdict history found." exit 0 — honest zero state |
| `task create/start/complete --skip-tier2` | ✅ | verdict `23211bc6` persisted, then visible in `report` |
| `gitreins commit` with staged lint-broken file | ✅ blocks | Tier 1 FAIL, exit 1, lane output names the file; HEAD unchanged |
| `gitreins commit` after fix | ✅ commits | "Tier 1 PASSED — committing..." → commit 8e1c239, exit 0, **0.51 s wall** |
| bare `git commit` with staged `sk-` token | ✅ blocks | hook FAILs secrets (gitleaks + builtin cross-check), exit 1 |
| bare `git commit` clean | ✅ passes | hook = same guard output, pinned venv path intact (DF-011 fix holds) |
| `gitreins commit` (CLI) with task in_progress | ⚠ | commits without a word about the open task (F1) |
| MCP `commit` with task in_progress | ✅ blocks | documented refusal message (re-verified, 13-tool server) |
| `gitreins commit` with nothing staged | ⚠ | green "PASSED — committing..." → git refusal → guard summary, exit 1 (F5) |
| `setup-tools` | ⚠ | multi-language header, Python-only list (F4); missing-tool path = bare `pip install mypy` |
| `setup-tools` in unknown-lang repo | ✅ | "No static analysis tools are tracked for unknown." exit 0 |

The consumer task itself: `consumer-app.py` (37 lines, stdlib only) reads
`gitreins report --json` and prints pass/fail counts — it ran green against
the repo's own history and satisfied all three criteria via
`task complete --skip-tier2`. Integration with the JSON surface needed no
source reading: `$schema`, `checks[].id/outcome/passed/summary`,
`metadata.redacted` all behaved as the help text promises.

## Findings

**F1 (P1, POC-66): the CLI commit door has no in_progress refusal.** With
task `consumer-14` in_progress, `gitreins commit "..."` ran guards and
committed (8e1c239). The same state via the MCP `commit` tool returns
`"Tasks still in progress: ... — commits are blocked while a task is
in_progress because task.complete runs the quality judge against the
committed state"` (re-verified live). The README's "MCP commit rule" names
only the MCP door, but the harness has two doors and agents are scripted on
the CLI (every quickstart in this repo teaches `gitreins commit`). The
judge-skip protection is one command choice away from being skipped.

**F2 (P2, POC-67): "full suite — safety trigger" banner lies about skipped
runs.** First hook run output:

```
Tier 1 Guards: FAIL  (test mode: diff, full suite — safety trigger)
  ~ tests — skipped (no test files match the changed sources (diff mode))
```

The banner and the lane line contradict each other. Mechanism:
cli.py:2087-2088 prints the note whenever `extra["test_targets"] is None and
mode == "diff"`, but guard_manager.py:1499-1500 sets `test_targets=None` both
for the real full-suite fallback and for the zero-match skip path
(guard_manager.py:2201-2209). The same wrong string lands in the run log via
`_log_test_scope` (guard_manager.py:1028). Anyone grepping logs for
full-suite coverage over-counts.

**F3 (P2, POC-68): the fresh-clone verdict fallback is still dead — now in
two layers.** Sequence proven on scratch clones: (a) as-cloned, `report`
serves 2 August verdicts because `.gitreins/history/2026-08-17/96dd2464/`
and `2026-08-18/9b129d91/` are STILL git-tracked (POC-58 part 1, pending);
(b) after untracking them, `report` prints "No verdict history found." even
though `origin/gitreins` carries 617 verdict entries — the fallback resolves
only LOCAL `refs/heads/gitreins`, never `refs/remotes/origin/gitreins`
(`git branch gitreins origin/gitreins` then `report` serves the branch);
(c) meanwhile canonical storage moved to `refs/gitreins/history` (POC-52,
8922d7d; 626 entries), which `git clone` never fetches (`+refs/heads/*` only)
and the legacy branch is 9 verdicts behind it. A fresh clone is 3 fixes away
from the history README promises. Note: an earlier draft of this finding
credited the fallback as fixed because the branch is now pushed — the
three-clone probe disproved that within the same run; the row filed on the
board carries the corrected version.

**F4 (P2, POC-69): setup-tools header overpromises; missing-tool hint
re-fights PEP 668.** `Static Analysis Tools for Python + C + SQL:` followed
by exactly two Python tools — the banner prints the detected language NAME
while the tool list keys on the primary type (cli.py:3114-3120). With mypy
off PATH the guidance is a bare `pip install mypy` — the same literal-pip
line PEP-668 blocks on stock Linux that POC-64 filed for the product
itself. The zero-state half is honest and exit-0.

**F5 (P2, POC-70): empty-index `gitreins commit` is a green-red sandwich.**
"Tier 1 PASSED — committing..." then git's "nothing to commit, working tree
clean" then the guard summary (whose DEGRADED header carries F2's false
safety-trigger note), exit 1. Unstaged-only edits behave identically, with
no hint that `git add` is what's missing. Commit is the first door every
quickstart teaches; a pre-check ("nothing staged — hint: git add <files>")
would fix the reading order.

## Performance (Step 2b)

- `gitreins report`: **89.0 ms ± 4.6 ms** warm (hyperfine, 20 runs,
  81-101 ms range), **0.08 s** cold in a brand-new clone.
- `gitreins commit` happy path: 0.51 s wall including guards + hook + commit
  (single-shot, diff mode, 1 staged file).
- Verdict: nothing here is slow enough that a user would notice. **No PERF
  row filed** — the headline operations are comfortably fast, and a finding
  nobody can feel would only devalue the real ones.

## Evidence

- Raw captures: `docs/dogfood/evidence/maintenance-loop-2026-09-25/run14.md`
  (probe outputs, F1-F5 verbatim, the F3 correction trail).
- Board rows: DF-GITREINS-POC-66 (P1), -67, -68, -69, -70 (P2).
- Skill: `skills/gitreins-usage/SKILL.md` v1.12.0 (run-14 section,
  pitfalls 47-50).
