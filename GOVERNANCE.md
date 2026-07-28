# Governance

This document outlines the governance structure for the GitReins project.

## Roles

### Maintainer

The project maintainer (Bane / Alexis Okuwa) has the final authority over:

- Project direction and roadmap
- Code review and merge decisions
- Release management
- Community standards enforcement

### Contributors

Contributors participate by:

- Submitting pull requests for features, bug fixes, and documentation
- Participating in code review
- Reporting issues and suggesting improvements
- Respecting the Code of Conduct

## Decision Making

- Technical decisions are made through code review on pull requests
- Significant changes are discussed via GitHub issues before implementation
- The maintainer resolves disagreements and makes final calls

## Code Review Process

1. All changes go through pull requests
2. Automated CI checks must pass (tests, lint, secrets)
3. The maintainer or designated reviewer must approve
4. GitReins quality gates must pass (Tier 1 guards + Tier 2 evaluator)

## Release Process

- Releases are tagged with semantic versioning
- Release notes are published in CHANGELOG.md
- Each release is pushed to PyPI and GitHub

## Contact

Maintainer: Alexis Okuwa <wojonstech@gmail.com>
