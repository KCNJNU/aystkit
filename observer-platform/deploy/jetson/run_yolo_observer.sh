#!/usr/bin/env bash
set -eo pipefail
source /opt/ros/humble/setup.bash
set -u
unset ROS_DISCOVERY_SERVER CYCLONEDDS_URI FASTRTPS_DEFAULT_PROFILES_FILE
export ROS_DOMAIN_ID=0 ROS_LOCALHOST_ONLY=0 RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export PYTHONDONTWRITEBYTECODE=1 YOLO_AUTOINSTALL=false
export OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
exec /home/nvidia/yolo26_test/venv/bin/python -B \
  /home/nvidia/.local/share/robot-cloud/yolo_observer.py
