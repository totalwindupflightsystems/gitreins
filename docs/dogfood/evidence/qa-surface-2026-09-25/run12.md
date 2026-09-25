# Dogfood run 12 evidence — 2026-09-25 (qa ledger + security-scan, host @ 36894b9)

Commands and raw outputs captured during the run. No tokens; scratch paths only.

## R1 record defaults
$ .venv/bin/gitreins qa record --kind dogfood --cell install-bunker=skipped --cell surface-qa=passed \
    --finding DF-RUN12:qa-surface-first-use --note "dogfood run 12: qa ledger + security-scan first real use" \
    --evidence docs/dogfood/evidence/qa-surface-2026-09-25/run12.md --agent gitreins-dogfood-lane --server kara-host
qa record: evidence path not found: docs/dogfood/evidence/qa-surface-2026-09-25/run12.md
qa ledger: recorded dogfood gitreins PASS in /home/kara/gitreins/.gitreins/qa-ledger.jsonl
(exit 0; row kept, evidence_missing=true — R4 contract verified by this same probe)

## Row read-back (qa list --json, last row)
{"project":"gitreins","kind":"dogfood","verdict":"PASS","status":"pass","commit":"36894b9",
 "agent":"gitreins-dogfood-lane","server":"kara-host","cells":{"install-bunker":"skipped","surface-qa":"passed"},
 "findings":[{"id":"DF-RUN12","title":"qa-surface-first-use"}],"evidence_missing":true,"harness_version":"0.15.0"}

## R2 malformed cell
$ gitreins qa record --cell justname
qa record: --cell expects NAME=STATUS (got 'justname')   exit=2

## R3 bad verdict
$ gitreins qa record --verdict MAYBE
argument --verdict: invalid choice: 'MAYBE' (choose from 'PASS', 'FAIL', 'UNKNOWN')   exit=2

## R5 dir override
$ GITREINS_QA_LEDGER=/tmp/dogfood-gitreins/qa-dir/ gitreins qa record --project scratch-proj --kind lane --cell smoke=passed
qa ledger: recorded lane scratch-proj PASS in /tmp/dogfood-gitreins/qa-dir/qa-ledger.jsonl

## R6 FAIL + skipped denominator
row: --verdict FAIL --cell smoke=failed --cell judge=skipped --exit-code 1
list: "✗ lane scratch-proj ... cells 0/1 passed, 1 skipped  exit 1"   ← display correct, suspicion retracted

## UNKNOWN row
$ GITREINS_QA_LEDGER=/tmp/dogfood-gitreins/qa-dir/ gitreins qa record --project scratch-proj --kind bunker \
    --verdict UNKNOWN --cell boot=unknown --ts 2026-09-25T01:00:00+00:00
list: "· bunker scratch-proj ... cells no graded outcome, 1 unknown"

## security-scan contract
$ gitreins security-scan            → "Antares: clean — no findings in staged files"      exit 0 (0.08s)
$ gitreins security-scan -d demo-calc    → clean, exit 0
$ gitreins security-scan -d demo-slugify → clean, exit 0

## security-scan heuristic gap (POC-59)
/tmp/dogfood-gitreins/vuln-dir/weak.py:  subprocess.call(input(), shell=True)   → CLEAN (exit 0)
/tmp/dogfood-gitreins/vuln-dir/heur.py:  "# this line mentions vulnerability and injection" + real shell=True call
$ gitreins security-scan -d /tmp/dogfood-gitreins/vuln-dir
Antares: 2 potential finding(s) in /tmp/dogfood-gitreins/vuln-dir:
  • .../heur.py:1 [CVE-SIMULATED conf=0.00] Heuristic match on keyword 'vulnerability' — real ML inference pending GR-117c
  • .../heur.py:3 [CVE-SIMULATED conf=0.00] Heuristic match on keyword 'unsafe' — real ML inference pending GR-117c
exit 1 — the real injection line (no keyword) produced nothing.

## Bunker install leg (agent e82ffda4, destroyed after)
$ git clone https://github.com/totalwindupflightsystems/gitreins ~/app   → HEAD 36894b9 (~7s)
$ python3 -m venv .venv && .venv/bin/pip install -q --upgrade pip && .venv/bin/pip install -q -e .
INSTALL_SECONDS=22 ; .venv/bin/gitreins --version → gitreins 0.15.0
cold smoke: qa list (empty state ok) → qa record smoke-fresh bunker clone=passed install=passed qa-smoke=passed
  → qa list shows "✓ bunker smoke-fresh 3/3 passed commit 36894b9" → security-scan -d demo-slugify clean exit 0
$ bunker destroy e82ffda4 --server bunker-las-03 → destroyed; bunker list | grep e82ffda4 → 0 matches
