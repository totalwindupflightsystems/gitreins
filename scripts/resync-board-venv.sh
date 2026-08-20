#!/usr/bin/env bash
# resync-board-venv.sh — Re-sync the board venv's gitreins engine from this repo HEAD.
#
# GR-GAP-040: the board venv (~/.hermes/venvs/board, python 3.14) had a stale
# snapshot install (0.8.2) whose engine/ package lacked the deepseek clamp
# (GR-068: _is_deepseek / _clamp_max_tokens), causing judge LLM HTTP 400s on
# max_output_tokens > 393216. This script reinstalls from the repo, preferring
# an editable install so the engine can never drift from repo HEAD again.
#
# Idempotent: re-running reinstalls the same editable pointer (fast no-op) and
# re-verifies. Exits non-zero if any verification fails.
#
# Usage: scripts/resync-board-venv.sh
# Env override: BOARD_VENV=/path/to/venv (default ~/.hermes/venvs/board)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="${BOARD_VENV:-$HOME/.hermes/venvs/board}"
PY="$VENV/bin/python3"
PIP="$VENV/bin/pip"
CLI="$VENV/bin/gitreins"

log() { echo "[resync] $*"; }
die() { echo "[resync] FATAL: $*" >&2; exit 1; }

[ -x "$PIP" ] || die "pip not found at $PIP (set BOARD_VENV to override)"
[ -f "$REPO_ROOT/pyproject.toml" ] || die "repo root not found at $REPO_ROOT"

# --- Install: editable preferred, force-reinstall snapshot fallback ----------
EDITABLE=0
if "$PIP" install --no-deps -e "$REPO_ROOT" >/tmp/resync-board-venv-pip.log 2>&1; then
    EDITABLE=1
    log "editable install OK (build isolation)"
elif "$PIP" install --no-deps --no-build-isolation -e "$REPO_ROOT" >>/tmp/resync-board-venv-pip.log 2>&1; then
    EDITABLE=1
    log "editable install OK (no-build-isolation fallback)"
else
    log "editable install failed — falling back to force-reinstall snapshot"
    "$PIP" install --force-reinstall --no-deps "$REPO_ROOT" >>/tmp/resync-board-venv-pip.log 2>&1
    log "snapshot force-reinstall OK"
fi

# --- Verify 1: deepseek clamp present and active -----------------------------
OUT1="$("$PY" -c "from engine.llm import LLMClient; print(hasattr(LLMClient,'_is_deepseek'), LLMClient._clamp_max_tokens(500000, provider_hint='deepseek'))")"
log "verify 1 (clamp): $OUT1"
[ "$OUT1" = "True 393216" ] || die "deepseek clamp verification failed: got '$OUT1', want 'True 393216'"

# --- Verify 2: engine resolves to the synced source --------------------------
OUT2="$("$PY" -c "import engine.llm; print(engine.llm.__file__)")"
log "verify 2 (source): $OUT2"
[ -n "$OUT2" ] || die "engine.llm resolved to empty path"
if [ "$EDITABLE" -eq 1 ]; then
    case "$OUT2" in
        "$REPO_ROOT/engine/llm.py") : ;;
        *) die "editable install but engine.llm resolves to '$OUT2' (want $REPO_ROOT/engine/llm.py)" ;;
    esac
else
    case "$OUT2" in
        "$VENV/lib/"*/site-packages/engine/llm.py) : ;;
        *) die "snapshot install but engine.llm resolves to '$OUT2' (want $VENV/lib/*/site-packages/engine/llm.py)" ;;
    esac
fi

# --- Bonus check: CLI version matches repo pyproject -------------------------
VER_REPO="$(sed -n 's/^version = "\(.*\)"/\1/p' "$REPO_ROOT/pyproject.toml" | head -1)"
VER_CLI="$("$CLI" --version)"
log "gitreins CLI version: $VER_CLI (repo: $VER_REPO)"
[ "${VER_CLI#gitreins }" = "$VER_REPO" ] || die "gitreins CLI version '$VER_CLI' != repo version '$VER_REPO'"

log "OK — board venv engine synced to $REPO_ROOT (editable=$EDITABLE)"
