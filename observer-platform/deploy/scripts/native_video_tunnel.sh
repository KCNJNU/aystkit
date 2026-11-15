#!/usr/bin/env bash
# Forward to the independent read-only status/video gateway; never starts cameras.
# User services provide these settings via ~/.config/robot-cloud/video-tunnel.env.
set -euo pipefail
# New robots must be assigned their own cloud socket directory and SSH grant.
video_cloud_host="${VIDEO_CLOUD_HOST:-8.140.19.17}"
video_cloud_user="${VIDEO_CLOUD_USER:-robot-video-tunnel}"
video_cloud_socket_dir="${VIDEO_CLOUD_SOCKET_DIR:-/var/lib/robot-video-tunnel/preview}"
[[ "$video_cloud_host" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$ ]] || exit 2
[[ "$video_cloud_user" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ ]] || exit 2
[[ "$video_cloud_socket_dir" =~ ^/[a-zA-Z0-9_./-]+$ && "$video_cloud_socket_dir" != *..* && "$video_cloud_socket_dir" != */ ]] || exit 2
[[ $# == 2 ]] || exit 2
case "$2" in
    status) native_port=18086 ;;
    usbFront) native_port=18082 ;;
    usbLeft) native_port=18083 ;;
    usbHub2) native_port=18084 ;;
    usbHub3) native_port=18085 ;;
    *) exit 2 ;;
esac
native_socket_name="jetson-${2}.sock"
if [[ "$2" == status ]]; then native_socket_name=jetson.sock; fi
case "$1" in
    source)
        : "${VIDEO_SOURCE_HOST:?Set VIDEO_SOURCE_HOST}"
        : "${VIDEO_SOURCE_USER:?Set VIDEO_SOURCE_USER}"
        : "${VIDEO_SOURCE_KEY:?Set VIDEO_SOURCE_KEY}"
        : "${VIDEO_SOURCE_KNOWN_HOSTS:?Set VIDEO_SOURCE_KNOWN_HOSTS}"
        source_host="$VIDEO_SOURCE_HOST"
        source_alias=()
        source_key_alias="${VIDEO_SOURCE_HOST_KEY_ALIAS:-}"
        if [[ "$source_host" == auto ]]; then
          source_host="${VIDEO_SOURCE_NAME:-nvidia-desktop.local}"
          # DHCP addresses can change; the original pinned host key cannot.
          source_key_alias="${source_key_alias:-192.168.1.50}"
        fi
        [[ "$source_host" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$ ]] || exit 2
        if [[ -n "$source_key_alias" ]]; then
          [[ "$source_key_alias" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$ ]] || exit 2
          source_alias=(-o "HostKeyAlias=$source_key_alias")
        fi
        [[ "$VIDEO_SOURCE_USER" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ ]] || exit 2
        [[ "$VIDEO_SOURCE_KEY" == /* && "$VIDEO_SOURCE_KNOWN_HOSTS" == /* ]] || exit 2
        exec /usr/bin/ssh -F /dev/null -N -T "${source_alias[@]}" -o BatchMode=yes -o IdentitiesOnly=yes \
          -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no \
          -o StrictHostKeyChecking=yes -o HostKeyAlgorithms=ssh-ed25519 \
          -o "UserKnownHostsFile=$VIDEO_SOURCE_KNOWN_HOSTS" \
          -o ForwardAgent=no -o ForwardX11=no \
          -i "$VIDEO_SOURCE_KEY" -o ExitOnForwardFailure=yes \
          -o ServerAliveInterval=10 -o ServerAliveCountMax=2 -o ConnectTimeout=8 \
          -L "127.0.0.1:${native_port}:127.0.0.1:8081" \
          "$VIDEO_SOURCE_USER@$source_host"
        ;;
    cloud)
        : "${VIDEO_CLOUD_KEY:?Set VIDEO_CLOUD_KEY}"
        : "${VIDEO_CLOUD_KNOWN_HOSTS:?Set VIDEO_CLOUD_KNOWN_HOSTS}"
        [[ "$VIDEO_CLOUD_KEY" == /* && "$VIDEO_CLOUD_KNOWN_HOSTS" == /* ]] || exit 2
        cloud_bind=()
        # Empty/auto follows the current route after Wi-Fi/4G changes.
        # A specific interface remains an explicit operator override.
        if [[ -n "${VIDEO_CLOUD_BIND_INTERFACE:-}" && "${VIDEO_CLOUD_BIND_INTERFACE}" != auto ]]; then
          [[ "$VIDEO_CLOUD_BIND_INTERFACE" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$ ]] || exit 2
          cloud_bind=(-o "BindInterface=$VIDEO_CLOUD_BIND_INTERFACE")
        fi
        exec /usr/bin/ssh -F /dev/null -N -T "${cloud_bind[@]}" -o BatchMode=yes -o IdentitiesOnly=yes \
          -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no \
          -o StrictHostKeyChecking=yes -o HostKeyAlgorithms=ssh-ed25519 \
          -o "UserKnownHostsFile=$VIDEO_CLOUD_KNOWN_HOSTS" \
          -o ForwardAgent=no -o ForwardX11=no -i "$VIDEO_CLOUD_KEY" \
          -o ExitOnForwardFailure=yes -o ServerAliveInterval=10 \
          -o ServerAliveCountMax=2 -o ConnectTimeout=8 \
          -R "${video_cloud_socket_dir}/${native_socket_name}:127.0.0.1:${native_port}" \
          "${video_cloud_user}@${video_cloud_host}"
        ;;
    *) exit 2 ;;
esac
