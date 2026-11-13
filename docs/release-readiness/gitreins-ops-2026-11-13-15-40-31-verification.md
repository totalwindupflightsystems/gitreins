# GitReins OPS — verification-only continuation, tick `gitreins-ops-2026-11-13-15-40-31`

This file records a **re-entry verification** of a tick that had already published its board
leg. It is not new work and it does not append a board event.

- Tick: `gitreins-ops-2026-11-13-15-40-31` (slug published once on `github/main`)
- Published event: id `1794171000000000066`, `event_type=audit`, `task_id=RELEASE-021`,
  timestamp `2026-11-13T16:16:14Z`
- Published row: `RELEASE-021` — status `complete` in
  `github/main:.coding-hermes/board/tasks.jsonl`
- Evidence artifact under verification: `docs/release-readiness/gitreins-ops-2026-11-13-15-40-31.md`,
  blob `e6cf7f83a4970a45ec25ab53497856ec1e7071c4`,
  `sha256(fba4cf4e37c6c6c648ca51e00a2a15c0c29b1ae3d67ad4dab0d7de67f5566d7d)`
- Judged run: `.gitreins/history/2026-11-13/8603ee11` — `passed=true`, `pass_class=judged`,
  `commit=c3d8dff2`, `working_tree_content_fingerprint=ea39034d…feb8`, `merge_recommendation=no_concerns_found`

## Discriminator that made this a verification pass

`git show github/main:.coding-hermes/board/events.jsonl | grep -c 'gitreins-ops-2026-11-13-15-40-31'`
printed `1` (1226 published events; the ISO start stamp `2026-11-13T15:40` printed `0` because the
event carries the tick slug in `detail` and stamps the publish time). One published event for the
slug is terminal for the board leg: no second event was appended, no row was re-transitioned.

## Acceptance arms re-measured live (2026-11-13, after the published event)

| Arm | Command | Output |
|---|---|---|
| 1 | `stat -c '%n mtime=%y' /home/kara/.local/bin/gitreins` | `/home/kara/.local/bin/gitreins mtime=2026-11-12 10:24:24.228328624 -0500` |
| 2 | `timeout 20 /home/kara/.local/bin/gitreins --version; echo "rc=$?"` | `gitreins 0.21.0` / `rc=0` |
| 2 | `ls -la /home/kara/.local/bin/gitreins` | symlink → `/home/kara/.local/share/pipx/venvs/gitreins/bin/gitreins` |
| 3 | `grep -E '^(Name\|Version):' …/gitreins-0.21.0.dist-info/METADATA` | `Name: gitreins` / `Version: 0.21.0` |
| 3 | `cat …/gitreins-0.21.0.dist-info/INSTALLER` | `pip` |
| 3 | `ls …/gitreins-0.21.0.dist-info/direct_url.json` | no such file (pip writes it only for VCS/direct-URL/editable) |
| 3 | `find …/venvs/gitreins -maxdepth 4 -name '*.pth'` | only `pipx_shared.pth` |
| 4 | `gitreins --help` | `help_rc=0`, usage line lists the shipped subcommand set |
| 4 | `grep -c 'class SecondPassReviewer' …/site-packages/engine/review_profiles.py` | `1`; `351:MAX_SECOND_PASS_FINDINGS = 10` |
| 5 | `hostname; findmnt -no SOURCE,FSTYPE,OPTIONS /` | `karaHermes-mde-7840hs` / `/dev/nvme0n1p2 ext4 rw,relatime` |
| pub | `curl -fsSL https://pypi.org/pypi/gitreins/json \| jq -r '[.info.version, (.urls\|length)] \| @tsv'` | `0.21.0	2`, `rc=0` |

The published-version fetch is the one arm the tier-2 judge recorded as not re-run (network); it
reproduces here at `0.21.0` with two artifacts, so the deployed/published pair still matches.

## What this pass did not do

- No board event appended, no row transitioned, no scheduler state written.
- No service restarted: this deployment owns no daemon and no listening port.
- The published evidence artifact was left byte-identical — editing it would invalidate the blob
  and digest that the judged run cites by line number.
- `OBS-GITREINS-4` remains `blocked` on the published board. An earlier session of this same tick
  slug measured it (the fleet-monitor scrape job is still absent and the remote `sudo` append was
  refused by the unattended consent gate); that row's premise still includes an owner decision
  (`OBS-GITREINS-5`), so it is named here and left for the next tick to own rather than re-filed
  or re-annotated during a verification pass.
