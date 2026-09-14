# Support

## Getting Help

If you need help with GitReins, here are the available support channels:

### Documentation

- [README.md](README.md) — project overview and quick start
- [CONTRIBUTING.md](CONTRIBUTING.md) — how to contribute
- [Specifications](specs/) — detailed technical documentation
- [CHANGELOG.md](CHANGELOG.md) — version history and release notes

### Issues

- Bug reports and feature requests: [GitHub Issues](https://github.com/totalwindupflightsystems/gitreins/issues)

### Direct Contact

For security vulnerabilities or sensitive issues, contact the maintainer:

- Email: wojonstech@gmail.com
- GitHub: [@wojonstech](https://github.com/wojonstech)

## Common Issues

### Guard fails with "No Python files staged"

This is normal when no Python files have been staged for commit. The guard
correctly skips lint and test checks for non-Python changes.

### LLM evaluator won't run

`gitreins task complete <id>` runs the Tier 2 evaluator and requires a
credential before it changes an in-progress task to complete. Set the primary
credential (or one of the supported provider-key fallbacks):

```bash
export GITREINS_LLM_API_KEY="your-provider-key"
export GITREINS_LLM_BASE_URL="https://api.openai.com/v1"  # optional
export GITREINS_LLM_MODEL="your-model"                   # optional
```

For an explicit Tier 1-only evaluation that does not require an LLM key, run:

```bash
gitreins task complete --skip-tier2 <id>
```

### Hanging pre-commit hooks

Increase the `hook_timeout` in `.gitreins/config.yaml`:

```yaml
hook_timeout: 300
```

### Test timeout in CI

Tests use `test_timeout: 180` by default. Long-running integration tests may
need more time. Configure in `.gitreins/config.yaml`:

```yaml
test_timeout: 300
```
