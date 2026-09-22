#!/usr/bin/env python3
"""Check that the DEPLOYED gitreins CLI matches this repo tree.

Review finding REVIEW-001: the fleet executes the installed `gitreins` (pre-commit hooks,
agent shells, guards), but nothing verified that the installed build is the code that was
merged. Both surfaces reported `0.14.0`, so a stale install was invisible.

This probe makes the drift loud and machine-checkable:

    python3 scripts/check_deployed_surface.py          # human output, exit 1 on drift
    python3 scripts/check_deployed_surface.py --json   # machine output

Run it after merging (and as a release-checklist gate). It is deliberately dependency-free:
stdlib only, and it never mutates anything.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run(cmd: list[str], cwd: str | None = None) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=120)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:  # pragma: no cover - environment dependent
        return 127, f"{type(exc).__name__}: {exc}"


def surface(help_text: str) -> set[str]:
    """Subcommand names from an argparse `--help` listing."""
    m = re.search(r"\{([a-z0-9_,\-]+)\}", help_text)
    if not m:
        return set()
    return {s.strip() for s in m.group(1).split(",") if s.strip()}


def repo_surface() -> tuple[set[str], str | None, str | None]:
    py = os.path.join(REPO, ".venv", "bin", "python")
    if not os.path.exists(py):
        py = sys.executable
    rc, out = run([py, "-m", "gitreins", "--help"], cwd=REPO)
    err = None if rc == 0 else f"repo CLI failed (rc={rc}): {out.strip()[:200]}"
    ver = None
    pyproject = os.path.join(REPO, "pyproject.toml")
    if os.path.exists(pyproject):
        with open(pyproject) as fh:
            m = re.search(r'^version\s*=\s*"([^"]+)"', fh.read(), re.M)
            if m:
                ver = m.group(1)
    return surface(out), ver, err


def installed_surface() -> tuple[set[str], str | None, str | None, str | None]:
    exe = shutil.which("gitreins")
    if not exe:
        return set(), None, None, "gitreins is not on PATH (not installed)"
    rc, out = run(["gitreins", "--help"])
    err = None if rc == 0 else f"installed CLI failed (rc={rc}): {out.strip()[:200]}"
    rc2, vout = run(["gitreins", "--version"])
    ver = vout.strip().split()[-1] if rc2 == 0 and vout.strip() else None
    return surface(out), ver, exe, err


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json", dest="as_json", action="store_true", help="emit a JSON report")
    ap.add_argument("--quiet", action="store_true", help="only print on drift")
    args = ap.parse_args()

    r_surf, r_ver, r_err = repo_surface()
    i_surf, i_ver, i_exe, i_err = installed_surface()

    missing = sorted(r_surf - i_surf)  # merged in the repo, absent from the deployed build
    extra = sorted(i_surf - r_surf)  # deployed but not in this checkout (foreign build)
    report = {
        "repo": REPO,
        "repo_version": r_ver,
        "repo_subcommands": sorted(r_surf),
        "installed_path": i_exe,
        "installed_version": i_ver,
        "installed_subcommands": sorted(i_surf),
        "missing_in_deployed": missing,
        "only_in_deployed": extra,
        "repo_error": r_err,
        "installed_error": i_err,
        "aligned": not missing and not extra and not r_err and not i_err and (r_ver == i_ver),
    }

    if args.as_json:
        print(json.dumps(report, indent=2))
    elif not args.quiet or not report["aligned"]:
        print(f"repo     : {REPO}  (version {r_ver}, {len(r_surf)} subcommands)")
        print(f"deployed : {i_exe}  (version {i_ver}, {len(i_surf)} subcommands)")
        if missing:
            print(f"  MISSING from the deployed build: {', '.join(missing)}")
            print("  -> the fleet would run older code: reinstall (pipx install --force <repo>)")
        if extra:
            print(f"  ONLY in the deployed build: {', '.join(extra)}")
        if r_ver and i_ver and r_ver != i_ver:
            print(f"  VERSION DRIFT: repo {r_ver} vs deployed {i_ver}")
        for label, err in (("repo", r_err), ("deployed", i_err)):
            if err:
                print(f"  {label} error: {err}")
        print("ALIGNED" if report["aligned"] else "DRIFT DETECTED")
    return 0 if report["aligned"] else 1


if __name__ == "__main__":
    sys.exit(main())
