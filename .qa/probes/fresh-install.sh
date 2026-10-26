#!/usr/bin/env bash
# DF-GITREINS-POC-103: fresh-install artifact identity for gitreins.
#
# The fleet battery (bunker-qa.sh, host-side, NOT in this repo) shipped a
# fresh-install detector hard-wired to one product: it looked for another
# product's console script on PATH / PREFIX/bin / ~/.local/bin. For THIS repo
# the install genuinely succeeds (`Successfully installed ... gitreins-0.17.1`)
# and the cell still graded FAIL — "install rc=0 but no artifact found"
# (docs/dogfood/2026-10-26-integration.md, agent-evidence.jsonl row 7). The
# message even named the wrong product's artifact.
#
# This is the battery's OPT-IN project hook for the fresh-install cell, same
# contract as .qa/chaos-probes/{errorpath,corruption,shutdown}.sh: when the
# repo ships an executable .qa/probes/fresh-install.sh the cell runs it in the
# repo root and its exit code IS the cell grade (0 = OK, 1 = FAIL, 2 = INFO).
# The probe derives the expected console script from the repo's own pyproject
# metadata — it never consults an unrelated product's artifact name, so such a
# binary on PATH can never satisfy this cell.
#
# Environment (provided by the harness / overridable for local runs):
#   QA_FRESH_PREFIX   extra candidate bin dir(s) to check, ':'-separated
#                     (defaults: $PREFIX/bin when PREFIX is set, $HOME/.local/bin)
#   QA_FRESH_SKIP_VERSION=1  skip the --version execution (offline stub runs)
#
# Exit: 0 with a human-readable summary on stdout when the expected console
# script exists, is executable and non-empty, and its --version succeeds;
# 1 with a project-specific reason naming the missing artifact otherwise.
set -u

# Every failure names the install transcript when the harness exports its path,
# so the complete transcript stays attached to the graded row (AC: the
# project-specific failure must preserve the transcript linkage).
fail() { echo "fresh-install FAIL: ${QA_FRESH_LOG:+install transcript: $QA_FRESH_LOG; }$*"; exit 1; }

pyproject=pyproject.toml
[ -f "$pyproject" ] || fail "no pyproject.toml — cannot derive the expected gitreins artifact"

# ── expected identity from package metadata: [project] name + the FIRST
# console script declared in [project.scripts]. QA-GITREINS-POC-6 established
# the script parse for the battery's start-command detector; both halves must
# agree with what `pip install .` actually creates.
META=""
if command -v python3 >/dev/null 2>&1; then
    META=$(python3 - "$pyproject" <<'PYEOF'
import configparser
import sys

cp = configparser.ConfigParser()
try:
    cp.read(sys.argv[1], encoding="utf-8")
except configparser.Error:
    sys.exit(1)
name = cp.get("project", "name", fallback="").strip().strip('"').strip("'")
try:
    scripts = sorted(cp["project.scripts"].items())
except KeyError:
    scripts = []
script = next((n.strip() for n, _t in scripts if n.strip()), "")
print(name)
print(script)
PYEOF
) || META=""
fi
PROJECT_NAME=$(printf '%s\n' "$META" | sed -n '1p')
EXPECTED=$(printf '%s\n' "$META" | sed -n '2p')
# Fallbacks when python3 is unavailable: the same greps the battery itself
# uses (DETECT_PKG_NAME for the name, the [project.scripts] parse for the
# script) so identity never depends on one tool being present.
if [ -z "$PROJECT_NAME" ]; then
    PROJECT_NAME=$(sed -n 's/^[[:space:]]*name[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "$pyproject" | head -1)
fi
if [ -z "$EXPECTED" ]; then
    EXPECTED=$(grep -A6 '^\[project\.scripts\]' "$pyproject" 2>/dev/null |
        grep -oE '^[A-Za-z0-9_.-]+[[:space:]]*=' | head -1 | tr -d ' =')
fi

# ── project mismatch: an unrelated project's artifact must not satisfy this
# cell. The probe ships FOR gitreins; run inside any other repo it must fail
# loudly instead of grading that repo by gitreins' (or any foreign) identity.
if [ "$PROJECT_NAME" != "gitreins" ]; then
    fail "project mismatch: this fresh-install probe ships for gitreins but the pyproject here declares name '${PROJECT_NAME:-<none>}' — refusing to grade an unrelated project's artifact"
fi
[ -n "$EXPECTED" ] || fail "no console script declared under [project.scripts] — cannot identify the installed gitreins artifact"

# ── locate the installed artifact: PATH first, then the documented bin dirs.
BIN_PATH=""
if command -v "$EXPECTED" >/dev/null 2>&1; then
    BIN_PATH=$(command -v "$EXPECTED")
else
    _saved_ifs=$IFS
    IFS=':'
    for d in ${QA_FRESH_PREFIX:-}:${PREFIX:+$PREFIX/bin:}$HOME/.local/bin; do
        [ -n "$d" ] || continue
        if [ -x "$d/$EXPECTED" ]; then
            BIN_PATH="$d/$EXPECTED"
            break
        fi
    done
    IFS=$_saved_ifs
fi
_missing="gitreins console script '$EXPECTED' not installed (not on PATH, PREFIX/bin/$EXPECTED missing, or \$HOME/.local/bin/$EXPECTED missing)"
[ -n "$BIN_PATH" ] || fail "$_missing — install rc=0 alone does not prove the artifact landed"

[ -f "$BIN_PATH" ] || fail "gitreins artifact candidate '$BIN_PATH' is not a regular file"
[ -s "$BIN_PATH" ] || fail "gitreins artifact '$BIN_PATH' exists but is EMPTY (zero-size)"
[ -x "$BIN_PATH" ] || fail "gitreins artifact '$BIN_PATH' exists but is not executable"

# ── run the product's own version command (skippable for offline stub runs)
if [ "${QA_FRESH_SKIP_VERSION:-0}" = "1" ]; then
    echo "fresh-install OK: artifact=$BIN_PATH ($(stat -c '%s bytes' "$BIN_PATH" 2>/dev/null || echo size-unknown)); --version skipped (QA_FRESH_SKIP_VERSION=1)"
    exit 0
fi
VER_OUT=$("$BIN_PATH" --version 2>&1)
VER_RC=$?
if [ "$VER_RC" -ne 0 ]; then
    echo "fresh-install FAIL: '$EXPECTED --version' exited rc=$VER_RC: $(printf '%s' "$VER_OUT" | tail -1 | cut -c1-160)" >&2
    exit 1
fi
echo "fresh-install OK: artifact=$BIN_PATH ($(stat -c '%s bytes' "$BIN_PATH" 2>/dev/null || echo size-unknown)); $EXPECTED --version → $(printf '%s' "$VER_OUT" | tail -1 | cut -c1-80)"
exit 0
