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
(install / act / compose / chaos cells). Its `docker-deploy` and `chaos-shutdown`
lifecycle cells are **opt-in** (`BUNKER_QA_ENABLE_DOCKER_LIFECYCLE=1`) because
`docker compose down/stop/kill` can affect an existing stack — that is the
2026-10-11 shape, not a disabled harness. It also runs the suite without
recording counts — the gap this lane closes for this repo.

This lane is deliberately narrower and safe to run at any time: no docker
lifecycle, no host state, one ephemeral agent that is destroyed on exit. It is
the repo-side implementation of the prototype in
`project-reviews/artifacts/subgoal-3-bunker-suite-hang-2026-09-23.md`.

**Teardown is repo-side too (DF-GITREINS-POC-101).** The battery's own
`dbus-daemon` and `sshd` sessions run under the disposable agent uid and outlive
the suite, and `bunker destroy` refuses to remove an agent that still has live
sessions — so the EXIT trap would leave the agent behind on every graded run.
`stop_agent_daemons()` (in `scripts/bunker-full-ci.sh`, called from `cleanup()`
*before* `bunker destroy`) resolves the disposable uid with `id -u` on the agent
and sends `pkill -TERM -u <uid> -x dbus-daemon` / `-x sshd` as that uid; an
unresolved, non-numeric or root uid is refused rather than signalled. The
regression pin is
`tests/test_bunker_identity.py::test_teardown_stops_agent_daemons_before_destroy`,
which extracts that shell function from the tracked harness and drives it with a
stubbed `bunker`, so the fix is reproducible from a clone with no host-side
script involved.

## Chaos cells: the missing-config row (chaos-errorpath)

The identity lane above answers *"did the suite pass on the build I asked for"*.
The fleet battery answers a different question — *is the product brittle on a
clean machine* — with the cells `fresh-install`, `ci-pass`, `docker-deploy`,
`ui-probe`, `upgrade`, `chaos-disconnect`, `chaos-shutdown`,
`chaos-corruption`, `chaos-resource` and `chaos-errorpath`. This repo owns the
last one's **premise** through the battery's opt-in project-probe hook.

### The spec row

With a repo present but `.gitreins/config.yaml` **absent**, a command that
requires that config must print a message naming the missing file (or a usage
banner) and exit **non-zero, promptly**. A hang, a crash, or a silent `rc=0` is
a defect, not an error path:

```text
$ cd "$(mktemp -d)" && git init -q . && gitreins guard
no .gitreins/config.yaml — run `gitreins init` first
$ echo $?
1
```

### The repo's probe

`.qa/chaos-probes/errorpath.sh` (executable, run from the repo root by the
battery). The harness consults it only when it exists and is executable; when
it does, it replaces the generic missing-config guess for this repo. Its exit
code *is* the cell grade:

| exit | grade | meaning |
|---|---|---|
| `0` | OK | the documented clean error was observed; line 1 carries `rc=` |
| `1` | FAIL | hang / crash / silent `rc=0` / a message that names nothing |
| `2` | INFO | environment gap — no CLI to probe, so nothing was observed |
| `124` | FAIL | the probe itself hung past the harness's own 120 s wrap |

It bounds each command itself (`QA_EP_TIMEOUT`, default 20 s) and isolates
`HOME`, so a hang is **classified** (exit 1 with a reason) instead of arriving
at the harness as the ambiguous `rc=124` that reads as "unverified". It probes
`guard` — a command that needs the config — and never `serve`, which is
long-running by design. Re-run it locally, with no agent:

```bash
.qa/chaos-probes/errorpath.sh; echo "probe rc=$?"
# clean error on missing config: '…/gitreins guard' -> rc=1, names the missing config (no .gitreins/config.yaml — run `gitreins init` first)
# probe rc=0
```

### Why the generic arm could not answer this (QA-GITREINS-2, 2026-10-23)

The generic probe discovers a start command from `gitreins --help` and preferred
long-running subcommands (`serve|server|daemon|start`). `gitreins serve` is the
judgment browser: it needs no config at all, announces its port, keeps serving —
so the probe could only ever end at its own bound:

```text
{"cell": "chaos-errorpath", "status": "INFO",
 "detail": "missing-config start probe timed out (rc=124) — error-path behavior UNVERIFIED: "}
```

A command that is *supposed* to stay up has no missing-config error path to
reach, so that timeout said nothing about the product at all. Two things changed:
this probe (above), and the harness calibration that trials a daemon candidate
under a short bound and drops it when it runs happily without config — so the
same artifact cannot reappear for another repo.

The regression pin is `tests/test_qa_chaos_probe.py`: the four documented
product shapes, the hang arm (exit `1`, never `124`), the environment-gap arm,
`HOME` isolation, and one arm that runs this repo's real console script so a
change to the missing-config message fails the suite instead of drifting.

## The fresh-install row's artifact identity (DF-GITREINS-POC-103)

The battery's `fresh-install` cell decides `OK` vs `FAIL` by looking for an
artifact after a successful install. Its generic detector searched PATH,
`PREFIX/bin` and `~/.local/bin` for **one hard-wired name — another product's
console script** — so this repo's genuinely-successful install graded FAIL on
2026-10-26 (agent `339cb0f1`):

```text
{"cell": "fresh-install", "status": "FAIL",
 "detail": "install rc=0 but no artifact found (toolsd not in PATH, PREFIX/bin/toolsd missing, or zero-size); Successfully installed ... gitreins-0.17.1 ..."}
```

