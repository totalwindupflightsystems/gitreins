# Install leg — ephemeral bunker battery, 2026-11-09

Ran by the duplicate-fire continuation of tick `gitreins-dogfood-2026-11-09-12-54-05`.
The primary tick (`206aa735`, 08:41:36 -0500) recorded the install leg as SKIPPED (`DF-029`) because
its clean-install command was stopped by the approval gate. That classification was not supported by a
host census: a census run afterwards showed `bunkerd` active on this host and two answering bunker
servers (`bunker-mvp`, `bunker-las-02`). The leg was therefore run for real here.

```
agent     5cbc4aa3 @ bunker-mvp (tailscale), ttl 4h
launched  2026-11-09T13:46:31Z   (bunker-qa.sh launch /home/kara/gitreins)
collected 2026-11-09T14:10Z      (bunker-qa.sh collect --evidence ...)
driver    ~/.hermes/scripts/bunker-qa.sh   (install leg still uses bunker-qa.sh)
raw       install-leg.jsonl (27 rows / 18 distinct cells, verbatim)
```

## Tally

| verdict | n | cells |
|---|---|---|
| OK | 17 | toolchain-bootstrap ×9, fresh-install, lint, tests, upgrade, chaos-errorpath, collect, … |
| PASS | 1 | chaos-resource |
| N/A | 6 | docker_build, compose_config, docker-deploy, ui-probe, chaos-shutdown, chaos-corruption |
| FAIL | 1 | ci-pass (harness-attributed, see below) |
| UNVERIFIED | 2 | ci-act (same cause), chaos-disconnect |

## What the leg actually proves

- **Fresh install on a clean machine: OK.** `pip install` of the checkout into a fresh venv succeeded on
  an agent that started with no project toolchain (`Successfully installed …`).
- **Upgrade path: OK.** `pip install gitreins==0.19.0` (previous release), then install of HEAD, clean.
- **Lint: OK** (`ruff check .`, rc=0). **tests: OK** (rc=0).
- **The repo's own suite on the clean agent:** `3625 passed, 32 skipped, 237 warnings in 153.86s`.
- **Resource ceiling: PASS** — suite survives a 3 GiB cap (child_saw = subshell_saw = 3145728 KB).
- **Error path: OK** — `gitreins guard` in a scratch repo with no `.gitreins/config.yaml` exits 1 naming
  the fix (`run 'gitreins init' first`), in under the 20 s bound, with HOME isolated. This is real
  usability evidence for the CLI's failure mode and is the behaviour `DF-030`'s family depends on.

Installability on a clean machine is therefore **PROVEN** for 0.20.0, which supersedes the premise of
`DF-029` ("installability UNPROVEN this tick"). No board row was added or edited by the duplicate fire;
`DF-029` should close as already-satisfied against this directory.

## The two cells that did not verify, and why they are not product findings

- **ci-pass FAIL / ci-act UNVERIFIED** — `act rc=1`, log shows `actions/setup-python` writing
  `/opt/hostedtoolcache/Python/3.11.18/...`. That path only exists on GitHub-hosted runners; `act`
  cannot satisfy it, so the cell measures the local act image, not gitreins CI. Harness-attributed.
- **chaos-disconnect UNVERIFIED** — the disconnect log carries no `NETWORK_CUT_APPLIED` marker, so the
  cell cannot claim the cut was applied. The suite did run to completion in that window
  (`3625 passed, 32 skipped in 153.86s`), matching the known harness artifact where `curl` honours
  `ALL_PROXY` for `127.0.0.1` URLs. No product claim is made either way.

No credential was minted, no repo visibility or permission was touched, and the agent was destroyed by
`collect` (no `--keep`).
