# .gitreins/review.yaml — superset design (survey of 14 reviewer products, 2026-11-11)

Research: deleg_b1bd3bf3 (CodeRabbit, Greptile, Copilot, Bugbot, PR-Agent/Qodo,
Ellipsis, Devin, Sourcery, Kodus, Cubic, Graphite, Devlo, Baz, Codex, Claude
Review + GitLab Duo / Reviewpad / CodeAnt / Bito / Snyk patterns). Every claim
below is backed by official docs or source; the full comparison lives in the
session ledger. Companion row: GR-204 (this file is GR-204's design body).

## What is already common (do NOT present as novel)

Repo-wide instructions; nested/path-specific instructions; ignore/include
globs; org/repo config layers; PR-label/branch/author/title/draft filters;
incremental review; named reviewers; severity thresholds; auto-approval and
request-changes; summary/diagrams/confidence/profiles.

## The defensible superset (what nobody composes in ONE portable contract)

1. One portable hierarchy with EXPLICIT merge semantics per field
   (defaults < org < repo < path overlays < PR policy; scalar=override,
   maps=deep_merge, instructions=append, passes=append, ignores=union,
   include_paths=intersect, conflict=deny_and_report).
2. Path overlays SEPARATE from path selection: one path match can append
   instructions, change profile/tools, set a gate, and trigger passes
   independently (products usually blur these).
3. Typed path-TRIGGERED passes: portable pass contract (model, tools,
   context, budget, timeout, output, blocking, dedup, evidence) — Ellipsis
   proves path->reviewer execution; nobody makes the pass contract portable.
4. CONTENT-aware triggers: trigger passes on detected diff facts
   (authentication_call, deserialization, unbounded_query, secret_access,
   public_signature_change) not just events/metadata/globs.
5. COMPOSABLE gates: block on a tuple (severity, confidence,
   introduced-by-diff, changed-line status, path, pass, category,
   disposition) — not severity alone.
6. Base-branch policy safety: the resolved policy comes from the PROTECTED
   base branch, so a PR cannot weaken its own gates (Greptile does this for
   auto-approve only; apply it to the whole policy).
7. Effective-configuration preview + trace: `gitreins review explain`,
   every finding carries pass id, matched layers/globs, appended
   instructions, effective model/tools, config hash, gate evaluation. Not
   found as a first-class surface in ANY reviewed product.
8. Evidence-bound pass identity on every finding.

## The narrow "nobody has this" claim (do not overstate)

In the reviewed official documentation, no product exposes a single portable
repo-as-code contract combining: hierarchical org/repo/path overlays +
independently configured path/content-triggered passes + composable
confidence/new-code/path gates + typed label lifecycle + protected policy
sourcing + effective-configuration/evidence trace. Each individual piece
exists somewhere; the composition does not.

## What NOT to import (weaknesses kept as guards)

- PR-Agent's same-model reflection scorer (ours goes independent, GR-199).
- num_max_findings=3 style truncation-by-default (rank, do not truncate).
- Advisory comment-dump semantics (we keep gate semantics).
- Cubic's "custom_rules: [] clears inherited agents" footgun (use unions +
  explicit deny, conflict=deny_and_report).
- Ellipsis's repo-local config REPLACING org config (we deep-merge with
  explicit locked: list for org-mandatory policy).

## YAML sketch (superset; trim at implementation)

