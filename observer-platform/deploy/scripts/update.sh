#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$project_dir"
if [[ "$(git rev-parse --show-toplevel)" != "$project_dir" ]]; then
  echo 'This project must be its own Git repository; refusing to update a parent repository.' >&2
  exit 1
fi
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo 'Tracked files have local changes. Commit or preserve them before updating.' >&2
  exit 1
fi
target_revision="${1:-origin/main}"
if [[ -f .runtime/active-image-tag ]]; then
  read -r old_revision < .runtime/active-image-tag
else
  echo 'No successful initial deployment; use start_cloud.sh first.' >&2
  exit 1
fi
[[ "$old_revision" =~ ^[0-9a-f]{40}$ ]] || { echo 'Invalid active image tag.' >&2; exit 1; }
docker image inspect "robot-observer-cloud:$old_revision" >/dev/null
git fetch origin
new_revision="$(git rev-parse --verify "${target_revision}^{commit}")"
echo "Update $old_revision -> $new_revision (data and secrets retained)."
git switch --detach "$new_revision"
bash deploy/scripts/start_cloud.sh
echo "Cloud updated. For rollback: bash deploy/scripts/rollback.sh $old_revision"
