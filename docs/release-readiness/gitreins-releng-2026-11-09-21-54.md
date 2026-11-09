# GitReins release readiness — 2026-11-09 21:54 tick

Target: `totalwindupflightsystems/gitreins` (canonical remote `github`), default branch `main`.
Tick: `gitreins-releng-2026-11-09-21-32-53` (second firing). This pass is a **delta re-verification**
of the cut recorded in `docs/release-readiness/gitreins-releng-2026-11-09-21-32.md`; it does not
perform a second cut.

## Decision

**NO NEW RELEASE — nothing released this pass.** `v0.21.0` was published 15 minutes before this
firing (GitHub Release `2026-11-09T21:39:20Z`) by the tag-triggered workflow, so the correct action
is verification, not a second cut. No tag was created, moved, or force-updated.

Delta since the certified release SHA `8ea9088f535151a87f12445c7bf5549bf5197432` is **2 commits**
(`5fa2b448`, `5cbde06a`), both board/readiness-evidence only — the shipped diff over
`v0.21.0..main` touches `.coding-hermes/board/*` and `docs/release-readiness/*` and no product path,
so releases rule 3 (no material feat/fix) makes a further cut NOT DUE.

## Evidence table

| Surface | Result | Evidence |
|---|---|---|
| Repo identity | PASS | `/home/kara/gitreins`; remote `github` = `git@github.com:totalwindupflightsystems/gitreins.git`; branch `main`; working tree clean; `HEAD` = `5cbde06a67c2cd2b66226a0e39e7f34a064238bf` = `refs/remotes/github/main`. |
| Tag immutability | PASS | `git ls-remote github refs/tags/v0.21.0` → object `34a4c4d3174004b1db8c880dbbe0f482a2f5a6d3`, peeled `8ea9088f535151a87f12445c7bf5549bf5197432` — byte-identical to the object recorded at cut time. No force-move. |
| Version at tagged tree | PASS | `git show 8ea9088f:pyproject.toml` → `version = "0.21.0"`; `CHANGELOG.md` carries one dated `## [0.21.0] - 2026-11-09` section under an empty `## [Unreleased]`. |
| GitHub Release | PASS | `gh release view v0.21.0` → `isDraft=false`, `isPrerelease=false`, published `2026-11-09T21:39:20Z`, marked Latest. Asset count 0 is by design: PyPI is the documented artifact surface. |
| PyPI artifact | PASS | `pypi.org/pypi/gitreins/json` → `info.version = 0.21.0`; `gitreins-0.21.0-py3-none-any.whl` (617 540 B) and `gitreins-0.21.0.tar.gz` (1 344 936 B) both served. |
| CI on tagged SHA | PASS | Run `42648256457` (workflow `CI`, push) completed/success on exact SHA `8ea9088f…`; Python 3.10/3.11/3.12 jobs green. |
| Publication run | PASS | Run `42704426258` (workflow `Release`, push on tag ref) completed/success on `8ea9088f…` — the sole publisher; no manual `gh release create` in this pass. |
| CI on current main tip | PASS | Run `42705921756` (workflow `CI`, push) completed/success on exact SHA `5cbde06a…`. |
| Candidate-bound GitReins verdicts | **CORRECTION: PRESENT, AND RED** | Three committed verdicts bind `8ea9088f…` under `.gitreins/history/2026-11-09/`: `9c10cd50` (15:36:21Z), `975a0de1` (15:48:33Z), `a534a995` (15:54:10Z), all `pass_class=judged`, `passed=false`, `working_tree_dirty_path_count=0`, task `RELEASE-020-R2`. The 21:32 artifact recorded this surface as UNVERIFIED ("no committed verdict found bound to the certified prep SHA"); that statement was wrong and is corrected here. |
| Verdict FAIL classification | ENVIRONMENTAL, NOT SOURCE | All three FAILs are tier-1 lane failures, not criteria failures: `tests/test_guard_go_lanes.py::TestFullTreeGoScope` (`failed to initialize build cache`) and `tests/test_version.py::test_gitreins_module_reexports_version`. Root cause reproduced on this host: `~/.cache/go-build -> /mnt/usb512/build-artifacts/go-build`, and `/mnt/usb512` is **not mounted** (empty dir), so the symlink is dangling and every `go build`/`go test` in a guard run dies at cache init. Hosted CI — the authoritative full gate — is green at the same SHA. See RELEASE-022. |
| Heartbeat blocker (RELEASE-019) | CLEARED | The `tests/test_job_store.py::test_heartbeat_keeps_a_long_job_live` failure that held RELEASE-019 open no longer reproduces: `1 passed in 5.18s` on the current tree's venv, and exact-HEAD CI `42705921756` is fully green across 3.10/3.11/3.12. RELEASE-019 is closed as superseded — `v0.20.0` and `v0.21.0` both shipped after it was filed and it now only re-dispatches a stale verdict. |
| Mirror parity | PASS | `python3 scripts/sync_gitlab_mirror.py --check` → `GITLAB MIRROR IN SYNC (content; 13 mirror-side merge commit(s))`, exit 0. Mirror tags stop at `v0.15.0`, but the documented contract in `CONTRIBUTING.md` is content parity of `main`, not tag mirroring, and `main` satisfies it. |
| Deployed binary | OPEN (RELEASE-021) | `scripts/check_deployed_surface.py` → `VERSION DRIFT: repo 0.21.0 vs deployed 0.20.0`, `DRIFT DETECTED`. `~/.local/bin/gitreins --version` = `gitreins 0.20.0`; pipx package `gitreins 0.20.0`. Subcommand count matches (25/25), so this is version drift only, not a missing surface. RELEASE-021 remains open and is the foreman's to execute. |
| Release tooling | PASS | `.github/workflows/release.yml` remains the sole publisher (tag↔`pyproject` verification, wheel/sdist build, PyPI upload, GitHub Release). `CONTRIBUTING.md` documents the serialized cut plus the deployed-surface checklist gate. No Makefile release target, Goreleaser, Dockerfile, or `scripts/release*` publisher exists — nothing to prefer over the workflow. |

