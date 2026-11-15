#!/bin/sh
# ECS-only forced-command entry point. No arbitrary command, arguments or shell.
set -eu
PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
umask 077

case "${SSH_ORIGINAL_COMMAND:-}" in
  receiver-status)
    backend_state=$(docker inspect --format '{{.State.Running}}' robot-observer-cloud-backend-1 2>/dev/null || true)
    nginx_state=$(docker inspect --format '{{.State.Running}}' robot-observer-cloud-nginx-1 2>/dev/null || true)
    if [ "$backend_state" = true ] && [ "$nginx_state" = true ]; then
      printf '%s\n' 'WEB_RECEIVER=RUNNING'
    else
      printf '%s\n' 'WEB_RECEIVER=STOPPED'
    fi
    ;;
  receiver-start)
    cd /opt/robot-cloud
    test -f .runtime/active-image-tag
    # Source is deployment-owned. The caller cannot select an image or command.
    flock -w 10 /run/lock/robot-web-receiver.lock \
      /bin/bash deploy/scripts/cloud.sh up -d --no-build --pull never \
      --wait --wait-timeout 60 backend nginx
    printf '%s\n' 'WEB_RECEIVER=RUNNING'
    ;;
  *)
    printf '%s\n' 'Only receiver-status or receiver-start is allowed.' >&2
    exit 64
    ;;
esac
