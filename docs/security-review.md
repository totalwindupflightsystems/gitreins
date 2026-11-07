# Staged Security Review (GR-156)

The `security_review:` config block runs a staged security pipeline over the
files a guard run is already grading. It is **off by default**: with `mode:
off` (or no block at all) the guard runs exactly the lanes it ran before — no
new step, no new output.

## The flag and its three modes

Top-level block in `.gitreins/config.yaml` (sibling of `guards:`,
`data_protection:`, `spec_alignment:`):

```yaml
security_review:
  mode: off                     # off | staged | full   (default: off)
  min_confidence: 0.7           # localization floor; hypotheses below it are dropped
  per_stage_timeout_seconds: 60 # each stage is bounded by this budget
  missing_tool_policy: fail     # skip | fail — how a stage degrades when its tool is absent
```

| Mode      | What runs                                        |
|-----------|--------------------------------------------------|
| `off`     | Nothing. Guard behavior is unchanged.            |
| `staged`  | Stage 1 (rules) + Stage 2 (localization).        |
| `full`    | Stage 1 + Stage 2 + Stage 3 (LLM judge).         |

`mode` is the only switch: there is no separate `enabled` key. An unknown
`mode` value degrades to `off`; an unknown `missing_tool_policy` degrades to
`fail`.

## Stage order

1. **`rules` — deterministic, offline.** semgrep and ast-grep scan the staged
   files (and only those files) with GitReins' bundled rule tables, rendered
   to temporary files — the tools never fetch rules from a registry and no
   rule file is written into your repository. There is **no ML and no LLM in
   this stage**. Every finding carries `rule_id`, CWE, `file:line` and
   `stage="rules"`. Both tools are optional installs (`pip install semgrep`,
   `cargo install ast-grep`); neither is required.

   **Bundled rule inventory (GR-GAP-068).** These are ALL the deterministic
   rules that ship — a green `rules` stage means these patterns found nothing,
   **not** that the diff is secure:

   | rule id | catches | CWE |
   |---|---|---|
   | `gitreins-eval-call` | `eval(...)` | CWE-95 |
   | `gitreins-exec-call` | `exec(...)` | CWE-94 |
   | `gitreins-pickle-loads` | `pickle.loads(...)` | CWE-502 |
   | `gitreins-yaml-unsafe-load` | `yaml.load(...)` (any loader) | CWE-502 |
   | `gitreins-os-system` | `os.system(...)` (ast-grep only) | CWE-78 |
   | `gitreins-sql-execute-percent` | `execute("SELECT …" % x)` | CWE-89 |
   | `gitreins-sql-execute-fstring` | `execute(f"SELECT … {x}")` | CWE-89 |
   | `gitreins-sql-execute-format` | `execute("SELECT …".format(…))` | CWE-89 |
   | `gitreins-sql-assign-interpolation` | SQL string built by interpolation, then executed | CWE-89 |
   | `gitreins-shell-true-nonliteral` | `subprocess.*(…, shell=True)` on a non-literal command | CWE-78 |
   | `gitreins-path-join-open` | `open(os.path.join(base, x))` with non-literal `x` | CWE-22 |

   Two honest limits of this table: pattern rules are **syntactic** — a
   value passed through an intermediate variable can evade the execute-form
   rules (the assign rule catches the common `q = "SELECT …" % x` shape), and
   `gitreins-os-system` only runs where the `ast-grep` binary is installed
   (`semgrep` covers the other ten). Grep is a floor, not a review — the
   `judge` stage and a human still own the rest.
2. **`localize` — the existing Antares scanner** (`engine/antares.py`,
   GR-117) over the *same* file set. Findings carry `stage="localize"` and a
   confidence score. Anything below `min_confidence` is **dropped and
   counted** — never silently promoted. Without the optional ML stack
   (`huggingface_hub`, `torch`, `transformers`) the scanner runs its disclosed
   keyword fallback (see DF-GITREINS-POC-59 in the scanner docs); fallback
   findings carry confidence 0.0, so they can never cross the floor on their
   own.
