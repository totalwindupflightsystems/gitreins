# Battery evidence — 2026-10-24 (QA-GITREINS-2)

A clean-machine battery of this repo, captured to close QA-GITREINS-2: the
`chaos-errorpath` cell used to report the missing-config error path as
**UNVERIFIED** on every run (a `rc=124` timeout), because the generic probe
selected `gitreins serve` — a long-running daemon that needs no config — as its
start command.

| field | value |
|---|---|
| tested commit | `def404ead0131765652cfe4c531ee0257896abf9` (the fix itself) |
| battery | `~/.hermes/scripts/bunker-qa.sh launch /home/kara/gitreins` |
| agent | `37e4148c` @ `bunker-mvp`, destroyed on collect |
| evidence | `gitreins-battery-20261024T005147Z.jsonl` (24 rows, this directory) |
| harness | `__fence-check` violations=0; classifier self-test 58/58, pytest-detect 39/39 |

## The cell that changed

The row is terminal and carries the observed exit code:

```text
{"project": "gitreins", "cell": "chaos-errorpath", "status": "OK",
 "detail": "project probe: clean error on missing config: '…/tools/venv/bin/gitreins guard'
           -> rc=1, names the missing config (no .gitreins/config.yaml — run `gitreins init` first)
           probed in a scratch git repo with no .gitreins/config.yaml (bound 20s; HOME isolated)"}
```

Note on the row's own acceptance probe: because this writer orders the keys
`cell, status, detail`, `grep -o 'chaos-errorpath[^,}]*'` stops at the comma
after the cell name and prints only `chaos-errorpath`. The grade and `rc=` are in
the SAME row — use a window:

```bash
grep -o 'chaos-errorpath.\{0,160\}' <evidence>.jsonl
# chaos-errorpath", "status": "OK", "detail": "project probe: clean error on missing config: …
```

## The whole run, for context

| cell | grade | note |
|---|---|---|
| `repo-head` | OK | tested HEAD == `def404e` (the committed fix) |
| `toolchain-bootstrap` | OK | go 1.27.3, venv, gcc wrappers, make, compose, buildx, node 22.15.0 |
| `fresh-install` | OK | `pip install` from the synced tree |
| `ci-pass` | OK | native suite PASS on the clean agent |
| `ci-act` | UNVERIFIED | act leg failed inside `docker exec` (harness/docker, not this repo's code) |
| `upgrade` | OK | `gitreins==0.16.0` (tag v0.16.0) → HEAD install clean |
| `docker-deploy`, `ui-probe`, `chaos-shutdown`, `chaos-corruption` | N/A | no compose file, no web UI, no db/state files |
| `chaos-disconnect` | UNVERIFIED | `disc.log` empty (the class already named in the detail: QA-WARPFS-17 / QA-BUNKER-29) |
| `chaos-resource` | PASS | suite survives the 3G cap (cap applied, child saw it) |
| `chaos-errorpath` | **OK** | the fix under test — see above |

Residuals are recorded here rather than fixed here: `ci-act` (act/docker exec)
and `chaos-disconnect` (empty `disc.log`) are separate findings in their own
right and were not in this row's scope.
