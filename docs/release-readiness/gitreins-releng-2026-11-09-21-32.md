# GitReins release readiness — 2026-11-09 21:32 tick

Target: totalwindupflightsystems/gitreins, canonical remote `github`, default branch `main`.
Certified release SHA: `8ea9088f535151a87f12445c7bf5549bf5197432`.
Current branch tip after the cut: `5fa2b448f8a9920feb3723b137d0bd9a371f6522` (board-only delta).
Previous release: annotated tag `v0.20.0`, peeled commit `9aa072e4fa55c48a5fbaaf2ff8cb682aeaff9ea1`.
New release: annotated tag `v0.21.0`, tag object `34a4c4d3174004b1db8c880dbbe0f482a2f5a6d3`, peeled commit `8ea9088f535151a87f12445c7bf5549bf5197432`.

## Decision

v0.21.0 minor was due under releases rule 4: the v0.20.0 range contained one `feat` and three `fix` commits after excluding merge commits from the conventional-commit census. No `BREAKING CHANGE:` footer or conventional-commit bang marker was found. The repository's version-prep commit and follow-up changelog-ordering commit were already on the certified branch before this tick.

The release was cut from the gated prep SHA, not from the later moving tip. No existing tag was moved or overwritten. The tag-triggered repository workflow was the sole publisher; no manual `gh release create` was run.

## Evidence table

| Surface | Result | Evidence |
|---|---|---|
| Identity/parity | PASS | `/home/kara/gitreins`; remote `git@github.com:totalwindupflightsystems/gitreins.git`; `github/main` and local main were `5fa2b448f8a9920feb3723b137d0bd9a371f6522`; parity `0 0` before tag; clean detached release worktree at `8ea9088f`. |
| Commit census | MINOR DUE | `v0.20.0..8ea9088f`: 63 commits including merges, 48 non-merge; conventional census: feat 1, fix 3, docs 3, chore 1, other 40; no breaking markers. The shipped diff touched 45 non-board/non-readiness/evidence paths. |
| Version prep | PASS | `pyproject.toml` and `uv.lock` report `0.21.0`; `CHANGELOG.md` has one dated `[0.21.0] - 2026-11-09` section beneath an empty `[Unreleased]`; README and onboarding current-release stamps report `0.21.0`. CLI, MCP, and importlib metadata all report `0.21.0`; `tests/test_version.py` passed 4/4; docs-drift passed. |
| Release tooling | PASS | `.github/workflows/release.yml` is the sole publisher. It verifies the tag against `pyproject.toml`, builds wheel/sdist, uploads PyPI, and creates the GitHub Release. `CONTRIBUTING.md` documents the serialized cut. No Makefile release target, Goreleaser config, Dockerfile, or `scripts/release*` publisher exists. |
| Exact prep-SHA CI | PASS | Run `42648256457`, completed/success, exact SHA `8ea9088f535151a87f12445c7bf5549bf5197432`; test 3.10, 3.11, and 3.12 all completed/success. |
| GitReins guard | PASS_DEGRADED | `/home/kara/gitreins/.venv/bin/gitreins guard --full --json` from a clean detached worktree returned rc 0 and `passed: true`; gitleaks, ruff/format, and mypy passed. The clean worktree had no `.venv`, so tests were skipped; LSP was also skipped because pylsp is unavailable. Hosted CI is the authoritative full test gate. |
| Candidate-bound GitReins verdict | UNVERIFIED | No committed `.gitreins/history` verdict was found bound to the certified prep SHA. Existing recent judged verdicts bind other SHAs and were not substituted. |
| Dogfood | MIXED / NOT A RELEASE BLOCKER | The 2026-11-09 clean-machine install leg reports fresh install OK, upgrade OK, lint OK, tests OK (`3625 passed, 32 skipped`), resource-cap PASS, and clean error-path OK. `ci-pass` is a harness-attributed act failure because the local image lacks GitHub hosted-toolcache paths; `ci-act` is UNVERIFIED; chaos-disconnect is UNVERIFIED because the network-cut marker is absent. |
| Previous artifact | PASS | v0.20.0 GitHub Release was non-draft/non-prerelease and PyPI served wheel plus sdist. PyPI is the documented artifact surface, so GitHub Release asset count 0 is by design. |
| Public posture | MIXED | Public MIT repository; secret scanning and push protection enabled; Dependabot security updates disabled; main branch protection API returns 404 (unprotected). |
| Tag safety | PASS | `v0.21.0` was absent locally and remotely before the cut. Remote annotated tag object is `34a4c4d3174004b1db8c880dbbe0f482a2f5a6d3`; peeled commit is the certified `8ea9088f...`. |
| Deployed binary | OPEN FOLLOW-UP | `/home/kara/.local/bin/gitreins` reports `gitreins 0.20.0`, while PyPI now serves `0.21.0`. This is one release behind the shipped version and needs a separate deployment refresh row; it did not block the source release. |

## Publication verification

Tag-triggered Release run `42704426258` completed/success on exact tag SHA `8ea9088f535151a87f12445c7bf5549bf5197432`. Both jobs passed: `Build and publish to PyPI` and `Create GitHub release`.

GitHub Release `v0.21.0` is published, non-draft, and non-prerelease: https://github.com/totalwindupflightsystems/gitreins/releases/tag/v0.21.0

PyPI reports version `0.21.0` with:

- `gitreins-0.21.0-py3-none-any.whl`, SHA-256 `318360f03d24a2145d58853016aca943b05fef0533a04e18ad5c3a3e11986ce7`
- `gitreins-0.21.0.tar.gz`, SHA-256 `f3aa78933cb5a8cd37dd2df407cc4b0715c5acd5a0c6014b7fe967edfbece34f`

A fresh virtual environment installed `gitreins==0.21.0` from PyPI and `gitreins --version` reported `gitreins 0.21.0`; importlib metadata reported `0.21.0`.

The default branch subsequently advanced only by board files: `5fa2b448` changes `.coding-hermes/board/events.jsonl` and `.coding-hermes/board/tasks.jsonl`; no shipped source differs between the certified release SHA and the current tip.

## Follow-ups

- Refresh the host's `/home/kara/.local/bin/gitreins` installation to 0.21.0 through the documented pipx path, and prove the installed artifact and behavior. This is separate from the completed source release.
- Restore or explicitly document branch protection if required by the project's public posture.
- Obtain a candidate-bound GitReins verdict for a future cut; this release relied on exact hosted CI plus the repository's version and publication checks.