## Notes for the foreman

- **RELEASE-019 → complete (superseded).** Its premise (v0.19.0 not due, exact-HEAD CI red on
  `ecf4ba4`, heartbeat test failing) is two releases stale; the heartbeat test passes at HEAD and the
  exact-HEAD CI that it asked for is green.
- **RELEASE-021 → unchanged, still open.** Deployed pipx binary is 0.20.0 against PyPI 0.21.0. Fresh
  probe evidence is recorded above; the row's acceptance criteria still stand.
- **RELEASE-022 → new, P1.** Dangling `GOCACHE` symlink on this host turns clean-source tree
  guard runs into tier-1 FAILs, which is what manufactured the three RED verdicts bound to the
  release SHA. Any future candidate-bound verdict taken on this host is worthless until it is fixed.

## Boundaries of this pass

Read-only against the published surfaces (git, `gh`, PyPI JSON, mirror check). The only writes are
this artifact, the board rows named above, and their events. No tag, no Release edit, no PyPI
upload, no host mutation — including no repair of the dangling `GOCACHE` symlink.

## Pass 3 — third delivery of the same tick (`2026-11-09T22:07Z`)

**Decision unchanged: NO NEW RELEASE, no tag created or moved.** This delivery re-derived every
claim above independently rather than trusting the committed record.

Delta since the pass-2 snapshot (`5cbde06a`) is one commit, `2b04dfc4` — the pass-2 evidence commit
itself (`releng: verify v0.21.0 publication, close RELEASE-019, file RELEASE-022`). Range
`v0.21.0..HEAD` is 3 commits, all board/readiness bookkeeping; `git diff --stat v0.21.0..HEAD`
excluding `.coding-hermes/board` and `docs/release-readiness` is **empty**, so releases rule 3 keeps
a further cut NOT DUE.

| Re-check | Result | Evidence |
|---|---|---|
| Worktree / tip | PASS | clean tree; `HEAD` = `github/main` = `2b04dfc4156485c510034abab2f85cd7d0d284f2` |
| Tag immutability | PASS | `ls-remote` → object `34a4c4d3…`, peeled `8ea9088f…` — identical to cut time; no `v0.21.1` exists |
| GitHub Release | PASS | `isDraft=false`, `isPrerelease=false`, published `21:39:20Z`, assets 0 (PyPI is the artifact surface) |
| PyPI artifacts | PASS | `0.21.0` wheel `318360f0…` 617 540 B, sdist `f3aa7893…` 1 344 936 B — digests byte-identical to cut time |
| CI on tagged SHA | PASS | run `42648256457` success on `8ea9088f…` |
| Publication run | PASS | run `42704426258` success on tag ref — sole publisher |
| CI on current tip | PASS | run `42707702262` success on exact `2b04dfc4…` (3.10/3.11/3.12) |
| RELEASE-021 (deployed binary) | STILL OPEN | `gitreins --version` → `gitreins 0.20.0`; PyPI serves `0.21.0` |
| RELEASE-022 (GOCACHE) | STILL OPEN | `go env GOCACHE` → `~/.cache/go-build` → `/mnt/usb512/build-artifacts/go-build`; `/mnt/usb512` exists but is an empty, unmounted dir |

