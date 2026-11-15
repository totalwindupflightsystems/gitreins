# GR-209-R2 foreman evidence

tested_commit: 054b2f106ec93bbd54a0416d0f12c45ee3beda75
worker_commit: 054b2f106ec93bbd54a0416d0f12c45ee3beda75
worker_branch: wt/GR-209-R2
remote_parity: github/main == 054b2f106ec93bbd54a0416d0f12c45ee3beda75
focused_tests: .venv/bin/python -m pytest tests/test_canary.py -q --override-ini=addopts= -> 16 passed
docs_drift: PASS (3720 tests / 119 test files)
ruff: PASS (check + format check)
guard: FAIL on inherited surfaces: .vfs/graph/.symbols_cache.json generic-api-key finding; tests/test_cli_doc_sync.py two pre-existing CLI-doc drift failures (live review command, pinned 25 vs 26)
ci_exact_head: in_progress (run created 2026-11-15T08:15:45Z)
judge: unresolved — task complete process ended without a GR-209-R2 verdict artifact; lifecycle remains in_progress

ch:trace row=GR-209-R2 spec=.coding-hermes/board/tasks.jsonl#GR-209 wave=none:serial test=tests/test_canary.py::test_multiple_defects_append_one_record_each doc=docs/operations.md evidence=.coding-hermes/evidence/GR-209-R2-foreman.md witness=none:judge-artifact-missing verdict=none:missing-gr-209-r2-artifact commit=054b2f106ec93bbd54a0416d0f12c45ee3beda75 memory=none:not-written
