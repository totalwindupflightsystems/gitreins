"""DF-GITREINS-POC-103: the fresh-install artifact-identity probe contract.

The fleet battery's fresh-install cell shipped a detector hard-wired to another
product's console script: it searched PATH / PREFIX/bin / ~/.local/bin for
``toolsd`` on EVERY repo, so this repo's genuinely-successful install
(``Successfully installed ... gitreins-0.17.1``) graded FAIL with
"install rc=0 but no artifact found (toolsd not in PATH...)" — evidence:
docs/dogfood/2026-10-26-integration.md and
docs/dogfood/evidence/2026-10-26-gitreins-cli-battery/agent-evidence.jsonl.

The fix follows the battery's own opt-in hook pattern
(.qa/chaos-probes/{errorpath,corruption,shutdown}.sh): this repo ships an
executable ``.qa/probes/fresh-install.sh`` that derives the expected artifact
from the repo's OWN pyproject metadata (``[project] name`` + the first
``[project.scripts]`` entry) and grades the cell by its exit code. The probe
never consults a foreign artifact name, so an unrelated project's binary on
PATH can never satisfy gitreins' fresh-install cell.

These tests drive the probe through STUB console scripts with an isolated
HOME/PATH — no real installation, fully offline (QA_FRESH_SKIP_VERSION=1
skips only the artifact's ``--version`` execution; the identity/location/
executability contract stays under test).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / ".qa" / "probes" / "fresh-install.sh"
PYPROJECT = REPO_ROOT / "pyproject.toml"

#: Phase A — the repo's live metadata: the declared console script, read with
#: the same tolerant parse the probe uses (configparser first, then the
#: battery's own [project.scripts] grep-shape fallback — this repo's
#: pyproject carries '%' values that configparser rejects, so the fallback is
#: the path that serves HERE): the pin tracks the real declaration instead of
#: a copy of it.
_DECLARED_SCRIPT = ""
if PYPROJECT.is_file():
    try:
        import configparser

        _cp = configparser.ConfigParser()
        _cp.read(PYPROJECT, encoding="utf-8")
        _scripts = sorted(_cp["project.scripts"].items())
        _DECLARED_SCRIPT = next((n.strip() for n, _t in _scripts if n.strip()), "")
    except Exception:  # configparser rejects this pyproject → fallback parse
        import re

        _text = PYPROJECT.read_text(encoding="utf-8")
        _m = re.search(
            r"^\[project\.scripts\][ \t]*$(.*?)(?=^\[|\Z)",
            _text,
            re.M | re.S,
        )
        if _m:
            _km = re.search(r"^([A-Za-z0-9_.-]+)[ \t]*=", _m.group(1), re.M)
            if _km:
                _DECLARED_SCRIPT = _km.group(1)


def test_fresh_install_identity_and_grades(tmp_path: Path) -> None:
    """One scenario, four identities: pin, foreign-artifact immunity, FAIL.

    Phase A pins the probe's derived identity to the repo's live
    ``[project.scripts]`` declaration. Phase B proves a fake installed
    console script grades OK while an unrelated product's ``toolsd`` sits on
    the PATH and is never consulted (green proof: artifact = the stub, never
    the foreign binary; negative proof: with ONLY ``toolsd`` present the cell
    fails with a gitreins-specific message). Phase C proves a gitreins-shaped
    install whose ``--version`` dies is a FAIL, not an OK.
    """
    assert PROBE.is_file(), f"missing {PROBE}"
    assert os.access(PROBE, os.X_OK), f"{PROBE} is not executable"
    assert _DECLARED_SCRIPT, f"{PYPROJECT} must declare a console script under [project.scripts]"

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    foreign = fake_bin / "toolsd"
    foreign.write_text("#!/usr/bin/env bash\necho foreign-artifact\n", encoding="utf-8")
    foreign.chmod(0o755)
    env = {
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "HOME": str(tmp_path),
        "QA_FRESH_SKIP_VERSION": "1",
    }
    # INT-CI-22: the probe resolves pyproject.toml relative to its CWD, so
    # the test must pin cwd=REPO_ROOT — it cannot depend on the worker's
    # ambient CWD (the unrestored os.chdir in an unrelated test file once
    # left xdist workers sitting in a tmp dir, and the unpinned probe then
    # graded the tmp dir: CI run 40333037944's "no pyproject.toml" red).
    probe_cwd = str(REPO_ROOT)

    # ── green: the declared console script installed (as a fake) → OK
    good = fake_bin / _DECLARED_SCRIPT
    good.write_text("#!/usr/bin/env bash\necho 'gitreins 0.0.0-stub'\n", encoding="utf-8")
    good.chmod(0o755)
    ok = subprocess.run(
        [str(PROBE)], capture_output=True, text=True, env=env, timeout=60, cwd=probe_cwd
    )
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert str(good) in ok.stdout, (
        f"OK must name the installed {_DECLARED_SCRIPT} artifact: {ok.stdout}"
    )
    assert "toolsd" not in ok.stdout, (
        f"a foreign artifact must never satisfy this cell: {ok.stdout}"
    )

    # ── negative: gitreins absent (only toolsd on PATH) → FAIL, project-named,
    # carrying the install-transcript path the harness exports (QA_FRESH_LOG)
    good.unlink()
    log_env = dict(env)
    log_env["QA_FRESH_LOG"] = "/home/agent/qa-logs-339cb0f1/inst.log"
    miss = subprocess.run(
        [str(PROBE)], capture_output=True, text=True, env=log_env, timeout=60, cwd=probe_cwd
    )
    assert miss.returncode == 1, miss.stdout + miss.stderr
    assert "FAIL" in miss.stdout
    assert _DECLARED_SCRIPT in miss.stdout, (
        f"the failure must name the project's expected artifact: {miss.stdout}"
    )
    assert "/home/agent/qa-logs-339cb0f1/inst.log" in miss.stdout, (
        f"the failure must preserve the install-transcript path: {miss.stdout}"
    )
    assert "toolsd" not in miss.stdout, (
        f"the failure must not be about the foreign artifact: {miss.stdout}"
    )

    # ── negative: installed but `--version` fails → FAIL (rc=0 install alone
    # does not grade OK; chmod re-applied because the rewrite above recreated
    # the file without the exec bit)
    good.write_text("#!/usr/bin/env bash\nexit 3\n", encoding="utf-8")
    good.chmod(0o755)
    bad_env = dict(env)
    bad_env.pop("QA_FRESH_SKIP_VERSION")
    bad = subprocess.run(
        [str(PROBE)], capture_output=True, text=True, env=bad_env, timeout=60, cwd=probe_cwd
    )
    assert bad.returncode == 1, bad.stdout + bad.stderr
    assert "FAIL" in bad.stderr, f"version failure must be graded: {bad.stderr}"
