# GitReins Dogfood Integration — 2026-09-24b — `gitreins serve`, the Judgment Browser

**Verdict: 🟡 PROMISING-BUT-ROUGH** — the API and its security model are the
best-documented, best-behaved surface in the project; the human-facing pane
built on top of it has never rendered the audit content the tool exists to
show.

**Run 11. Surfaces 1-10 (PyPI/CLI/guards/judge/MCP/resolve/security-scan/
Go-lane/fleet + `report`) untouched this run; this run took the judgment
browser: the documented HTTP API contract, its security model, the SPA, and
the fresh-clone/install leg.**

## The promise under test

judgment-viewer.md: *"a read-only local web server that renders the judgment
history of a checkout: every verdict … the criteria and Tier 1/Tier 2 evidence
inside each verdict, the worker evidence embedded next to the verdict … what
each judgment cost in tokens … the board event timeline, the QA run ledger"* —
plus an explicitly documented API contract ("changes are additive") and an
explicit security model (strict path validation, manifest-bound evidence,
loopback default, warned non-loopback opt-out).

## How I used it (as a user, not a tester)

- Started `gitreins serve` on this checkout (216 verdicts, 163 usage lines,
  344-row board, 529 events, QA ledger) — the real data a real user browses.
- Curl-probed **every documented endpoint** and its error paths: stats,
  verdicts list, verdict detail, evidence artifacts, tasks, events, ticks,
  qa, unknown routes.
- Attacked the documented security model: 5 traversal shapes across the
  verdict and evidence routes, `--path-as-is`, unlisted files on disk,
  POST to a GET route, invalid hex/date, case sensitivity.
- Drove the **SPA in real Chrome** (Playwright): cold boot, stat cards,
  filter tabs (PASS/FAIL), search, opening **all 216 verdict rows one by
  one**, evidence load-on-click, panel rendering, longtask census,
  screenshots.
- Exercised `gitreins report` and `report --json`, the flag surface
  (`--help` parity, `--repo` error contract, `--port 0`, `--host 0.0.0.0`),
  and the static variant (`scripts/judgment_viewer.py`).
- Fresh-machine leg on bunker-las-03 (agent fbf41e9d): clone → venv install →
  serve → onboarding install/init/guard/commit loop → destroy.

## What a user meets, in order

1. **The browser looks alive and fast** — 844 ms cold boot, 722 ms reload,
   0 long tasks, every panel (events 529, ticks, QA) renders, search and
   tabs work. Nothing about the list view is wrong.
2. **Click a verdict and the audit trail disappears.** The detail pane shows
   only the header: task id/title, date·hash·PASS, worktree/branch, and
   "CRITERIA (0)". No criteria, no Tier 1 gate output, no Tier 2 judge
   summary, no verdict summary, no token/cost line, no evidence section —
   **for any verdict, ever**. (POC-56, P0.)
3. The API behind it serves everything perfectly. A client that reads the
   documented contract and codes against `/api/*` gets a first-class,
   honest product: exact error codes (400/404/501), traversal-proof routes,
   manifest-bound evidence, honest empty-boards/ticks semantics. The pane is
   the only thing broken — and it is the thing humans read.
4. Contract drift accumulates quietly: the stats header says **JUDGMENTS:
   212** over a list of 216 rows with no word anywhere that resolution-gate
   records are excluded from the count (POC-57).
5. The fresh-clone story rots silently: two tracked verdict dirs ship stale
   2026-08-17/18 history to every new clone, and the `gitreins` branch the
   docs offer as the fresh-clone fallback has never been pushed anywhere
   (POC-58).
6. The onboarding first-commit loop still hits the known POC-51 wall on a
   fresh box (pytest not installed → hook blocks the first commit) — **fixed by
   DF-GITREINS-POC-51 after this run** (a missing runner is now a skip naming
   the fix, in the hook and the standalone guard alike; see the table below);
   POC-47 (fleet merge gate) verified FIXED on HEAD.

