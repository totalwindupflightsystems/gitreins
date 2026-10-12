# Bunker full CI

Run this repo's **full CI suite on a clean JIT bunker agent** and print *which
build the run measured*. The whole point of the lane is that the answer to
"which revision did we just test?" is in the output, not in the operator's
memory.

```bash
# the one command (synchronous; launch it in the background and follow the log)
scripts/bunker-full-ci.sh run

# with the fleet's usual shape
scripts/bunker-full-ci.sh run --server bunker-las-02 --ref main > /tmp/bunker-full-ci.log 2>&1 &
tail -f /tmp/bunker-full-ci.log
```

## Why this exists

A battery that reports `native suite PASS` without naming the build is not
evidence. The 2026-09-23 capture of the test-suite hang exported two revisions
with `git archive` — no `.git` — and both sides reported an *identical*
`17 failed / 25 errors`. Read on its own, that looks like a version finding. It
was an artifact of the export: the git-less tree made the same git-dependent
tests misbehave on both sides by construction. The prototype closed with the
honest note that the standing harness recorded a verdict but never the suite's
counts, so nothing would have caught it automatically. This lane is that gap
closed:

| the fact | how the lane establishes it |
|---|---|
| the tested revision | the agent holds a **real clone** of one commit; `git rev-parse HEAD` there is the identity |
| the tree that landed | the agent's `git ls-files \| wc -l` is asserted against the source tree's tracked count at that commit |
| the deployed CLI | installed **from that clone** (`pip install -e`), version read back, `gitreins.__file__` checked to resolve *inside* the clone |
| the CLI's surface | `scripts/check_deployed_surface.py --json` (REVIEW-001) — deployed surface vs tree surface |
| the toolchain | **fresh** (created by this run) or **reused** (a venv that already existed on the agent), named either way |
| the suite's result | rc, wall time, and the pytest summary line — counts, never a bare verdict word |
| whether the suite has tests at all | `pytest --collect-only -q --override-ini=addopts=` (the same mechanism `scripts/check_docs_drift.py` uses), so a zero-test `rc=0` run is graded **FAIL**, never PASS (QA-AUGER-1) |

## The command

| mode | what it does |
|---|---|
| `run` | spawn (or reuse) an agent, ship a `git bundle`, clone it there, build the toolchain, print the identity, run the suite, record the QA-ledger row, destroy the agent |
| `dry-run` | local preflight: resolves the commit, builds the bundle, prints the two scripts that *will* run on the agent and the identity the lane will print. No agent, no suite, and no `bunker` CLI needed |
| `self-test` | grades the fixture table offline — the clean pass, the wrong-tree deploy, the partial deploy, the partial ship, the stale CLI, the vacuous pass, the honest red suite. Exits non-zero if any grade regresses |

