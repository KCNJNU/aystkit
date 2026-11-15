#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
command -v docker >/dev/null || { printf '%s\n' 'Docker CLI not found.' >&2; exit 1; }
[[ -f "$project_root/.env" ]] || { printf '%s\n' 'Root .env is missing.' >&2; exit 1; }
docker compose --project-directory "$project_root" --file "$project_root/docker-compose.yml" --env-file "$project_root/.env" stop
printf '%s\n' 'Warehouse containers stopped. Data volumes were preserved.'
