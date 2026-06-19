# GitReins Improvement Tasks

## [x] GR-020: Add `gitreins install` command
- **Priority:** high
- **Model:** MiniMax-M3
- **Files:** gitreins/cli.py, gitreins/install.py (new)
- **AC:** `gitreins install` creates .gitreins/config.yaml and git pre-commit hook

## [x] GR-021: Fix YAML `on:` key parsing bug
- **Priority:** medium
- **Model:** deepseek-v4-flash
- **Files:** engine/pipeline.py, .gitreins/config.yaml
- **AC:** `"on":` (quoted) in config.yaml preserves correct trigger list instead of parsing as boolean

## [ ] GR-022: Go project support
- **Priority:** medium
- **Model:** MiniMax-M3
- **Files:** engine/guard_manager.py, engine/guards.py (new)
- **AC:** Guards detect Go projects (go.mod present) and run `go test` / `golangci-lint` instead of pytest/ruff

## [ ] GR-023: Update gitreins-workflow skill
- **Priority:** medium
- **Model:** MiniMax-M3
- **Files:** ~/.hermes/skills/devops/gitreins-workflow/SKILL.md
- **AC:** Skill reflects actual v0.1.0 reality — no `gitreins install`, manual .gitreins/ setup documented

## [ ] GR-024: Pre-commit hook for Go repos
- **Priority:** low
- **Model:** deepseek-v4-flash
- **Files:** gitreins/hooks/pre-commit (new)
- **AC:** Go-aware pre-commit hook runs go vet + go test before allowing commit

## [ ] GR-025: ASCE integration test
- **Priority:** low
- **Model:** deepseek-v4-flash
- **Files:** /home/kara/asce/.gitreins/config.yaml
- **AC:** Verify GitReins guards run against ASCE Go codebase without false positives
