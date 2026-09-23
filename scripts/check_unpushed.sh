#!/usr/bin/env bash
# Report unpushed content commits and tracked working-tree changes.
set -euo pipefail

repo_root=$(git rev-parse --show-toplevel)
cd "$repo_root"

# Refresh the refs when possible, but keep the hygiene check useful offline.
if ! git fetch --quiet origin; then
    printf 'WARN fetch: git fetch failed; using the existing upstream ref\n' >&2
fi

if ! upstream=$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null); then
    printf 'ALARM unpushed-content: current branch has no upstream; content cannot be verified\n'
    upstream_missing=1
else
    upstream_missing=0
fi

content_alarm=0
if (( upstream_missing )); then
    content_alarm=1
else
    ahead_count=$(git rev-list --count "$upstream"..HEAD)
    nonmerge_count=$(git rev-list --count --no-merges "$upstream"..HEAD)
    mapfile -t changed_paths < <(git diff --name-only "$upstream"..HEAD)
    changed_count=${#changed_paths[@]}
    mapfile -t content_commits < <(git log --no-merges --oneline "$upstream"..HEAD)

    if (( nonmerge_count > 0 || changed_count > 0 )); then
        content_alarm=1
        printf 'ALARM unpushed-content: %d total commit(s) ahead, %d non-merge content commit(s), %d changed path(s) against %s\n' \
            "$ahead_count" "$nonmerge_count" "$changed_count" "$upstream"
        if ((${#content_commits[@]} > 0)); then
            printf '  %s\n' "${content_commits[@]}"
        else
            printf '  (tree difference came from merge commit(s); no non-merge commit lines)\n'
        fi
        if (( changed_count > 0 )); then
            printf '  changed paths:\n'
            printf '    %s\n' "${changed_paths[@]}"
        fi
    else
        printf 'PASS unpushed-content: %d total commit(s) ahead, %d non-merge content commit(s), %d changed path(s) against %s\n' \
            "$ahead_count" "$nonmerge_count" "$changed_count" "$upstream"
    fi
fi

# --untracked-files=no keeps normal untracked work out of this warning while
# retaining staged/unstaged modifications, additions, and deletions.
mapfile -t dirty_paths < <(git status --porcelain=v1 --untracked-files=no)
if ((${#dirty_paths[@]} > 0)); then
    printf 'WARN dirty-tree: %d tracked change(s) in the working tree\n' "${#dirty_paths[@]}"
    printf '  %s\n' "${dirty_paths[@]}"
else
    printf 'PASS dirty-tree: no tracked working-tree changes\n'
fi

exit "$content_alarm"
