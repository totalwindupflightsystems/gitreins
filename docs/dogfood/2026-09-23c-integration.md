# GitReins Dogfood — Run 9 (2026-09-23c): the Go guard lane

**Target:** `gitreins` (workdir `/home/kara/gitreins`, HEAD `beda743`)
**Angle:** the **Go guard lane** (`guards.go: build/lint/tests` → `go_build`,
`go_lint`, `go_tests`). Advertised in `README.md` ("Go projects (auto-detected
via go.mod)", the config reference) and implemented in `engine/guards.py`. Never
exercised by runs 1–8, which were all Python consumer repos (CLI, guard, judge,
PyPI wheel, MCP server, QA ledger, resolution gate, security-scan).
**Promise (null hypothesis):** *"On a Go project, `gitreins install`/`init` wires
a commit gate that compiles, vets and tests my Go code — and refuses a commit
whose Go code does not build."*
**Verdict:** 🟡 **PROMISING-BUT-ROUGH** — the lane's per-lane capability is real
and it does block an uncompilable **staged** change, but two false-green paths
make it untrustworthy as a gate exactly where a Go repo needs it.

---

## 1. The null hypothesis, tested for real

A real Go consumer project (`/tmp/dogfood-gitreins/go-app`, `example.com/quotasvc`):
a `quota` package with limits, a `cmd/quotasvc` CLI, and tests. `gitreins init`
detected it correctly on the first try — this part is genuinely good:

```
GitReins init: /tmp/dogfood-gitreins/go-app
  Language:    Go
  Packages:    2
  Test cmd:    go test -short -count=1 ./...
  Test mode:   full
  Eval cap:    15 iterations
  Static analysis: disabled (compiled language or explicitly off)
Updated: guards, evaluator, history, ... pre-commit hook, .gitleaks.toml, .gitignore
```

`init` writes the lane's own defaults, not an install baseline:

```yaml
guards:
  secrets: true
  lint: false        # Python lint — correctly off for a Go repo
  tests: false       # Python tests — correctly off
  test_mode: full
  go:
    build: true
    lint: true
    tests: true
  allow_skips: true
```

Time-to-first-success: **~1 min** (init + one guard run), friction 1 (see F4).
Headline timings (`hyperfine`, real repo, 2 files in scope): the full run
**822 ms ± 33 warm / 736 ms ± 8 whole-tree** — nothing a user feels. **No PERF
row filed**; there is nothing here worth a perf task.

## 2. What works (regression facts, all reproduced live)

| # | Probe | Result |
|---|---|---|
| G1 | `init` detects Go, writes `guards.go.*`, leaves Python lanes off | ✅ correct |
| G2 | staged **uncompilable** `.go` file → `go_build`/`go_lint`/`go_tests` all FAIL, exit 1, real compiler text named | ✅ blocks |
| G3 | same file, real `git commit` through the installed pre-commit hook → `COMMIT_RC=1`, HEAD unmoved | ✅ blocks |
| G4 | staged clean-in-scope change → clean PASS, exit 0 | ✅ |
| G5 | empty index → no crash, lanes report `No Go files staged`, exit 0 (`allow_skips: true`) | ✅ safe |
| G6 | no Go toolchain + Go files staged → lanes FAIL and the log says `error: [Errno 2] No such file or directory: 'go'` | ✅ correct, though the failure is misattributed (F5) |
| G7 | `guards.go.lint: false` really does drop the `go_lint` lane from the run (3 lanes → 2) | ✅ knob works |
| G8 | `guards.go.tests: false` really does drop the lane, exit code unchanged (build/lint graded independently) | ✅ knob works |
| G9 | the judge's own Tier-1 leg **does** catch a committed-uncompilable HEAD (its `tests` step shells the Go suite, output names `broken.go:3:28`) | ✅ judge is honest |
| G10 | installability on a bare Debian box (bunker) — clone, venv install, init, gate, commit all reproduced fresh | ✅ see §5 |

## 3. Findings

### F1 — P1 · `gitreins guard --full` grades only the index for the Go lanes → false PASS on a tree that does not compile

`docs/cli-reference.md:252` promises: *"A `--full` run does not produce skips on
a clean tree: the tests lane runs the configured `guards.test_command` …".* For a
Go repo, `--full` is silently narrower than a bare `guard`; the lanes' scope is
decided by the **index**, which `--full` exists to escape.

Repro on a fresh Go repo (`/tmp/dogfood-gitreins/go-f1`), one commit, empty
index, one untracked uncompilable file:

```bash
printf 'package quota\n\nfunc Broken() int { return "not an int" }\n' > internal/quota/broken.go
go build ./...                      # FAILS: cannot use "not an int" as int value
git status --short                  # ?? internal/quota/broken.go
gitreins guard --full               # → Tier 1 Guards: PASS  (test mode: full, whole tree)
```

The run log's tell, verbatim, for all three lanes (`overall: PASS`,
`guards: 4 (0 failed, 0 skipped)`):

```
[PASS] go_build  passed=true  exit_code=n/a
--- output (untruncated) ---
No Go files staged
```

