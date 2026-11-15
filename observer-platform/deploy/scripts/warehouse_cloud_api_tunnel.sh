#!/usr/bin/env bash
# Robot upload transport only. Does not start a warehouse, driver or remote shell.
set -euo pipefail
[[ $# == 0 ]] || exit 2
: "${WAREHOUSE_CLOUD_KEY:?Set WAREHOUSE_CLOUD_KEY}"
: "${WAREHOUSE_CLOUD_KNOWN_HOSTS:?Set WAREHOUSE_CLOUD_KNOWN_HOSTS}"
warehouse_cloud_host="${WAREHOUSE_CLOUD_HOST:-8.140.19.17}"
warehouse_cloud_user="${WAREHOUSE_CLOUD_USER:-robot-warehouse-upload}"
[[ "$warehouse_cloud_host" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$ ]] || exit 2
[[ "$warehouse_cloud_user" =~ ^[a-zA-Z_][a-zA-Z0-9_-]*$ ]] || exit 2
warehouse_forwards=(-L 127.0.0.1:13000:127.0.0.1:13000)
case "${WAREHOUSE_CLOUD_LEGACY_API_ALIAS-true}" in
    true)
        # In-flight recorders cache the old loopback API base. Preserve their
        # connections to the migrated database without restarting capture.
        warehouse_forwards+=(-L 127.0.0.1:3000:127.0.0.1:13000)
        ;;
    false) ;;
    *) exit 2 ;;
esac
for warehouse_private_path in "$WAREHOUSE_CLOUD_KEY" "$WAREHOUSE_CLOUD_KNOWN_HOSTS"; do
    [[ "$warehouse_private_path" == /* && -f "$warehouse_private_path" &&
       ! -L "$warehouse_private_path" && -O "$warehouse_private_path" &&
       -r "$warehouse_private_path" ]] || exit 2
done
case "$(stat -c '%a' -- "$WAREHOUSE_CLOUD_KEY")" in 400|600) ;; *) exit 2 ;; esac
case "$(stat -c '%a' -- "$WAREHOUSE_CLOUD_KNOWN_HOSTS")" in
    400|440|444|600|640|644) ;; *) exit 2 ;;
esac
warehouse_bind=()
if [[ -n "${WAREHOUSE_CLOUD_BIND_INTERFACE:-}" && "$WAREHOUSE_CLOUD_BIND_INTERFACE" != auto ]]; then
    [[ "$WAREHOUSE_CLOUD_BIND_INTERFACE" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$ ]] || exit 2
    warehouse_bind=(-o "BindInterface=$WAREHOUSE_CLOUD_BIND_INTERFACE")
fi
exec /usr/bin/ssh -4 -F /dev/null -N -T "${warehouse_bind[@]}" \
    -o BatchMode=yes -o IdentitiesOnly=yes -o IdentityAgent=none \
    -o PasswordAuthentication=no -o KbdInteractiveAuthentication=no \
    -o StrictHostKeyChecking=yes -o HostKeyAlgorithms=ssh-ed25519 \
    -o "UserKnownHostsFile=$WAREHOUSE_CLOUD_KNOWN_HOSTS" \
    -o ForwardAgent=no -o ForwardX11=no -i "$WAREHOUSE_CLOUD_KEY" \
    -o ExitOnForwardFailure=yes -o ConnectTimeout=8 \
    -o ServerAliveInterval=10 -o ServerAliveCountMax=2 \
    "${warehouse_forwards[@]}" \
    "${warehouse_cloud_user}@${warehouse_cloud_host}"