No further committed evidence is warranted for this firing beyond this block; the next material
delta is a fix landing for RELEASE-021 or RELEASE-022, not another re-verification.

## Pass 4 — current re-verification (`2026-11-09T22:19Z`)

Decision unchanged: NO NEW RELEASE. The certified current tip is `a3d9ba067a3b28dc76255336195f281884dfa6df`; it is four commits after `v0.21.0`, and every in-range commit is release bookkeeping (`releng:` or `foreman:`). The shipped-source diff from `v0.21.0` to the tip is empty, so releases rule 3 applies. No tag was created, moved, or force-updated.

| Re-check | Result | Evidence |
|---|---|---|
| Identity and convergence | PASS | `/home/kara/gitreins`; remote `github` = `git@github.com:totalwindupflightsystems/gitreins.git`; `github/main`, `ls-remote`, and GitHub API all converge on `a3d9ba067a3b28dc76255336195f281884dfa6df`; parity `0 0`; clean checkout. |
| Commit census | NOT DUE | `v0.21.0..a3d9ba06` = 4 commits, 4 non-merge; `feat=0`, `fix=0`, `perf=0`, `chore=0`, `docs=0`, `refactor=0`, `test=0`, `other=4`; no `BREAKING CHANGE:` footer or bang marker. Changed paths are board/readiness evidence only. |
| Exact-tip CI | PASS | Run `42709505586`, completed/success on full SHA `a3d9ba067a3b28dc76255336195f281884dfa6df`; jobs `test (3.10)`, `test (3.11)`, and `test (3.12)` all completed/success. |
| Prior release | PASS | `v0.21.0` remains immutable: annotated tag object `34a4c4d3174004b1db8c880dbbe0f482a2f5a6d3` peels to `8ea9088f535151a87f12445c7bf5549bf5197432`; GitHub Release is published, non-draft, non-prerelease; PyPI serves the wheel and sdist. |
| Release tooling/version | PASS | Tag-triggered `.github/workflows/release.yml` is the sole publisher; current `pyproject.toml` version is `0.21.0`; no Makefile release target, Goreleaser config, Dockerfile, or `scripts/release*` publisher exists. |
| GitReins verdicts | UNVERIFIED for current tip; environmental REDs for tag | No committed verdict is bound to `a3d9ba06`. Three judged `passed=false` verdicts (`9c10cd50`, `975a0de1`, `a534a995`) bind the prior release SHA `8ea9088f`; their tier-1 failures are attributed to the dangling `GOCACHE` symlink and remain covered by RELEASE-022, not treated as source failures. |
| Dogfood/QA | MIXED and stale for this candidate | The latest tracked clean-machine install evidence has install/lint/tests/resource/error-path cells OK/PASS, with `3625 passed, 32 skipped`; `ci-act` is UNVERIFIED because the local image lacks GitHub toolcache paths, and `chaos-disconnect` is UNVERIFIED because no network-cut marker exists. It is not a waiver for a future material cut. |
| Deployed surface | OPEN | `/home/kara/.local/bin/gitreins --version` and pipx metadata still report `gitreins 0.20.0`; PyPI and the repository report `0.21.0`. RELEASE-021 remains the canonical follow-up. |
| Public posture | MIXED | Public MIT repository; secret scanning and push protection enabled; Dependabot security updates disabled; `main` branch protection returns 404 (unprotected). |
| Open blockers | OPEN | RELEASE-021 (stale deployed binary) and RELEASE-022 (unusable host GOCACHE) remain pending. Neither changes the source-release decision for this bookkeeping-only delta. |

