# Fix verification — did the first round's findings actually get fixed? (GR-235)

GR-234 forwards every review stage's findings verbatim to the next stage
(`StageContext.prior_findings`). Until GR-235, the second pass re-reviewed the
diff with its own criteria but **nothing specifically verified that the FIRST
round's findings were actually handled** — a reviewer could flag the same
defect round after round and the verdict still read as a clean pass. The
fix-verification stage closes that gap.

## Where it runs

The stage is registered in the **default tier-2 pipeline** (`engine/pipeline.py`,
`load_pipeline_config`) after `tier2`:

```yaml
pipeline:
  stages:
    - id: tier1
      parallel: true
      on: [pre-commit, pre-eval]
      steps: [...]
    - id: tier2
      type: ai_eval
      on: [pre-eval]
    - id: fix_verification
      type: fix_verification
      on: [pre-eval]
      condition: "true"
```

It is **deterministic — no LLM call, no new transport**. Its input is the
immediately preceding review stage's serialized steps (the GR-234
`StageContext.prior_findings`); its output is per-finding dispositions.

**Config precedence:** a repo whose `.gitreins/config.yaml` already declares
`pipeline.stages` keeps working **unchanged** — the declared config wins and
no stage is appended. To opt in there, declare the stage in the config:

```yaml
pipeline:
  stages:
    - id: tier2
      type: ai_eval
      on: [pre-eval]
    - id: fix_verification
      type: fix_verification
      on: [pre-eval]
```

A stage with no predecessor (first stage in a pipeline) returns a **named
skip** ("no prior stage findings to verify") — it never reads as a graded
verification.

## What counts as a first-round finding

Read structurally from the prior stage's steps — never by parsing prose into
facts beyond the finding's own text:

* every verdict **item** whose status is `FAIL` or `NOT_VERIFIABLE` (a `PASS`
  item is a graded criterion, not an open finding);
* every structured `data["findings"]` entry (the GR-171 second-pass shape:
  `severity` + `file:line`), should a custom stage forward one.

A severity word (`critical`/`blocker`/`high`/`medium`/`moderate`/`low`/
`minor`/`info`) and a repo-relative `path:line` location are parsed from the
finding's own text. A finding with no readable severity is treated as
**capping** (fail-closed: an unclassified finding is never provably low).

## Disposition semantics

For each finding the flagged code is located in the judged tree; the judged
diff (`git diff HEAD`) supplies the evidence. One finding, one of four
dispositions:

| flagged code in the tree | disposition |
| --- | --- |
| absent; the diff removes/modifies the file | `fixed` — the diff removed the flagged code (or the whole file) |
| absent; the code moved to another file of the judged change | `superseded` — invalidated by that upstream change; the second round re-flags the code at its new location |
| absent; the diff does not touch the file | `superseded` — the finding no longer names the judged code |
| present at the flagged line | `not_fixed` — the region is unchanged |
| present, moved within the file | `partially_fixed` — the region changed but the flagged code survives |
| no path / no code location resolvable | `not_fixed` — **fail-closed**: an unverifiable finding is never reported handled |

The flagged content ("needle") is resolved in priority order: an explicitly
quoted snippet from the finding text **grounded in the pre-diff (HEAD) or
current content** (a snippet in neither is treated as a paraphrase and
discarded), then the flagged line as HEAD had it, then the flagged line as the
current tree has it.

Evidence is recorded per finding: the resolved `path`/`line`, the judged
diff's change status for the file, the line where flagged code still lives,
and (for `fixed`) an excerpt of the diff that removed it.

## The verdict cap: `needs_review`, never a hard pass

The binding rule (the row's AC): **`not_fixed` or `partially_fixed` on any
critical/high/medium (or unnamed-severity) finding caps the final verdict at
`needs_review`** — the run is never allowed to read as a clean pass while
flagged code survives the judged diff. `low`/`info` findings are advisory and
never cap.

The cap is a **downgrade policy, not a stage failure**: the stage itself
passes when the verification ran (its job is to report), and the cap travels
in machine-readable fields that the gates read:

* `stages.fix_verification.verdict_cap: "needs_review"` (folded onto the
  stage dict) and the step's `data["verdict_cap"]` + `capped_findings`;
* the pipeline result's top-level `verdict_cap: "needs_review"`;
* `engine.gate.merge_recommendation` degrades to `needs_review` — one more
  route to the middle value beside GR-178's `NOT_VERIFIABLE` and GR-231's
  missing-artifact routes (advisory; `pass_class` still governs classes);
* the **merge gate holds**: `WorktreeManager._refuse_unhandled_findings`
  refuses a merge whose verdict carries open capping dispositions, with the
  same "HOLD:" vocabulary as the other gate refusals.

A reader can therefore tell what the gate does without reading the stage
source: **`stages.fix_verification.dispositions` in verdict.json names, for
each first-round finding, its disposition and its evidence; an open
critical/high/medium finding there means `needs_review`, and a merge is
refused.**

## verdict.json shape

Additive only — records without the stage (or with every finding handled)
serialize byte-identically to the pre-GR-235 shape:

```json
{
  "verdict_cap": "needs_review",
  "merge_recommendation": "needs_review",
  "stages": {
    "fix_verification": {
      "id": "fix_verification",
      "passed": true,
      "verdict_cap": "needs_review",
      "dispositions": [
        {
          "finding_id": "tier2:item_0",
          "severity": "high",
          "disposition": "not_fixed",
          "evidence": "flagged code still present at foo.py:2 — region unchanged",
          "path": "foo.py",
          "line": 2
        }
      ],
      "steps": [{"data": {"dispositions": ["...as above"], "counts": {"not_fixed": 1}}}]
    }
  }
}
```

The single reader for the gate semantics is
`engine.gate.fix_verification_unhandled(record)` — one definition, two
callers (the advisory downgrade and the merge-gate hold). Tests:
`tests/test_gr235_fix_verification.py`.
