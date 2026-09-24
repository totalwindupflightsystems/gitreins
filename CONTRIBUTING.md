# Contributing to GitReins

### After merging: verify what you deployed

The fleet does not run this checkout — it runs the installed `gitreins` (pre-commit hooks, agent shells, guard calls across every repo that has GitReins wired). A merge that is not reinstalled is invisible to all of them, and both copies report the same version string, so the drift is silent. After any merge that changes the CLI surface, run:

```bash
pipx install --force .                      # deploy this checkout
python3 scripts/check_deployed_surface.py   # exits 1 on drift, prints what is missing
```

The probe compares the repo's subcommand list and version against the deployed binary and names any subcommand that exists only in the repo. Treat a non-zero exit as a failed deploy, not a warning. Release cuts run it as a checklist gate.


### Remotes: the GitLab mirror is kept in step

This repo has two remotes: `github` (github.com/totalwindupflightsystems/gitreins — live, CI and releases run here)
and the GitLab mirror `origin` (gitlab.readydedis.com/totalwindup/gitreins-poc). Keep both at the same content.

The mirror **protects `main`** (push: no one, merge: maintainers), so you cannot just add a second push URL —
a plain `git push` is rejected by its pre-receive hook. Sync it the way that project expects:

```bash
python3 scripts/sync_gitlab_mirror.py          # push mirror/main, open+merge the MR, verify
python3 scripts/sync_gitlab_mirror.py --check  # verify only
```

It pushes this repo's `main` to the unprotected `mirror/main` branch on GitLab, opens (or reuses) a merge
request into `main`, merges it with the API using `GITLAB_TOKEN` (environment or `~/.hermes/.env`), and then
verifies that the mirror's `main` contains this repo's `main`. A mirror-side merge commit is reported as
content-in-sync; the token is never printed.

History: the mirror sat three months behind after the June 2026 v0.1.1 line; its `main` was force-synced on
2026-09-22 and that old line is preserved on the remote as `archive/pre-gitreins-sync-2026-06`.

Open question recorded for the owner: whether this project should move under the `coding-hermes` org/group
instead of `totalwindupflightsystems` / `totalwindup`. Until that is decided the rule is simply that both
remotes carry the same content.

## Setup

**Preferred: uv (fast, deterministic)**

```bash
git clone https://github.com/totalwindupflightsystems/gitreins.git
cd gitreins
uv venv
uv pip install -e ".[dev]"
```

**Alternative: pip + venv**

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Or install from PyPI:

```bash
pip install gitreins
```

**Venv convention:** Use a single `.venv/` at the project root. Do NOT create nested venvs (e.g., `gitreins/.venv/`). All commands (`uv run`, `gitreins guard`, `gitreins` CLI entry) resolve against the project root `.venv/`. `uv run` automatically detects and ignores mismatched `VIRTUAL_ENV` from other projects.

## Running Tests

```bash
pytest tests/ -v
```

All tests must pass before submitting a PR. Currently **2394 tests across 68
test files** (canonical count: `pytest --collect-only -q`, which is exactly what
CI recomputes).

**Count claims are gated.** `scripts/check_docs_drift.py` runs the live
collection (`pytest --collect-only -q --override-ini=addopts=`, exactly the
CI command) and fails when any `N tests pass`, `N tests across`, or
`N test files` claim in README.md **or this file** disagrees with it — so a
commit that adds, removes, or renames tests MUST update the counts in the same
commit. CI's `Verify README test-count drift` step runs that script (it is the
single implementation; the numbers in this file are machine-checked too). Pass
`--static` to skip the live comparison — the message then says so and certifies
only that the documented claims agree with each other.

## Load reproduction

Some failures only appear under load. Reproduce them without leaving load behind:

- **Never spawn detached CPU loops** (`setsid sh -c 'while :; do :; done' &`) —
  they are session leaders, so a killed runner leaves them spinning on the host
  that also runs the gateway, the scheduler and DuckBrain (INT-FLAKE-5: 24+
  orphaned burners, load average 33.8).
- **Use `scripts/loadgen.py`** — daemonic children plus `PR_SET_PDEATHSIG`, so the
  kernel kills them when the runner dies even on SIGKILL; capped workers,
  duration and CPU set; it verifies its own cleanup and exits non-zero on a
  survivor, and refuses to start on a host running the shared services unless
  explicitly allowed. Full rule and rationale: `docs/load-reproduction.md`.

## Documentation Checks

Run these before a docs change (both also run in CI):

```bash
python scripts/check_docs_drift.py      # README version == pyproject version, AND every README/CONTRIBUTING test-count claim == the live pytest collection
python scripts/check_cli_examples.py    # every documented `gitreins ...` example parses with the real argparse
```

`check_docs_drift.py` is the single implementation behind CI's
`Verify README test-count drift` and `Verify README version drift` steps — the
live collection (`N tests pass` / `N tests across` / `N test files` claims in
README.md and CONTRIBUTING.md must equal it) happens inside the script, not in
a duplicated bash block, and an unmeasurable collection is a FAIL, never a
green. `--static` skips the live comparison (the message says so).

`check_cli_examples.py` replays each documented example through the CLI's own
parser (handlers stubbed, so nothing executes) — a README example that cannot
parse is a broken promise, and the checker is what keeps the README's
`--depends-on` example honest.

## Project Structure

