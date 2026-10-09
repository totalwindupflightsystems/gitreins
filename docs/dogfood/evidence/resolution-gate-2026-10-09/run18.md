# Run 18 raw captures — resolution gate surfaces (2026-10-09)

All probes driven live on the control host (/home/kara/gitreins) and a scratch copy
(/tmp/dogfood-gitreins/scratch) with `.gitreins/config.yaml` `predispatch: true` and a
`pipeline.stages` `commit_audit` block appended — the real repo's config was never modified.

## resolve --json (surface off, fresh clone) — ABSTAIN exact
```
{"verdict": "ABSTAIN", "abstain_reason": "no-credentials", "exit_code": 1}
# via fakehome + env -u on all three credential vars
{"verdict": "ABSTAIN", "abstain_reason": "no-credentials", "exit_code": 1}
```

## failover
```
badkey attempts: ["candidate 1/7: rejected (401)", "candidate 2/7: ok"]  -> REVIEW 0.74
nokey attempts:  ["candidate 1/6: ok"]                                   -> REVIEW 0.75
```

## preflight probes (scratch, predispatch: true)
```
pf_done  (command_hygiene refuses burn loops)  band=RESOLVED  p=0.89 decision=skip-dispatch
pf_go    (guard engine in Go? FALSE)           band=RESOLVED  p=0.87 decision=skip-dispatch
pf_rust  (Rust implementation? FALSE)          band=RESOLVED  p=0.89 decision=skip-dispatch
pf_k8s   (Kubernetes operator? FALSE)          band=REVIEW    p=0.53 decision=dispatch-with-note missing_kind=implementation
pf_flake8(flake8 lint lane? FALSE)             band=REVIEW    p=0.67 decision=dispatch-with-note
pf_gitleaks (secrets lane blocks keys? TRUE)   band=REVIEW    p=0.63 decision=dispatch-with-note
```
Inner `verdict` is a dict (not escaped string), carrying model echo
typesafe/jev-1.13-20260917. Preflight exits 0 on every record including ABSTAIN.

## commit-audit (scratch, mode: block)
```
armed-stage un-armed first: "no pipeline stage with type commit_audit for trigger commit-msg — audit NOT run" exit 0 (documented)
aud2_ok   "docs: add scratch note" over README diff      -> "✓ Commit message looks good." exit 0
aud2_bad  "feat: rewrite guard engine in Rust..." same diff -> "✓ ... looks good." exit 0  (FALSE NEGATIVE, POC-85)
aud3_quick same message over planted.py diff  -> BLOCKED exit 1, accurate issue list
aud3_deep  same                              -> BLOCKED exit 1, multi-issue + suggested message
```

## push-check
```
push-check HEAD origin/main  -> "PUSH REFUSED: secret scan could not verify..." exit 1 (bad ref pair, fail-closed)
push-check HEAD github/main  -> "PUSH SECRET CHECK: clean" exit 0, 86-96 ms
```

## persistence
```
report -> "─── Resolution gate (4) ───" section, records marked [cli], not in judge rollup
usage.jsonl tail: {"step": "resolution", "tokens_in": 12426/13339, "tokens_out": 96} x2
```

## perf
```
resolve warm x3: 2561 / 2494 / 2723 ms ; cold (fresh hilo cache): 3188 ms
push-check x3: 86 / 91 / 96 ms ; doctor: 96 ms
```

## bunker install leg (bunker-mvp, agent a4912683 — destroyed + verified)
```
las-03 ssh timeout; bunker4 ssh timeout; las-02 spawn fail (slice-limits) then retry OK,
node dropped pre-clone; mvp spawn OK
git clone (ssh origin): "Host key verification failed" 3s  [POC-87]
git clone (https): CLONE_OK 3549660, SECONDS=3
venv + pip install -e .: INSTALL_SECONDS=13 ; gitreins --version -> 0.16.0
first resolve on fresh clone: ABSTAIN empty-bundle, hint "check that hilo is installed" [POC-86]
preflight on fresh clone: ABSTAIN surface-disabled -> dispatch, exit 0 (exact)
destroy: "Agent a4912683 destroyed." ; list -> "No agents found."
```
