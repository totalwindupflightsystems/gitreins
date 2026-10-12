#!/usr/bin/env bash
# bunker-full-ci.sh — run THIS repo's full CI suite on a JIT bunker agent and
# PRINT THE IDENTITY OF THE BUILD IT MEASURED.  (REVIEW-GITREINS-011)
#
# WHY THIS EXISTS
#   The fleet runs QA batteries on clean JIT agents, and the standing harness
#   only ever recorded `native suite PASS` — never WHICH build it measured, and
#   never the suite's pass/fail counts. A 2026-09-23 capture exported two
#   revisions with `git archive` (no `.git`) and both sides reported the same
#   "17 failed / 25 errors": an environment artifact that read exactly like a
#   version finding. This script makes that misreading impossible:
#
#     * the tested tree is a real CLONE of one commit — `git rev-parse HEAD`
#       on the agent is the identity, and the clone is also what makes the
#       git-dependent tests meaningful (the git-less export is the artifact);
#     * the deployed CLI is installed FROM that clone, and its version + surface
#       are probed with scripts/check_deployed_surface.py (REVIEW-001);
#     * the toolchain is reported fresh-vs-reused;
#     * the suite's counts (passed/failed/errors/skipped) are recorded, and a
#       zero-test rc=0 run is graded FAIL, never PASS (QA-AUGER-1);
#     * the whole thing is graded by scripts/bunker_identity.py, which exits 3
#       when the run stops describing the requested build.
#
# USAGE
#   scripts/bunker-full-ci.sh run [options]        # the one command (synchronous)
#   scripts/bunker-full-ci.sh dry-run [options]    # local preflight: no agent, no suite
#   scripts/bunker-full-ci.sh self-test            # grade the fixture table, offline
#   scripts/bunker-full-ci.sh help
#
# OPTIONS
#   --repo DIR        repo to test            (default: the repo this script lives in)
#   --ref REV         revision to test        (default: HEAD)
#   --server NAME     bunker server           (default: $BUNKER_FULL_CI_SERVER)
#   --ttl DUR         agent TTL               (default: 4h)
#   --agent ID        use an EXISTING agent instead of spawning one (never destroyed)
#   --keep            leave a spawned agent alive for inspection
#   --timeout SECS    suite timeout on the agent (default: 5400)
#   --pytest-args "…" extra pytest args, e.g. "tests/test_cli.py -x" (default: none → full suite)
#   --reuse-toolchain require the agent's existing venv+CLI instead of creating them
#   --deploy-only     deploy + print identity, do not run the suite
#   --evidence DIR    evidence directory (default: /tmp/bunker-full-ci-<sha>-<ts>)
#
# EXIT CODES — the grade, not a narration
#   0  IDENTITY-OK / SUITE-PASS          5th  the run is evidence for <sha>
#   1  IDENTITY-OK / SUITE-FAIL          the tested build is exact, the suite is red
#   2  INFRA                             the run could not produce facts at all
#   3  IDENTITY-MISMATCH                 the run does NOT describe the requested build
#
# The suite run takes minutes: launch it in the background and follow the log
#   nohup scripts/bunker-full-ci.sh run > /tmp/bunker-full-ci.log 2>&1 &
#   tail -f /tmp/bunker-full-ci.log
# The identity beacon is printed as soon as the deploy is measured, and the
# full block (with counts) at the end — so a killed caller still knows the build.
set -uo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$SCRIPT_DIR/.." && pwd)
IDENTITY_PY="$SCRIPT_DIR/bunker_identity.py"
REMOTE_REPO_NAME="ci-gitreins"

EXIT_OK=0
EXIT_SUITE_FAIL=1
EXIT_INFRA=2
EXIT_NOT_EVIDENCE=3

