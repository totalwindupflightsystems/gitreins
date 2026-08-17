# Verdict: GR-GAP-031

**Task:** Add docs/cli-reference.md — CLI API reference
**Evaluated:** 2026-08-17T11:16:52.105977
**Result:** ✓ PASS

## Pipeline Stages

- ✓ **tier1**
  -   ✓ lint: 
  ✓ secrets: [90m6:15AM[0m [32mINF[0m [1mscanned ~4866132 bytes (4.87 MB) in 1.19s[0m
[90m6:15AM[0m [32m
  ✓ tests: ============================= test session starts ==============================
platform linux -- P
- ✓ **tier2**
  - COMPLETE
  ✓ docs/cli-reference.md exists covering all 11 subcommands with options + exit codes: docs/cli-reference.md exists (9102 bytes). It documents all 11 subcommands: install (exit 0/1), init (--reset, exit 0/1), task (create/start/complete/list/delete with --depends-on, --force, --status options and exit codes), guard (--dead-code, exit 0/1), judge (--skip-tier2/--async/--status, exit 0/1/2), commit (--skip-tier2, exit 0/1), commit-audit (message arg, exit 0/1), mcp-server (env vars, exit 0/1), security-scan (-d/--output/--force-ml, exit 0/1/2), setup-tools (exit 0/1), report (-n/--interactive, exit 0). Each section lists options and exit codes.
docs/cli-reference.md exists and documents all 11 subcommands with their options and exit codes.

## Summary

Judge Result: GR-GAP-031

Stage tier1: PASS
    ✓ lint: 
  ✓ secrets: [90m6:15AM[0m [32mINF[0m [1mscanned ~4866132 bytes (4.87 MB) in 1.19s[0m
[90m6:15AM[0m [32m
  ✓ tests: ============================= test session starts ==============================
platform linux -- P

Stage tier2: PASS
  COMPLETE
  ✓ docs/cli-reference.md exists covering all 11 subcommands with options + exit codes: docs/cli-reference.md exists (9102 bytes). It documents all 11 subcommands: install (exit 0/1), init (--reset, exit 0/1), task (create/start/complete/list/delete with --depends-on, --force, --status options and exit codes), guard (--dead-code, exit 0/1), judge (--skip-tier2/--async/--status, exit 0/1/2), commit (--skip-tier2, exit 0/1), commit-audit (message arg, exit 0/1), mcp-server (env vars, exit 0/1), security-scan (-d/--output/--force-ml, exit 0/1/2), setup-tools (exit 0/1), report (-n/--interactive, exit 0). Each section lists options and exit codes.
docs/cli-reference.md exists and documents all 11 subcommands with their options and exit codes.

Overall: PASS ✓
