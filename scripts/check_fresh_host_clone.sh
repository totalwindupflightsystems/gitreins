#!/usr/bin/env bash
# DF-GITREINS-POC-87 — the fresh-host clone contract, asserted instead of assumed.
#
# WHY THIS EXISTS
#   Every documented clone path in this repo's history named the repo and nothing
#   else, while the repo's own origin is SSH. On a brand-new box with no
#   github.com entry in ~/.ssh/known_hosts and no ssh-agent, `git clone
#   git@github.com:...` dies in about a second with
#
#       Host key verification failed.
#       fatal: Could not read from remote repository.
#
#   (dogfood 2026-10-09 run 18, bunker leg: ssh rc=128 in 3s, https clone OK in
#   3s). A fresh user following "clone the repo" therefore hits a wall that is
#   not a product defect and is not fixable by reading the code. README.md now
#   names both ways out — trust github.com with ssh-keyscan, or clone over https
#   — and this probe keeps that signpost honest by reproducing BOTH arms.
#
# THE CONTRACT IT ENFORCES (docs/bunker-qa.md → "The fresh-host clone row")
#   Arm 1 (ssh): with an isolated, empty HOME and no ssh-agent, cloning the
#   documented PUBLIC repo over SSH fails with "Host key verification failed".
#   Arm 2 (https): cloning the same repo over https succeeds (exit 0) and leaves
#   a real clone behind (.git present).
#   If arm 1 ever stops reproducing the failure the documented prerequisite has
#   become fiction; if arm 2 fails, the README points at a path that does not
#   work. Either is a FAIL, never a silent pass.
#
# CONTRACT (exit codes; the exit code IS the grade)
#   0 = OK    both arms reproduced: ssh fails on host keys, https clone succeeds
#   1 = FAIL  a documented path does not behave as the README says
#   2 = INFO  environment gap — no git, no network, or both arms unreachable, so
#             NOTHING was observed. A gap is never reported as a pass.
#   (The harness/CI wraps this in its own timeout; the per-arm bound below is
#   shorter on purpose so a hang is classified here.)
#
# ISOLATION (what makes this "fresh host", stated so it is not mistaken for the
# operator's box):
#   * HOME is a throwaway directory: no ~/.ssh/known_hosts, no ~/.gitconfig.
#   * `ssh -F /dev/null` ignores the operator's ssh config; GlobalKnownHostsFile
#     is /dev/null so /etc/ssh/ssh_known_hosts cannot mask the fresh-host case.
#   * BatchMode=yes is the deterministic stand-in for "a user who cannot be
#     prompted": a real fresh box with no tty fails exactly the same way.
#   * SSH_AUTH_SOCK / GIT_SSH / GIT_TERMINAL_PROMPT / GIT_ASKPASS are neutralised
#     and GIT_CONFIG_NOSYSTEM=1 so a system url.<base>.insteadOf rewrite cannot
#     turn the ssh arm into an https one (that would test nothing).
#
# ENV OVERRIDES (used by tests/test_fresh_host_clone_probe.py; defaults are real)
#   QA_FHC_GIT=path        git binary to drive            (default: git on PATH)
#   QA_FHC_SSH_URL=url     ssh clone URL                  (default: this repo)
#   QA_FHC_HTTPS_URL=url   https clone URL                (default: this repo)
#   QA_FHC_TIMEOUT=secs    per-arm wall bound             (default: 120)
#   QA_FHC_KEEP=1          keep the temp dirs for inspection
#
# Usage:
#   scripts/check_fresh_host_clone.sh; echo "probe rc=$?"
set -u

SSH_URL="${QA_FHC_SSH_URL:-git@github.com:totalwindupflightsystems/gitreins.git}"
HTTPS_URL="${QA_FHC_HTTPS_URL:-https://github.com/totalwindupflightsystems/gitreins.git}"
GIT_BIN="${QA_FHC_GIT:-git}"
TIMEOUT_S="${QA_FHC_TIMEOUT:-120}"
case "$TIMEOUT_S" in ''|*[!0-9]*) TIMEOUT_S=120 ;; esac

if ! command -v "$GIT_BIN" >/dev/null 2>&1; then
    echo "fresh-host-clone INFO: environment gap — '$GIT_BIN' is not on PATH, so neither clone arm was observed"
    exit 2
fi

work=$(mktemp -d "${TMPDIR:-/tmp}/gitreins-fresh-clone.XXXXXX") || {
    echo "fresh-host-clone INFO: environment gap — cannot create a scratch directory"
    exit 2
}
cleanup() { [ "${QA_FHC_KEEP:-0}" = "1" ] || rm -rf "$work"; }
trap cleanup EXIT INT TERM