This pass adds no new RELEASE-* row: the two actionable findings already have canonical pending rows, and a duplicate would create foreman work without a new acceptance contract. The evidence is appended here rather than replacing prior passes.

## Pass 5 — current re-verification (`2026-11-09T22:33Z`)

Decision unchanged: NO NEW RELEASE. The certified tip is `cd696f8e048e2ce2910300fc246bb7f3892d9a30`, converged across `github/main`, `git ls-remote`, and the GitHub API. The five commits in `v0.21.0..cd696f8e` are all `releng:`/`foreman:` bookkeeping; `feat=0`, `fix=0`, `perf=0`, `chore=0`, `docs=0`, `refactor=0`, `test=0`, `other=5`, with no `BREAKING CHANGE:` footer or bang marker. The shipped-source diff is empty. Releases rule 3 therefore says NOT DUE. No tag was created, moved, or force-updated.

| Re-check | Result | Evidence |
|---|---|---|
| Identity and convergence | PASS | `/home/kara/gitreins`; canonical remote `github` = `git@github.com:totalwindupflightsystems/gitreins.git`; `github/main`, `ls-remote`, and `gh api repos/totalwindupflightsystems/gitreins/commits/main` all report `cd696f8e048e2ce2910300fc246bb7f3892d9a30`. The carrier checkout is one commit behind and was not used for evidence. |
| Commit census | NOT DUE | `v0.21.0..cd696f8e` = 5 non-merge commits; all five are release/foreman bookkeeping. `git diff --name-only v0.21.0..cd696f8e -- . ':(exclude).coding-hermes/board' ':(exclude)docs/release-readiness'` is empty. |
| Exact-tip CI | PASS | Run `42711089908`, workflow `CI`, completed/success on exact full SHA `cd696f8e048e2ce2910300fc246bb7f3892d9a30`; jobs `test (3.10)`, `test (3.11)`, and `test (3.12)` all completed/success. |
| Prior release | PASS | Remote `v0.21.0` remains annotated object `34a4c4d3174004b1db8c880dbbe0f482a2f5a6d3`, peeled to `8ea9088f535151a87f12445c7bf5549bf5197432`; GitHub Release is non-draft/non-prerelease; PyPI serves wheel `318360f03d24a2145d58853016aca943b05fef0533a04e18ad5c3a3e11986ce7` (617540 bytes) and sdist `f3aa78933cb5a8cd37dd2df407cc4b0715c5acd5a0c6014b7fe967edfbece34f` (1344936 bytes). |
| Release tooling/version | PASS | `.github/workflows/release.yml` is the sole publisher and verifies tag↔`pyproject.toml`, builds wheel/sdist, uploads PyPI, and creates the GitHub Release. `pyproject.toml` remains `0.21.0`; no Makefile release target, Goreleaser config, Dockerfile, or `scripts/release*` publisher exists. |
| GitReins verdicts | UNVERIFIED for current tip | No committed verdict binds `cd696f8e`; the three prior RED verdicts bind `8ea9088f` and remain environmental under RELEASE-022, not evidence for this tip. |
| Dogfood/QA | MIXED and stale for this candidate | The latest tracked install leg proves clean installation and the suite (`3625 passed, 32 skipped`), with lint/tests/resource/error-path cells OK/PASS. `ci-pass`/`ci-act` are harness-attributed FAIL/UNVERIFIED because the local image lacks GitHub toolcache paths; `chaos-disconnect` is UNVERIFIED because no network-cut marker exists. This is not a waiver for a future material release. |
| Deployed surface | OPEN (RELEASE-021) | `/home/kara/.local/bin/gitreins --version` reports `gitreins 0.20.0` while PyPI and the repository report `0.21.0`; the deployed-surface script exists but is not executable in this lane (`rc=126`), so the direct version probe is the authoritative evidence here. |
| Public posture | MIXED | Repository is public MIT; secret scanning and push protection are enabled; Dependabot security updates are disabled; `main` branch protection returns HTTP 404 (unprotected). |
| Open blockers | OPEN | RELEASE-021 (stale deployed binary) and RELEASE-022 (dangling host `GOCACHE`, which makes local Go guard/judge evidence unusable) remain the canonical pending rows. They do not alter this bookkeeping-only NOT-DUE decision. |

This pass adds no RELEASE-* row: RELEASE-021 and RELEASE-022 already cover the only actionable findings. The report is append-only; prior passes are preserved.
