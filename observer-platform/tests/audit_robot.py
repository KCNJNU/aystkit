"""Real read-only robot audit. No fixtures, cloud upload, driver startup or control.

Run from the project with the existing ROS environment sourced:
    python -m tests.audit_robot
"""
import json
import os
from pathlib import Path
import time

from collector.config import Config
from collector.main import build_node
from collector.state import CollectorState


def main():
    import rclpy
    rclpy.init(args=[])
    node = None
    try:
        state = CollectorState(Config.from_env(audit=True))
        node = build_node(state)
        deadline = time.monotonic() + 10
        while rclpy.ok() and time.monotonic() < deadline:
            rclpy.spin_once(node, timeout_sec=.2)
        result = node.audit()
        own_publishers = node.get_publisher_names_and_types_by_node(node.get_name(), node.get_namespace())
        controls = [name for name, _ in own_publishers if name not in ('/parameter_events', '/rosout')]
        device_fds = []
        for descriptor in Path('/proc/self/fd').iterdir():
            try:
                target = os.readlink(descriptor)
            except OSError:
                continue
            if target.startswith('/dev/'):
                device_fds.append(target)
        serial_fds = [target for target in device_fds if target.startswith(('/dev/ttyUSB', '/dev/ttyACM',
                      '/dev/ttyCH', '/dev/wheeltec', '/dev/serial/'))]
        result['runtimeSafetyEvidence'] = dict(ownPublishers=own_publishers, controlPublishers=controls,
                                             deviceFds=sorted(set(device_fds)), serialDeviceFds=serial_fds,
                                             cloudUploadStarted=False)
        result['systemStatus'] = state.collector.collect()['status']
        print(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2))
        if controls or serial_fds:
            raise SystemExit('Read-only boundary failed')
    finally:
        if node:
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