MODE="${1:-help}"
[ $# -gt 0 ] && shift || true

usage() {
    sed -n '2,45p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

log()  { printf '%s\n' "$*"; }
step() { printf '\n── %s ──\n' "$*"; }
die()  { printf 'bunker-full-ci: %s\n' "$*" >&2; exit "$EXIT_INFRA"; }

# ── the two remote phases ───────────────────────────────────────────────────
# They are generated as real files, shipped verbatim, and printed by dry-run:
# nothing about what runs on the agent is hidden inside a nested ssh string.

phase1_script() {
    cat <<'PHASE1'
#!/usr/bin/env bash
# PHASE 1 — deploy identity: clone the tested commit, build the toolchain from
# it, probe the deployed CLI. Emits `FACT key=value` lines; never guesses.
set -uo pipefail
emit() { printf 'FACT %s=%s\n' "$1" "$2"; }
fail() { printf 'PHASE1-FAIL %s\n' "$1" >&2; exit 2; }

emit requested_sha "$REQUESTED_SHA"
emit requested_ref "${REQUESTED_REF:-HEAD}"
emit deployed_expected_files "$EXPECTED_FILES"
emit repo_path "$REMOTE_REPO"
emit agent_id "$AGENT_ID"
emit agent_server "$AGENT_SERVER"
emit python_version "$(python3 -V 2>&1 | awk '{print $2}')"
emit git_version "$(git --version 2>/dev/null | awk '{print $3}')"

# Pre-existing state FIRST — that is what "fresh vs reused" means.
PRE_VENV=""
[ -x "$CI_VENV/bin/python" ] && PRE_VENV="$CI_VENV"
PRE_CLI="$(command -v gitreins 2>/dev/null || true)"
emit pre_existing_venv "$PRE_VENV"
emit pre_existing_cli "$PRE_CLI"

command -v python3 >/dev/null 2>&1 || fail "python3 is not on the agent's PATH"
command -v git     >/dev/null 2>&1 || fail "git is not on the agent's PATH"

if [ -n "$PRE_VENV" ] && [ "${REUSE_TOOLCHAIN:-0}" = "0" ]; then
    emit toolchain_mode reused
    emit toolchain_note "venv existed before this run; it is re-pointed at the clone below"
elif [ -n "$PRE_VENV" ]; then
    emit toolchain_mode reused
    emit toolchain_note "--reuse-toolchain: the existing venv is used as-is"
else
    python3 -m venv "$CI_VENV" >/dev/null 2>&1 || fail "python3 -m venv failed (python3-venv missing on the agent?)"
    emit toolchain_mode fresh
    emit toolchain_note "venv created by this run"
fi
emit toolchain_path "$CI_VENV"
PY="$CI_VENV/bin/python"

# Ship-side assertions: the bundle must be complete BEFORE anything is trusted.
[ -f "$BUNDLE" ] || fail "the shipped bundle is missing ($BUNDLE)"
BUNDLE_BYTES="$(stat -c '%s' "$BUNDLE" 2>/dev/null || echo 0)"
emit bundle_bytes "$BUNDLE_BYTES"
emit bundle_expected_bytes "${EXPECTED_BUNDLE_BYTES:-}"

# Clone FIRST: the toolchain is installed FROM the clone, so the tree has to
# exist before `pip install -e` — and the landed file count is asserted against
# the source tree's tracked count, which is what catches a partial ship
# (GAP-144) before anything is measured.
rm -rf "$REMOTE_REPO"
git clone --quiet "$BUNDLE" "$REMOTE_REPO" || fail "git clone from the shipped bundle failed"
( cd "$REMOTE_REPO" && git checkout --quiet "$REQUESTED_SHA" ) \
    || fail "checkout $REQUESTED_SHA failed — the commit is not in the bundle"
DEPLOYED_SHA="$(git -C "$REMOTE_REPO" rev-parse HEAD)"
DEPLOYED_FILES="$(git -C "$REMOTE_REPO" ls-files | wc -l | tr -d ' ')"
emit deployed_sha "$DEPLOYED_SHA"
emit deployed_files "$DEPLOYED_FILES"
emit deployed_dir "$REMOTE_REPO"

if [ "${REUSE_TOOLCHAIN:-0}" = "0" ]; then
    "$PY" -m pip install --quiet --disable-pip-version-check --upgrade pip >/dev/null 2>&1 || true
    "$PY" -m pip install --quiet --disable-pip-version-check -e "$REMOTE_REPO" \
        || fail "pip install -e <clone> failed (network or dependency resolution on the agent)"
    "$PY" -m pip install --quiet --disable-pip-version-check pytest pytest-timeout pytest-xdist \
        || fail "pip install pytest/pytest-timeout/pytest-xdist failed"
fi

export PATH="$CI_VENV/bin:$PATH"
CLI_PATH="$(command -v gitreins || true)"
[ -n "$CLI_PATH" ] || fail "no gitreins on PATH after install"
CLI_VERSION="$("$CI_VENV/bin/gitreins" --version 2>&1 | awk '{print $NF}')"
CLI_DIST_ROOT="$("$PY" -c 'import os, gitreins; print(os.path.dirname(os.path.dirname(os.path.abspath(gitreins.__file__))))' 2>/dev/null || true)"
FROM_TREE=0
[ "$CLI_DIST_ROOT" = "$REMOTE_REPO" ] && FROM_TREE=1
emit cli_path "$CLI_PATH"
emit cli_version "$CLI_VERSION"
emit cli_dist_root "$CLI_DIST_ROOT"
emit cli_from_tree "$FROM_TREE"

# The surface probe is the repo's own check (REVIEW-001): deployed vs tree.
cd "$REMOTE_REPO" || fail "cannot cd to $REMOTE_REPO"
"$PY" scripts/check_deployed_surface.py --json > "$HOME/ci-surface.json" 2> "$HOME/ci-surface.err"
SURF_RC=$?
emit surface_rc "$SURF_RC"
if [ -s "$HOME/ci-surface.json" ]; then
    emit surface_json "$("$PY" -c 'import json, sys; print(json.dumps(json.load(open(sys.argv[1]))))' "$HOME/ci-surface.json")"
else
    emit surface_json ""
    printf 'surface probe produced no output:\n' >&2
    head -5 "$HOME/ci-surface.err" >&2 || true
fi
printf 'PHASE1-OK deployed=%s files=%s cli=%s from_tree=%s surface_rc=%s\n' \
    "${DEPLOYED_SHA:0:12}" "$DEPLOYED_FILES" "$CLI_VERSION" "$FROM_TREE" "$SURF_RC"
PHASE1
}

phase2_script() {
    cat <<'PHASE2'
#!/usr/bin/env bash
# PHASE 2 — the suite. Bounded, logged, and counted: rc, wall time and the
# pytest summary line are facts, because "PASS" without counts is not evidence.
set -uo pipefail
emit() { printf 'FACT %s=%s\n' "$1" "$2"; }
PY="$CI_VENV/bin/python"
LOG="$HOME/ci-full.log"
cd "$REMOTE_REPO" || { printf 'PHASE2-FAIL cannot cd to %s\n' "$REMOTE_REPO" >&2; exit 2; }

SUITE_ARGS_LOCAL=()
if [ -n "${SUITE_ARGS:-}" ]; then read -r -a SUITE_ARGS_LOCAL <<< "$SUITE_ARGS"; fi
SUITE_CMD="$PY -m pytest -q -p no:cacheprovider ${SUITE_ARGS:-}"
emit suite_cmd "$SUITE_CMD"
emit suite_log "$LOG"
emit suite_started "$(date -u +%Y-%m-%dT%H:%M:%SZ)"

# The collected total FIRST, by the same mechanism the repo's docs gate uses
# (scripts/check_docs_drift.py: `pytest --collect-only -q --override-ini=addopts=`).
# It is the authority on whether the suite actually has tests: a run whose
# summary line cannot be parsed is still gradeable, and a zero-test rc=0 run
# can never read as a pass.
COLLECT_LOG="$HOME/ci-collect.log"
COLLECT_CMD="$PY -m pytest --collect-only -q --override-ini=addopts= -p no:cacheprovider ${SUITE_ARGS:-}"
emit collect_cmd "$COLLECT_CMD"
timeout -k 30 600 "$PY" -m pytest --collect-only -q --override-ini=addopts= -p no:cacheprovider "${SUITE_ARGS_LOCAL[@]}" > "$COLLECT_LOG" 2>&1
COLLECT_RC=$?
COLLECTED="$(grep -oE '^[0-9]+ tests? collected' "$COLLECT_LOG" | tail -1 | awk '{print $1}')"
emit collect_rc "$COLLECT_RC"
emit suite_collected "${COLLECTED:-0}"
if [ "$COLLECT_RC" != "0" ]; then
    printf 'collection was not measurable (rc=%s); tail:\n' "$COLLECT_RC" >&2
    tail -5 "$COLLECT_LOG" >&2 || true
fi

START=$(date +%s)
timeout -k 30 "$SUITE_TIMEOUT" "$PY" -m pytest -q -p no:cacheprovider "${SUITE_ARGS_LOCAL[@]}" > "$LOG" 2>&1
RC=$?
END=$(date +%s)
DUR=$(( END - START ))
# pytest's summary line is the last line that ends with its duration; failure
# text earlier in the log can carry numbers, so anchor on the duration shape.
SUMMARY="$(grep -E ' in [0-9]+(:[0-9]{2})*(\.[0-9]+)?s?$' "$LOG" | tail -1)"
emit suite_ran 1
emit suite_rc "$RC"
emit suite_duration_s "$DUR"
emit suite_summary "$SUMMARY"
[ "$RC" = "124" ] && emit notes "suite killed at the ${SUITE_TIMEOUT}s bound (rc=124)"

printf '\n--- last 15 lines of %s ---\n' "$LOG"
tail -15 "$LOG"
printf -- '--- end of suite log ---\n'
printf 'PHASE2-OK rc=%s duration=%ss\n' "$RC" "$DUR"
PHASE2
}

# ── host helpers ────────────────────────────────────────────────────────────

identity_python() {
    # The identity gate is stdlib-only: prefer the repo venv, fall back to python3.
    if [ -x "$REPO_ROOT/.venv/bin/python" ]; then
        printf '%s\n' "$REPO_ROOT/.venv/bin/python"
    elif command -v python3 >/dev/null 2>&1; then
        command -v python3
    else
        return 1
    fi
}

resolve_server() { # <explicit server or empty> -> a server name for mutating commands
    # `bunker spawn`/`destroy` never fall back to the shared active server, so
    # an omitted --server is resolved here from the CLI's own active server.
    local server="$1"
    if [ -z "$server" ]; then
        server=$(bunker list 2>&1 >/dev/null | sed -n 's/.*reading server "\(.*\)".*/\1/p' | head -1)
    fi
    printf '%s\n' "$server"
}

record_ledger() { # <gitreins bin> <evid dir> <sha> <agent> <server> <exit code> <note>
    local gitreins_bin="$1" evid="$2" sha="$3" agent="$4" server="$5" code="$6" note="$7"
    local verdict cell_identity cell_suite
    case "$code" in
        0) verdict=PASS;    cell_identity=OK;         cell_suite=PASS ;;
        1) verdict=FAIL;    cell_identity=OK;         cell_suite=FAIL ;;
        3) verdict=FAIL;    cell_identity=FAIL;       cell_suite=UNVERIFIED ;;
        *) verdict=UNKNOWN; cell_identity=UNVERIFIED; cell_suite=UNVERIFIED ;;
    esac
    "$gitreins_bin" qa record --kind bunker --agent "$agent" --server "$server" \
        --verdict "$verdict" --exit-code "$code" --commit "$sha" \
        --cell "identity=$cell_identity" --cell "suite=$cell_suite" \
        --evidence "$evid/facts.json" --note "$note" >/dev/null 2>&1 \
        || log "WARN: could not record the QA-ledger row (recording never fails a run)"
}

