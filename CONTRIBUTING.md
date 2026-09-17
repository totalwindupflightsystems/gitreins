# Contributing to GitReins

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

All tests must pass before submitting a PR. Currently **1836 tests across 52
test files** (canonical count: `pytest --collect-only -q`, which is exactly what
CI recomputes).

**Count claims are gated.** CI's `Verify README test-count drift` step collects
the suite and fails the build when any `N tests pass`, `N tests across`, or
`N test files` claim in README.md disagrees with the live collection — so a
commit that adds, removes, or renames tests MUST update README's counts in the
same commit. The numbers in this file are not machine-checked; keep them equal
to README's.

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

Run these before a docs change (the first two also run in CI):

```bash
python scripts/check_docs_drift.py      # README version == pyproject version; README count claims agree
python scripts/check_cli_examples.py    # every documented `gitreins ...` example parses with the real argparse
```

The suite's own count gate (`Verify README test-count drift`) is the third:
README's `N tests pass` / `N tests across` / `N test files` claims must equal the
live pytest collection.

`check_cli_examples.py` replays each documented example through the CLI's own
parser (handlers stubbed, so nothing executes) — a README example that cannot
parse is a broken promise, and the checker is what keeps the README's
`--depends-on` example honest.

## Project Structure

```
engine/            — Core engine (evaluator, guards, pipeline, LSP, LLM client, task manager, judge, dead_code)
gitreins/          — CLI entry point and install script
gitreins_mcp/      — MCP stdio server (12 tools)
tests/             — pytest test suite (1836 tests across 52 files; canonical count in README)
tests/reliability/ — 7 adversarial benchmark projects
scripts/           — Repo-level checkers run by CI (docs drift, CLI examples, board ids) + the judgment viewer
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

1. Bump version in `engine/version.py` and `pyproject.toml`
2. Update CHANGELOG (if exists)
3. Tag: `git tag -a vX.Y.Z -m "Release vX.Y.Z"`
4. Push tag: `git push origin vX.Y.Z`
5. Build: `python3 -m build --wheel`
6. Publish: tag the release (`git tag vX.Y.Z && git push --tags`); CI builds, publishes to PyPI, and creates the GitHub release automatically

## Questions?

Open an issue or start a discussion.