home="$work/home"
mkdir -p "$home/.ssh"
ssh_known_hosts="$home/.ssh/known_hosts"
iso_ssh="ssh -F /dev/null -o BatchMode=yes -o StrictHostKeyChecking=ask"
iso_ssh="$iso_ssh -o UserKnownHostsFile=$ssh_known_hosts -o GlobalKnownHostsFile=/dev/null"
iso_ssh="$iso_ssh -o ConnectTimeout=10"

# ── arm 1: ssh clone on a fresh host must FAIL on host keys ────────────────────
ssh_log="$work/ssh-arm.log"
start=$SECONDS
env -u SSH_AUTH_SOCK -u GIT_SSH -u GIT_SSH_COMMAND \
    HOME="$home" GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=false GIT_CONFIG_NOSYSTEM=1 \
    GIT_SSH_COMMAND="$iso_ssh" \
    timeout "$TIMEOUT_S" "$GIT_BIN" clone --depth 1 --single-branch \
    "$SSH_URL" "$work/ssh-clone" >"$ssh_log" 2>&1
ssh_rc=$?
ssh_secs=$((SECONDS - start))
ssh_out=$(cat "$ssh_log" 2>/dev/null || true)
ssh_tail=$(printf '%s' "$ssh_out" | tail -1 | cut -c1-160)

case "$ssh_rc" in
    0)
        echo "fresh-host-clone FAIL: the ssh clone of $SSH_URL SUCCEEDED under an isolated HOME with no known_hosts — the probe is not isolating what it claims, so the documented prerequisite is unverified"
        find "$work/ssh-clone" -maxdepth 1 -mindepth 1 2>/dev/null | head -3
        exit 1
        ;;
    124)
        echo "fresh-host-clone INFO: environment gap — the ssh arm hit the ${TIMEOUT_S}s bound (no answer from the network); nothing observed"
        exit 2
        ;;
esac

if ! printf '%s\n' "$ssh_out" | grep -qi 'host key verification failed'; then
    # Distinguish "we never reached the host" (a gap: no network) from "we
    # reached it and something else went wrong" (a defect worth failing on).
    if printf '%s\n' "$ssh_out" | grep -qiE 'could not resolve hostname|network is unreachable|connection timed out|connection refused|temporary failure in name resolution|no route to host'; then
        echo "fresh-host-clone INFO: environment gap — the ssh arm could not reach the host (${ssh_tail}); nothing observed"
        exit 2
    fi
    echo "fresh-host-clone FAIL: the ssh arm failed (rc=$ssh_rc) but NOT with 'Host key verification failed' — a different failure means the documented prerequisite is not the one a fresh user hits: ${ssh_tail}"
    exit 1
fi
echo "fresh-host-clone arm1 OK: ssh clone without known_hosts fails on host keys (rc=$ssh_rc, ${ssh_secs}s)"

# ── arm 2: the https clone of the same public repo must SUCCEED ───────────────
https_log="$work/https-arm.log"
start=$SECONDS
env -u SSH_AUTH_SOCK -u GIT_SSH -u GIT_SSH_COMMAND \
    HOME="$home" GIT_TERMINAL_PROMPT=0 GIT_ASKPASS=false GIT_CONFIG_NOSYSTEM=1 \
    timeout "$TIMEOUT_S" "$GIT_BIN" clone --depth 1 --single-branch \
    "$HTTPS_URL" "$work/https-clone" >"$https_log" 2>&1
https_rc=$?
https_secs=$((SECONDS - start))
https_tail=$(tail -1 "$https_log" 2>/dev/null | cut -c1-160)
https_dir="$work/https-clone"

if [ "$https_rc" -ne 0 ]; then
    # The ssh arm just proved the host is reachable, so this is not a network
    # gap — the README's recommended path does not work.
    echo "fresh-host-clone FAIL: the https clone of $HTTPS_URL exited rc=$https_rc while the host was reachable over ssh — the documented clone path does not work: ${https_tail}"
    exit 1
fi
if [ ! -d "$https_dir" ] || [ -z "$(find "$https_dir" -maxdepth 1 -mindepth 1 2>/dev/null | head -1)" ]; then
    echo "fresh-host-clone FAIL: the https clone reported rc=0 but left no clone behind at $https_dir — a success that ships nothing is not a clone"
    exit 1
fi
files=$(cd "$https_dir" && find . -mindepth 1 -maxdepth 1 -print 2>/dev/null | wc -l | tr -d ' ')
head_ref=$("$GIT_BIN" --git-dir="$https_dir/.git" rev-parse --short HEAD 2>/dev/null || echo unknown)

echo "fresh-host-clone arm2 OK: https clone succeeded (rc=0, ${https_secs}s, top-level entries=$files, HEAD=$head_ref)"
echo "fresh-host-clone OK: both documented fresh-host arms reproduced — ssh needs github.com in known_hosts (or an https origin); https clone works as shipped"
exit 0
