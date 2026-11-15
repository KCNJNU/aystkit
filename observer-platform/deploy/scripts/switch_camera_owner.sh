#!/usr/bin/env bash
# RK3588 launcher. Only an explicit web/peer action controls camera ownership.
set -euo pipefail
observer_home=/home/orangepi
jetson_host=10.48.106.37
ecs_host=8.140.19.17
helper_path=/home/nvidia/.local/share/robot-cloud/camera_owner.py
mode=status
dry_run=false
mode_selected=false
for argument in "$@"; do
  case "$argument" in
    web|peer|status)
      [[ "$mode_selected" == false ]] || { echo '只能选择一种模式。' >&2; exit 64; }
      mode=$argument; mode_selected=true ;;
    --dry-run) dry_run=true ;;
    -h|--help)
      printf '%s\n' \
        '用法：switch_camera_owner.sh status|web|peer [--dry-run]' \
        '  web   相机交给网页预览；启动只读转发和现有网站。' \
        '  peer  停已知网页采集，交给标定／其他程序；网站及占用提示保留。' \
        '  status 查看占用、转发和网站状态，不改变任何模式。' \
        '  --dry-run 仅预演，不启动／停止程序，不写模式状态。' \
        '切回web前先保存并关闭标定／未知采集程序；不会强杀它们。' \
        'peer只释放设备，不自动启动对方程序；同一相机不要重复采集。'
      exit 0 ;;
    *) printf '不支持的参数。运行 --help 查看用法。\n' >&2; exit 64 ;;
  esac
done

jetson_ssh=(/usr/bin/ssh -T -o BatchMode=yes -o IdentitiesOnly=yes
  -o PasswordAuthentication=no -o StrictHostKeyChecking=yes
  -o HostKeyAlgorithms=ssh-ed25519 -o HostKeyAlias=10.48.106.96
  -o UserKnownHostsFile="$observer_home/.ssh/known_hosts"
  -o ConnectTimeout=8 -o ServerAliveInterval=10 -o ServerAliveCountMax=2
  -i "$observer_home/.ssh/id_ed25519" "nvidia@$jetson_host")
receiver_ssh=(/usr/bin/ssh -T -o BatchMode=yes -o IdentitiesOnly=yes
  -o PasswordAuthentication=no -o StrictHostKeyChecking=yes
  -o HostKeyAlgorithms=ssh-ed25519
  -o UserKnownHostsFile="$observer_home/.ssh/known_hosts_ecs_observer_linux"
  -o ConnectTimeout=8 -o ServerAliveInterval=10 -o ServerAliveCountMax=2
  -i "$observer_home/.local/state/robot-cloud/web-receiver_ed25519" "root@$ecs_host")
native_units=()
for key in usbFront usbLeft usbHub2 usbHub3; do
  native_units+=("robot-cloud-native-source-tunnel@$key.service"
                "robot-cloud-native-cloud-tunnel@$key.service")
done
remote_action=(/usr/bin/python3 -B "$helper_path" "$mode")
[[ "$dry_run" == false ]] || remote_action+=(--dry-run)

if [[ "$mode" == status || "$dry_run" == true ]]; then
  "${jetson_ssh[@]}" "${remote_action[@]}"
  if ! "${receiver_ssh[@]}" receiver-status; then
    echo '网站状态无法查询：请检查网络或部署接收端受限公钥。' >&2
  fi
  /usr/bin/systemctl --user show "${native_units[@]}" -p Id -p ActiveState
  if [[ "$dry_run" == true && "$mode" != status ]]; then
    printf '%s\n' '预演：确保网站及八条只读状态／视频转发运行；现在未执行。'
  fi
  exit 0
fi

# Serialize the complete Jetson / RK3588 / ECS handoff, not just camera startup.
# The read-only status/dry-run branch above never creates this lock.
umask 077
switch_lock_dir="$observer_home/.local/state/robot-cloud"
switch_lock_file="$switch_lock_dir/camera-switch.lock"
[[ -d "$switch_lock_dir" && ! -L "$switch_lock_dir" &&
   "$(/usr/bin/stat -c %u "$switch_lock_dir")" == "$(/usr/bin/id -u)" &&
   "$(/usr/bin/stat -c %a "$switch_lock_dir")" == 700 ]] || {
  echo '本机切换状态目录权限不安全，未操作相机。' >&2; exit 3;
}
if [[ -e "$switch_lock_file" || -L "$switch_lock_file" ]]; then
  [[ -f "$switch_lock_file" && ! -L "$switch_lock_file" &&
     "$(/usr/bin/stat -c '%u:%a:%h' "$switch_lock_file")" == "$(/usr/bin/id -u):600:1" ]] || {
    echo '本机切换锁文件不安全，未操作相机。' >&2; exit 3;
  }
fi
exec 9>"$switch_lock_file"
/usr/bin/flock -n 9 || { echo '已有切换正在执行，请稍后运行status。' >&2; exit 3; }

# Check fixed receiver permission before changing the camera owner. This key
# cannot get a shell, stop containers, forward ports, or change credentials.
"${receiver_ssh[@]}" receiver-status >/dev/null || {
  echo '接收端预检查失败；未切换相机。请检查网络或受限部署公钥。' >&2
  exit 3
}
# The website and read-only status transport are independent of camera ownership.
# Restore them first: a busy camera must never make the entire site unavailable.
if ! "${receiver_ssh[@]}" receiver-start; then
  echo '网站启动失败，未切换相机。请运行status检查。' >&2
  exit 4
fi
if ! "${jetson_ssh[@]}" /usr/bin/env XDG_RUNTIME_DIR=/run/user/1000 \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus \
    /usr/bin/systemctl --user start robot-cloud-camera-status-gateway.service; then
  echo '网站已恢复，但相机占用检查未启动；未切换相机，请运行status检查。' >&2
  exit 4
fi
if ! /usr/bin/systemctl --user start "${native_units[@]}"; then
  echo '网站已恢复，但视频状态转发启动失败；未切换相机，请运行status检查。' >&2
  exit 4
fi
if ! "${jetson_ssh[@]}" "${remote_action[@]}"; then
  echo '网站保持可用，相机交接未完成。请查看页面占用提示或运行status；未强杀其他程序。' >&2
  exit 4
fi
if [[ "$mode" == web ]]; then
  printf '%s\n' '已切到网页模式：https://aystkit.cn（原布局／图表／录制下载保留）'
  printf '%s\n' '四路高清仍受当前3 Mbps公网带宽限制；首次连接需缓冲。'
else
  printf '%s\n' '已交给对方模式。可启动标定／其他采集；网站继续运行，相机窗口显示实际占用状态。'
fi