# ── dry-run ─────────────────────────────────────────────────────────────────

dry_run() {
    local REPO="$REPO_ROOT" REF="HEAD" SERVER="${BUNKER_FULL_CI_SERVER:-}" TTL=4h AGENT=""
    local SUITE_TIMEOUT=5400 PYTEST_ARGS="" EVIDENCE_DIR="" REUSE_TOOLCHAIN=0 DEPLOY_ONLY=0
    parse_args "$@"
    cd "$REPO" || die "no such repo: $REPO"
    local sha dirty expected evid py
    sha=$(git rev-parse --verify "${REF}^{commit}" 2>/dev/null) || die "cannot resolve ${REF} in $REPO"
    dirty=$(git status --porcelain | wc -l | tr -d ' ')
    expected=$(git ls-tree -r --name-only "$sha" | wc -l | tr -d ' ')
    evid=$(mktemp -d "${TMPDIR:-/tmp}/bunker-full-ci-dryrun-XXXXXX")
    py=$(identity_python) || die "no python3 for the identity gate"

    git bundle create "$evid/repo.bundle" "$REF" >/dev/null 2>&1 \
        || git bundle create "$evid/repo.bundle" "$sha" --branches >/dev/null 2>&1 \
        || die "git bundle create failed for ${REF} — the deploy cannot be shipped"
    local bundle_bytes
    bundle_bytes=$(stat -c '%s' "$evid/repo.bundle")
    phase1_script > "$evid/ci-phase1.sh"
    phase2_script > "$evid/ci-phase2.sh"

    log "==================== BUNKER FULL-CI — DRY RUN ===================="
    log "would test      : $REPO ref=$REF sha=${sha:0:12}…  ($( [ "$dirty" = "0" ] && echo clean || echo "$dirty uncommitted path(s) — NOT in the clone" ))"
    log "tracked files   : $expected  (the landed count is asserted against this)"
    log "bundle          : $evid/repo.bundle ($((bundle_bytes / 1024)) KB)  ← shipped once, cloned on the agent"
    log "agent           : $( [ -n "$AGENT" ] && echo "reuse $AGENT" || echo "spawn on ${SERVER:-<active server>} ttl $TTL" )"
    log "toolchain       : $( [ "$REUSE_TOOLCHAIN" = "1" ] && echo "reuse the agent's existing venv (its age is printed)" || echo "fresh venv + pip install -e <clone> + pytest plugins" )"
    if [ "$DEPLOY_ONLY" = "1" ]; then
        log "suite           : SKIPPED (--deploy-only)"
    else
        log "suite           : $evid/ci-phase2.sh → \"\$PY -m pytest -q -p no:cacheprovider ${PYTEST_ARGS}\" (timeout ${SUITE_TIMEOUT}s)"
    fi
    log "identity printed: requested sha · tested-tree sha · landed file count · deployed CLI version ·"
    log "                  surface probe (scripts/check_deployed_surface.py) · fresh-vs-reused toolchain · suite counts"
    log "exit codes      : 0 identity ok+suite pass · 1 suite red · 3 IDENTITY MISMATCH · 2 infra"
    log ""
    log "--- phase 1 script (shipped verbatim to the agent) ---"
    cat "$evid/ci-phase1.sh"
    log "--- phase 2 script (shipped verbatim to the agent) ---"
    cat "$evid/ci-phase2.sh"
    log ""
    log "VERDICT         : NOT EVALUATED (dry-run — nothing deployed, no suite run)"
    log "evidence        : $evid"
    log "================================================================="
    return 0
}

