#!/usr/bin/env bash
# Safe, non-destructive merge of upstream main into checked-out intrinsic_jetson.
set -euo pipefail

remote="origin"
source="main"
target="intrinsic_jetson"
if (($# >= 1)); then remote="$1"; fi
if (($# >= 2)); then source="$2"; fi
if (($# >= 3)); then target="$3"; fi
if [[ "$source:$target" != "main:intrinsic_jetson" ]]; then
    echo "Unsupported sync direction (must be main -> intrinsic_jetson)" >&2
    exit 2
fi

branch="$(git symbolic-ref --quiet --short HEAD || true)"
if [[ "$branch" != "$target" ]]; then
    echo "Refusing sync: current branch '$branch', expected '$target'" >&2
    exit 2
fi

git fetch --no-tags "$remote" "$source" "$target"
upstream="refs/remotes/$remote/$source"
target_ref="refs/remotes/$remote/$target"
if [[ "$(git rev-parse HEAD)" != "$(git rev-parse "$target_ref")" ]]; then
    echo "Target branch advanced since checkout; refusing to overwrite" >&2
    exit 3
fi

if git merge-base --is-ancestor "$upstream" HEAD; then
    echo "main already included; no sync commit necessary."
    if [[ -n "$(printenv GITHUB_OUTPUT || true)" ]]; then
        echo "noop=true" >> "$GITHUB_OUTPUT"
    fi
    exit 0
fi

# Ordinary merge. No force/rebase and no 'ours' or 'theirs' conflict strategies.
if ! git merge --no-ff --no-edit -m "chore(sync): merge main into intrinsic_jetson" "$upstream"; then
    echo "Merge conflict: remote intrinsic_jetson is unchanged." >&2
    git status --short >&2 || true
    git merge --abort || true
    exit 4
fi
if [[ -n "$(printenv GITHUB_OUTPUT || true)" ]]; then
    echo "noop=false" >> "$GITHUB_OUTPUT"
fi
git log -1 --oneline
