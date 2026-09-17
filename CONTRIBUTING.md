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

All tests must pass before submitting a PR. Currently **1744 tests across 46
test files** (canonical count: `pytest --collect-only -q`, which is exactly what
CI recomputes).

**Count claims are gated.** CI's `Verify README test-count drift` step collects
the suite and fails the build when any `N tests pass`, `N tests across`, or
`N test files` claim in README.md disagrees with the live collection — so a
commit that adds, removes, or renames tests MUST update README's counts in the
same commit. The numbers in this file are not machine-checked; keep them equal
to README's.

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
engine/          — Core engine (evaluator, guards, pipeline, LLM client, task manager, judge, dead_code)
gitreins/        — CLI entry point and install script
gitreins_mcp/    — MCP stdio server (12 tools)
tests/           — pytest test suite (1744 tests across 46 files; canonical count in README)
tests/reliability/ — 7 adversarial benchmark projects
docs/            — Architecture, component map, evaluator loop, technology choices
.memory-bank/    — Institutional memory (ADRs, findings, work-item status)
assets/          — Banner images and branding
```

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