Root cause: the lanes are gated on "is there a `.go` file in my scope", and the
scope is `git diff --cached` (`engine/guards.py:82-100` — `_changed_go_files`
falls back to its own staged discovery whenever the caller passes `None`, and
`GuardManager._scope_files_or_none()` at `engine/guard_manager.py:1037` passes
`None` for every scope except `working-tree`). With an empty index there are no
staged `.go` files, so all three lanes return `passed=True` with
`output="No Go files staged"` **before any tool runs** — and `--full`, which did
populate `self.changed_files` with the whole tree, never hands that set over.

Two aggravators, both proven on the same repo:

* **A committed broken HEAD passes too.** Commit `broken.go`, leave the index
  clean, run `--full` → `Tier 1 Guards: PASS (test mode: full, whole tree)`
  while `go build ./...` fails. (Once *any* `.go` file is staged, the lanes run
  `go build ./...` / `go vet ./...` / `go test ./...` — global commands that
  would have caught it. The gate is a coin flip on index emptiness.)
* **Guard and judge disagree on the same tree.** The judge's Tier-1 `tests` step
  does see the broken file (verdict `45186e59`: `internal/quota/broken.go:3:28
  ... FAIL [build failed]`, exit 1). Same class as the resolved POC-12/POC-16
  pair, on a new lane.

Workaround that works today: `gitreins guard --scope working-tree` (correctly
grades modified + untracked `.go` files, FAILs as it should). The flag is
undocumented in README/AGENTS — found via `--help` after reading source to
explain the PASS.

**POC-42**

### F2 — P1 · the Go `go_lint` lane treats **every** golangci-lint finding as "linter unavailable" and falls through to a weaker tool → PASS on real lint errors

`engine/guards.py:111-147`:

```python
result = run_bounded(["golangci-lint", "run", "--new-from-rev=HEAD~1", *go_files], ...)
if result.get("exit_code") == 0:
    return GoGuardResult(name="go_lint", passed=True, output="golangci-lint: clean")
# Fall through to go vet on failure
result = run_bounded(["go", "vet", "./..."], ...)     # ← and its verdict IS the lane's verdict
```

The fallback is meant for a **missing binary**. It also fires when
golangci-lint *did its job*: a non-zero exit caused by **findings** is treated
identically to an absent linter, and `go vet` — which has no errcheck-class
analysers — then answers `clean` and the lane reports `passed=True`.

A/B on one repo, one file (`internal/quota/lint_probe.go`: compiles, passes
`go vet`, ignores an `os.Mkdir` error), HEAD~1 present so the revision is valid:

```
$ golangci-lint run --new-from-rev=HEAD~1 internal/quota/lint_probe.go
internal/quota/lint_probe.go:7:10: Error return value of `os.Mkdir` is not checked (errcheck)
1 issues:
* errcheck: 1                                     ← exit 1, no "bad revision" noise

$ go vet ./...                                    ← clean
$ gitreins guard --scope working-tree             ← Tier 1 Guards: PASS
  ✓ go_lint — ok                                  ← console line: "the linter passed"
  (run log: "go vet: clean")
```

Across 28 captured `go_lint` results in this run: **15 fell back and were graded
by `go vet` alone**, 9 never ran (F1), 2 genuinely ran golangci-lint and found
nothing, 2 correctly FAILed. So in practice the lane is a `go vet` lane, and the
console never says so — `engine/types.py:88` renders `go_lint` as `— ok`, the
same silent-degradation shape as the resolved POC-15 scanner-attribution defect,
on a new lane. Since `go_build` already fails on type errors, the lane as shipped
adds almost nothing.

Second, unproven-but-named aggravator: `--new-from-rev=HEAD~1` cannot resolve in
a single-commit repo (golangci-lint prints `fatal: bad revision 'HEAD~1'` and
disables its diff processor); that warning alone does not change the exit code
(a repo with no issues and no `HEAD~1` still reports `golangci-lint: clean`), so
it is a sharpener, not the bug.

The `lint: false` knob works (G7) — a user can turn the lane off — but cannot
tell from the green summary that it is a vet-only lane.

**POC-43**

### F3 — P2 · the whole-tree "grade everything" promise is unbacked: the DEGRADED-PASS safety net does not cover the Go lane names

