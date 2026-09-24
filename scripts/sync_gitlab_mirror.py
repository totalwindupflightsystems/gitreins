#!/usr/bin/env python3
"""Keep the GitLab mirror's `main` in step with this repository.

Why a script instead of a second push URL: the GitLab mirror protects `main` (push access
"no one", merges for maintainers), so a plain `git push` is rejected by its pre-receive hook.
The mirror is therefore updated the way that project expects:

    1. push this repo's main to the UNPROTECTED branch `mirror/main` on GitLab
    2. open (or reuse) a merge request mirror/main -> main and merge it with the API
    3. assert GitLab's main now points at the same commit as this repo

Usage:
    python3 scripts/sync_gitlab_mirror.py            # sync + verify
    python3 scripts/sync_gitlab_mirror.py --check    # verify only, no writes

Token: GITLAB_TOKEN from the environment, else from ~/.hermes/.env. The token is never
printed; only HTTP status codes and commit SHAs are.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

GITLAB = "https://gitlab.readydedis.com"
# D2 decided 2026-09-24 (Bane): the CANON stays on GitHub
# (totalwindupflightsystems/gitreins); this script now mirrors into the coding-hermes
# group. The previous target, totalwindup/gitreins-poc, is frozen and kept until Bane
# cuts the link, then delete it. Seeded and verified 2026-09-24: trees identical,
# 0 missing commits, 0 differing files, 42/42 tags.
PROJECT_PATH = "coding-hermes/gitreins-mirror"
# The mirror-side remote: `origin` still points at the OLD totalwindup/gitreins-poc
# project (frozen until Bane cuts the link) - do not point this back at it.
REMOTE = "gitlab-mirror"  # the GitLab remote in this checkout
MIRROR_BRANCH = "mirror/main"


def token() -> str | None:
    tok = os.environ.get("GITLAB_TOKEN")
    if tok:
        return tok
    env = os.path.expanduser("~/.hermes/.env")
    if os.path.exists(env):
        with open(env, errors="replace") as fh:
            for line in fh:
                if line.startswith("GITLAB_TOKEN="):
                    return line.split("=", 1)[1].strip()
    return None


def git(*args: str, check: bool = True) -> str:
    p = subprocess.run(["git", *args], capture_output=True, text=True, timeout=300)
    out = (p.stdout or "") + (p.stderr or "")
    if check and p.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed:\n{out.strip()}")
    return out.strip()


def api(tok: str, method: str, path: str, payload: dict | None = None):
    url = f"{GITLAB}/api/v4/{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("PRIVATE-TOKEN", tok)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return exc.code, body[:300]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="verify parity only, never write")
    args = ap.parse_args()

    local = git("rev-parse", "main")
    proj = urllib.parse.quote(PROJECT_PATH, safe="")
    tok = token()

    git("fetch", "-q", REMOTE, "main")
    remote_main = git("rev-parse", f"{REMOTE}/main")
    print(f"local main   : {local[:8]}")
    print(f"gitlab main  : {remote_main[:8]}")
    if remote_main == local:
        print("GITLAB MIRROR IN SYNC (identical commit)")
        return 0
    # Content check first: if local main is already contained in the mirror's main, the mirror
    # is up to date even when GitLab's side carries its own merge commit. Nothing to do.
    anc = (
        subprocess.run(
            ["git", "merge-base", "--is-ancestor", "main", f"{REMOTE}/main"],
            capture_output=True,
            timeout=60,
        ).returncode
        == 0
    )
    if anc:
        ahead = git("rev-list", "--count", f"main..{REMOTE}/main")
        print(f"GITLAB MIRROR IN SYNC (content; {ahead} mirror-side merge commit(s))")
        return 0
    if args.check:
        print("GITLAB MIRROR BEHIND (run without --check to sync)")
        return 1
    if not tok:
        print("GITLAB_TOKEN not found (env or ~/.hermes/.env) - cannot open the merge request")
        return 2

    print(f"\n1. push {MIRROR_BRANCH} (unprotected) ...")
    git("push", "--force", REMOTE, f"main:refs/heads/{MIRROR_BRANCH}")
    mirror_sha = re.split(r"\s+", git("ls-remote", REMOTE, f"refs/heads/{MIRROR_BRANCH}"))[0]
    print(f"   {MIRROR_BRANCH} = {mirror_sha[:8]}")

    print("2. open/reuse the mirror merge request ...")
    code, mrs = api(
        tok,
        "GET",
        f"projects/{proj}/merge_requests"
        f"?state=opened&source_branch={urllib.parse.quote(MIRROR_BRANCH)}"
        f"&target_branch=main",
    )
    if code != 200:
        print(f"   MR lookup failed: {code} {mrs}")
        return 3
    if mrs:
        iid = mrs[0]["iid"]
        print(f"   reusing !{iid}")
    else:
        code, mr = api(
            tok,
            "POST",
            f"projects/{proj}/merge_requests",
            {
                "source_branch": MIRROR_BRANCH,
                "target_branch": "main",
                "title": f"mirror: sync main to {local[:8]}",
                "description": "Automated mirror sync from the GitHub canonical repo "
                "(scripts/sync_gitlab_mirror.py).",
                "remove_source_branch": False,
            },
        )
        if code not in (200, 201):
            print(f"   MR create failed: {code} {mr}")
            return 4
        iid = mr["iid"]
        print(f"   opened !{iid}")

    print("3. merge it ...")
    # GitLab computes mergeability asynchronously: merging immediately after creating the MR
    # returns 422 "Branch cannot be merged". Poll the MR status until it settles.
    import time

    merged = False
    for attempt in range(12):
        code, mr = api(tok, "GET", f"projects/{proj}/merge_requests/{iid}")
        if code == 200:
            status = mr.get("detailed_merge_status") or mr.get("merge_status")
            has_conflicts = mr.get("has_conflicts")
            if status in ("mergeable", "can_be_merged") and not has_conflicts:
                code, res = api(tok, "PUT", f"projects/{proj}/merge_requests/{iid}/merge")
                print(f"   merge: {code} {str(res)[:140]}")
                if code in (200, 201):
                    merged = True
                    break
            else:
                print(f"   status={status} conflicts={has_conflicts} (waiting)")
        time.sleep(5)
    if not merged:
        print("   could not merge automatically - inspect the MR by hand")

    print("4. verify ...")
    # Re-fetch: the merge just moved the mirror's main, and a stale origin/main would report
    # "still behind" for a sync that actually landed.
    git("fetch", "-q", REMOTE, "main")
    remote_main = git("rev-parse", f"{REMOTE}/main")
    print(f"   gitlab main = {remote_main[:8]}  local = {local[:8]}")
    if remote_main == local:
        print("GITLAB MIRROR IN SYNC (identical commit)")
        return 0
    # A merge commit on the GitLab side still means the CONTENT is synced: local main is an
    # ancestor. That is acceptable, but report it so a growing gap is visible.
    p = subprocess.run(
        ["git", "merge-base", "--is-ancestor", "main", f"{REMOTE}/main"],
        capture_output=True,
        timeout=60,
    )
    if p.returncode == 0:
        ahead = git("rev-list", "--count", f"main..{REMOTE}/main")
        print(
            f"GITLAB MIRROR CONTENT IN SYNC ({ahead} mirror-side merge commit(s) - "
            "the expected shape with merge_method=merge; do NOT switch the project to "
            "ff-only, the merge request then fails with need_rebase)"
        )
        return 0
    print("GITLAB MIRROR STILL BEHIND (check the MR state)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