## The serve API: what held up (nearly everything)

| Probe | Documented | Observed |
|---|---|---|
| `GET /` | 200 HTML | 200, 14,087 bytes |
| `GET /api/stats` | 200 with total/passed/failed/pass_rate/usage/board | 200 — but total=212 vs list=216 (POC-57) |
| `GET /api/verdicts` | 200 `{"verdicts":[…]}` | 200 — 216 rows, exactly the disk set |
| `GET /api/verdicts/<d>/<h>` | 200 full verdict + joined usage | 200, usage joined (1.31M in / 8k out on the probe verdict) |
| evidence `patch` (declared) | 200 text/plain | 200, 694 B, `X-Gitreins-Evidence` header |
| evidence `worktree` (declared) | 200 | 200, 28,664 B |
| evidence `summary.md` (on disk, NOT declared) | 404 | **404** — manifest strictly binds |
| evidence `..%2f..%2fconfig.yaml` | never escape | 404/400 (5 traversal shapes, all blocked) |
| `POST /api/stats` | — | 501 (no method confusion) |
| `/api/verdicts/1999-01-01/deadbeef` | 404 | 404 |
| `/api/verdicts/notadate/x`, `/api/verdicts/2026-13-99/…` | 400 malformed | 400/404, both refused |
| `--repo /nonexistent` | exit 2 before bind, named error | exact: `error: --repo is not a directory: …`, rc=2 |
| `--port 0` | banner prints real port | prints `http://127.0.0.1:45447/` (real port) |
| `--host 0.0.0.0` | warns, on **stderr** | warns — on **stdout** (POC-57 #3) |
| board present/absent semantics | 200 `[]` + `board.configured` | exact |
| ticks `--project gitreins` | 500 rows max | 500 rows, 99 KB payload, live DB join |
| `GITREINS_SERVE_VERBOSE` | quiet by default | request logs appear only with the flag |
| static variant `scripts/judgment_viewer.py` | same data → HTML file | `--repo/--out/--project` as documented |

## POC-56 anatomy (the P0)

`gitreins/serve.py:584-591`, `show()`:

```js
d.innerHTML='<button class="close" …>✕ close</button>'+
 '<h3>'+ … +'</h3>'+ … +
 '<div class="sec">Criteria ('+items.length+')</div>'+
 items.map(it=> …).join('')
 ||'<p …>no per-criterion items recorded</p>'+
 ((t1.summary)?'<div class="sec">Tier 1 — static gates</div><pre>'+ … +'</pre>':'')+
 ((t2.summary)? … :'')+
 (v.summary? … :'')+
 telemetry(v)+
 evidenceSection(v);
```

`||` binds the whole left chain (`header + … + join('')`) as its left
operand. That string is **never empty** (the header is always non-empty), so
the right operand — the fallback paragraph through the Evidence section —
is dead code. The ticks/qa lists use the same `||` idiom correctly (their
left side is a bare `.join('')`, which is `''` when empty → falsy); the
detail pane breaks it by prepending the header. Standalone repro:

```bash
node -e "const h='HDR',t='|T1|T2|EV'; console.log(h + [].map(x=>x).join('') || 'FALLBACK'+t)"
# → "HDR"            (intended: "HDR" + "no items" fallback + "|T1|T2|EV")
```

Born in the original serve commit `c377f0f` (2026-09-12) — every version of
serve has had it; JVIEW-005 (`e5a7fbf`) extended the dead right operand with
the evidence section, which is why evidence has never been visible either.
Verified across 216/216 live rows in Chrome (zero exceptions — the bug is
silent, the pane just stops).

**Fix direction:** wrap the alternative in parens —
`(items.map(...).join('') || '<p>…</p>')` — and add the missing gate: a
render test that fetches a verdict payload with a tier1/tier2 summary and an
evidence manifest and asserts the pane contains `Tier 1 — static gates` and
`Evidence (`.

## POC-58 anatomy (history surface rot)

- `git check-ignore -v .gitreins/history` → not ignored **as tracked files**;
  `git ls-files .gitreins/history` lists
  `2026-08-17/96dd2464/{verdict.json,summary.md}` and
  `2026-08-18/9b129d91/{verdict.json,summary.md}` (added before
  `.gitignore:23` existed). Fresh clone on the bunker served
  `total: 2, pass_rate: 100` — a user browsing a fresh clone sees two
  August verdicts and no others, silently.
- `git ls-remote origin refs/heads/gitreins` → **empty**. Remotes:
  origin = gitlab totalwindup/gitreins-poc, gitlab-mirror =
  coding-hermes/gitreins-mirror, github = totalwindupflightsystems/gitreins.
  The branch exists only on this machine. judgment-viewer.md:186-189's
  "run `gitreins report` for the branch fallback" therefore cannot work for
  any fresh clone.

## The fresh-machine leg (installability)

bunker-las-03, agent `fbf41e9d`, ttl 2h, bare Debian agent user:

| Step | Result |
|---|---|
| clone (github, depth 50) | 7.1 s, HEAD 00e0e09 — mirror current |
| `python3 -m venv .venv && .venv/bin/pip install -e .` | **18 s**, rc=0 |
| `.venv/bin/gitreins --version` | gitreins 0.15.0 |
| `serve` + `/api/stats` | 200 — and `total: 2` (the tracked-history surprise, POC-58) |
| onboarding loop: install → init → guard | all rc=0; honest degraded hints (gitleaks path, lint/tests skip reasons) |
| onboarding's own first commit | **BLOCKED, rc=1** — `✗ tests (full) — /bin/sh: 1: pytest: not found` (POC-51 class, known open row; init wrote `test_command: pytest`, pytest not installed, bare name not resolvable) |

**Status of the POC-51 row: FIXED** (after this run). A pytest runner that is
missing on the host — no `pytest` on PATH and nothing importable, a pinned
interpreter that does not exist, an interpreter that exists without pytest
installed in it, a `.venv/bin/pytest` that was never created — is graded
**skipped** with the fix named, in the hook and in the standalone guard alike,
exactly like a linter that is not on PATH. The onboarding first commit above
now lands as a DEGRADED pass (exit 0 with the `allow_skips: true` that
`install`/`init` write, exit 2 with `allow_skips: false`), and a pytest run that
starts and fails still blocks the commit.

Agent destroyed and verified gone (`bunker list` → 0 matches).

## Performance (folded per Step 2b)

- `/api/stats`: 43.5 ms ± 4.5 warm (hyperfine, 15 runs) — no PERF row.
- SPA cold boot 844 ms / reload 722 ms / detail open 201 ms / 0 long tasks.
- `gitreins report -n 3`: 87.6 ms ± 5.9 warm.
- Nothing a user waits on; the pain on this surface is correctness, not speed.

## The verdict in one line

The serve API earns its documented contract; the pane that renders it has
never shown a single verdict's gates, judge output, cost, or evidence —
the audit trail the browser exists to display is invisible to the humans
it exists for. Rows DF-GITREINS-POC-56…58 carry the details.

## Reproduction pointers

- Live server: `cd ~/gitreins && .venv/bin/gitreins serve --port 8619 --project gitreins`
- Probe scripts (retained): `/tmp/dg-browser.py`, `/tmp/dg-sweep.py`,
  `/tmp/dg-evprobe.py`, `/tmp/dg-t2items.py` (API-vs-pane capture),
  screenshots `/tmp/dg-serve-view.png`, `/tmp/dg-serve-detail.png`.
- Bunker install leg: las-bunker-03, agent fbf41e9d, destroyed + verified.
- Board rows: DF-GITREINS-POC-56 (P0), -57 (P2), -58 (P2) on
  `.coding-hermes/board/tasks.jsonl` (344 → 347 rows, tail verified).
