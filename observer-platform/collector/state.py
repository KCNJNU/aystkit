"""Independent observation DTO assembly; no local Observer service dependency."""
from __future__ import annotations

import math
import os
import sqlite3
import time

from .legacy.devices import DeviceInterfaceStore, TopicSample, VISION_TOPICS
from .legacy.sensors import SENSOR_TOPICS, summarize
from .legacy.system_metrics import SystemCollector
from .legacy.telemetry import TelemetryStore
from .legacy.truth import json_safe
from .media import MediaStore, header_timestamp
from .local_chassis import LocalChassis, SOURCE_DESCRIPTION
from .faults import source_timestamp


def topic_specs(config):
    specs = dict(SENSOR_TOPICS)
    specs['registered'] = ('FAST-LIO 配准点云', '/cloud_registered_body_xyzi', 'sensor_msgs/msg/PointCloud2', 2)
    specs['bluesea'] = ('蓝海二维雷达', '/bluesea_new/scan', 'sensor_msgs/msg/LaserScan', 2)
    specs['scan'] = ('二维雷达扫描', '/scan', 'sensor_msgs/msg/LaserScan', 2)
    if config.role == 'cameras':
        specs = {key: spec for key, spec in specs.items() if key in ('fisheye', 'color', 'depth', 'depthPoints') and key in config.media_keys}
    for key, spec in list(specs.items()):
        alias = 'REGISTERED_TOPIC' if key == 'registered' else 'BLUESEA_TOPIC' if key == 'bluesea' else None
        topic = os.environ.get('COLLECTOR_TOPIC_' + key.upper(), os.environ.get(alias, spec[1]) if alias else spec[1])
        if not topic.startswith('/') or '?' in topic or '#' in topic or any(ch.isspace() for ch in topic):
            raise ValueError('Invalid configured ROS topic: ' + key)
        specs[key] = (spec[0], topic, spec[2], spec[3])
    return specs