```yaml
version: 1

policy_source:
  ref: base                      # resolve policy from the PROTECTED base branch
  require_protected_ref: true
  allow_pr_override: false

inheritance:
  layers: [defaults, organization, repository, path_overlays, pr_policy]
  merge:
    scalar: override
    maps: deep_merge
    lists:
      instructions: append
      passes: append
      ignores: union
      include_paths: intersect
      exclude_paths: union
  conflict: deny_and_report

organization:
  extends: org://engineering/review-baseline
  locked: [policy_source, gates.security, passes.security]   # org cannot be downgraded by repo
  defaults:
    finding:
      minimum_confidence: 0.85

repository:
  profiles:
    balanced: {model: review-default, tools: [diff, repository_search, tests]}
    strict:   {model: review-high-reasoning, tools: [diff, repository_search, tests, static_analysis]}
  ignores:
    paths: ["**/vendor/**", "**/generated/**", "**/*.lock"]
    findings:
      - rule_id: "STYLE-*"
        disposition: suppress
        reason: "enforced by formatter"

paths:
  include: ["src/**", "services/**", "migrations/**"]
  exclude: ["**/fixtures/**", "**/snapshots/**"]
  overlays:
    - id: api-boundary
      match: {any: ["api/**", "routes/**", "**/*controller*"]}
      instructions:
        append: |
          Treat this as an API boundary. Check authentication, authorization,
          input validation, output filtering, backward compatibility, and
          error disclosure. Require concrete evidence for every blocking finding.
      review: {profile: strict, tools_add: [api_contracts]}
      gates: {minimum_severity: high, minimum_confidence: 0.90}
      labels: {add: [gitreins:scope/api]}
    - id: tests
      match: {any: ["**/*_test.go", "**/*.spec.ts", "**/*.test.ts"]}
      instructions:
        append: |
          Check edge cases, negative paths, determinism, isolation, and whether
          the test exercises production wiring rather than a mock-only path.
      gates:
        require: [test_coverage_delta, changed_behavior_has_test]
    - id: generated
      match: {any: ["**/generated/**", "**/*.gen.*"]}
      action: exclude_from_review
      reason: "generated source"

passes:
  - id: correctness
    enabled: true
    scope: {include: ["**"]}
    instructions: {append: "Find correctness defects, unhandled errors, broken invariants, and regressions introduced by the diff."}
    execution: {model: review-default, budget: 120, timeout_seconds: 600, tools: [diff, repository_search, tests]}
    output: {finding_types: [bug, regression], blocking_severities: [critical, high]}
  - id: security
    enabled: true
    trigger:
      any:
        paths: ["auth/**", "security/**", "**/*middleware*", "**/*permission*"]
        diff_signals: [authentication_call, authorization_check, secret_access, deserialization, shell_execution]
    instructions:
      append: |
        Perform a focused security review. Trace attacker-controlled input to
        sensitive effects. Check authentication, authorization, injection,
        secret exposure, SSRF, unsafe deserialization, and auditability.
    execution: {model: security-review, budget: 180, timeout_seconds: 900, tools: [diff, repository_search, dependency_graph, security_scanner]}
    gates:
      minimum_severity: high
      minimum_confidence: 0.90
      require: [evidence_on_changed_line_or_direct_dataflow]
      on_fail: request_changes
    output: {labels: {add: [gitreins:pass/security]}}
  - id: performance
    enabled: true
    trigger:
      any:
        paths: ["**/*query*", "**/*repository*", "db/**", "storage/**"]
        diff_signals: [new_loop_over_remote_data, unbounded_query, n_plus_one_candidate, lock_scope_change]
    execution: {model: performance-review, budget: 150, tools: [diff, repository_search, query_analyzer, benchmark_lookup]}
    gates: {minimum_severity: high, minimum_confidence: 0.85, require: [evidence_or_reproducible_reasoning]}
  - id: public-api
    enabled: true
    trigger:
      any:
        paths: ["api/**", "openapi/**", "sdk/**"]
        diff_signals: [public_signature_change, schema_change]
    gates:
      require: [backward_compatibility_assessed]
      on_fail: request_changes

triggers:
  automatic: {events: [opened, synchronize, reopened, ready_for_review], review_new_pushes: true}
  skip:
    drafts: false
    labels: ["gitreins:skip"]
    title_patterns: ["^WIP:", "\\[skip-review\\]"]
  dynamic:
    - id: large-diff
      when: {any: [{files_changed_gt: 50}, {changed_lines_gt: 1000}]}
      then: {add_pass: architecture-impact, set_profile: strict}
    - id: high-risk-metadata
      when: {any: [{labels: ["security-sensitive", "breaking-change"]}, {target_branch: ["main", "release/**"]}]}
      then: {require_passes: [security, public-api], require_human_approval: true}

gates:
  security:
    locked: true
    when: {any_pass_has: {severity_at_least: high, confidence_at_least: 0.90}}
    action: request_changes
  path_rules:
    - match: ["migrations/**"]
      require: [rollback_plan, data_loss_analysis]
      action: fail
  finding_policy:
    block_when:
      all: [severity_at_least: high, confidence_at_least: 0.85, introduced_by_diff: true, disposition: unresolved]
    do_not_block_when:
      any:
        - baseline_existing: true
        - outside_changed_lines: true
          unless: direct_dataflow_from_changed_code

labels:
  inputs:
    - {name: gitreins:skip, effect: skip_automatic_review}
    - {name: security-sensitive, effect: require_pass, pass: security}
  outputs:
    - {name: gitreins:pass/security, when: pass.security.completed}
    - {name: gitreins:blocked, when: gate.failed}
  lifecycle: {owner: gitreins, remove_stale_outputs: true}

explainability:
  effective_config: {enabled: true, commands: ["gitreins review explain --base origin/main --head HEAD"]}
  trace_each_finding:
    include: [pass_id, matched_layers, matched_globs, appended_instructions,
              effective_model, effective_tools, config_hash, diff_range, gate_evaluation]
  dry_run: {enabled: true, show_suppressed_rules: true, show_unavailable_passes: true}
```
