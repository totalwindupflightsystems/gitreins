# PYTYP-2603 — Tranche 1: zero ANN findings in production code

Status: TRANCHES — this slice is complete; the project is NOT. The board row's
live probe (46 findings at HEAD) mixes production and test code. Measured
split, before this tranche:

| Scope                          | ANN findings | Where                          |
|--------------------------------|-------------:|--------------------------------|
| `gitreins/cli.py`              |           27 | 25× ANN001, 2× ANN202          |
| `engine/commit_audit.py`       |            1 | 1× ANN001 (`profile`)          |
| Production total (`engine/` + `gitreins/`) | 28 | ruff 0.16.10, `--select ANN` |
| `tests/` (untouched)           |           18 | 14 in test_push_secrets.py, 4 in test_exfiltration_hygiene.py |
| Board probe total              |           46 | production + tests             |

## What this tranche did

Annotated every function argument and return type ruff ANN flagged in the
touched scope (the two production files above):

- `gitreins/cli.py`: 27 findings closed — parser-builder registry params
  typed via a module-level `_SubParsers = argparse._SubParsersAction[
  argparse.ArgumentParser]` alias, guard banner/printer typed with
  `GuardManager`/`Tier1Result`, judge eval helper typed with
  `Judge`/`Task`/`JudgeResult`, security render helpers typed with
  `AntaresFinding`/`AntaresScanner` (module object typed `ModuleType`, not
  `Any` — this repo's ANN ruleset bans ANN401), and the setup-tools group
  printer typed `Callable[[str], str | None]`. All cross-module names ride
  the existing `TYPE_CHECKING` block; `cli.py` has
  `from __future__ import annotations`, so none of this changes bytecode.
- `engine/commit_audit.py`: 1 finding closed — `profile` on
  `CommitAuditor._run_findings_pass` is now `ReviewProfile` (direct import;
  `engine.review_profiles` is stdlib-only, so no import cycle).

## Result (after)

```
ruff check --select ANN gitreins/cli.py engine/commit_audit.py
→ All checks passed!            (28 → 0 on the touched scope)
ruff check --select ANN engine/ gitreins/
→ All checks passed!            (production repo-wide 28 → 0)
```

## Explicitly NOT claimed

- Tests still carry 18 ANN findings (untouched — they are out of this
  tranche's production-code scope).
- `mypy --strict engine gitreins` reported 488 errors in 34 files before this
  tranche; no mypy gate was run or passed, and no mypy policy was changed.
- This is a tranche toward the 5,731-finding board figure (which counts all
  ANN/D-numbered findings repo-wide, tests included), not project completion.

## Runtime-behavior guarantee

- `gitreins/cli.py` keeps `from __future__ import annotations`; every
  cross-module name rides the existing `TYPE_CHECKING` block, and the
  `_SubParsers` alias lives inside it too (TYPE_CHECKING-guarded because
  `argparse._SubParsersAction` is not subscriptable at runtime before
  Python 3.12). One stdlib import added. No call sites, defaults, or
  control flow changed.
- `engine/commit_audit.py` gains one import (cycle-checked) and one
  annotation on a parameter that previously carried a comment placeholder.
- Verified: import probe OK under the repo venv (Python 3.10.20);
  focused tests `tests/test_cli.py tests/test_commit_audit.py
  tests/test_antares.py` → 346 passed, 1 skipped (base HEAD on the same
  antares file: 57 passed, confirming the errors mid-tranche were the
  runtime-evaluated alias, now fixed); `ruff check` (full ruleset) and
  `ruff format --check` clean on both touched files.