class CollectorState:
    def __init__(self, config, collector=None, clock=time.monotonic):
        self.config, self.clock = config, clock
        self.started = clock()
        self.specs = topic_specs(config)
        self.telemetry = TelemetryStore(clock) if config.role == 'primary' else None
        self.local_chassis = LocalChassis(clock) if config.chassis_source == 'local_ws' else None
        self.devices = DeviceInterfaceStore(clock) if config.role == 'primary' else None
        # A camera-only source must not sample system data or claim a robot primary snapshot.
        self.collector = (collector or SystemCollector()) if config.role == 'primary' else None
        self.samples = {key: TopicSample(spec[1], spec[2], clock, spec[3]) for key, spec in self.specs.items()}
        self.media = MediaStore(config, clock)
        self.errors = {}
        self.test_metrics = None  # Read-only counter, attached only in the running primary collector.
        self.faults = None  # Attached only in the explicitly running collector, never audit/tests.
        self.bridge_specs = {}
        self.bridge_samples = {}
        for key in ('fisheye', 'color', 'depth'):
            if key in config.media_keys:
                topic = '/observer_camera/' + key + '/compressed'
                self.bridge_specs[key] = (topic, 'sensor_msgs/msg/CompressedImage')
                self.bridge_samples[key] = TopicSample(topic, 'sensor_msgs/msg/CompressedImage', clock)
        if 'depthPoints' in config.media_keys:
            self.bridge_specs['depthPoints'] = ('/observer_camera/depth_points', 'std_msgs/msg/String')
            self.bridge_samples['depthPoints'] = TopicSample('/observer_camera/depth_points', 'std_msgs/msg/String', clock)

    def receive_sensor(self, key, message):
        try:
            value = summarize(message)
            value['messageTimestamp'] = header_timestamp(message)
            if hasattr(message, 'pose'):
                pose = message.pose.pose
                value['orientation'] = {'x': pose.orientation.x, 'y': pose.orientation.y,
                                        'z': pose.orientation.z, 'w': pose.orientation.w}
            if key == 'plan':
                # Bound visualization path, preserving each pose's actual coordinate frame.
                stride = max(1, math.ceil(len(message.poses) / 200))
                value['poses'] = [{'frameId': entry.header.frame_id or message.header.frame_id,
                                  'position': {'x': entry.pose.position.x, 'y': entry.pose.position.y, 'z': entry.pose.position.z},
                                  'orientation': {'x': entry.pose.orientation.x, 'y': entry.pose.orientation.y,
                                                  'z': entry.pose.orientation.z, 'w': entry.pose.orientation.w}}
                                 for entry in message.poses[::stride]]
            self.samples[key].receive(value)
            self.errors.pop(key, None)
        except Exception as exc:
            self.errors[key] = type(exc).__name__ + ': ' + str(exc)[:200]
        self.media.receive(key, message, self.specs[key][1])
        if self.faults is not None:
            try:
                if self.specs[key][3] is not None and key != 'diagnostics':
                    self.faults.observe_stream(self.specs[key][1], source_timestamp(message),
                                               self.specs[key][3], timing=key == 'imu')
                if key == 'diagnostics':
                    self.faults.observe_diagnostics(message)
            except (ValueError, TypeError, OSError, sqlite3.Error):
                pass  # Fault storage cannot interrupt the observation callback.

    def receive_bridge(self, key, message):
        self.bridge_samples[key].receive({'frameId': getattr(getattr(message, 'header', None), 'frame_id', None)})
        self.media.receive(key, message, self.bridge_specs[key][0], compressed=key != 'depthPoints', bridge_cloud=key == 'depthPoints')

    def sensor_snapshot(self):
        result = {key: dict(item.snapshot(), label=self.specs[key][0], updateMode='event' if self.specs[key][3] is None else 'stream') for key, item in self.samples.items()}
        for key, message in self.errors.items():
            result[key].update(status='ERROR', reason=message, value=None)
        for key, item in self.bridge_samples.items():
            bridge = item.snapshot()
            if bridge['status'] == 'AVAILABLE':
                result[key] = dict(bridge, label=self.specs.get(key, (key,))[0], updateMode='stream')
        return result

    def observation(self):
        if self.config.role != 'primary':
            raise ValueError('A camera-only source cannot upload observation or system metrics')
        ros_telemetry = self.telemetry.snapshot()
        ros_data = ros_telemetry.pop('payload')
        # Select one authority explicitly. A disconnected selected source never
        # fills its fields or Pages from the other source.
        telemetry, robot_data = ros_telemetry, ros_data
        source = 'ROS2 /chassis_host/telemetry'
        if self.local_chassis is not None:
            telemetry = self.local_chassis.snapshot()
            robot_data = telemetry.pop('payload')
            source = SOURCE_DESCRIPTION
        sensors = self.sensor_snapshot()
        devices = self.devices.snapshot()
        devices['sensors'] = sensors
        devices['cameras'], devices['visuals'] = self.media.snapshots(sensors)
        if self.local_chassis is not None:
            devices['localChassis'] = dict(telemetry)
        topics = {'/chassis_host/telemetry': ros_telemetry,
                  **self.devices.topic_snapshots(),
                  **{item['topic']: item for item in sensors.values()}}
        for item in self.bridge_samples.values():
            topics[item.topic] = item.snapshot()
        ros_status = 'AVAILABLE' if any(item.get('publisherCount', 0) > 0 for item in topics.values()) else 'WAITING_UPSTREAM'
        try:
            system = self.collector.collect()
        except Exception as exc:
            system = {'status': 'ERROR', 'reason': type(exc).__name__ + ': ' + str(exc)[:200]}
        if self.faults is not None:
            try:
                self.faults.update_context(system)
            except (ValueError, TypeError, OSError, sqlite3.Error):
                pass
        system['source'] = '远程工控机 · ' + self.config.source_id
        return json_safe({'system': system,
                          'robot': {'status': telemetry['status'], 'source': source, 'telemetry': telemetry, 'data': robot_data},
                          'devices': devices,
                          'ros2': {'status': ros_status, 'domainId': os.environ.get('ROS_DOMAIN_ID', '0'),
                                   'rmwImplementation': os.environ.get('RMW_IMPLEMENTATION') or 'runtime default',
                                   'localhostOnly': os.environ.get('ROS_LOCALHOST_ONLY', '0'),
                                   'discoveryServer': os.environ.get('ROS_DISCOVERY_SERVER'), 'topics': topics,
                                   'jetsonDiscovery': 'UNAVAILABLE', 'deployment': 'REMOTE_IPC'},
                          'pages': telemetry['pages'],
                          'errors': {'telemetryParseCount': ros_telemetry['parseErrorCount'], 'lastTelemetryError': ros_telemetry['lastError'],
                                     'telemetryNonFiniteCount': ros_telemetry.get('nonFiniteCount', 0),
                                     'localChassisParseCount': telemetry['parseErrorCount'] if self.local_chassis else None,
                                     'localChassisConnectionErrorCount': telemetry['connectionErrorCount'] if self.local_chassis else None,
                                     'lastLocalChassisError': telemetry['reason'] if self.local_chassis else None}})

    def board_pose(self, message):
        position, rotation = message.pose.position, message.pose.orientation
        siny = 2 * (rotation.w * rotation.z + rotation.x * rotation.y)
        cosy = 1 - 2 * (rotation.y * rotation.y + rotation.z * rotation.z)
        self.devices.receive_board_pose({'frameId': message.header.frame_id or None,
                                        'position': {'xM': position.x, 'yM': position.y, 'zM': position.z},
                                        'yawRad': math.atan2(siny, cosy)})
