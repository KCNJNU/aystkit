#!/usr/bin/env bash
# Run only on the operator's ECS. This never starts a robot-side component.
set -euo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$project_dir"
[[ "$(git rev-parse --show-toplevel)" == "$project_dir" ]] || { echo 'Not an independent repository.' >&2; exit 1; }
[[ -z "$(git status --porcelain --untracked-files=no)" ]] || { echo 'Preserve local tracked edits first.' >&2; exit 1; }
export CLOUD_IMAGE_TAG="$(git rev-parse HEAD)"
if [[ ! -f .runtime/nginx/observer.conf ]]; then
  python3 deploy/scripts/render_nginx.py --http
fi
bash deploy/scripts/cloud.sh config --quiet
bash deploy/scripts/cloud.sh build backend
docker run --rm --network none -e PYTHONPYCACHEPREFIX=/tmp/pycache --entrypoint python "robot-observer-cloud:$CLOUD_IMAGE_TAG" -m compileall -q cloud shared
bash deploy/scripts/cloud.sh up -d --wait --wait-timeout 90
bash deploy/scripts/cloud.sh exec -T nginx nginx -t
bash deploy/scripts/cloud.sh exec -T nginx nginx -s reload
curl -fsS --max-time 10 --retry 5 --retry-delay 1 --retry-all-errors --retry-max-time 30 http://127.0.0.1:8888/readyz >/dev/null
curl -fsS --max-time 10 --retry 5 --retry-delay 1 --retry-all-errors --retry-max-time 30 http://127.0.0.1/healthz >/dev/null
python3 deploy/scripts/image_tag.py "$CLOUD_IMAGE_TAG"
echo "Cloud ready with commit $CLOUD_IMAGE_TAG. Secrets and runtime Nginx retained."
