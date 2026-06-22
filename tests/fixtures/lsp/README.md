# LSP Test Fixtures

Each subdirectory contains a minimal project with one known LSP-detectable error.
These fixtures exist to test:

1. **LSP runner** (`engine/lsp_runner.py`) — invoking each LSP server and parsing diagnostics.
2. **Tier 1 guard** (`GuardManager._check_lsp()`) — blocking commits when LSP errors are found.
3. **Tier 2 evaluator** (`read_lsp_diagnostics` tool) — feeding diagnostics to the LLM for judgment.

## Layout

```
lsp/
├── .gitreins/config.yaml    # Shared config — LSP enabled, all servers configured
├── python/main.py           # pyright: str passed where int expected
├── go/main.go               # gopls: string passed where int expected
├── typescript/main.ts       # ts_ls: string passed where number expected
├── rust/main.rs             # rust-analyzer: &str passed where u32 expected
├── cpp/main.c               # clangd: const char* passed where int expected
├── ruby/main.rb             # ruby-lsp: wrong number of arguments
├── kotlin/main.kt           # kotlin-lsp: String passed where Int expected
├── sql/query.sql            # sql-language-server: nonexistent column
├── swift/main.swift         # sourcekit-lsp: String passed where Int expected
└── cmake/CMakeLists.txt     # neocmakelsp: add_executable missing source files
```

## How to test

```bash
# After implementing engine/lsp_runner.py:
cd tests/fixtures/lsp
python -c "
from engine.lsp_runner import run_lsp_check
diags = run_lsp_check('pyright', 'python/main.py')
assert len(diags) > 0, 'pyright should find type error'
print(f'pyright: {len(diags)} diagnostics')
"

# Tier 1 guard:
cd tests/fixtures/lsp
git init && git add python/main.py
gitreins guard  # should show '✗ lsp' with the type error

# Tier 2 evaluator:
# Create a task that expects type safety, enable lsp_diagnostics, run judge
gitreins task create lsp-smoke "LSP evaluator smoke test" \
  "Function get_user in python/main.py must only accept int for user_id"
gitreins judge lsp-smoke
# Should FAIL because LSP sees str passed where int expected
```

## Verified on this machine (2026-06)

| Language | Error Detected | Server |
|----------|---------------|--------|
| Python | ✓ — `"abc123"` not assignable to `int` | pyright |
| Go | ✓ — `"abc"` not usable as `int` | gopls |
| TypeScript | ✓ — `'string'` not assignable to `'number'` | ts_ls |
| Rust | Not yet tested | rust-analyzer |
| C | Not yet tested | clangd |
| Ruby | Not yet tested | ruby-lsp |
| Kotlin | Not yet tested | kotlin-lsp |
| SQL | Not yet tested | sql-language-server |
| Swift | Not yet tested | sourcekit-lsp |
| CMake | Not yet tested | neocmakelsp |
