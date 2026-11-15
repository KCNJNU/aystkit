#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# ROS setup scripts reference optional variables without nounset-safe defaults.
set +u
source /opt/ros/humble/setup.bash
# Optional ROS workspace for message definitions; never starts drivers or control.
if [[ -n "${ROBOT_ROS_SETUP:-}" ]]; then
  source "$ROBOT_ROS_SETUP"
fi
set -u
cd "$project_dir"
exec "$project_dir/.venv/bin/python" -m collector.main
