# Dogfood Log — gitreins-poc

| Date | Verdict | Promise | Top findings | Time-to-first-success |
|---|---|---|---|---|
| 2026-08-03 | 🟡 PROMISING-BUT-ROUGH | "Install gitreins into any repo, define criteria tasks, get agentic per-criterion judging, and commit through Tier-1 guards that block secrets" | (1) P0 `gitreins init` generates gitleaks config with invalid regexes → secrets guard permanently fails when gitleaks installed; (2) P1 default `test_command: pytest` can't import root package (pytest 9) → tests guard blocks commits that pass via `python3 -m pytest`; (3) P1 MCP judge times out at 300s on repos whose suite >5 min (full suite here ~11 min) | ~20 min to first green guard (after 2 config workarounds); judge on tiny repo: 76 s |

Details: `docs/dogfood/2026-08-03-integration.md` (real-use report), `docs/dogfood/diagnostics.md` (build/error trail), `skills/gitreins-usage/SKILL.md` (agent usage skill). Board tasks: DF-001..DF-006.