`engine/types.py:44` — `_SUBSTANTIVE_STEPS = frozenset({"lint", "tests", "lsp"})`:
only those step ids arm `degraded`. A Go repo's gates are named `go_lint`,
`go_tests`, `go_build`, so a run where a Go lane did no work is **not** degraded,
never prints the DEGRADED PASS header, and exits 0 under `allow_skips: false`
too. Observed: `Tier 1 Guards: PASS  (test mode: full, whole tree)` with
`guards: 4 (0 failed, 0 skipped)` and zero files graded (F1's log) — the exact
failure mode TRUST-001/the degraded machinery exists to make impossible for the
Python lanes, unguarded for the Go ones.
**POC-44**

### F4 — P2 · `gitreins init` prints a Go test command it never writes, and the Go lane never reads the key it claims to use

`init` prints `Test cmd: go test -short -count=1 ./...` and the guard docs talk
about "the configured `guards.test_command`", but `init` writes **no**
`test_command` key for a detected Go project (verbatim `guards:` block in §1;
the Python path does write one), and the Go lane hard-codes
`["go", "test", "-count=1", "-short", "./..."]` (`engine/guards.py:168-173`) —
`guards.test_command` is read by the Python tests lane only. Config and code
disagree about where the Go test invocation comes from, so a Go user's
customization is silently ignored (`go test ./...` runs instead). Low user
impact today (the hard-coded argv is the sane default); the printed promise is
what is wrong.
**POC-45**

### F5 — P2 · a missing Go toolchain is reported as a code failure with no reason on the console

`engine/guards.py:204-206` surfaces the spawn error as `error=result["error"]`:
the run log reads `error: [Errno 2] No such file or directory: 'go'`, while the
console prints a bare `✗ go_build` / `✗ go_lint` / `✗ go_tests` with no
reason line (`GuardResult.error` does not reach `summary`). The lane therefore
fails a commit on a Go repo whose toolchain is simply not on `PATH`, with no
diagnosis, while the Python side solved this class deliberately
(`_resolve_test_command` GR-GAP-037: a missing runner names the fix;
`_pytest_not_found_hint` names the interpreter). On a fresh box (no Go) this is
the difference between "your code is broken" and "install Go".
**POC-46**

## 4. Friction log (user side, counted)

1. `--full` looks like the strong mode and is the weakest one for Go (F1) —
   cost one debug cycle with `--scope working-tree` to find the working form.
2. `guard --help` is the only place `--scope` is explained; README/AGENTS never
   mention it. Reading source (`engine/guards.py`) was required to explain why.
3. A green `✓ go_lint — ok` is not attributable to a linter (F2).
4. `gitreins init` output says "Test cmd: …" for a key it did not write (F4).
5. Missing toolchain failure is a bare ✗ (F5).

**Friction count: 5 (all 5 inside the documented flow).** Not one of them is a
crash; all five are the product telling the user something that is not true.

## 5. Install leg — ephemeral bunker (las-bunker-03)

Agent `2db38df6` spawned (`bunker spawn --server bunker-las-03 --ttl 2h`),
used, `bunker destroy` → `Agent 2db38df6 destroyed.` and `bunker list | grep -c`
returns 0 (verified gone). Bunker CLI 0.1.3.

* **README quickstart `pip install gitreins` fails on fresh Debian 13** —
  `PIP_RC=1`, PEP-668 `externally-managed-environment`. Same known finding as
  earlier runs (workaround: a venv). Reported here for the record, not filed
  again.
* venv install of the sdist from the repo: **~24 s** to a working `gitreins`
  console (`gitreins --version` OK), then `gitreins init` on the Go repo.
* Clone inside the bunker with existing (public) access: OK, `HEAD=beda743`.
  No repo visibility/permission change was made anywhere in this run.
* The box has **no Go toolchain and no golangci-lint** — real fresh-user state.
  With a genuinely broken `.go` file staged: **`✗ go_build` / `✗ go_lint` /
  `✗ go_tests`, exit 1, commit refused (COMMIT_RC=1, HEAD unmoved)**; the log
  names the `Errno 2` cause, the console does not (F5).
* With the same broken file unstaged: **`Tier 1 Guards: PASS (test mode: full,
  whole tree)`** — F1 reproduced on the pristine box, not just on the dev host.
* gitleaks is absent there and the guard says so loudly on stdout, with the
  built-in cross-check as fallback — that path behaves exactly as documented.

## 6. Working configuration (copy-paste)

```yaml
# .gitreins/config.yaml — Go project, gate that actually grades the tree
guards:
  secrets: true
  lint: false            # Python lint — leave off for Go
  tests: false           # Python tests — leave off for Go
  test_mode: full
  allow_skips: false     # optional: make a no-work run exit 2 instead of 0
  go:
    build: true
    lint: true           # runs go vet; golangci-lint only if HEAD~1 resolves (F2)
    tests: true
```

```bash
# Grade what is on disk, not just what is staged (F1 workaround):
gitreins guard --scope working-tree

# Grade only the index (the README default — what the pre-commit hook does):
gitreins guard
```

## 7. Diagnostic trail

`docs/dogfood/diagnostics.md` (09-23c section): how the Go lane is wired
(`run_all` gate order, `_scope_files_or_none`, `_changed_go_files`' two-branch
discovery, `run_bounded`'s sanitized env and group reap), why the `--full` scope
stops at the index, why golangci-lint can never win its branch, and the working
`--scope working-tree` path.

## 8. Artifacts left behind

* `docs/dogfood/2026-09-23c-integration.md` (this file)
* `docs/dogfood/diagnostics.md` — 09-23c section
* `docs/dogfood/evidence/go-lane-2026-09-23c/` — raw run logs for every claim
* `skills/gitreins-usage/SKILL.md` v1.6.0 — Go lane section + pitfalls 29–33
* board rows `DF-GITREINS-POC-42` (P1), `-43` (P1), `-44` (P2), `-45` (P2),
  `-46` (P2)

No code was changed. Foreman not woken; cooldowns untouched per the 2026-09-09
fleet law.