| option | meaning |
|---|---|
| `--repo DIR` | repo to test (default: the repo the script lives in) |
| `--ref REV` | revision to test (default: `HEAD`) — the A/B knob: `--ref 7c817f9^` tests the old side |
| `--server NAME` | bunker server (default: `$BUNKER_FULL_CI_SERVER`, else the CLI's active server) |
| `--ttl DUR` | agent TTL (default `4h`) |
| `--agent ID` | use an **existing** agent instead of spawning one (it is never destroyed) |
| `--keep` | leave a spawned agent alive for inspection |
| `--timeout SECS` | suite timeout on the agent (default `5400`) |
| `--pytest-args "…"` | extra pytest args, e.g. `"tests/test_cli.py -x"`. Default is empty: the repo's own configuration, the full suite |
| `--reuse-toolchain` | keep the agent's existing venv + CLI instead of building them — the way to see the `reused` case |
| `--deploy-only` | deploy and print the identity; do not run the suite |
| `--evidence DIR` | evidence directory (default `/tmp/bunker-full-ci-<sha>-<ts>`) |

## The output

Everything the lane measured, in one block, with a graded verdict:

```text
==================== BUNKER FULL-CI IDENTITY ====================
mode            : run
agent           : 4f1c9a2b8e @ bunker-las-02
requested       : /home/kara/gitreins ref=HEAD sha=762935469111…
source tree     : clean
tested tree     : /home/bunker-4f1c9a2b8e/ci-gitreins sha=762935469111…  files 386/386 landed
deployed CLI    : /home/bunker-4f1c9a2b8e/ci-tools/venv/bin/gitreins version=0.16.0 (from tested tree: yes)
surface probe   : ALIGNED (rc=0) — tree 18 == deployed 18 subcommands [scripts/check_deployed_surface.py]
toolchain       : FRESH (/home/bunker-…/ci-tools/venv)
suite           : rc=0 · 2931 passed · 0 failed · 0 errors · 16 skipped (collected 2947) · 71s
                  summary: 2931 passed, 16 skipped in 71.20s
                  cmd: …/ci-tools/venv/bin/python -m pytest -q -p no:cacheprovider
                  log: /home/bunker-…/ci-full.log
VERDICT         : IDENTITY-OK / SUITE-PASS (exit 0)
=================================================================
```

One line before the suite starts is printed as soon as the deploy is measured,
so a caller that is killed mid-suite still knows which build the agent holds:

```text
bunker full-ci: sha=762935469111 cli=0.16.0 surface=ALIGNED toolchain=FRESH rc=never-ran tests=0p/0f/0e/0s verdict=SUITE-FAIL
```

### Exit codes

| code | verdict | meaning |
|---|---|---|
| `0` | `IDENTITY-OK / SUITE-PASS` | the run is evidence for that commit |
| `1` | `IDENTITY-OK / SUITE-FAIL` | the tested build is exact; its suite is red. Still a result |
| `3` | `IDENTITY-MISMATCH` | the run does **not** describe the requested build — not evidence |
| `2` | `INFRA` | no facts could be collected at all (spawn/ship/clone failure) |

`1` and `3` are deliberately different codes: a red suite is a result about the
build, a mismatch is the absence of one.

## A mismatched deploy reports it, loudly

Every identity disagreement is printed as a greppable `!! MISMATCH:` line that
names the fact that disagrees, and the run exits `3`:

```text
!! MISMATCH: the tested tree is 1e65720ae48a but 'HEAD' resolves to 762935469111 — the suite measured a different build than the one requested
!! MISMATCH: the deployed CLI (/usr/local/bin/gitreins, version 0.14.0) was NOT installed from the tested tree — the run would exercise different code than the clone
!! MISMATCH: CLI version drift: tree 0.16.0 vs deployed 0.14.0
!! MISMATCH: the deployed CLI surface does not match the tested tree (scripts/check_deployed_surface.py rc=1) (missing from the deployed CLI: qa, preflight)
!! MISMATCH: partial deploy: 49 of 386 tracked files landed on the agent (GAP-144 — assert the landed count before believing a timing)
!! MISMATCH: the shipped bundle landed short: 524288 of 5312820 bytes on the agent (GAP-144 — a partial scp leaves a tree that looks deployed)
!! MISMATCH: vacuous pass: pytest exited 0 with ZERO tests collected — a zero-test suite is not evidence about the build (QA-AUGER-1)
VERDICT         : IDENTITY-MISMATCH — this run is NOT evidence about the requested build (exit 3)
```

The classes are pinned as a fixture table, so the loud path is a tested
property rather than prose. Run it offline — no agent, no network:

```bash
scripts/bunker-full-ci.sh self-test
#   PASS  clean-pass-identity (exit 0)
#   PASS  wrong-tree-deployed (exit 3)
#   PASS  partial-deploy-49-of-290 (exit 3)
#   PASS  partial-bundle-ship (exit 3)
#   PASS  collected-but-summary-unreadable (exit 3)
#   PASS  stale-cli-mismatched-deploy (exit 3)
#   PASS  vacuous-zero-test-pass (exit 3)
#   PASS  red-suite-identity-exact (exit 1)
#   PASS  reused-toolchain-warns-not-fails (exit 0)
#   PASS  dirty-source-tree-is-named (exit 0)
#   PASS  suite-never-ran (exit 1)
# self-test PASS: 11/11 cases
```

Two warnings do **not** change the grade, because they do not invalidate the
identity — but they are printed where they cannot be missed:

* `!! WARN: toolchain REUSED …` — a venv that predates the run can carry a stale
  CLI; the identity's CLI/surface facts are what decide whether it did.
* `!! WARN: the SOURCE tree has uncommitted changes …` — the clone contains the
  *committed* tree of the tested sha, so working-tree edits are not part of what
  was tested. The `requested`, `tested tree`, and `source tree` lines together
  make that impossible to misread.

## The recipe works from a clean clone

Nothing here depends on machine state: the commit is bundled, shipped as one
file, cloned on the agent, and the toolchain is built inside the agent. After
cloning the repo anywhere, the preflight needs no toolchain at all:

```bash
git clone <repo> /tmp/gitreins-clean && cd /tmp/gitreins-clean
scripts/bunker-full-ci.sh dry-run      # no agent, no bunker CLI, no venv, no suite
scripts/bunker-full-ci.sh self-test    # the grade contract, offline
scripts/bunker-full-ci.sh run          # on a bunker
```

`dry-run` is covered by the test suite from a freshly initialised repo with a
single commit and the `bunker` CLI removed from `PATH`, so "works from a clean
clone" is a tested claim rather than an aspiration.

## Recording the result

The run records its own QA-ledger row through the repo's own API, so the
verdict survives the destroyed agent:

```bash
gitreins qa list --json | tail -1
```

The row is `kind=bunker`, carries the tested `commit`, the `agent`/`server`, and
`identity=OK|FAIL` + `suite=PASS|FAIL|UNVERIFIED` cells. The `note` carries the
counts, never a bare verdict word:

```text
bunker full-ci: sha=762935469111 cli=0.16.0 surface=ALIGNED toolchain=FRESH rc=0 tests=2931p/0f/0e/16s verdict=PASS
```

Recording never fails a run: if the ledger write fails, the run says so on
stderr and its grade is unchanged.

## Evidence

Everything lands in the evidence directory (printed at the end, and named in the
ledger row):

| file | contents |
|---|---|
| `facts.json` | the assembled identity facts — the input the grade is derived from |
| `identity.txt` | the printed identity block |
| `phase1.log`, `phase2.log` | the agent's raw output, `FACT` lines included |
| `repo.bundle` | exactly what was shipped (a `git bundle` of the tested ref) |

## Relationship to the standing harness

`~/.hermes/scripts/bunker-qa.sh` is the fleet's general fresh-system battery
(install / act / compose / chaos cells). It is **disabled** (2026-10-11) because
its `docker-deploy` cell re-chowns the host's `~/.hermes` and breaks the
gateway; its header names the fix required before re-enabling. It also runs the
suite without recording counts — the gap this lane closes for this repo.

This lane is deliberately narrower and safe to run at any time: no docker
lifecycle, no host state, one ephemeral agent that is destroyed on exit. It is
the repo-side implementation of the prototype in
`project-reviews/artifacts/subgoal-3-bunker-suite-hang-2026-09-23.md`.

## Limits (stated, not hidden)

* **Synchronous.** A full run is minutes (spawn + pip install + suite). Run it
  in the background; the identity beacon is printed before the suite so a killed
  caller still learns the build.
* **The agent needs the network** for `pip install -e <clone>` and the pytest
  plugins. A run that cannot install fails loudly at phase 1 with `INFRA` and
  the agent's log — it never silently tests an older installed CLI.
* **A dirty source tree is not deployed.** Only the committed tree of the tested
  ref is; the warning names it.
* **The suite's absolute result is environment-sensitive.** Optional tools
  (`ruff`, `pylsp`, `mypy`, `pyright`) may be absent on a bare agent, so counts
  can differ from CI on the same commit. The lane reports counts and the
  identity; it does not claim the counts equal the hosted runner's.
