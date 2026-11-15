#!/usr/bin/env bash
set -euo pipefail
archive_project="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
set +u
source /opt/ros/humble/setup.bash
archive_ros_setup="${ROBOT_ROS_SETUP:-$HOME/code/upper_computer/install/setup.bash}"
if [[ ! -r "$archive_ros_setup" ]]; then
    printf '归档工作区 ROS 环境不存在：%s；无法加载雷达自定义消息。\n' "$archive_ros_setup" >&2
    exit 2
fi
source "$archive_ros_setup"
set -u
cd "$archive_project"
exec /usr/bin/python3 -m collector.archive_main "$@"
