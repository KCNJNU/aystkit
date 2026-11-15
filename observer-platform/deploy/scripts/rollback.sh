#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$project_dir"
[[ "$(git rev-parse --show-toplevel)" == "$project_dir" ]] || { echo 'Not an independent project repository.' >&2; exit 1; }
[[ -z "$(git status --porcelain --untracked-files=no)" ]] || { echo 'Preserve local tracked edits first.' >&2; exit 1; }
[[ $# == 1 ]] || { echo 'Usage: bash deploy/scripts/rollback.sh COMMIT' >&2; exit 1; }
previous_revision="$(git rev-parse --verify "${1}^{commit}")"
docker image inspect "robot-observer-cloud:$previous_revision" >/dev/null
git switch --detach "$previous_revision"
export CLOUD_IMAGE_TAG="$previous_revision"
bash deploy/scripts/cloud.sh config --quiet
bash deploy/scripts/cloud.sh up -d --no-build --wait --wait-timeout 90
bash deploy/scripts/cloud.sh exec -T nginx nginx -t
bash deploy/scripts/cloud.sh exec -T nginx nginx -s reload
curl -fsS --max-time 10 --retry 5 --retry-delay 1 --retry-all-errors --retry-max-time 30 http://127.0.0.1:8888/readyz >/dev/null
curl -fsS --max-time 10 --retry 5 --retry-delay 1 --retry-all-errors --retry-max-time 30 http://127.0.0.1/healthz >/dev/null
python3 deploy/scripts/image_tag.py "$previous_revision"
echo 'Cloud image reverted; robot control, original Observer, data volume and secrets retained.'
