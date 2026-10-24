#!/usr/bin/env bash
# QA-GITREINS-2 — real chaos-errorpath premise for the gitreins CLI.
#
# WHY THIS EXISTS
#   The fleet battery's generic missing-config probe runs "$BIN" in an empty
#   directory. For this repo the auto-detector picks the first long-running
#   subcommand in `gitreins --help` — `serve` — which is the judgment browser and
#   needs no config at all: it announces its port, keeps running, and only stops
#   at the probe's own bound (rc=124). The chaos-errorpath cell therefore read
#   "missing-config start probe timed out — error-path behavior UNVERIFIED" on
#   every battery run (2026-10-23 evidence under docs/dogfood/evidence/), so the
#   one property the cell exists to observe was never observed.
#
#   This probe replaces that guess with the property the cell actually tests.
#
# THE SPEC IT ENFORCES (docs/bunker-qa.md → "Chaos cells: the missing-config row")
#   With a repo present but .gitreins/config.yaml absent, a command that requires
#   that config must print an actionable message naming the missing file (or a
#   usage banner) and exit NON-ZERO, promptly. A hang, a crash, or a silent rc=0
#   is a defect, not an error path.
#
# CONTRACT (bunker-qa.sh project-probe hook: .qa/chaos-probes/errorpath.sh)
#   0 = OK      the documented clean error was observed; rc is in line 1
#   1 = FAIL    hang / crash / silent success / message that names nothing
#   2 = INFO    environment gap: no CLI to probe, so nothing was observed
#   The harness wraps this in `timeout 120`; the per-command bound below is
#   shorter on purpose, so a hang is CLASSIFIED here (exit 1, terminal FAIL)
#   instead of arriving at the harness as an ambiguous rc=124.
#
# ENV OVERRIDES (tests only; the defaults are the real thing)
#   QA_EP_BIN=path        CLI to probe                     (default: gitreins on PATH,
#                                                           else `python3 -m gitreins`)
#   QA_EP_ARGS="guard"    the subcommand that needs config (never a daemon: `serve`
#                         is long-running by design, so it has no error path to reach)
#   QA_EP_TIMEOUT=20      per-command wall bound in seconds
set -u

timeout_s="${QA_EP_TIMEOUT:-20}"
case "$timeout_s" in ''|*[!0-9]*) timeout_s=20 ;; esac

args_spec="${QA_EP_ARGS:-guard}"
read -r -a ep_args <<<"$args_spec"
[ "${#ep_args[@]}" -eq 0 ] && ep_args=(guard)

if [ -n "${QA_EP_BIN:-}" ]; then
    if [ ! -x "$QA_EP_BIN" ]; then
        echo "environment gap: QA_EP_BIN=$QA_EP_BIN is not an executable — missing-config behavior not observed"
        exit 2
    fi
    base_cmd=("$QA_EP_BIN")
elif command -v gitreins >/dev/null 2>&1; then
    base_cmd=("$(command -v gitreins)")
elif command -v python3 >/dev/null 2>&1 &&
    ( cd / && timeout 10 python3 -m gitreins --version ) >/dev/null 2>&1; then
    # Fresh agent where the console script's directory is not on PATH, but the
    # package itself runs: probe the same CLI through its module entry. The check
    # is a REAL RUN from a neutral cwd — `python3 -c 'import gitreins'` is not
    # enough, because a namespace package left on sys.path imports with
    # __file__=None and then cannot execute anything.
    base_cmd=(python3 -m gitreins)
else
    echo "environment gap: no gitreins executable to probe (PATH lookup empty, python3 -m gitreins does not run) — missing-config behavior not observed"
    exit 2
fi
ep_cmd=("${base_cmd[@]}" "${ep_args[@]}")
cmd_label="${ep_cmd[*]}"

tmp="$(mktemp -d "${TMPDIR:-/tmp}/gitreins-errorpath-XXXXXX")" || {
    echo "environment gap: could not create a scratch directory for the probe"
    exit 2
}
trap 'rm -rf "$tmp"' EXIT
mkdir -p "$tmp/home" "$tmp/repo"

# The premise is "repo present, config absent" — not "no repo" (that is a
# different, and more obvious, error). git may legitimately be absent on a bare
# agent; the CLI reports the same missing-config message either way.
if command -v git >/dev/null 2>&1; then
    ( cd "$tmp/repo" && git init -q . ) >/dev/null 2>&1 || true
fi

(
    cd "$tmp/repo" &&
        env -u GITREINS_CONFIG HOME="$tmp/home" \
            timeout -k 5 "$timeout_s" "${ep_cmd[@]}"
) >"$tmp/ep.log" 2>&1
rc=$?

first_line="$(head -1 "$tmp/ep.log" 2>/dev/null | cut -c1-200 | tr -d '\r')"
# Grade on the LOG, not just line 1: a CLI may print a warning before the error,
# and the harness only quotes the first line as detail. Both are bounded so a
# chatty log cannot flood the cell.
config_hit="$(head -25 "$tmp/ep.log" 2>/dev/null | grep -m1 -iE '\.gitreins/config\.yaml|gitreins init' | cut -c1-200 | tr -d '\r')"
usage_hit="$(head -25 "$tmp/ep.log" 2>/dev/null | grep -m1 -E '^(usage|USAGE|Usage):' | cut -c1-200 | tr -d '\r')"

if [ "$rc" -eq 124 ]; then
    echo "HANGS: '$cmd_label' never returned within ${timeout_s}s with no config present — a missing config must fail fast, not block: ${first_line:-<no output>}"
    exit 1
fi
if [ "$rc" -ge 129 ]; then
    echo "CRASH: '$cmd_label' died on a signal (rc=$rc) with no config present: ${first_line:-<no output>}"
    exit 1
fi
if grep -qiE 'panic|traceback \(most recent|segmentation fault|core dumped|fatal python error' "$tmp/ep.log" 2>/dev/null; then
    echo "CRASH: '$cmd_label' raised on the missing-config path (rc=$rc): $(grep -m1 -iE 'panic|traceback|segmentation fault|core dumped|fatal python error' "$tmp/ep.log" | cut -c1-200)"
    exit 1
fi
if [ "$rc" -eq 0 ]; then
    echo "SILENT SUCCESS: '$cmd_label' exited 0 with no config present — nothing on stdout names the missing config, so the error path was not exercised: ${first_line:-<no output>}"
    exit 1
fi
if [ "$rc" -eq 127 ]; then
    echo "environment gap: '$cmd_label' exited 127 (executable not found in the probe environment) — missing-config behavior not observed"
    exit 2
fi
if [ -n "$config_hit" ]; then
    echo "clean error on missing config: '$cmd_label' -> rc=$rc, names the missing config (${config_hit})"
    echo "probed in a scratch git repo with no .gitreins/config.yaml (bound ${timeout_s}s; HOME isolated)"
    exit 0
fi
if [ -n "$usage_hit" ]; then
    echo "usage banner on missing config: '$cmd_label' -> rc=$rc (${usage_hit})"
    echo "probed in a scratch git repo with no .gitreins/config.yaml (bound ${timeout_s}s; HOME isolated)"
    exit 0
fi
echo "UNHELPFUL ERROR: '$cmd_label' exited rc=$rc but neither named the missing config nor printed usage — the operator has nothing to act on: ${first_line:-<no output>}"
exit 1
