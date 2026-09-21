# GitReins Agent Rules

## GitReins Quality Harness (MANDATORY)

This repo uses GitReins as its quality gate. Every commit runs static guards.
If guards fail, the commit is BLOCKED. You cannot skip this.

### Quick check before committing:

```bash
# Works from any checkout path — the venv path is derived from the repo root.
# ($HOME/go/bin is the standard Go user-tools dir; adjust if gitleaks lives elsewhere.)
PATH="$HOME/go/bin:$(git rev-parse --show-toplevel)/.venv/bin:$PATH" gitreins guard
```

### What's checked:
- **secrets** — API keys, tokens, passwords (BLOCKS on fail — no exceptions)
- **lint** — ruff (WARNS on fail)
- **tests** — pytest for changed packages (BLOCKS on fail)

### Test mode: diff
Only packages with staged changes are tested. Pre-existing failures in
untouched code will NOT block your commit. If you change pyproject.toml,
Makefile, .gitreins/config.yaml, or a config file, the full suite runs
as a safety net.

### Tasks and evaluation:

```bash
# Create a task with criteria
gitreins task create fix-auth "Fix authentication" \
  "Login accepts email+password and returns JWT" \
  "Invalid credentials return 401" \
  "Rate limiting works after 5 failed attempts"

# Do the work, then evaluate:
gitreins task start fix-auth
# ... implement ...
gitreins task complete fix-auth    # triggers LLM evaluation

# Or evaluate standalone:
gitreins judge fix-auth
```

**MCP commit rule:** the MCP `commit` tool is blocked while any task is
`in_progress` — complete tasks (`task.complete`) or delete them
(`task.delete`) first, then retry the commit.

### Worker briefs and test-count sync

Any worker brief whose diff adds, removes, or renames test files MUST carry this
explicit acceptance criterion:

> update test-count sites (README.md x2, CONTRIBUTING.md x2) to the new collection total

`scripts/check_docs_drift.py` is the single implementation: it runs the live
collection (`pytest --collect-only -q --override-ini=addopts=`) and fails when any
`N tests pass` / `N tests across` / `N test files` claim in README.md **or**
CONTRIBUTING.md disagrees with it. It is chained into the local guard's
`test_command` (`.gitreins/config.yaml`), so a drifted claim FAILS the commit
before push — run it directly before pushing test-touching work:

```bash
python scripts/check_docs_drift.py
```

Three CI reds (GR-GAP-019 era, INT-CI-12, tick 319) came from briefs that added
tests without this criterion.

### If guards fail:
1. READ the output — the guard tells you exactly what failed and where
2. Fix the issues. Do NOT commit with `--no-verify` unless it's a docs-only
   change or a GitReins self-upgrade.
3. Re-run `gitreins guard` until it passes
4. Then commit

### Never:
- Commit API keys or tokens — secrets guard catches these, and it's correct
- Skip guards with `--no-verify` for code changes
- Push if guards failed (let CI catch it if you must, but fix locally)
- Commit `.gitreins/tasks.yaml` — it's local task state
- **Use `os.kill()` or `os.killpg()` without PID validation** — `int(mock.pid)` == 1 kills init.
  Always validate: `isinstance(pid, int) and not isinstance(pid, bool) and pid > 1`.
  This bug took down Kara's entire session every 2-5 minutes for 30+ hours. The fix is
  ALREADY applied in `engine/lsp.py:482-502` (validation at :486, `os.killpg` guarded at :491,
  `proc.kill()` fallback at :495) — do not regress it.
- **Spawn detached CPU burn loops** (`setsid sh -c 'while :; do :; done' &`, backgrounded
  `timeout N nice ...` burner loops, unbounded `yes`/`cat /dev/zero`/`dd`) — when the caller
  exits they are orphaned to `systemd --user` and keep running: this host hit loadavg 220+
  with 278 survivors on 2026-09-18. The tier-2 evaluator now REFUSES them (see
  `engine/command_hygiene.py` — it names the correct primitive in the refusal) and the
  terminal tool vetoes them at the boundary. To WAIT use `sleep <seconds>`. To generate
  BOUNDED load for a flake/load repro use the documented path: **`docs/load-reproduction.md`**
  (`python3 scripts/loadgen.py --workers N --seconds S` — capped workers, capped duration,
  PDEATHSIG teardown so children die with the runner, refuses to run on a shared host, and
  fails the run if any child survives).
