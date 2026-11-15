#!/usr/bin/env bash
set -Eeuo pipefail

project_root="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
check_only=false
skip_seed=false
no_build=false
ready_timeout=180

usage() {
  printf '%s\n' 'Usage: bash scripts/start-linux.sh [--check-only] [--skip-seed] [--no-build] [--ready-timeout SECONDS]'
}

while (($#)); do
  case "$1" in
    --check-only) check_only=true ;;
    --skip-seed) skip_seed=true ;;
    --no-build) no_build=true ;;
    --ready-timeout)
      shift
      if (($# == 0)) || [[ ! "$1" =~ ^[0-9]+$ ]] || ((10#$1 < 15 || 10#$1 > 1800)); then
        printf '%s\n' '--ready-timeout requires an integer between 15 and 1800.' >&2
        exit 2
      fi
      ready_timeout=$((10#$1))
      ;;
    --help|-h) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
  shift
done

fail() { printf '%s\n' "$1" >&2; exit 1; }
compose() {
  docker compose --project-directory "$project_root" --file "$project_root/docker-compose.yml" --env-file "$project_root/.env" "$@"
}
trap 'printf "%s\n" "Startup failed. Inspect: docker compose logs --tail 100 backend worker migration storage-init" >&2' ERR

command -v docker >/dev/null || fail 'Docker CLI not found.'
command -v curl >/dev/null || fail 'curl is required for readiness checks.'
[[ -f "$project_root/.env" ]] || fail 'Root .env is missing. Copy .env.example to .env and configure the administrator account before starting.'
docker compose version >/dev/null
[[ "$(docker info --format '{{.OSType}}')" == linux ]] || fail 'A running Linux Docker Engine is required.'
compose config --quiet

if "$check_only"; then
  printf '%s\n' 'Preflight passed. No containers were started.'
  exit 0
fi

wait_http() {
  local url="$1" deadline=$((SECONDS + ready_timeout))
  until curl --fail --silent --output /dev/null --max-time 5 "$url"; do
    ((SECONDS < deadline)) || fail "Service did not become ready: $url"
    sleep 3
  done
}

up_arguments=(up -d)
if "$no_build"; then up_arguments+=(--no-build --pull never); else up_arguments+=(--build); fi
compose "${up_arguments[@]}"
wait_http 'http://127.0.0.1:3000/ready'

if ! "$skip_seed"; then
  compose run --rm --no-deps migration npm run prisma:seed --workspace @cleaning-robot/api
fi
compose exec -T worker git -C /app/data/dataset-repository init
wait_http 'http://127.0.0.1:5173/healthz'
wait_http 'http://127.0.0.1:5173/nest/ready'

running_services="$(compose ps --status running --services)"
for service in postgres redis minio backend worker web; do
  grep -Fxq "$service" <<< "$running_services" || fail "Required service is not running: $service"
done
compose ps
printf '%s\n' 'Warehouse is ready: http://localhost:5173' 'Remote browsers: use this server LAN IP on port 5173.'
