# Dogfood integration report — 2026-09-23b (run 8): the security-scan guard

**Surface under test (never touched by runs 1–7):** `gitreins security-scan`
(the Antares CVE localization scanner, opt-in Tier-1 guard) and the
`security_scan` gate inside the live `gitreins guard` commit gate. Verified
gitreins 0.15.0 on PATH == repo checkout == bunker clone (b15a427).

## Promise under test

*"A user can opt in to a Tier-1 guard that localizes known CVEs against their
staged Python code (README's documented `.gitreins/config.yaml` block), backed
by the Antares-1b model, tunable by `min_confidence` and `cve_source` — and it
never blocks a commit on missing infrastructure."* — README §Security Scan,
docs/cli-reference.md §9.

## Verdict: the scanner pipeline WORKS; the documented enablement path does not

| # | Probe | Result |
|---|---|---|
| S1 | `security-scan` on staged SQL-injection + MD5 code (no keywords) | `clean`, exit 0, 0.096s warm — heuristic does NOT fire on actual vulnerable patterns, only on keyword lines |
| S2 | `--directory` + `--output json` on the same tree | identical `[]`, exit 0 — both modes consistent |
| S3 | `--force-ml` without the ML stack | message + **exit 2** — matches the README table (an earlier exit-0 reading was my own `$?`-after-pipe bug, retracted; `${PIPESTATUS[0]}` shows 2) |
| S4 | staged lines containing "injection"/"unsafe" | 3 findings, exit 1, correct file:line, `CVE-SIMULATED conf=0.00` text AND json shapes consistent |
| S5 | enable via README's documented block (`defaults.security_scan.enabled: true`) + `gitreins guard` | **`Tier 1 Guards: PASS` with NO security_scan line** — the documented config shape is DEAD for the guard |
| S6 | same key duplicated under `guards:` (found by reading guard_manager.py:950) | `✗ security_scan — db.py:11 [CVE-SIMULATED...]`, **exit 1** — gate fires and BLOCKS |
| S7 | guard on the fresh bunker box (agent a8015da1) | same FAIL + finding, gitleaks-absent warning degrades secrets lane gracefully — fresh-machine path proven |
| S8 | `qa record --project gitreins --verdict FAIL --cell security-scan=text` + `qa list` | recorded, listed, exit 0 |
| S9 | `gitreins report -n 3` | "No verdict history found", exit 0 — correct for a scratch repo with no judge runs |

## The one thing a new user MUST know (Finding 1, P1)

The README's config block lives under `defaults:` — **that is where the CLI
reads it (`cli.py:2941`) and NOT where the guard reads it.** The guard reads
`guards.security_scan.enabled` (`guard_manager.py:950–954`). Following the
README verbatim produces a commit gate that prints `Tier 1 Guards: PASS` with
no `security_scan` line — the guard you enabled never runs, and nothing tells
you. The working shape (proven twice, locally and on the bunker):

```yaml
defaults:
  security_scan:        # read ONLY by `gitreins security-scan` (cli.py:2941)
    enabled: true
guards:
  security_scan:        # read ONLY by `gitreins guard` (guard_manager.py:950)
    enabled: true
  secrets: true
  lint: false
  tests: false
```

## Findings (full rows on the board: DF-GITREINS-POC-38/39/40)

1. **POC-38 (P1) config-home split** — README + cli-reference + onboarding all
   document the `defaults:` shape; the real guard key lives under `guards:` and
   is documented nowhere. Silent no-op, worse than an error.
2. **POC-39 (P1) `min_confidence` is a no-op on findings** — it filters only the
   CVE feed (`cve_feed.py:221`); heuristic findings are hard-coded conf 0.0
   (`antares.py:258`) and the guard fails on ANY finding
   (`guard_manager.py:2416`). The documented `min_confidence: 0.7` cannot
   suppress a heuristic hit, so the word "injection" in a COMMENT fails a commit.
3. **POC-40 (P2) dep hint + dead guard keys** — `--force-ml`'s error names only
   `huggingface_hub` for the download dep but inference also needs
   `transformers`; the guard constructs the scanner bare
   (`guard_manager.py:2395`), so the config's `model:`/`cve_source:` keys are
   read by nothing on the guard path.

## What works well (measured, not vibes)

- Exit-code contract 0/1/2 holds exactly (README table), verified with
  `${PIPESTATUS[0]}`.
- Heuristic → text and json outputs agree line-for-line.
- Fresh-box install: 20s pip venv install → `gitreins install` → guard
  reproduces the finding end-to-end on a bare Debian user with no toolchains.
- The scanner is genuinely lightweight: 0.096s warm per staged scan, no
  model download, no network dependency in heuristic mode.
- gitleaks-absent degradation names the fallback and keeps the lane honest.

## Perf

`gitreins security-scan` (staged, heuristic): **0.096s warm** — no PERF row
filed; nothing here a user would feel.

## Integration example (working, as a real consumer would write it)

```bash
mkdir myrepo && cd myrepo && git init
pip install gitreins            # 0.15.0
gitreins install
# enable the guard at the WORKING location (see above), then:
cat > db.py <<'PY'
def get_user(conn, uid):
    q = "SELECT * FROM users WHERE id = %s" % uid  # + the word 'injection' nearby
PY
git add db.py
gitreins security-scan          # exit 1 + CVE-SIMULATED findings
gitreins guard                  # security_scan lane FAILs the run (exit 1)
```