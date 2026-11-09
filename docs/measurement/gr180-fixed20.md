# GR-180 fixed-20 reflection measurement

ch:trace row=GR-180 spec=engine/review_profiles.py#FindingReflector wave=none:serial test=tests/test_gr180_fixed20_measurement.py::test_fixed_case_count_and_metrics doc=docs/measurement/gr180-fixed20.md evidence=docs/measurement/gr180-fixed20.md witness=none:worker-local-measurement verdict=none:control-root-judge-pending commit=fa911010986cc42d9756402a92e75ace1b419830 memory=none:foreman-write

STATUS: complete — fixed-20 measurement acceptance verified at worker level on branch wt/GR-180
TIER: T2 — real execution (focused suite 10/10 passed + exact measurement command run, exit 0); artifacts committed; no live-model or human-judged claim
EVIDENCE: this artifact (command + byte-verified stdout below) and tests/test_gr180_fixed20_measurement.py (focused run: 10 passed)

Command (run from the repository root, no network, deterministic):

```bash
$ .venv/bin/python scripts/gr180_fixed20_measurement.py
```

Exact commits measured (identification required by the acceptance):

- Implementation (shipped `FindingReflector`): introduced by 98d751cf and present
  unchanged at base c10c22fe1f16f387c297017989233b0c5c01e2f3 —
  `git diff c10c22fe fa911010 -- engine/` is empty (0 lines).
- Measurement tree (harness + this artifact): worker branch wt/GR-180 at
  fa911010986cc42d9756402a92e75ace1b419830. That commit lands the harness and is
  NOT merged into github/main at the time of writing; no main SHA and no judge
  verdict are claimed from this worker branch.
- Re-verification: the stdout block below was re-run on the fa911010 tree on
  2026-11-08 and diffed against this recorded block — byte-for-byte identical.

Exact observed output (stdout):

```text
GR-180 fixed-20 reflection measurement (shipped FindingReflector, threshold 0.60)
labels are FIXTURE TRUTH LABELS; precision is a LABEL PROXY, not a human-judged claim
case count=20
candidate findings before=20 after=20
surviving findings before=20 after=13
fabricated findings before=7 after=1
precision before=0.6500 after=0.9231

per-case ledger (label | stub score | expected | actual):
  case-01          correct   0.9/0.9                  kept     kept
  case-02          correct   0.9/0.8                  kept     kept
  case-03          correct   0.8/0.9                  kept     kept
  case-04          correct   0.9/0.9                  kept     kept
  case-05          correct   0.8/0.8                  kept     kept
  case-06          correct   0.7/0.8                  kept     kept
  case-07          correct   0.9/0.7                  kept     kept
  case-08          correct   0.8/0.7                  kept     kept
  case-09          correct   0.7/0.9                  kept     kept
  case-10          correct   0.9/0.6                  kept     kept
  case-11          correct   0.6/0.6                  kept     kept
  case-12          correct   0.3/0.8                  dropped  dropped
  case-13          fabricated 0.2/0.2                  dropped  dropped
  case-14          fabricated 0.15/0.9                 dropped  dropped
  case-15          fabricated 0.9/0.1                  dropped  dropped
  case-16          fabricated 0.2/0.4                  dropped  dropped
  case-17          fabricated no score entry           dropped  dropped
  case-18          fabricated high/0.9                 dropped  dropped
  case-19          correct   0.7/0.7                  kept     kept
  case-20          fabricated 0.85/0.8                 kept     kept

drop reasons (fail-closed visibility, AC3):
  index 11 (case-12): correctness below threshold 0.60 (correctness 0.30, evidence 0.80)
  index 12 (case-13): correctness and evidence below threshold 0.60 (correctness 0.20, evidence 0.20)
  index 13 (case-14): correctness below threshold 0.60 (correctness 0.15, evidence 0.90)
  index 14 (case-15): evidence below threshold 0.60 (correctness 0.90, evidence 0.10)
  index 15 (case-16): correctness and evidence below threshold 0.60 (correctness 0.20, evidence 0.40)
  index 16 (case-17): reflection returned no score for finding 16
  index 17 (case-18): unusable reflection score for finding 17: correctness 'high' is not a number
  reflection error: None
```

## How to read this number (scope and honesty boundary)

- **Corpus**: the 20 cases in `scripts/gr180_fixed20_measurement.py::CASES` —
  fixed, explicit, repository-owned; the case count is asserted in the source
  and pinned by `tests/test_gr180_fixed20_measurement.py`. It never changes
  between runs (determinism is itself a pinned test).
- **What is real**: parsing, per-finding score validation, threshold
  comparison, and the fail-closed drop paths are the shipped
  `engine/review_profiles.py::FindingReflector.reflect()` at the base commit
  above — the same code the judge runs (engine/judge.py wires it). Only the
  model's scoring reply is stubbed (`StubReflectionLLM`), so the run needs no
  network and is byte-reproducible.
- **What "precision" is**: a LABEL PROXY. The corpus has no independent human
  truth labels; the `correct`/`fabricated` labels are fixture truth authored in
  the harness, computed as surviving-labeled-correct / surviving. Do not quote
  0.6500 -> 0.9231 as live-model reflection quality — that measurement is
  GR-171 dry-run territory (`scripts/review_dry_run.py` with human labeling).
- **Result**: the filter removed 6 of 7 labeled fabricated findings
  (cases 13-18, including the two fail-closed paths: no score entry, unusable
  score) while the label-proxy precision rose 0.6500 -> 0.9231. This is a real
  improvement, not a rigged number: the fixture deliberately includes two
  scorer misses — case-12 (a `correct` finding scored low, so the filter drops
  a real finding) and case-20 (a `fabricated` finding scored high, so one
  survives) — which is why after-precision is 0.9231 and not 1.0.
- **No engine change was needed**: the measurement runs entirely through the
  shipped filter; the diff is harness + tests + this artifact.
