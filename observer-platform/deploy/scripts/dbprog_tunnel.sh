#!/usr/bin/env bash
# Dedicated transport for DBProg API and signed-object reads. Does not start DBProg.
# The user unit reads the existing private video-tunnel.env; no credentials are copied.
set -euo pipefail
# New robots must be assigned their own cloud socket directory and SSH grant.
video_cloud_host="${VIDEO_CLOUD_HOST:-8.140.19.17}"
video_cloud_user="${VIDEO_CLOUD_USER:-robot-video-tunnel}"
video_cloud_socket_dir="${VIDEO_CLOUD_SOCKET_DIR:-/var/lib/robot-video-tunnel/preview}"
[[ "$video_cloud_host" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$ ]] || exit 2
[[ "$video_cloud_user" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ ]] || exit 2
[[ "$video_cloud_socket_dir" =~ ^/[a-zA-Z0-9_./-]+$ && "$video_cloud_socket_dir" != *..* && "$video_cloud_socket_dir" != */ ]] || exit 2
[[ $# == 0 ]] || exit 2
: "${VIDEO_CLOUD_KEY:?Set VIDEO_CLOUD_KEY}"
: "${VIDEO_CLOUD_KNOWN_HOSTS:?Set VIDEO_CLOUD_KNOWN_HOSTS}"
[[ "$VIDEO_CLOUD_KEY" == /* && "$VIDEO_CLOUD_KNOWN_HOSTS" == /* ]] || exit 2
[[ -f "$VIDEO_CLOUD_KEY" && -r "$VIDEO_CLOUD_KEY" &&
   -f "$VIDEO_CLOUD_KNOWN_HOSTS" && -r "$VIDEO_CLOUD_KNOWN_HOSTS" ]] || exit 2

dbprog_bind=()
# Empty/auto follows the active network; an interface name explicitly pins it.
if [[ -n "${VIDEO_CLOUD_BIND_INTERFACE:-}" && "${VIDEO_CLOUD_BIND_INTERFACE}" != auto ]]; then
    [[ "$VIDEO_CLOUD_BIND_INTERFACE" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$ ]] || exit 2
    dbprog_bind=(-o "BindInterface=$VIDEO_CLOUD_BIND_INTERFACE")
fi

exec /usr/bin/ssh -F /dev/null -N -T "${dbprog_bind[@]}" \
    -o BatchMode=yes -o IdentitiesOnly=yes \
    -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no \
    -o StrictHostKeyChecking=yes -o HostKeyAlgorithms=ssh-ed25519 \
    -o "UserKnownHostsFile=$VIDEO_CLOUD_KNOWN_HOSTS" \
    -o ForwardAgent=no -o ForwardX11=no -i "$VIDEO_CLOUD_KEY" \
    -o ExitOnForwardFailure=yes -o ServerAliveInterval=10 \
    -o ServerAliveCountMax=2 -o ConnectTimeout=8 \
    -R "${video_cloud_socket_dir}/dbprog-api.sock:127.0.0.1:3000" \
    -R "${video_cloud_socket_dir}/dbprog-objects.sock:127.0.0.1:9000" \
    "${video_cloud_user}@${video_cloud_host}"
