"""Independent subscriptions-only ROS collector; audit mode never connects to cloud."""
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sqlite3
import time

from .config import Config
from .legacy.devices import VISION_TOPICS
from .legacy.telemetry import TOPIC, TOPIC_TYPE
from .state import CollectorState
from .transport import Uploader

LOG = logging.getLogger(__name__)


def build_node(state, uploader=None):
    """Import ROS only on the robot, leaving normal protocol/unit tests ROS-independent."""
    import rclpy
    import cv2
    from rclpy.node import Node
    from rclpy.qos import DurabilityPolicy, HistoryPolicy, QoSProfile, ReliabilityPolicy
    from rosidl_runtime_py.utilities import get_message

    cv2.setNumThreads(1)

    class ReadOnlyCollector(Node):
        def __init__(self):
            super().__init__('robot_cloud_collector_' + state.config.role, enable_rosout=False, start_parameter_services=False)
            self.subscriptions_owned = []
            self.preview_health = None
            if (uploader is not None and state.config.role == 'primary'
                    and state.config.berxel_transport == 'bridge'
                    and {'color', 'depth'}.issubset(state.config.media_keys)):
                from shared.preview_health import PreviewHealthWriter
                self.preview_health = PreviewHealthWriter()
            reliable = QoSProfile(reliability=ReliabilityPolicy.RELIABLE, durability=DurabilityPolicy.VOLATILE,
                                  history=HistoryPolicy.KEEP_LAST, depth=10)
            if state.config.role == 'primary':
                self.subscriptions_owned.append(self.create_subscription(get_message(TOPIC_TYPE), TOPIC,
                                                                         lambda message: state.telemetry.receive(message.data), reliable))
                callbacks = {'detectionStatus': lambda message: state.devices.receive_detection_status(message.data),
                             'detectedIds': lambda message: state.devices.receive_detected_ids(list(message.data)),
                             'boardPose': state.board_pose,
                             'dockingState': lambda message: state.devices.receive_docking_state(message.data),
                             'arucoPreview': lambda message: state.devices.receive_aruco_preview(message.data)}
                for key, spec in VISION_TOPICS.items():
                    self.subscriptions_owned.append(self.create_subscription(get_message(spec['topicType']), spec['topic'], callbacks[key], reliable))
            for key, (_, topic, kind, _) in state.specs.items():
                # A remote preview bridge avoids subscribing to three full-size
                # Berxel streams; diagnostics still retain their real topics.
                if state.config.berxel_transport == 'bridge' and key in ('color', 'depth', 'depthPoints'):
                    continue
                latched = key in ('map', 'localCostmap', 'globalCostmap')
                qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE if latched else ReliabilityPolicy.BEST_EFFORT,
                                 durability=DurabilityPolicy.TRANSIENT_LOCAL if latched else DurabilityPolicy.VOLATILE)
                self.subscriptions_owned.append(self.create_subscription(get_message(kind), topic, lambda message, key=key: state.receive_sensor(key, message), qos))
            bridge_qos = QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT, durability=DurabilityPolicy.VOLATILE)
            for key, (topic, kind) in state.bridge_specs.items():
                self.subscriptions_owned.append(self.create_subscription(get_message(kind), topic, lambda message, key=key: state.receive_bridge(key, message), bridge_qos))
            self.fault_tails = []
            if state.faults is not None:
                from .faults import RosLogTail
                roots = (os.environ.get('ROS_LOG_DIR', ''), '/tmp/dawn_ros_logs', str(Path.home() / '.ros/log'))
                for root in dict.fromkeys(path for path in roots if path):
                    tail = RosLogTail(state.faults, root, file_limit=12, byte_limit=16384)
                    tail.poll()
                    self.fault_tails.append(tail)
                self.subscriptions_owned.append(self.create_subscription(get_message('rcl_interfaces/msg/Log'), '/rosout', self.fault_rosout, reliable))
                # Read the CustomMsg metadata separately: PointCloud2 lacks point offset times.
                try:
                    custom_kind = get_message('livox_ros_driver2/msg/CustomMsg')
                    self.subscriptions_owned.append(self.create_subscription(custom_kind, '/livox/lidar', self.fault_lidar, bridge_qos, raw=True))
                    state.faults.raw_livox_enabled = True
                except (ImportError, ModuleNotFoundError, AttributeError, RuntimeError, ValueError):
                    state.faults.emit('FAULT_MONITOR_LIMITED', 'collector', '未安装 Livox CustomMsg 类型，点时间回绕监测暂不可用', 'WARN')
                for action in ('navigate_to_pose', 'follow_path', 'spin', 'navigate_through_poses'):
                    topic = '/' + action + '/_action/status'
                    self.subscriptions_owned.append(self.create_subscription(get_message('action_msgs/msg/GoalStatusArray'), topic,
                        lambda message, topic=topic: self.fault_action(topic, message), reliable))
                self.create_timer(1.0, self.fault_tick)
            if state.test_metrics is not None:
                # Drive-axis odometry includes lever-arm compensation; the
                # raw FAST-LIO IMU trajectory is not a wheel travel distance.
                self.subscriptions_owned.append(self.create_subscription(
                    get_message('nav_msgs/msg/Odometry'), '/odom_nav',
                    self.test_odometry, bridge_qos))
                self.create_timer(1.0, self.publish_test_metrics)
            self.create_timer(1.0, self.inspect_graph)
            if uploader:
                self.create_timer(1.0, self.publish_latest)
                self.create_timer(0.2, self.publish_media)
            self.inspect_graph()

        def test_odometry(self, message):
            try:
                state.test_metrics.observe(message)
            except (TypeError,ValueError,OSError,sqlite3.Error):
                LOG.error('Test odometry capture temporarily unavailable')

        def publish_test_metrics(self):
            try:
                if uploader is not None:
                    uploader.enqueue('test_metrics','/observer/test_metrics',state.test_metrics.payload())
            except (TypeError,ValueError,OSError,sqlite3.Error):
                LOG.error('Test metrics storage/upload temporarily unavailable')

        def fault_action(self, topic, message):
            try:
                state.faults.observe_action_status(topic, message)
            except (ValueError, TypeError, OSError, sqlite3.Error):
                LOG.error('Navigation fault capture temporarily unavailable')

        def fault_rosout(self, message):
            try:
                stamp = message.stamp.sec + message.stamp.nanosec / 1e9
                state.faults.observe_log(message.name, message.msg, int(message.level), stamp)
            except (ValueError, TypeError, OSError, sqlite3.Error):
                LOG.error('Fault log capture temporarily unavailable')

        def fault_lidar(self, message):
            try:
                from .faults import custom_message_metadata
                metadata = custom_message_metadata(message)
                state.faults.observe_stream('/livox/lidar', metadata.timestamp, timing=True)
                state.faults.observe_points(metadata)
            except (ValueError, TypeError) as exc:
                try:
                    state.faults.emit('FAULT_MONITOR_LIMITED', 'livox_metadata',
                        '雷达原始消息时间元数据暂时无法解析', 'WARN', {'reason': type(exc).__name__})
                except (ValueError, OSError, sqlite3.Error):
                    LOG.error('Lidar fault capture temporarily unavailable')
            except (OSError, sqlite3.Error):
                LOG.error('Lidar fault capture temporarily unavailable')

        def fault_tick(self):
            try:
                state.faults.tick()
                for tail in self.fault_tails:
                    tail.poll()
                batch = state.faults.spool.batch()
                if uploader is not None and batch:
                    uploader.enqueue('events', '/observer/events', {'events': batch})
            except (ValueError, TypeError, OSError, sqlite3.Error):
                LOG.error('Fault log storage/upload temporarily unavailable')

        def inspect_graph(self):
            def graph(topic):
                infos = self.get_publishers_info_by_topic(topic)
                return len(infos), [info.topic_type for info in infos]
            if state.telemetry:
                state.telemetry.set_graph(*graph(TOPIC))
                for key, spec in VISION_TOPICS.items():
                    state.devices.set_graph(key, *graph(spec['topic']))
            for key, item in state.samples.items():
                item.set_graph(*graph(item.topic))
            for item in state.bridge_samples.values():
                item.set_graph(*graph(item.topic))

        def publish_latest(self):
            try:
                uploader.enqueue('heartbeat', '/observer/heartbeat', {'uptime_s': max(0, time.monotonic() - state.started)})
                if state.config.role == 'primary':
                    uploader.enqueue('observation', '/observer/observation', state.observation())
            except Exception as exc:
                # Malformed optional data must never interrupt local ROS processing.
                LOG.warning('Observation upload encoding rejected: %s', type(exc).__name__)
            if self.preview_health is not None:
                try:
                    channels = {}
                    for key in ('color', 'depth'):
                        meta = state.media.status(key)
                        channels[key] = {'frameSeq': meta.get('frameSeq', 0),
                                         'status': meta['status'], 'ageS': meta['ageS']}
                    self.preview_health.write(channels)
                except (OSError, ValueError):
                    LOG.warning('Preview receiver health report unavailable')

        def publish_media(self):
            try:
                state.media.publish(uploader, state.sensor_snapshot())
            except Exception as exc:
                LOG.warning('Media upload encoding rejected: %s', type(exc).__name__)

        def audit(self):
            self.inspect_graph()
            rows = []
            items = ([] if state.telemetry is None else [state.telemetry.snapshot()]) + list(state.sensor_snapshot().values())
            if state.devices:
                items += list(state.devices.topic_snapshots().values())
            items += [item.snapshot() for item in state.bridge_samples.values()]
            for value in items:
                infos = self.get_publishers_info_by_topic(value['topic'])
                rows.append({field: value.get(field) for field in ('topic', 'topicType', 'status', 'publisherCount', 'observedTypes', 'messageCount', 'ageS', 'hz')})
                rows[-1]['publishers'] = [{'node': info.node_name, 'namespace': info.node_namespace,
                                          'type': info.topic_type,
                                          'reliability': str(info.qos_profile.reliability),
                                          'durability': str(info.qos_profile.durability), 'depth': info.qos_profile.depth}
                                         for info in infos]
            return {'mode': 'READ_ONLY_AUDIT', 'role': state.config.role, 'cloud_connected': False,
                    'topics': rows, 'safety': {'SERIAL_ACCESS': 'NO', 'CONTROL_PUBLISHER': 'NO', 'READ_ONLY': 'YES'}}

    return ReadOnlyCollector()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Subscriptions-only robot cloud collector')
    parser.add_argument('--audit-seconds', type=float, default=None, help='Read-only ROS audit; no cloud URL/token needed, no upload')
    args = parser.parse_args(argv)
    if args.audit_seconds is not None and not 0.1 <= args.audit_seconds <= 120:
        parser.error('--audit-seconds must be in 0.1..120')
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    config = Config.from_env(audit=args.audit_seconds is not None)
    import rclpy
    from rclpy.executors import ExternalShutdownException
    rclpy.init(args=[])
    state, node, uploader, http_cameras = CollectorState(config), None, None, None
    fault_spool = None
    metric_spool = None
    try:
        if args.audit_seconds is None:
            uploader = Uploader(config)
            if config.role == 'primary':
                from .faults import FaultMonitor, FaultSpool, default_spool_path
                fault_spool = FaultSpool(default_spool_path(config.source_id))
                state.faults = FaultMonitor(fault_spool)
                uploader.enable_events(fault_spool.ack, state.faults)
                from .test_metrics import TestMetricSpool, TestMetricTracker, default_spool_path as metric_path
                metric_spool = TestMetricSpool(metric_path(config.source_id))
                state.test_metrics = TestMetricTracker(metric_spool, fault_monitor=state.faults)
                uploader.enable_test_metrics(metric_spool.ack, state.test_metrics.apply_control)
            uploader.start()
            if state.local_chassis is not None:
                state.local_chassis.start()
            if config.http_camera_url:
                # An explicitly enabled, GET-only loopback receiver. It never
                # starts a driver or runs during --audit-seconds.
                from .http_camera import HttpCameraCollector
                http_cameras = HttpCameraCollector(config, uploader)
                http_cameras.start()
        node = build_node(state, uploader)
        if args.audit_seconds is None and state.faults is not None:
            state.faults.emit('FAULT_MONITOR_STARTED', 'collector', '故障日志监测已启用', 'INFO',
                {'rosout': True, 'diagnostics': True, 'navigation_action_status': True,
                 'raw_livox_metadata': getattr(state.faults, 'raw_livox_enabled', False),
                 'imu_source_gap_threshold_s': 0.25, 'lidar_source_gap_threshold_s': 0.5,
                 'host_temperature_threshold_c': 85, 'point_offset_sample_limit': 1025,
                 'spool_capacity': fault_spool.capacity,
                 'log_roots': [str(tail.root) for tail in getattr(node, 'fault_tails', [])],
                 'scope': '订阅和已知日志新增证据；仅已开始的数据流监测中断，不推断根因'}, interval=0)
        if args.audit_seconds is not None:
            deadline = time.monotonic() + args.audit_seconds
            while rclpy.ok() and time.monotonic() < deadline:
                rclpy.spin_once(node, timeout_sec=min(0.2, max(0.01, deadline - time.monotonic())))
            print(json.dumps(node.audit(), ensure_ascii=False, allow_nan=False, indent=2))
        else:
            LOG.info('Read-only %s collector started; chassis source=%s; no control/serial/Observer dependency',
                     config.role, config.chassis_source)
            rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        if http_cameras:
            http_cameras.stop()
        if state.local_chassis is not None:
            state.local_chassis.stop()
        if getattr(state, 'faults', None) is not None:
            try:
                state.faults.flush_suppressed(force=True)
            except (ValueError, OSError, sqlite3.Error):
                LOG.error('Final fault summary storage unavailable')
        if getattr(state,'test_metrics',None) is not None:
            try:
                state.test_metrics.flush()
            except (ValueError,OSError,sqlite3.Error):
                LOG.error('Final test metrics storage temporarily unavailable')
        if uploader:
            uploader.stop()
        if node:
            node.destroy_node()
        if fault_spool is not None:
            fault_spool.close()
        if metric_spool is not None:
            metric_spool.close()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