```
engine/            — Core engine (evaluator, guards, pipeline, LSP, LLM client, task manager, judge, dead_code)
gitreins/          — CLI entry point and install script
gitreins_mcp/      — MCP stdio server (13 tools)
tests/             — pytest test suite (2394 tests across 68 files; canonical count in README)
tests/reliability/ — 7 adversarial benchmark projects
scripts/           — Repo-level checkers run by CI (docs drift, CLI examples, CLI-reference sync, board ids) + the judgment viewer
docs/              — Architecture, component map, evaluator loop, MCP API, CLI reference, dogfood reports
specs/             — Design specs (PRD → architecture → per-subsystem design)
.memory-bank/      — Institutional memory (ADRs, findings, work-item status)
.coding-hermes/    — Foreman board (JSONL canonical store: board/events/tasks/fixtures + schema)
.gitreins/         — GitReins runtime config; `history/` holds committed sample verdicts
assets/            — Banner images and branding
sandbox/           — Evaluation scratch space (excluded from ruff; see docs/sandbox.md)
bin/               — Machine-specific MCP launcher for a developer checkout (not part of the package)
skills/            — Agent-facing usage skill for this repo
website/           — Static landing page
.vfs/              — Hilo code-graph cache; `.vfs/graph/edges.jsonl` is tracked on purpose
```

### Intentional exceptions (CLN-1)

A folder-hygiene pass (CLN-1, 2026-09-17) inventoried every tracked top-level
entry against the list above: no tracked file was a stray, so nothing was
deleted, and these items that *look* like artifacts are deliberately kept —

- `.coding-hermes/board/tasks.jsonl.bak-20260904`, `.coding-hermes/tasks.md.bak`
  — dated snapshots from the DuckDB board migration; `tasks.md.bak` is the store
  66 parser-dropped tasks were backfilled from. Snapshots, not strays.
- `.gitreins/history/**/verdict.json` + `summary.md` — committed sample verdicts
  so `scripts/judgment_viewer.py` renders on a fresh clone. New verdicts are
  written unversioned (`.gitignore`) and committed to the `gitreins` branch.
- `.vfs/graph/edges.jsonl` — the code graph ships so Hilo queries work cold; its
  siblings (`graph.db`, `.last_reconcile`, `.parse_cache.json`) stay ignored.
- `bin/hermes-mcp-wrapper.sh` — a launcher pinned to one developer checkout
  (absolute paths, `~/.hermes/.env`); kept because that developer's MCP client
  invokes it by path. The package does not install it.
- `sandbox/*.py` — scratch scripts, excluded from ruff and never collected by
  pytest; they stay out of the suite on purpose.

Removal rule for future cleanups: grep `tests/`, `.github/workflows/` and the
docs for a reference before deleting anything, and check whether the content is
reproducible from something still tracked. Untracked strays (build output,
`*.bak.<epoch>`, cache files) belong in `.gitignore` instead.

## Development Workflow

1. Fork the repo
2. Create a feature branch: `git checkout -b feat/my-feature`
3. Write tests first (TDD)
4. Implement the feature
5. Run `pytest tests/ -v` — all tests must pass
6. Run `gitreins guard` — Tier 1 guards must pass
7. Submit a PR against `main`

## Commit Convention

- `feat:` — new feature
- `fix:` — bug fix
- `test:` — test additions or changes
- `docs:` — documentation only
- `chore:` — maintenance, config, dependencies
- `ci:` — CI/CD changes

## Release Process

One serialized cut, in order. Only the last step publishes — never build/publish from a
second place, and never tag a tree whose version, lock, changelog and docs disagree.

1. **Bump the version in `pyproject.toml`** — `[project].version` is the single source of
   truth. `engine/version.py` resolves it at runtime (installed package metadata via
   `importlib.metadata`, falling back to `pyproject.toml` on a bare checkout), so there
   is no version literal to edit there.
2. **Refresh the lock and the installed metadata** — `uv lock` makes the `gitreins` entry
   in `uv.lock` follow `pyproject.toml`; then `uv pip install -e . --no-deps` so
   `importlib.metadata` reports the new version to `gitreins --version`, the MCP
   handshake and `tests/test_version.py`. Confirm with `uv lock --check`.
3. **Cut the CHANGELOG** — move the `## [Unreleased]` entries under a new
   `## [X.Y.Z] — YYYY-MM-DD` heading and leave a fresh, empty `## [Unreleased]` header at
   the top for the next cycle.
4. **Sync every current-release stamp** — the README release banner (version plus the
   `N tests pass` / `M test files` counts, both read from a live collection:
   `python -m pytest --collect-only -q --override-ini=addopts=`), the
   `docs/onboarding.md` version stamp, and any docstring, comment or skill file that
   asserts the current release. Historical citations ("since vX.Y.Z", a dated dogfood
   record) stay as they are.
5. **Run the gates** — `python scripts/check_docs_drift.py`,
   `python scripts/check_cli_examples.py`, `python scripts/check_board_ids.py
   .coding-hermes/board`, the full suite (`python -m pytest -x --tb=short`),
   `ruff check` and `ruff format --check` on the files you touched, and `gitreins guard`
   (whose lint lane now runs both over the graded scope).
   The guard runs the full suite in this mode, because `pyproject.toml` is one of its
   safety-trigger config files.
6. **Commit the cut on `main` and push it** — version, lock, changelog and docs land in
   ONE commit, so no checkout can observe a half-cut release.
7. **Tag and push the tag** —
   `git tag -a vX.Y.Z -m "Release vX.Y.Z" && git push <remote> vX.Y.Z`.
   That is the only publish step: CI's release workflow reads the tag, verifies it
   matches `pyproject.toml`, builds the wheel and sdist, uploads them to PyPI and creates
   the GitHub release automatically. There is no local `python -m build` or `twine` step.

`<remote>` is your push remote for the public repository (`github` in this checkout;
`origin` where the repo is the only remote).

## Questions?

Open an issue or start a discussion.
