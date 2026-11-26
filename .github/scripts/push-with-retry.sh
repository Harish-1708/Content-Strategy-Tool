#!/usr/bin/env bash
# Push the commit a save step just made, and do not lose the run's data when another run saved at the same time.
#   1. pull --rebase; when both runs changed the same file the two are MERGED, not chosen between: a json file is merged key by key and list by list (merge-json-conflict.py: the other run's keys and items survive, a value both changed takes
#      THIS run's, the newer one), any other file takes this run's version (the other run's version stays in the history); then push;
#   2. if that fails, try again (a few tries, with a short wait), because a concurrent save is the usual cause;
#   3. if every try fails, keep the data: write the commit as a patch in unsaved-data/ (the workflow uploads it as an artifact when the run failed) and fail loudly. Data is never dropped silently.
# Environment: SAVE_TRIES (default 5), SAVE_WAIT (seconds, default 6: waits 6, 12, 18 ...).
set -u
tries="${SAVE_TRIES:-5}"
wait_s="${SAVE_WAIT:-6}"
branch="$(git rev-parse --abbrev-ref HEAD)"
here="$(cd "$(dirname "$0")" && pwd)"

resolve_conflicts() {
  local guard=0
  while { [ -d "$(git rev-parse --git-path rebase-merge)" ] || [ -d "$(git rev-parse --git-path rebase-apply)" ]; } && [ "$guard" -lt 20 ]; do
    guard=$((guard + 1))
    for f in $(git diff --name-only --diff-filter=U); do
      case "$f" in
        *.json) python3 "$here/merge-json-conflict.py" "$f" || git checkout --theirs -- "$f" ;;
        *) git checkout --theirs -- "$f" ;;
      esac
      git add -f -- "$f"
    done
    GIT_EDITOR=true git rebase --continue >/dev/null 2>&1 || true
  done
  [ ! -d "$(git rev-parse --git-path rebase-merge)" ] && [ ! -d "$(git rev-parse --git-path rebase-apply)" ]
}

n=1
while [ "$n" -le "$tries" ]; do
  if git pull --rebase --autostash origin "$branch" || resolve_conflicts; then
    if git push origin "HEAD:$branch"; then
      echo "SAVED: pushed $(git rev-parse --short HEAD) (try $n of $tries)"
      exit 0
    fi
    echo "push refused on try $n of $tries (another run saved in between?)"
  else
    echo "pull --rebase failed on try $n of $tries"
    git rebase --abort 2>/dev/null || true
  fi
  sleep $((wait_s * n))
  n=$((n + 1))
done
mkdir -p unsaved-data
git format-patch -1 HEAD -o unsaved-data >/dev/null 2>&1 || true
echo "::error title=Save failed::the commit could not be pushed after $tries tries; the data is kept as a patch in the 'unsaved-data' artifact of this run"
exit 1