# ── argument parsing ────────────────────────────────────────────────────────

parse_args() {
    while [ $# -gt 0 ]; do
        case "$1" in
            --repo) REPO="${2:?--repo needs a value}"; shift 2 ;;
            --ref) REF="${2:?--ref needs a value}"; shift 2 ;;
            --server) SERVER="${2:?--server needs a value}"; shift 2 ;;
            --ttl) TTL="${2:?--ttl needs a value}"; shift 2 ;;
            --agent) AGENT="${2:?--agent needs a value}"; shift 2 ;;
            --keep) KEEP=1; shift ;;
            --timeout) SUITE_TIMEOUT="${2:?--timeout needs a value}"; shift 2 ;;
            --pytest-args) PYTEST_ARGS="${2:?--pytest-args needs a value}"; shift 2 ;;
            --reuse-toolchain) REUSE_TOOLCHAIN=1; shift ;;
            --deploy-only) DEPLOY_ONLY=1; shift ;;
            --evidence) EVIDENCE_DIR="${2:?--evidence needs a value}"; shift 2 ;;
            -h|--help) usage; exit 0 ;;
            *) die "unknown option: $1 (see --help)" ;;
        esac
    done
    return 0
}

# ── run ─────────────────────────────────────────────────────────────────────

run_pipeline() {
    local REPO="$REPO_ROOT" REF="HEAD" SERVER="${BUNKER_FULL_CI_SERVER:-}" TTL=4h AGENT=""
    local SUITE_TIMEOUT=5400 PYTEST_ARGS="" EVIDENCE_DIR="" REUSE_TOOLCHAIN=0 DEPLOY_ONLY=0
    local KEEP=0
    parse_args "$@"

    cd "$REPO" || die "no such repo: $REPO"
    command -v bunker >/dev/null 2>&1 || die "the bunker CLI is not on PATH"
    SERVER=$(resolve_server "$SERVER")
    [ -n "$SERVER" ] || die "no bunker server: pass --server NAME or set BUNKER_FULL_CI_SERVER"
    local py
    py=$(identity_python) || die "no python3 for the identity gate"

    local sha dirty expected dirty_flag=()
    sha=$(git rev-parse --verify "${REF}^{commit}" 2>/dev/null) || die "cannot resolve ${REF} in $REPO"
    dirty=$(git status --porcelain | wc -l | tr -d ' ')
    expected=$(git ls-tree -r --name-only "$sha" | wc -l | tr -d ' ')
    [ "$dirty" != "0" ] && dirty_flag=(--dirty)

    local evid
    if [ -n "$EVIDENCE_DIR" ]; then
        evid="$EVIDENCE_DIR"; mkdir -p "$evid"
    else
        evid=$(mktemp -d "${TMPDIR:-/tmp}/bunker-full-ci-${sha:0:12}-XXXXXX")
    fi

    step "request"
    log "repo     : $REPO"
    log "requested: ref=$REF sha=$sha ($( [ "$dirty" = "0" ] && echo clean || echo "DIRTY — $dirty uncommitted path(s), not in the clone" ))"
    log "files    : $expected tracked at that commit"
    log "evidence : $evid"

    step "bundle"
    git bundle create "$evid/repo.bundle" "$REF" >/dev/null 2>&1 \
        || git bundle create "$evid/repo.bundle" "$sha" --branches >/dev/null 2>&1 \
        || die "git bundle create failed for ${REF}"
    log "bundle  : $evid/repo.bundle ($(( $(stat -c '%s' "$evid/repo.bundle") / 1024 )) KB) — a real clone source, .git included"

    phase1_script > "$evid/ci-phase1.sh"
    phase2_script > "$evid/ci-phase2.sh"

    local spawned=0
    # The EXIT trap fires after run_pipeline's locals are gone, so every value it
    # reads is defaulted here — an unbound variable under `set -u` would mask the
    # run's own exit code with a teardown error (observed: "spawned: unbound variable"
    # after a graded run had already printed its verdict).
    cleanup() {
        if [ "${spawned:-0}" = "1" ] && [ "${KEEP:-0}" != "1" ] && [ "${BUNKER_FULL_CI_KEEP:-0}" != "1" ]; then
            log "── teardown: destroying agent ${AGENT:-} on ${SERVER:-}"
            bunker destroy "${AGENT:-}" --server "${SERVER:-}" >/dev/null 2>&1 || true
        elif [ "${spawned:-0}" = "1" ]; then
            log "── teardown kept: agent ${AGENT:-} on ${SERVER:-} (--keep)"
        fi
    }
    trap cleanup EXIT

    if [ -z "$AGENT" ]; then
        local spawn_out
        spawn_out=$(bunker spawn --server "$SERVER" --ttl "$TTL" 2>&1) || die "bunker spawn failed on ${SERVER}: $(printf '%s' "$spawn_out" | tail -3)"
        AGENT=$(printf '%s' "$spawn_out" | grep -oP 'keys/\K[a-f0-9]+' | head -1)
        [ -n "$AGENT" ] || die "could not parse the agent id from bunker spawn output: $(printf '%s' "$spawn_out" | tail -3)"
        spawned=1
        log "── agent spawned: $AGENT on $SERVER (ttl $TTL)"
    else
        log "── reusing agent $AGENT on $SERVER (it will not be destroyed)"
    fi

    local remote_home
    remote_home=$(bunker run --server "$SERVER" --timeout 120 "$AGENT" -- sh -c 'echo $HOME' 2>/dev/null | tr -d '\r' | tail -1)
    [ -n "$remote_home" ] || remote_home="/home/bunker-$AGENT"
    log "── agent home: $remote_home"

    step "ship"
    # Never swallow a ship error: a partial copy is the GAP-144 failure mode, and
    # the reason has to be readable without re-running the whole lane.
    ship() { # <local file> <remote path>
        local out
        # `bunker cp` is a MUTATING command: it never falls back to the shared
        # active server, so --server must be passed explicitly.
        if ! out=$(bunker cp --server "$SERVER" "$1" "$AGENT:$2" 2>&1); then
            printf 'bunker cp failed (%s -> %s):\n%s\n' "$1" "$2" "$out" >&2
            return 1
        fi
        return 0
    }
    ship "$evid/repo.bundle"  "$remote_home/repo.bundle"  || die "could not ship the bundle to $AGENT"
    ship "$evid/ci-phase1.sh" "$remote_home/ci-phase1.sh" || die "could not ship the phase 1 script to $AGENT"
    ship "$evid/ci-phase2.sh" "$remote_home/ci-phase2.sh" || die "could not ship the phase 2 script to $AGENT"
    log "shipped : repo.bundle, ci-phase1.sh, ci-phase2.sh"

    step "phase 1 — deploy identity"
    local p1 p1_ok=1
    p1=$(bunker run --server "$SERVER" --timeout 900 "$AGENT" -- env \
        "REQUESTED_SHA=$sha" "REQUESTED_REF=$REF" "EXPECTED_FILES=$expected" \
        "EXPECTED_BUNDLE_BYTES=$(stat -c '%s' "$evid/repo.bundle")" \
        "REMOTE_REPO=$remote_home/$REMOTE_REPO_NAME" "BUNDLE=$remote_home/repo.bundle" \
        "CI_VENV=$remote_home/ci-tools/venv" "REUSE_TOOLCHAIN=$REUSE_TOOLCHAIN" \
        "AGENT_ID=$AGENT" "AGENT_SERVER=$SERVER" \
        bash "$remote_home/ci-phase1.sh" 2>&1)
    printf '%s\n' "$p1" | tee "$evid/phase1.log"
    printf '%s\n' "$p1" | grep -q '^PHASE1-OK' || p1_ok=0

    # Facts are assembled either way: a failed deploy is exactly the case the
    # identity block must describe loudly, not hide behind an infra error.
    printf '%s\n' "$p1" | "$py" "$IDENTITY_PY" collect --facts-lines - --requested-sha "$sha" \
        --ref "$REF" --repo-path "$REPO" --expected-files "$expected" --mode run \
        "${dirty_flag[@]}" --out "$evid/facts.json" \
        || die "could not assemble the deploy facts ($evid/phase1.log)"

    if [ "$p1_ok" = "0" ]; then
        step "identity (phase 1 did not complete)"
        local p1_code=0
        if "$py" "$IDENTITY_PY" render --facts "$evid/facts.json" | tee "$evid/identity.txt"; then
            p1_code=0
        else
            p1_code=$?
        fi
        log ""
        log "phase 1 did not complete — no deploy identity was measured (log: $evid/phase1.log)"
        [ "$p1_code" = "$EXIT_NOT_EVIDENCE" ] || p1_code=$EXIT_INFRA
        exit "$p1_code"
    fi

    # The beacon: one grep-able line as soon as the deploy is measured, so a
    # killed caller still knows which build the agent is holding.
    log ""
    log "IDENTITY BEACON (suite not yet run):"
    "$py" "$IDENTITY_PY" render --facts "$evid/facts.json" --line || true

    local suite_ran=0
    if [ "$DEPLOY_ONLY" = "1" ]; then
        step "suite — SKIPPED (--deploy-only)"
    else
        step "phase 2 — full suite (timeout ${SUITE_TIMEOUT}s)"
        suite_ran=1
        bunker run --server "$SERVER" --timeout $((SUITE_TIMEOUT + 300)) "$AGENT" -- env \
            "REMOTE_REPO=$remote_home/$REMOTE_REPO_NAME" "CI_VENV=$remote_home/ci-tools/venv" \
            "SUITE_TIMEOUT=$SUITE_TIMEOUT" "SUITE_ARGS=$PYTEST_ARGS" \
            bash "$remote_home/ci-phase2.sh" 2>&1 | tee "$evid/phase2.log" || true
    fi

    cat "$evid/phase1.log" "$evid/phase2.log" 2>/dev/null > "$evid/facts-lines.txt" || true

    step "identity"
    local code=0
    if "$py" "$IDENTITY_PY" collect --facts-lines "$evid/facts-lines.txt" --requested-sha "$sha" \
        --ref "$REF" --repo-path "$REPO" --expected-files "$expected" --mode run \
        "${dirty_flag[@]}" --out "$evid/facts.json" --grade | tee "$evid/identity.txt"; then
        code=0
    else
        code=$?   # pipefail: the grader's exit code, not tee's
    fi

    local extra=""
    [ "$suite_ran" = "0" ] && extra=" (deploy-only: suite not run)"

    # Only the three graded codes are legitimate results; anything else is infra.
    case "$code" in
        "$EXIT_OK"|"$EXIT_SUITE_FAIL"|"$EXIT_NOT_EVIDENCE") ;;
        *) log "WARN: unexpected grade '$code' from the identity gate — reported as infra"; code=$EXIT_INFRA ;;
    esac

    local gitreins_bin=""
    if [ -x "$REPO/.venv/bin/gitreins" ]; then gitreins_bin="$REPO/.venv/bin/gitreins"
    elif command -v gitreins >/dev/null 2>&1; then gitreins_bin=$(command -v gitreins); fi
    if [ -n "$gitreins_bin" ]; then
        local note
        note=$("$py" "$IDENTITY_PY" render --facts "$evid/facts.json" --line 2>/dev/null || true)
        record_ledger "$gitreins_bin" "$evid" "$sha" "$AGENT" "$SERVER" "$code" "${note}${extra}"
        log "ledger  : recorded (kind=bunker, commit=${sha:0:12}, exit=$code)"
    else
        log "WARN: no gitreins binary on the host — QA-ledger row not recorded"
    fi

    log ""
    log "evidence: $evid"
    return "$code"
}

# ── entry ───────────────────────────────────────────────────────────────────

case "$MODE" in
    run)       run_pipeline "$@" ;;
    dry-run)   dry_run "$@" ;;
    self-test) python3 "$IDENTITY_PY" self-test ;;
    help|-h|--help) usage ;;
    *)         printf 'bunker-full-ci: unknown mode %s\n\n' "$MODE" >&2; usage; exit "$EXIT_INFRA" ;;
esac