3. **`judge` — `full` mode only.** Receives only the stage-1/2 hypotheses at
   or above `min_confidence`. Every finding the judge confirms must cite
   `file:line`; a hypothesis the judge cannot cite is reported as
   **`unconfirmed`** — never as a pass, and never as a confirmed finding.
   Stage 3 talks only to your configured LLM endpoint (the same
   `engine.llm` client the rest of GitReins uses); stages 1 and 2 never touch
   the network.

Every reported finding names its producing stage: output lines are tagged
`[rules]` / `[localize]` / `[judge]`, and only a stage-3 confirmation is
labelled `model-confirmed`. A rule hit stays a rule hit even after the judge
sees it.

## Exit contract

The guard's `security_review` lane:

- **FAILS** when any stage reports a finding, or when a stage cannot run and
  `missing_tool_policy: fail` (the default) applies — the error names the
  stage and why (`rules (missing_tool): semgrep not on PATH ...`).
- **Treats one absent rules tool as partial coverage, not absence**: the
  `rules` stage passes when at least one of semgrep/ast-grep ran, even if the
  other is not installed — the surviving tool's findings are graded, and the
  absent tool is named as a per-tool note in the stage detail and on the
  lane's `warning` field (`rules partial coverage: ast-grep unavailable
  (cargo install ast-grep)`), so a passing run never reads as full coverage.
  Only a stage where NO rules tool ran is degraded by `missing_tool_policy`.
- **PASSES clean** only when every enabled stage ran, found nothing, and
  nothing degraded. `unconfirmed` hypotheses do not fail the lane, but they
  are listed in the output and in the lane warning — they are never reported
  as confirmed or silently dropped.
- **Never yields a silent pass** on missing tools: with
  `missing_tool_policy: skip` a degraded stage becomes a named skip
  (`skipped stage rules: skipped by missing_tool_policy=skip: ...`) which the
  guard reports as a skip (DEGRADED accounting), not as a graded clean scan.
- Each stage is bounded by `per_stage_timeout_seconds`: stage 1 spawns its
  tools under that subprocess timeout, stage 2 checks a deadline between
  files (files over 1 MB are skipped), stage 3 joins the judge under the same
  budget. A timed-out stage degrades exactly like a missing tool. No tool
  process outlives its stage.
- The graded scope is the guard run's change set (staged index, or the
  working-tree scope under `--scope working-tree`) — the pipeline never
  widens it to the whole tree, and never writes into the repository.
- Per-stage machine-readable status (mode, stages, findings, escalations,
  unconfirmed, dropped count) rides in the tier-1 result's
  `extra["security_review"]` for CLI/MCP/judge consumers.

A run with nothing in scope (no staged files) is a named skip
(`no files in scope`), not a graded pass.

## Relationship to the `security_scan` guard

`guards.security_scan.enabled` (Antares, GR-117) is a **separate,
independent** lane: it fails on ANY Antares finding, including zero-confidence
heuristic hits, and has no stages, floor, or judge. `security_review` is the
staged pipeline above: it reuses the same Antares scanner under stage 2 but
applies `min_confidence`, adds deterministic rules ahead of it, and (in
`full` mode) puts a judge behind it. The two lanes have different config
blocks, different names in guard output (`security_scan` vs
`security_review`), and can be enabled independently — enabling one never
enables the other.

## Requirements and degradation

Nothing new is required to *enable* the flag. Per stage:

| Stage      | Needs                                             | Missing behavior                       |
|------------|---------------------------------------------------|----------------------------------------|
| `rules`    | at least one of `semgrep` / `ast-grep` on PATH    | one tool absent → **passes** with partial coverage, the absent tool named in the stage detail and the lane warning; neither tool present → `missing_tool_policy` applies |
| `localize` | nothing (heuristic fallback is built in)          | ML stack absent → disclosed fallback; scanner import failure → `missing_tool_policy` |
| `judge`    | a configured LLM credential (as for Tier 2)       | `missing_tool_policy` applies          |

Install hints are printed in the lane output when a tool is absent. Tests in
`tests/test_security_stages.py` cover the full matrix hermetically — the four
tool arms (both / semgrep-only / ast-grep-only / neither) under both
policies — and never require semgrep, ast-grep, the ML stack, or network
access.
