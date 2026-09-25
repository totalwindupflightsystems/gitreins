# Dogfood Run 12 — QA Ledger (`gitreins qa`) + `gitreins security-scan` — 2026-09-25

**Angle.** Runs 1–11 covered the CLI basics, judge, MCP server, resolve/preflight, the Go
guard lanes, the worktree fleet, `gitreins serve`, and fresh installs. The one documented
surface no run ever touched: the **QA ledger** (`qa list` / `qa record` — the out-of-harness
QA record the README advertises: "a fleet lane, a bunker battery") and the opt-in
**`security-scan`** (Antares CVE localization). This run used both for real: this run's own
outcome was recorded with `qa record`, and both commands were exercised warm on the host and
cold on a fresh bunker box.

**Promise under test.** "A QA run produced outside the harness (a fleet lane, a bunker
battery) can be recorded into the harness's ledger and read back — with the documented
contract for defaults, validation, evidence stamping, and ledger relocation — and
`security-scan` localizes CVE-relevant findings in staged files or a directory."

## What actually happened (host, v0.15.0 @ 36894b9)

### `qa list` — read path
- Exit 0, 0.11s warm, human table + `--json` both correct. JSON rows carry exactly the
  documented keys (`ts, project, status, verdict, kind, cells, findings, evidence, note,
  agent, server, commit, harness_version` + `evidence_missing` when stamped).
- `report` prints the QA block after verdict history exactly as §11 documents, including the
  "newest N of M" header line.

### `qa record` — write path (probes R1–R6)
| Probe | Documented behavior | Observed | Verdict |
|---|---|---|---|
| R1 defaults | project=dir name, commit=HEAD, verdict PASS | `project=gitreins`, `commit=36894b9`, PASS | ✓ |
| R2 bad `--cell` | exit 2, message on stderr | exact documented message | ✓ |
| R3 bad `--verdict` | argparse rejects | exit 2 | ✓ |
| R4 missing `--evidence` | row kept + `evidence_missing: true` + stderr warning | exactly that | ✓ |
| R5 `GITREINS_QA_LEDGER=<dir>/` | writes `qa-ledger.jsonl` in that dir | scratch dir worked, repo untouched | ✓ |
| R6 cell denominator | skipped cells don't count toward N passed | `0/1 passed, 1 skipped` for failed+skipped; `no graded outcome, 1 unknown` for UNKNOWN | ✓ (initial suspicion of a 2/3 denominator retracted — list output is correct) |

- `--ts` override honored; verdict/status derivation PASS→pass, FAIL→fail verified.
- Honest "run 12" row recorded into the repo ledger via the documented path (then left in
  place — it is a true record of this run).

### `security-scan`
- Contract exact: clean → exit 0 (`Antares: clean`), findings → exit 1, `--output json`
  emits the documented finding shape. 0.08s on an empty staged set.
- **The no-ML fallback is a 7-keyword grep, and the CLI docs don't say so.** With no
  transformers stack (or without `--force-ml`), `engine/antares.py:236` `_scan_with_heuristic`
  flags any line containing one of `CVE, vulnerability, injection, exploit, unsafe,
  deserialization, hardcoded`. A file with `subprocess.call(input(), shell=True)` — a real
  command-injection pattern — scans **clean**, while a *comment* containing "vulnerability"
  produces a finding. The `CVE-SIMULATED conf=0.00` label and the "real ML inference pending
  GR-117c" description are honest about confidence, but a user reading
  `docs/cli-reference.md §9` ("Run the Antares CVE localization scanner") has no way to know
  that "clean" can mean "only a keyword grep ran". Filed as POC-59.

## Fresh-machine leg (las-bunker-03, agent e82ffda4, ttl 2h)
- `git clone` from the public GitHub origin: 7s @ 36894b9 (existing public access only; no
  visibility/permission change).
- Documented install (`python3 -m venv .venv && pip install -e .`): **22s**, `gitreins
  0.15.0` on PATH inside the venv. No sudo needed, no surprises.
- Cold-run smoke: `qa list` (clean empty state), `qa record` + read-back (row round-trips
  byte-identical semantics), `security-scan` on a demo dir (clean, exit 0). All pass on a
  box with nothing preinstalled.
- Agent **destroyed and verified gone**.

## Performance (coding-hermes-perf law: measure, don't guess)
| Operation | Command | Warm | Cold (fresh bunker) |
|---|---|---|---|
| qa list | `gitreins qa list -n 5` | 0.11s | n/a (1 row) |
| qa record | `gitreins qa record ...` | ~0.1s | ~0.1s |
| security-scan (clean) | `gitreins security-scan` | 0.08s | <1s |
| security-scan (dir) | `security-scan -d demo-slugify` | 0.08s | <1s |

**No PERF rows.** Nothing here is slow enough that a user would notice; the ML path
(transformers inference) was NOT measured — it is opt-in and not installed on either box.

## Verdict: 🟢 (for these two surfaces) — SHIPPABLE
Both surfaces do exactly what their docs promise on the happy path, on the host and on a
fresh machine, with correct validation semantics and honest degraded states. One real gap
found (POC-59, P2 docs/behavior disclosure on the security-scan fallback) and one observation
(the repo rename left one old `project: gitreins-poc` row; identity is per-recording-time
directory name — fine, but worth knowing).

## Left behind
- `docs/dogfood/2026-09-25-integration.md` (this file)
- `docs/dogfood/diagnostics.md` — 09-25 section (antares fallback anatomy, ledger resolution chain)
- `skills/gitreins-usage/SKILL.md` — v1.10.0, QA-ledger section + pitfalls 44–45
- Board rows DF-GITREINS-POC-59 (P2) + DF-GITREINS-POC-60 (P2, rename observation)
- Ledger row: this run, `kind=dogfood`, agent `gitreins-dogfood-lane`
