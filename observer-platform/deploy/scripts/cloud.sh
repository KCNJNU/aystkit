#!/usr/bin/env bash
# All routine Compose operations resolve the last successfully deployed image.
set -euo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$project_dir"
if [[ -z "${CLOUD_IMAGE_TAG:-}" ]]; then
  if [[ -f .runtime/active-image-tag ]]; then
    read -r CLOUD_IMAGE_TAG < .runtime/active-image-tag
  else
    [[ "$(git rev-parse --show-toplevel)" == "$project_dir" ]] || { echo 'Not an independent repository.' >&2; exit 1; }
    CLOUD_IMAGE_TAG="$(git rev-parse HEAD)"
  fi
fi
[[ "$CLOUD_IMAGE_TAG" =~ ^[0-9a-f]{40}$ ]] || { echo 'Use a full commit image tag.' >&2; exit 1; }
export CLOUD_IMAGE_TAG
exec docker compose --env-file .env -f deploy/docker-compose.yml "$@"