The transcript inside the same failure row proves the install landed; the
detector just looked for the wrong product's artifact. This repo owns that
identity through the same opt-in hook pattern as the chaos cells:
`.qa/probes/fresh-install.sh` (executable, run from the repo root). It derives
the expected console script from the repo's OWN pyproject metadata
(`[project] name` + the first `[project.scripts]` entry — `gitreins`, the
console script `pip install .` really creates), locates it on PATH /
`PREFIX/bin` / `~/.local/bin`, and asserts it is a non-empty executable whose
`gitreins --version` exits `0`. It never consults a foreign artifact name, so
an unrelated project's binary on PATH can never satisfy this cell, and it
refuses to grade any repo whose pyproject does not declare `gitreins` (a
project-mismatch FAIL, not a silent pass). Failure output names the project,
the expected artifact, and — when the harness exports it — the install
transcript path (`QA_FRESH_LOG`). Exit code = cell grade: `0` OK (line 1 names
the artifact and the version output), `1` FAIL. Re-run it locally, no agent:

```bash
.qa/probes/fresh-install.sh; echo "probe rc=$?"
# fresh-install OK: artifact=…/bin/gitreins (… bytes); gitreins --version → gitreins 0.17.1
# probe rc=0
```

The regression pin is `tests/test_qa_fresh_install_probe.py`: the identity pin
against the live `[project.scripts]` declaration, green with a fake installed
console script while `toolsd` sits unused on the PATH, the missing-artifact
FAIL that names `gitreins` (never the foreign name), and a `--version` failure
that fails the cell even though the install itself returned `0`.

*Boundary, stated:* the generic detector lives in the fleet harness
(`bunker-qa.sh` — host-side, not this repository), and the harness-side
consultation of `.qa/probes/` is the fleet change that must follow. Until it
does, this tracked probe + test are the normative identity contract for this
repo's `fresh-install` row: a fresh-install FAIL whose message names a foreign
artifact is a harness defect, not a product one, and this file is the
specification to fix the harness against.

## The fresh-host clone row (DF-GITREINS-POC-87)

`fresh-install` answers "does the product install on a clean machine". It cannot
answer a question that comes *before* the install: **can a fresh user even get the
code?** This repo's own origin is SSH, and a brand-new box has no `github.com`
entry in `~/.ssh/known_hosts`, so the clone the docs implied dies immediately:

```text
$ git clone git@github.com:totalwindupflightsystems/gitreins.git
Host key verification failed.
fatal: Could not read from remote repository.
# rc=128, 3s   (dogfood 2026-10-09 run 18, bunker leg)
$ git clone https://github.com/totalwindupflightsystems/gitreins.git
# rc=0, 3s — the public https path needs no key at all
```

Neither failure is a product bug, so the fix is a signpost: README.md now states
both ways out (`ssh-keyscan github.com >> ~/.ssh/known_hosts` before an ssh
clone, or use the https origin). The signpost is only worth what it is checked
against, so this repo ships a probe for it:

| probe | row | what it asserts |
|---|---|---|
| `.qa/probes/fresh-install.sh` | `fresh-install` | a successful install left THIS repo's console script behind (never a foreign artifact name) |
| `.qa/chaos-probes/errorpath.sh` | `chaos-errorpath` | a command that needs the config prints an actionable error and exits non-zero, promptly |
| `scripts/check_fresh_host_clone.sh` | DF-GITREINS-POC-87 (standalone) | an isolated fresh HOME makes the ssh arm fail on host keys **and** the https arm clone successfully |

### The probe contract

`scripts/check_fresh_host_clone.sh` (executable, run from anywhere — it uses a
throwaway `HOME` and `mktemp` scratch, never the caller's state). Its exit code
*is* the grade:

| exit | grade | meaning |
|---|---|---|
| `0` | OK | both arms reproduced: ssh fails with `Host key verification failed`, https clone exits `0` and leaves a real clone (`.git` present) |
| `1` | FAIL | a documented path does not behave as the README says — the ssh arm succeeded (the probe is not isolating what it claims), failed for a *different* reason, or the https clone failed while the host was reachable |
| `2` | INFO | environment gap — no `git`, no answer from the network, so nothing was observed (never reported as a pass) |

Isolation is what makes the result mean "fresh host" rather than "the operator's
box": throwaway `HOME` (no `known_hosts`, no `~/.gitconfig`), `ssh -F /dev/null`
with `GlobalKnownHostsFile=/dev/null`, `BatchMode=yes` as the deterministic
stand-in for a user who cannot be prompted, `SSH_AUTH_SOCK`/`GIT_SSH`/`GIT_SSH_COMMAND`
neutralised, `GIT_CONFIG_NOSYSTEM=1` so a system `url.*.insteadOf` rewrite cannot
turn the ssh arm into an https one. The ssh arm runs first on purpose: its
host-key failure proves the host is reachable, which is what makes a subsequent
https failure a FAIL instead of a network gap.

```bash
scripts/check_fresh_host_clone.sh; echo "probe rc=$?"
# fresh-host-clone arm1 OK: ssh clone without known_hosts fails on host keys (rc=128, 0s)
# fresh-host-clone arm2 OK: https clone succeeded (rc=0, 2s, top-level entries=41, HEAD=…)
# probe rc=0
```

The regression pin is `tests/test_fresh_host_clone_probe.py`: the shipped probe
is executable and drives a STUB git through `QA_FHC_GIT` (no network in the
suite), pinning OK / FAIL / INFO grading, plus a docs gate that fails if
README.md stops naming **both** options (https clone URL and `ssh-keyscan
github.com >> ~/.ssh/known_hosts`) — the drift that opened this row.

*Boundary, stated:* this is a standalone probe, not a fleet-battery cell — the
battery has no clone cell to hook into, and the same property is already
exercised end-to-end by the `fresh-install` cell's own clone step on a real
agent. If a clone cell is added to `bunker-qa.sh`, this script is the
implementation to consult.

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
