# Load reproduction on a shared host

Some failures only appear under load (the gopls quiescent-spawn race, the
detached-judge poll deadline, worktree reaping while a parallel repro farm
runs). Reproducing them is part of the job — but the load must never outlive the
run that needed it.

## Why this page exists (INT-FLAKE-5)

A tick reproduced a load-dependent flake with detached shell loops:

```sh
setsid sh -c 'while :; do :; done' &   # never do this
```

`setsid` detaches the loop into its own session, so it is not in the runner's
process group and **survives the runner being killed** — including the runner
being killed because it was finished, timed out, or stopped by an operator. The
result was 24+ orphaned CPU burners and a load average of 33.8 on the machine
that also runs the Hermes gateway, the fleet scheduler and DuckBrain: a
verification script became a control-plane incident, fixed only by killing the
survivors by hand.

## The rule

1. **Never spawn detached CPU loops** (`setsid`, `nohup`, `&` from a script that
   may be killed, `screen`/`tmux`-hosted spin loops). Nothing that can outlive
   its runner.
2. **Use `scripts/loadgen.py`** — children are daemonic *and* armed with
   `PR_SET_PDEATHSIG`, so the kernel SIGKILLs them when the parent dies, even if
   the parent was SIGKILLed. Worker count (`MAX_WORKERS`), duration
   (`MAX_SECONDS`) and the CPU set are capped, and the runner verifies its own
   cleanup and exits non-zero if a survivor is found:

   ```sh
   python scripts/loadgen.py --workers 4 --seconds 60 --cpus 0-3
   ```
3. **Refuse by default on a shared host.** `loadgen` will not start when the
   shared Hermes services are running (gateway / cron scheduler / DuckBrain /
   schedulerd) unless `--allow-shared-host` (or
   `GITREINS_LOADGEN_ALLOW_SHARED_HOST=1`) is set — and that override is only for
   runs where the affected fleet lane is paused. The intended host for long or
   heavy reproduction is a bunker box.
4. **Disclose the load in the tick report**: the harness used, the worker count,
   the duration, and the load average observed.
5. **Keep the guarantee tested.** `tests/test_loadgen.py` includes a live check
   that SIGKILLs the runner and asserts no burner survives; run it on any change
   to `scripts/loadgen.py`.

## What counts as "shared host"

If `ps` shows the Hermes gateway (`hermes_cli.main gateway run`), the cron
scheduler (`cron.scheduler`), DuckBrain (`duckbrain.js`) or the fleet scheduler
daemon (`schedulerd`), you are on the control plane. `loadgen`'s detector uses
exactly those markers, so an unknown box starts clean and a shared one refuses.
