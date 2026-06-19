# Security Policy

## Reporting a Vulnerability
If you discover a security vulnerability in GitReins, please report it privately. Do NOT open a public issue.
We will respond within 48 hours and work with you on a fix and coordinated disclosure.

## Scope
- The GitReins engine, CLI, and MCP server
- The pre-commit hook and guard pipeline
- The .gitreins/ storage format

## Out of Scope
- Vulnerabilities in user-provided LLM APIs
- Vulnerabilities in user repositories that use GitReins

## Supported Versions
| Version | Supported |
|---------|-----------|
| 0.1.x   | Yes       |

## Security Features
GitReins includes a Tier 1 secrets guard that scans staged changes for API keys, tokens, and credentials before allowing commits. This runs as a pre-commit hook. See docs/sandbox.md for details.
