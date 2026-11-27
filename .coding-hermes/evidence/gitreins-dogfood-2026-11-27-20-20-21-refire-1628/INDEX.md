# Evidence — refire 1628 install battery (bunker-las-03, agent 1fb44061)

Tick: gitreins-dogfood-2026-11-27-20-20-21 (dispatch at 16:28:24 host-local; battery launched by
that fire, collected and recorded in DuckBrain by the 16:54:53 fire as
`/dogfood/2026-11-27-20-20-21-refire-1628-collected`). Copied into the repo by delivery 9
(17:58:33 dispatch) because it existed only under `~/.hermes/qa/evidence/` and the cache path the
manifest names.

Tested revision: `540623797458b9eda0afd87d04d05e7c51fa6b7e` (github/main at the time; unchanged
product tree through delivery 9).

Source dir: `/home/kara/.hermes/qa/evidence/gitreins/qa-20261127T215020Z/`
(`manifest.json` `source` field: `/home/kara/.hermes/cache/scratch/gitreins-dogfood-refire-1628/evidence.jsonl`).

Files copied and their transformations (all byte-identical copies; no rewrites):

| file | sha256 | rows/lines |
| --- | --- | --- |
| `evidence.jsonl` | `be6ab99e5c6055a4ca8cdcf8db58ee9af2d14d9c885bd65a1912956d534a1672` | 29 rows |
| `manifest.json` | (copied verbatim; `evidence_sha256` matches the row above) | — |
| `logs/ci.log` | `bcf816d8dffdba3b4f3187764573e246be22b2cb30ec105b6d6d3cc21c0aac03` | 1043 lines |

Only `ci.log` was copied out of the source `logs/qa-logs-2750892/` tree (268 KB total); the other
per-cell logs (inst, disc, buildx, compose, install-artifacts.before/after) remain on the host at
the source path above. `ci.log` is the one that carries the failure mechanism cited by the
delivery-9 log entry.

Independent checks run against these bytes at copy time (delivery 9):

- `grep -ril 'no space left\|errno 28' logs/` over the source log tree → no hits, so this battery is
  not the DF-GITREINS-POC-104-R2 ENOSPC class.
- `ci.log:1008` and `ci.log:1037` → `/var/run/act/workflow/3: line 12: go: command not found`,
  `exitcode '127'`. That is the mechanism already owned by pending row QA-GITREINS-3
  (`ci-pass` reds under act at the system-tools install step while hosted CI is green at the same
  SHA). Hosted CI was **not** exercised by this battery — the act leg is the only CI surface it ran.
