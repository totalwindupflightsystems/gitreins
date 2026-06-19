# Contributing to GitReins

## Setup

```bash
git clone https://github.com/totalwindupflightsystems/gitreins.git
cd gitreins
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Or install from PyPI:

```bash
pip install gitreins
```

## Running Tests

```bash
pytest tests/ -v
```

All tests must pass before submitting a PR. Currently 322 tests, 94% engine coverage.

## Project Structure

```
engine/          — Core engine (evaluator, guards, pipeline, LLM client, task manager, judge, dead_code)
gitreins/        — CLI entry point and install script
gitreins_mcp/    — MCP stdio server (9 tools)
tests/           — pytest test suite (322 unit + integration tests)
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
6. Publish: `twine upload dist/*.whl`

## Questions?

Open an issue or start a discussion.
