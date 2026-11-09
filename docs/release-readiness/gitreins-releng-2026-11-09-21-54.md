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
