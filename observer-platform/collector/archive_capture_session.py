"""Passively record available sources without starting camera or radar drivers.

The desktop capture button owns a short-lived garage lease. On exit it seals
accepted data and releases only its own auxiliary services. Shared camera
sources remain available to navigation and perception, including late joiners.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import select
import shlex
import signal
import stat
import subprocess
import sys
import time
from uuid import uuid4

from .archive_core import GarageGate, atomic_json
from .archive_radar_monitor import RadarObserver
from .archive_devices import (CAMERA_SHUTDOWN_TIMEOUT, DeviceStartupCancelled, DeviceStartupError,
                              inspect_capture_radar, start_capture_devices,
                              stop_owned_capture_cameras as stop_capture_cameras)

RECORDER_UNITS = ('robot-native-archive.service', 'robot-native-archive-source-link.service')
LEASE_SECONDS = 20
VIDEO_TOPICS = frozenset(('/berxel/color/image_raw', '/berxel/depth/image_raw', '/fisheye_front/image_raw'))
LIDAR_TOPICS = frozenset(('/livox/lidar', '/scan', '/bluesea_new/scan'))
LIDAR_DEVICES = (
    ('mid360', 'MID360', '雷达MID360', (
        ('/livox/lidar', 'livox_ros_driver2/msg/CustomMsg'),
        ('/livox/imu', 'sensor_msgs/msg/Imu'))),
    ('stl19p', 'STL-19P 二维雷达', '二维雷达scan', (
        ('/scan', 'sensor_msgs/msg/LaserScan'),)),
    ('bluesea', '蓝海二维雷达', '二维雷达蓝海', (
        ('/bluesea_new/scan', 'sensor_msgs/msg/LaserScan'),)),
)
LIDAR_COUNTERS = ('written', 'received', 'dropped', 'dds_lost')
PROGRESS_INTERVAL_SECONDS = 15
STOP_CONFIRM_TIMEOUT = CAMERA_SHUTDOWN_TIMEOUT + 150


class CaptureError(RuntimeError):
    pass


class _CaptureStartCancelled(Exception):
    """Unwind startup through camera cleanup without arming the recording lease."""


class CaptureTerminal:
    """Remember an interactive terminal so a silent hangup cannot become headless capture."""
    def __init__(self, stream):
        self.stream = stream
        self.interactive = bool(stream.isatty())
        try:
            self.descriptor = stream.fileno() if self.interactive else None
        except (AttributeError, OSError, ValueError):
            self.descriptor = None

    def disconnected(self):
        if not self.interactive:
            return False
        try:
            if not self.stream.isatty():
                return True
            if self.descriptor is None:
                return False
            if self.stream.fileno() != self.descriptor:
                return True
            events = select.poll()
            events.register(self.descriptor, select.POLLHUP | select.POLLERR | select.POLLNVAL)
            return bool(events.poll(0))
        except (OSError, ValueError):
            return True


def _shutdown_notice(message, *, error=False):
    """A closed terminal must not prevent releasing the lease or cameras."""
    try:
        print(message, file=sys.stderr if error else sys.stdout, flush=True)
    except (OSError, ValueError):
        pass


def read_environment(path):
    """Read systemd KEY=value data; never source a user's file in a shell."""
    result = {}
    try:
        lines = Path(path).read_text().splitlines()
    except FileNotFoundError:
        return result
    for line in lines:
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        key, separator, raw = line.partition('=')
        if separator and key.startswith('ARCHIVE_'):
            parts = shlex.split(raw, comments=True)
            if len(parts) > 1:
                raise CaptureError('归档环境文件格式错误：' + key)
            result[key] = parts[0] if parts else ''
    return result


def process_identity(pid):
    """Pair PID with its Linux start time to avoid signalling a reused PID."""
    if type(pid) is not int or pid < 1:
        return None
    try:
        fields = (Path('/proc') / str(pid) / 'stat').read_text().rsplit(')', 1)[1].split()
        return fields[19]
    except (OSError, IndexError):
        return None


def boot_identity():
    try:
        return Path('/proc/sys/kernel/random/boot_id').read_text().strip() or None
    except OSError:
        return None


def status_object(value):
    """A partial/invalid nested status is missing evidence, not a crash."""
    return value if isinstance(value, dict) else {}


class CaptureSession:
    def __init__(self, control, *, runner=subprocess.run, clock=time.time):
        self.control = Path(control)
        self.web_gate = self.control / 'garage.json'
        self.gate = self.control / 'desktop-lease.json'
        self.lease = self.gate
        self.marker = self.control / 'desktop-capture.json'
        self.runner, self.clock = runner, clock
        self.token = uuid4().hex
        self.previous = None
        self.capture_started_at = None
        self.usb_sink_participated = False
        self.video_sources, self.lidar_sources = set(), set()
        self._written_baseline = {}
        self._loss_previous = None
        self._loss_observed = False
        self._loss_uncertain = False
        self._gate_revisions = set()
        self._progress_at = None
        self._progress_state = None
        self.device_startup = None
        self.recording_dds = None
        self._lidar_baseline = {}
        self._lidar_evidence = {}
        self._lidar_expected_seen = set()
        self.radar_observation = None
        self._radar_observer = None
        self.camera_shutdown = None

    def start_radar_observer(self, values):
        self._radar_observer = RadarObserver(values, clock=self.clock)
        self._radar_observer.start()

    def refresh_radar_observation(self):
        if self._radar_observer is not None:
            value = self._radar_observer.snapshot()
            if value is not None:
                self.radar_observation = value

    def stop_radar_observer(self):
        if self._radar_observer is not None:
            self.refresh_radar_observation()
            if self._radar_observer.close():
                self._radar_observer = None

    def read(self, path):
        try:
            if path.is_symlink() or path.stat().st_size > 262144:
                return {}
            value = json.loads(path.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    @contextmanager
    def lock(self):
        self.control.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.control / 'desktop-capture.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise CaptureError('采集锁文件无效')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CaptureError('已有本工具的采集会话正在运行，请用 --status 或 --stop') from None
            yield
        finally:
            os.close(fd)

    def claim(self):
        self.previous = self.read(self.gate)
        if GarageGate(self.web_gate).enabled():
            raise CaptureError('采集开关已由网页或其他会话开启；请先在原入口结束，本工具没有更改它')
        if self.gate.is_symlink():
            raise CaptureError('采集开关不能是符号链接')
        local = self.read(self.control/'status.json')
        source = self.read(self.control/'source.json')
        self._written_baseline = self._written_counts(local, source)
        self._loss_previous = self._loss_snapshot(local, source)
        topics = status_object(status_object(local.get('ros')).get('topics'))
        self._lidar_baseline = {topic: {key: row[key] for key in LIDAR_COUNTERS
            if type(row.get(key)) is int and row[key] >= 0}
            for topic, row in topics.items() if isinstance(row, dict)
            and any(topic == required for _, _, _, required_topics in LIDAR_DEVICES
                    for required, _ in required_topics)}
        self.capture_started_at = self.clock()
        try:
            self.renew(first=True)
            atomic_json(self.marker, dict(schemaVersion=1, state='recording', token=self.token,
                pid=os.getpid(), processStart=process_identity(os.getpid()), bootId=boot_identity(),
                startedAt=self.capture_started_at, deviceStartup=self.device_startup,
                recordingDds=self.recording_dds, lidarCounterBaseline=self._lidar_baseline,
                radarObservation=self.radar_observation,
                lidarDevices=self.lidar_diagnostics(local)))
        except BaseException:
            self.release()
            raise

    def renew(self, *, first=False):
        current = self.read(self.gate)
        if not first and current.get('desktopSession') != self.token:
            return False
        revision = current.get('updatedAt') if not first else datetime_iso(self.clock())
        atomic_json(self.gate, dict(schemaVersion=1, scope='garage', enabled=True,
            desktopSession=self.token, updatedAt=revision,
            renewedAt=datetime_iso(self.clock()),
            expiresAt=self.clock() + LEASE_SECONDS))
        self._gate_revisions.add(revision)
        return True

    def release(self):
        if self.read(self.gate).get('desktopSession') == self.token:
            # Restore a previously closed choice, while preserving any new
            # web/user choice which replaced our token during capture.
            restored = dict(self.previous or {}, schemaVersion=1, scope='garage', enabled=False,
                            updatedAt=datetime_iso(self.clock()), expiresAt=None)
            restored.pop('desktopSession', None)
            atomic_json(self.gate, restored)
            self._gate_revisions.add(restored['updatedAt'])
        marker = self.read(self.marker)
        if marker.get('token') == self.token:
            atomic_json(self.marker, {**marker, 'state': 'stopped', 'stoppedAt': self.clock(),
                'videoSources': sorted(self.video_sources), 'lidarSources': sorted(self.lidar_sources)})

    @staticmethod
    def _written_counts(local, source):
        result = {}
        for origin, report in (('ipc', local), ('source', source.get('recorder'))):
            if not isinstance(report, dict):
                continue
            ros = report.get('ros')
            topics = ros.get('topics') if isinstance(ros, dict) else None
            if isinstance(topics, dict):
                for topic, row in topics.items():
                    value = row.get('written') if isinstance(row, dict) else None
                    if type(value) is int and value >= 0:
                        result[(origin, topic)] = value
        camera = source.get('camera')
        sink = camera.get('sink') if isinstance(camera, dict) else None
        counters = sink.get('counters') if isinstance(sink, dict) else None
        if isinstance(counters, dict):
            for key in ('written', 'enqueued'):
                if type(counters.get(key)) is int and counters[key] >= 0:
                    result[('usb', key)] = counters[key]
        return result

    def observe_sources(self):
        """Track this session's writes, independently of stale process reports.

        A camera process may start after recording was armed. Only a fresh
        acknowledgement of our revision may enroll its USB sink in shutdown.
        ROS image recording remains valid without that separate USB process.
        """
        if self.capture_started_at is None:
            return
        local = self.read(self.control/'status.json')
        source = self.read(self.control/'source.json')
        self._observe_losses(local, source)
        now = self.clock()
        def fresh(report):
            stamp = status_timestamp(report.get('updatedAt')) if isinstance(report, dict) else 0
            return self.capture_started_at <= stamp <= now + 2 and now - stamp <= 10
        current_source = (fresh(source) and source.get('connected') is True
            and source.get('gateAcknowledged') is True
            and isinstance(source.get('gateRevision'), str)
            and source.get('gateRevision') in self._gate_revisions)
        reports = {'ipc': local} if fresh(local) else {}
        if 'ipc' in reports:
            self._lidar_evidence = self._lidar_topic_evidence(local)
        remote = source.get('recorder')
        if current_source and fresh(remote):
            reports['source'] = remote
        counts = self._written_counts(local, source)
        for (origin, topic), count in counts.items():
            if origin not in reports:
                continue
            baseline = self._written_baseline.get((origin, topic), count)
            # Counter reset means the worker restarted, so its positive count
            # belongs to that fresh process instead of the previous baseline.
            wrote = count > baseline or 0 < count < baseline
            if wrote and topic in VIDEO_TOPICS:
                self.video_sources.add(topic)
            if wrote and topic in LIDAR_TOPICS:
                self.lidar_sources.add(topic)
            self._written_baseline.setdefault((origin, topic), count)
        # A source may first report after this session already wrote packets.
        # A cumulative counter without a baseline is ambiguous; a segment
        # opened during this session with written topic counts is not.
        for report in reports.values():
            store = status_object(report.get('store'))
            for segment in status_object(store.get('active')).values():
                if not isinstance(segment, dict):
                    continue
                started = status_timestamp(segment.get('startedAt'))
                if (not self.capture_started_at <= started <= now + 2
                        or type(segment.get('messages')) is not int or segment['messages'] <= 0):
                    continue
                for topic, row in status_object(segment.get('topics')).items():
                    if not isinstance(row, dict) or type(row.get('count')) is not int or row['count'] <= 0:
                        continue
                    if topic in VIDEO_TOPICS:
                        self.video_sources.add(topic)
                    if topic in LIDAR_TOPICS:
                        self.lidar_sources.add(topic)
        camera = source.get('camera')
        if not current_source or not fresh(camera):
            return
        sink = camera.get('sink')
        store = camera.get('store')
        if not isinstance(sink, dict):
            return
        active = store.get('active') if isinstance(store, dict) else None
        wrote_usb = False
        accepted_usb = False
        for key in ('written', 'enqueued'):
            count = counts.get(('usb', key))
            if count is None:
                continue
            baseline = self._written_baseline.get(('usb', key), count)
            changed = count > baseline or 0 < count < baseline
            wrote_usb |= key == 'written' and changed
            accepted_usb |= key == 'enqueued' and changed
            self._written_baseline.setdefault(('usb', key), count)
        has_active_frames = (isinstance(active, dict)
            and any(isinstance(row, dict) and type(row.get('frames')) is int and row['frames'] > 0
                    for row in active.values()))
        if (accepted_usb or wrote_usb or has_active_frames
                or (sink.get('enabled') is True and sink.get('writerRunning') is True)):
            self.usb_sink_participated = True
        if wrote_usb or has_active_frames:
            self.video_sources.add('USB相机')

    def _lidar_session_live(self, marker):
        if self.capture_started_at is not None:
            return True
        lease = self.read(self.lease)
        expiry = lease.get('expiresAt')
        return (marker.get('state') == 'recording' and isinstance(marker.get('token'), str)
            and lease.get('desktopSession') == marker['token'] and lease.get('enabled') is True
            and type(expiry) in (int, float) and self.clock() < expiry <= self.clock() + 60)

    def _lidar_topic_evidence(self, report, marker=None):
        """Conservative session counts; a late cumulative total is not proof.

        Counters can reset when the recorder restarts, so these are lower
        bounds, never a claim to count every packet. A current-session active
        segment also proves writes when no pre-capture baseline was available.
        Only IPC reports can satisfy the IPC radar requirements.
        """
        marker = marker if marker is not None else self.read(self.marker)
        owned = self.capture_started_at is not None
        evidence = {topic: dict(row) for topic, row in self._lidar_evidence.items()}
        if not owned:
            for device in marker.get('lidarDevices', []) if isinstance(marker.get('lidarDevices'), list) else []:
                for row in device.get('topics', []) if isinstance(device, dict) and isinstance(device.get('topics'), list) else []:
                    if isinstance(row, dict) and isinstance(row.get('topic'), str):
                        evidence[row['topic']] = {key: row[key] for key in LIDAR_COUNTERS
                            if type(row.get(key)) is int and row[key] >= 0}
        started = self.capture_started_at if owned else marker.get('startedAt')
        if type(started) not in (int, float):
            return evidence
        now, stamp = self.clock(), status_timestamp(report.get('updatedAt'))
        if (not self._lidar_session_live(marker)
                or not started <= stamp <= now + 2 or now - stamp > 10):
            return evidence
        baseline = self._lidar_baseline if owned else status_object(marker.get('lidarCounterBaseline'))
        topics = status_object(status_object(report.get('ros')).get('topics'))
        for _, _, _, required in LIDAR_DEVICES:
            for topic, _ in required:
                row, before = status_object(topics.get(topic)), status_object(baseline.get(topic))
                current = evidence.setdefault(topic, {})
                for key in LIDAR_COUNTERS:
                    count, initial = row.get(key), before.get(key)
                    if type(count) is int and count >= 0 and type(initial) is int and initial >= 0:
                        delta = count - initial if count >= initial else count
                        current[key] = max(current.get(key, 0), delta)
        for segment in status_object(status_object(report.get('store')).get('active')).values():
            if (not isinstance(segment, dict) or not started <= status_timestamp(segment.get('startedAt')) <= now + 2
                    or type(segment.get('messages')) is not int or segment['messages'] <= 0):
                continue
            for topic, row in status_object(segment.get('topics')).items():
                if topic in evidence and isinstance(row, dict) and type(row.get('count')) is int and row['count'] > 0:
                    evidence[topic]['written'] = max(evidence[topic].get('written', 0), row['count'])
        return evidence

    def lidar_diagnostics(self, report=None, marker=None):
        """Expose per-device proof and existing recorder errors, without ROS calls."""
        report = report if report is not None else self.read(self.control/'status.json')
        marker = marker if marker is not None else self.read(self.marker)
        startup = self.device_startup if self.device_startup is not None else marker.get('deviceStartup')
        ipc = status_object(status_object(status_object(startup).get('hosts')).get('ipc'))
        rows = ipc.get('devices') if isinstance(ipc.get('devices'), list) else []
        startup_rows = {row.get('key'): row for row in rows if isinstance(row, dict) and isinstance(row.get('key'), str)}
        available = ipc.get('available') if isinstance(ipc.get('available'), list) else []
        evidence = self._lidar_topic_evidence(report, marker)
        ros = status_object(report.get('ros'))
        topics = status_object(ros.get('topics'))
        selected = status_object(report.get('recordingPolicy')).get('rosSensors')
        now, stamp = self.clock(), status_timestamp(report.get('updatedAt'))
        started = self.capture_started_at if self.capture_started_at is not None else marker.get('startedAt')
        fresh = (-2 <= now - stamp <= 10 and type(started) in (int, float) and stamp >= started)
        live = self._lidar_session_live(marker)
        stopped = marker.get('state') == 'stopped'
        observation = status_object(getattr(self, 'radar_observation', None) or marker.get('radarObservation'))
        checked = observation.get('checkedAt')
        observation_fresh = (observation.get('readOnlySources') is True
            and type(checked) in (int, float) and type(started) in (int, float)
            and started <= checked and 0 <= now - checked <= 30)
        observed_ipc = status_object(status_object(observation.get('hosts')).get('ipc')) if observation_fresh else {}
        observed_rows = {row['key']: row for row in observed_ipc.get('devices', [])
            if isinstance(row, dict) and isinstance(row.get('key'), str)} if isinstance(observed_ipc.get('devices'), list) else {}
        graph_state = status_object(observation.get('graph')).get('state') if observation_fresh else None
        expected_seen = getattr(self, '_lidar_expected_seen', set())
        if not isinstance(expected_seen, set):
            expected_seen = set()
        self._lidar_expected_seen = expected_seen
        if self.capture_started_at is None or marker.get('token') == self.token:
            for row in marker.get('lidarDevices', []) if isinstance(marker.get('lidarDevices'), list) else []:
                if isinstance(row, dict) and isinstance(row.get('key'), str) and row.get('expected') is True:
                    expected_seen.add(row['key'])
        result = []
        for key, label, sensor, required in LIDAR_DEVICES:
            startup_source = startup_rows.get(key, {})
            startup_availability = startup_source.get('state', 'available' if key in available else 'unknown')
            source = observed_rows.get(key, startup_source)
            availability = source.get('state', startup_availability)
            device_graph_state = status_object(observed_rows.get(key, {}).get('recordingGraph')).get('state')
            fallback_graph_state = (graph_state if key in observed_rows
                and availability in ('started', 'reused', 'partial', 'available') else None)
            graph_mismatch = (device_graph_state if device_graph_state in ('aligned', 'mismatch', 'unknown')
                              else fallback_graph_state) == 'mismatch'
            expected = (startup_availability in ('started', 'reused', 'partial', 'available')
                or availability in ('started', 'reused', 'partial', 'available') or key in expected_seen
                or any(evidence.get(topic, {}).get(name, 0) > 0
                       for topic, _ in required for name in ('received', 'written')))
            if expected:
                expected_seen.add(key)
            details = []
            for topic, expected_type in required:
                row, counts = status_object(topics.get(topic)), evidence.get(topic, {})
                written, received = counts.get('written', 0), counts.get('received', 0)
                error = row.get('error') or ros.get('gateError') or ros.get('tickError')
                if stopped:
                    state, reason = ('recorded', 'session_stopped') if written else ('missing', 'no_this_session_write')
                elif not live:
                    state, reason = 'status_unavailable', 'recording_lease_not_active'
                elif not fresh:
                    state, reason = 'status_unavailable', 'recorder_status_missing_or_stale'
                elif error:
                    state, reason = 'error', str(error)
                elif row.get('type') is not None and row.get('type') != expected_type:
                    state, reason = 'error', 'subscription_type_mismatch'
                elif not row:
                    state, reason = ('not_subscribed', 'sensor_not_selected' if isinstance(selected, list)
                        and sensor not in selected else 'topic_status_missing')
                elif row.get('state') == 'DISABLED':
                    state, reason = 'disabled', 'recorder_subscription_disabled'
                elif row.get('state') == 'STALE':
                    state, reason = 'stale', 'upstream_stale'
                elif not written:
                    state, reason = ('not_written', 'received_but_not_written') if received else (
                        'waiting_upstream', 'no_this_session_messages')
                elif row.get('state') == 'AVAILABLE' and type(row.get('ageS')) in (int, float) and 0 <= row['ageS'] <= 2:
                    state, reason = 'receiving', 'this_session_write_confirmed'
                else:
                    state, reason = 'written', 'this_session_write_confirmed'
                if graph_mismatch and not received and not written and state == 'waiting_upstream':
                    state, reason = 'error', 'recording_dds_graph_mismatch'
                if (not received and not written and not expected
                        and state in ('waiting_upstream', 'not_subscribed')
                        and availability in ('failed', 'skipped', 'inactive')):
                    state, reason = 'not_connected', source.get('error') or source.get('message') or availability
                details.append(dict(topic=topic, expectedType=expected_type, type=row.get('type'),
                    state=state, reason=reason, recorderState=row.get('state'), error=error,
                    ageS=row.get('ageS'), **{name: counts.get(name, 0) for name in LIDAR_COUNTERS},
                    countsAreLowerBounds=True, processDropReasons=row.get('reasons')))
            missing = [row['topic'] for row in details if not row['written']]
            state = ('recorded' if stopped else 'receiving') if all(row['state'] == ('recorded' if stopped else 'receiving') for row in details) else (
                'error' if any(row['state'] == 'error' for row in details) else
                'not_connected' if all(row['state'] == 'not_connected' for row in details) else
                'partial' if any(row['written'] for row in details) else 'missing')
            result.append(dict(key=key, label=label, expected=expected, availability=availability,
                startupReason=startup_source.get('error') or startup_source.get('message') or ipc.get('error'),
                complete=not missing, state=state, missingTopics=missing, topics=details))
        return result

    def missing_data(self):
        missing = [] if self.video_sources else ['视频']
        expected = [row for row in self.lidar_diagnostics() if row['expected']]
        if expected:
            missing.extend('雷达 ' + row['label'] + ' (' + topic + ')'
                           for row in expected for topic in row['missingTopics'])
        elif not self.lidar_sources:
            # --no-device-start and old/partial startup reports retain the
            # former minimum category check rather than inventing hardware.
            missing.append('雷达')
        return missing

    def recording_warnings(self):
        """Separate a working camera preview from an actually running archive tap."""
        if self.capture_started_at is None or self.clock() - self.capture_started_at < 15:
            return []
        now = self.clock()
        lease = self.read(self.lease)
        expiry = lease.get('expiresAt')
        if (lease.get('desktopSession') != self.token or lease.get('enabled') is not True
                or type(expiry) not in (int, float) or not now < expiry <= now + 60):
            return []
        def fresh(report):
            return -2 <= now - status_timestamp(report.get('updatedAt')) <= 10
        warnings = []
        observation = status_object(self.radar_observation)
        checked = observation.get('checkedAt')
        if (type(checked) in (int, float) and 0 <= now - checked <= 30
                and status_object(observation.get('graph')).get('state') == 'mismatch'):
            warnings.append('雷达接收端与驱动的 DDS 通信配置不同；本轮保持设备和算法运行，'
                '请结束采集后核对接收端与驱动的 DDS 配置。')
        local = self.read(self.control/'status.json')
        if fresh(local) and status_object(local.get('store')).get('enabled') is False:
            warnings.append('本机录制开关尚未生效，雷达订阅没有进入保存状态。')
        source = self.read(self.control/'source.json')
        if not fresh(source) or source.get('connected') is not True:
            return warnings
        remote = status_object(source.get('recorder'))
        if (fresh(remote) and source.get('gateEnabled') is True
                and status_object(remote.get('store')).get('enabled') is False):
            warnings.append('相机主机已收到开关，但录制进程尚未进入保存状态。')
        startup = status_object(status_object(self.device_startup).get('hosts'))
        devices = status_object(startup.get('jetson')).get('devices')
        devices = devices if isinstance(devices, list) else []
        usb_ready = any(isinstance(row, dict) and row.get('key') == 'usb-cameras'
            and row.get('dataVerified') is True for row in devices)
        camera = status_object(source.get('camera'))
        if usb_ready and not fresh(camera):
            warnings.append('USB 相机画面已就绪，但原始视频归档状态缺失或过期；预览正常不代表视频已保存，请检查归档 tap。')
        for label, report in (('本机', local), ('相机主机', remote)):
            if not fresh(report):
                continue
            errors = status_object(status_object(report.get('store')).get('errors'))
            if errors.get('storage'):
                warnings.append(label + '写盘保护：' + str(errors['storage']))
        return warnings

    def show_progress(self):
        """Keep a waiting session visibly alive without claiming unwritten data."""
        if self.capture_started_at is None:
            return
        now = self.clock()
        source = self.read(self.control/'source.json')
        age = now-status_timestamp(source.get('updatedAt'))
        if not -2 <= age <= 10:
            connection = '等待状态同步'
        elif source.get('connected') is not True:
            connection = '未连接；继续保存本机已有数据'
        elif (source.get('gateAcknowledged') is True and source.get('gateEnabled') is True
                and isinstance(source.get('gateRevision'), str)
                and source.get('gateRevision') in self._gate_revisions):
            connection = '已连接，保存开关已同步'
        else:
            connection = '已连接，等待采集同步'
        missing = self.missing_data()
        devices = self.lidar_diagnostics()
        warnings = self.recording_warnings()
        state = (tuple(missing), connection, tuple(warnings), tuple((row['key'], tuple(
            (topic['state'], topic['reason']) for topic in row['topics'])) for row in devices))
        stamp = time.monotonic()
        if (self._progress_at is not None and state == self._progress_state
                and stamp-self._progress_at < PROGRESS_INTERVAL_SECONDS):
            return
        self._progress_at, self._progress_state = stamp, state
        elapsed = int(max(0, now-self.capture_started_at))
        video = '本次已确认写盘' if self.video_sources else '等待数据，尚未确认写盘'
        expected = [row for row in devices if row['expected']]
        lidar = ('本次各路已确认写盘' if all(row['complete'] for row in expected)
                 else '本次尚有话题未确认写盘') if expected else (
                     '本次已确认写盘' if self.lidar_sources else '等待数据，尚未确认写盘')
        print(f'[采集进度] 已运行 {elapsed} 秒；视频输入：{video}；雷达：{lidar}；相机主机：{connection}。',
              flush=True)
        for row in devices:
            if not row['expected'] and row['availability'] == 'unknown':
                continue
            detail = '；'.join(topic['topic'] + ' 本轮写盘至少 ' + str(topic['written'])
                + ' 条，' + topic['state'] + '：' + str(topic['reason']) for topic in row['topics'])
            print('  ' + row['label'] + '（' + row['availability'] + '）：' + detail, flush=True)
        if missing:
            print('正在等待' + '、'.join(missing)
                  + '数据；脚本仍在运行。请检查设备启动结果及连接；按 Enter / Ctrl+C 可结束。', flush=True)
        for warning in warnings:
            print('录制异常：' + warning, flush=True)
        self.record_result()

    def record_result(self):
        marker = self.read(self.marker)
        if marker.get('token') == self.token:
            atomic_json(self.marker, {**marker, 'videoSources': sorted(self.video_sources),
                'lidarSources': sorted(self.lidar_sources), 'missingData': self.missing_data(),
                'lidarDevices': self.lidar_diagnostics(), 'recordingDds': self.recording_dds,
                'radarObservation': self.radar_observation,
                'recordingWarnings': self.recording_warnings()})

    @staticmethod
    def _loss_snapshot(local, source):
        """Keep cumulative counters separate for each independently running writer."""
        reports = {}
        if isinstance(local.get('ros'), dict):
            reports['ipc'] = local
        recorder, camera = source.get('recorder'), source.get('camera')
        if isinstance(recorder, dict) and isinstance(recorder.get('ros'), dict):
            reports['source'] = recorder
        if isinstance(camera, dict) and isinstance(camera.get('sink'), dict):
            reports['usb'] = camera
        result = {}
        for origin, report in reports.items():
            losses, progress = {}, {}
            def walk(value, path=()):
                for key, child in value.items():
                    current = (*path, key)
                    name = key.lower()
                    if type(child) is int and child >= 0:
                        if ((('dropped' in name or name == 'store_rejected')
                                and 'inactive' not in name and 'sample' not in name)
                                or name in ('sequencegaps', 'corruptpackets', 'writeerrors',
                                            'messagelost', 'dds_lost')):
                            losses[current] = child
                        # Stable lifetime counters also expose a worker restart
                        # when its old nonzero loss counters have reset to zero.
                        if name in ('written', 'received', 'writtenbytes', 'finalizedsegments'):
                            progress[current] = child
                    elif isinstance(child, dict):
                        walk(child, current)
            walk(report)
            result[origin] = losses, progress
        return result

    def _observe_losses(self, local, source):
        """Accumulate this capture's loss evidence; never clear it after a restart."""
        current = self._loss_snapshot(local, source)
        previous = self._loss_previous
        if previous is None:
            # Legacy callers without a claim-time baseline cannot attribute an
            # existing positive counter to a previous capture.
            self._loss_observed |= any(value > 0 for losses, _ in current.values()
                                       for value in losses.values())
        else:
            if set(previous) != set(current):
                self._loss_uncertain = True
            for origin, (losses, progress) in current.items():
                old_losses, old_progress = previous.get(origin, ({}, {}))
                self._loss_observed |= any(value > old_losses.get(key, 0)
                                           for key, value in losses.items())
                if (any(value < old_losses[key] for key, value in losses.items() if key in old_losses)
                        or any(value < old_progress[key] for key, value in progress.items()
                               if key in old_progress)):
                    # We cannot reconstruct unobserved losses across a restart.
                    self._loss_uncertain = True
        self._loss_previous = current
        return self._loss_observed or self._loss_uncertain

    def persist_session_end(self, values, *, sealed, incomplete):
        from .archive_session_end import persist_session_end
        upload_path = values.get('ARCHIVE_UPLOAD_CONFIG', str(Path.home()/'.config/robot-cloud/archive-upload.json'))
        robot_id = values.get('ARCHIVE_ROBOT_ID') or self.read(Path(upload_path)).get('robotId')
        marker = self.read(self.marker)
        loss = self._observe_losses(self.read(self.control/'status.json'),
                                    self.read(self.control/'source.json'))
        incomplete = incomplete or loss
        return persist_session_end(self.control, self.token, robot_id,
            self.capture_started_at if self.capture_started_at is not None else marker.get('startedAt', self.clock()),
            now=self.clock(), sealed=sealed, capture_incomplete=incomplete,
            requires_camera=self.usb_sink_participated,
            stop_requested_at=marker.get('stoppedAt', self.clock()))

    def ensure_recorders(self):
        # This list deliberately excludes drivers, FAST-LIO, YOLO, preview,
        # tunnels and the database. Existing algorithms retain ownership.
        for unit in RECORDER_UNITS:
            result = self.runner(['systemctl', '--user', 'start', unit], capture_output=True, text=True, timeout=25)
            if result.returncode:
                raise CaptureError('无法启动归档辅助服务：' + unit)

    def ensure_devices(self, values, *, cancelled):
        self.device_startup = start_capture_devices(values, cancelled=cancelled)
        if not cancelled():
            from .archive_graph import RecordingGraphError, ensure_recording_dds
            try:
                self.recording_dds = ensure_recording_dds(
                    values, self.control, self.device_startup, runner=self.runner)
            except (RecordingGraphError, OSError, ValueError, subprocess.SubprocessError) as error:
                raise CaptureError('归档 DDS 图对齐未完成：' + str(error)) from error
        if not self.device_startup['ok']:
            print('部分设备未就绪，将继续保存其余可用数据；本次完整性会标记为未完成。', flush=True)
        ipc = status_object(status_object(self.device_startup.get('hosts')).get('ipc'))
        rows = ipc.get('devices')
        lidar_rows = [row for row in rows if isinstance(row, dict)
            and row.get('key') in ('mid360', 'stl19p', 'bluesea')] if isinstance(rows, list) else []
        if lidar_rows and all(row.get('state') == 'inactive' for row in lidar_rows):
            print('当前没有雷达驱动发布数据。手柄控制不会启动雷达；如需保存雷达，'
                  '请保持算法原有雷达驱动运行。采集脚本不会另起一份驱动。', flush=True)

    def ensure_passive_sources(self, values, *, cancelled):
        """Inspect existing local radar without controlling camera or radar sources."""
        try:
            ipc = inspect_capture_radar(values, cancelled=cancelled)
        except (DeviceStartupError, OSError, ValueError, subprocess.SubprocessError) as error:
            ipc = dict(ok=False, role='ipc', recordingOnly=True,
                readOnlySources=True, sourceControl=False, devices=[], error=str(error))
        rows = ipc.get('devices') if isinstance(ipc.get('devices'), list) else []
        failures = [dict(host='ipc', key=row.get('key', 'ipc'), state=row.get('state', 'failed'))
                    for row in rows if isinstance(row, dict) and row.get('state') not in ('started', 'reused')]
        if not ipc.get('ok') and not failures:
            failures.append(dict(host='ipc', key='ipc', state='failed'))
        self.device_startup = dict(ok=ipc.get('ok') is True, hosts=dict(ipc=ipc),
            incompleteDevices=failures, readOnlySources=True, sourceControl=False)
        if not cancelled():
            from .archive_graph import RecordingGraphError, ensure_recording_dds
            try:
                self.recording_dds = ensure_recording_dds(
                    values, self.control, self.device_startup, runner=self.runner)
            except (RecordingGraphError, OSError, ValueError, subprocess.SubprocessError) as error:
                self.recording_dds = dict(state='unavailable', reason='alignment_failed', error=str(error))
                self.device_startup['ok'] = False
                self.device_startup['incompleteDevices'].append(
                    dict(host='ipc', key='recording-dds', state='failed'))
                print('归档 DDS 对齐未完成：' + str(error) + '；保留设备和算法，继续录制已有可用数据。', flush=True)
        if not self.device_startup['ok']:
            print('部分现有数据源尚未就绪；继续保存可用数据，缺失会标记为未完成。', flush=True)
        if rows and all(row.get('state') == 'inactive' for row in rows if isinstance(row, dict)):
            print('当前没有雷达驱动发布数据；采集保持只读等待，由设备原有入口管理驱动。', flush=True)

    def wait_ready(self, seconds=25):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            report = self.read(self.control / 'status.json')
            ros = status_object(report.get('ros'))
            if (0 <= self.clock() - status_timestamp(report.get('updatedAt')) <= 8
                    and ros.get('state') in ('DISABLED', 'LISTENING', 'WAITING_UPSTREAM')
                    and not ros.get('tickError') and not ros.get('gateError')
                    and ros.get('subscriptionCleanupPending') == 0
                    and report.get('state') not in ('stopped', 'stop_failed')):
                return
            time.sleep(.25)
        raise CaptureError('归档进程未确认就绪，采集租约未开启；请检查归档服务')

    def wait_sealed(self, seconds=25):
        deadline = time.monotonic() + seconds
        since = self.clock()
        while time.monotonic() < deadline:
            self.observe_sources()
            # A subsequent owner's capture must not hold this session open.
            if GarageGate(self.web_gate).enabled():
                return False
            status = self.read(self.control / 'status.json')
            store, ros = status_object(status.get('store')), status_object(status.get('ros'))
            source = self.read(self.control / 'source.json')
            remote = status_object(source.get('recorder'))
            remote_store = status_object(remote.get('store'))
            remote_ros = status_object(remote.get('ros'))
            camera = status_object(source.get('camera'))
            camera_store, camera_sink = status_object(camera.get('store')), status_object(camera.get('sink'))
            camera_required = self.capture_started_at is None or self.usb_sink_participated
            camera_sealed = (camera.get('state') != 'stop_failed'
                and status_timestamp(camera.get('updatedAt')) >= since
                and isinstance(camera.get('store'), dict)
                and camera_sink.get('enabled') is False
                and camera_store.get('active') == {}
                and all(type(camera_sink.get(key)) is int
                        and camera_sink[key] == 0 for key in ('queueFrames', 'queueBytes', 'inFlightFrames')))
            source_sealed = (source.get('connected') is True
                and source.get('gateAcknowledged') is True and source.get('gateEnabled') is False
                and status_timestamp(source.get('updatedAt')) >= since
                and status_timestamp(remote.get('updatedAt')) >= since
                and remote_ros.get('active') is False
                and all(type(remote_ros.get(key)) is int and remote_ros[key] == 0
                        for key in ('queueMessages', 'queueBytes', 'inFlightMessages', 'subscriptionCleanupPending'))
                and remote_store.get('active') == {}
                and (not camera_required or camera_sealed))
            if (status_timestamp(status.get('updatedAt')) >= since
                    and store.get('active') == {} and ros.get('active') is False
                    and source_sealed
                    and all(type(ros.get(key)) is int and ros[key] == 0
                            for key in ('queueMessages', 'queueBytes', 'inFlightMessages', 'subscriptionCleanupPending'))):
                return True
            time.sleep(.25)
        return False

    @staticmethod
    def camera_ownership(startup):
        host = status_object(status_object(status_object(startup).get('hosts')).get('jetson'))
        return host.get('cameraOwnership')

    def close_cameras(self, values, *, timeout=CAMERA_SHUTDOWN_TIMEOUT, ownership=None):
        _shutdown_notice('正在收尾本次采集的辅助服务；共享相机、雷达和算法保持运行。')
        try:
            if GarageGate(self.web_gate).enabled() or self.read(self.lease).get('enabled') is True:
                result = dict(ok=True, sharedSourcesRetained=True, devices=[],
                              reason='another_or_unreleased_capture_gate')
            else:
                result = stop_capture_cameras(values, timeout=timeout, ownership=(
                    self.camera_ownership(self.device_startup) if ownership is None else ownership))
            if not isinstance(result, dict) or type(result.get('ok')) is not bool:
                raise DeviceStartupError('没有收到有效的采集辅助服务收尾报告')
        except (OSError, ValueError, DeviceStartupError, subprocess.SubprocessError) as error:
            result = dict(ok=False, devices=[], errors=[str(error)])
            _shutdown_notice('采集辅助服务收尾未确认：' + str(error) + '；共享相机未被全局关闭。', error=True)
        self.camera_shutdown = result
        marker = self.read(self.marker)
        if marker.get('token') == self.token:
            try:
                atomic_json(self.marker, {**marker, 'cameraShutdown': result})
            except OSError as error:
                _shutdown_notice('采集辅助服务收尾结果无法写入状态文件：' + str(error), error=True)
        if result['ok']:
            _shutdown_notice('本次采集辅助服务已收尾；共享相机保留，雷达、导航和建图继续运行。')
        else:
            _shutdown_notice('部分采集辅助服务收尾未确认；本次退出标记为未完成。', error=True)
        return result['ok']

    def stop(self, values=None, *, timeout=STOP_CONFIRM_TIMEOUT):
        marker = self.read(self.marker)
        token = marker.get('token')

        def end_proof():
            return (self.read(self.control / 'session-ends' / (token + '.json'))
                    if isinstance(token, str) and len(token) == 32
                    and all(c in '0123456789abcdef' for c in token) else {})

        def stop_remaining_cameras():
            # A replacement capture must not start between the stale-owner check
            # and the fallback camera shutdown. Never wait for a new owner's lock.
            try:
                with self.lock():
                    current = self.read(self.marker)
                    identity = ('token', 'pid', 'processStart', 'bootId')
                    if any(current.get(key) != marker.get(key) for key in identity):
                        _shutdown_notice('已出现其他采集会话，保留它的相机；本次结束未确认。', error=True)
                        return 3
                    lease = self.read(self.lease)
                    if lease.get('enabled') is True and lease.get('desktopSession') != token:
                        _shutdown_notice('其他入口已开启采集，保留其设备与开关；本次结束未确认。', error=True)
                        return 3
                    ownership = self.camera_ownership(current.get('deviceStartup'))
                    passive = (status_object(current.get('deviceStartup')).get('sourceControl') is False
                        or status_object(current.get('cameraShutdown')).get('reason') == 'passive_sources'
                        or not ownership)
                    if passive:
                        self.camera_shutdown = dict(ok=True, skipped=True, reason='passive_sources')
                        if current:
                            atomic_json(self.marker, {**current, 'cameraShutdown': self.camera_shutdown})
                    elif not self.close_cameras(values or {}, ownership=ownership):
                        return 3
                    if isinstance(token, str) and len(token) == 32:
                        proof = end_proof()
                        if (current.get('state') != 'stopped' or proof.get('sessionId') != token
                                or proof.get('state') != 'sealed'):
                            _shutdown_notice('采集辅助服务已收尾，但本次录制结束证明仍待确认；未报告完整停止。', error=True)
                            return 3
                        _shutdown_notice('录制结束标记已保存；共享相机保持运行。')
                    return 0
            except CaptureError:
                _shutdown_notice('另一采集或收尾操作仍持有会话锁，保留其设备；请等待确认后再重试。', error=True)
                return 3
        finishing = (marker.get('state') == 'stopped'
                     and not isinstance(marker.get('cameraShutdown'), dict))
        if marker.get('state') != 'recording' and not finishing:
            _shutdown_notice('没有本工具正在运行的采集会话；不会关闭其他入口的相机。')
            return stop_remaining_cameras()
        pid = marker.get('pid')
        if (not marker.get('bootId') or boot_identity() != marker['bootId']
                or process_identity(pid) != marker.get('processStart') or not marker.get('processStart')):
            _shutdown_notice('上次采集进程已退出，短期租约会自动失效；只检查本次采集的辅助服务。')
            return stop_remaining_cameras()
        if not finishing:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                _shutdown_notice('本次采集进程已经退出；只检查本次采集的辅助服务。')
                return stop_remaining_cameras()
        _shutdown_notice('结束请求已接收，正在等待录制收尾和结束标记保存；共享相机和算法继续运行。')
        deadline, notice = time.monotonic() + timeout, time.monotonic() + 15
        while True:
            current = self.read(self.marker)
            if current.get('token') != token:
                _shutdown_notice('采集会话已被其他入口替换，未操作新会话；结束结果尚未确认。', error=True)
                return 3
            shutdown = status_object(current.get('cameraShutdown'))
            if current.get('state') == 'stopped' and type(shutdown.get('ok')) is bool:
                if not shutdown['ok']:
                    _shutdown_notice('录制租约已结束，但部分采集辅助服务收尾未确认。', error=True)
                    return 3
                # A closed lease alone is not proof that accepted packets were sealed.
                proof = end_proof()
                if proof.get('sessionId') == token and proof.get('state') in ('sealed', 'pending'):
                    if proof['state'] == 'pending':
                        _shutdown_notice('结束标记已保存，源端录制收尾仍待确认；共享相机保留，后台会继续处理。', error=True)
                        return 3
                    _shutdown_notice('已确认录制结束，结束标记已保存；共享相机保留，后台继续生成和补传已保存视频。')
                    if proof.get('captureIncomplete') is True:
                        _shutdown_notice('本次数据已标记为不完整，请保留日志并检查缺失或丢帧信息。', error=True)
                    return 0
                if current.get('sessionEndError'):
                    _shutdown_notice('录制已结束，但结束标记保存失败；本次停止尚未完整确认。', error=True)
                    return 3
            if process_identity(pid) != marker.get('processStart'):
                _shutdown_notice('采集进程已退出但未留下完整结束确认，正在检查本次辅助服务。', error=True)
                stop_remaining_cameras()
                return 3
            if time.monotonic() >= deadline:
                _shutdown_notice('结束确认等待超时；现有收尾继续运行，未强制终止写盘进程。请稍后再次运行结束采集脚本。', error=True)
                return 3
            if time.monotonic() >= notice:
                _shutdown_notice('仍在等待采集收尾确认，请保留此窗口。')
                notice = time.monotonic() + 15
            time.sleep(.25)


def datetime_iso(timestamp):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def status_timestamp(value):
    from datetime import datetime
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return parsed.timestamp() if parsed.tzinfo else 0
    except (AttributeError, ValueError, TypeError):
        return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='只订阅并录制已有相机、雷达数据；默认不启动、重配或关闭设备，设备由原有入口管理。')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--stop', action='store_true', help='结束本工具录制，共享相机、雷达和算法继续运行')
    mode.add_argument('--status', action='store_true', help='只读查看采集状态')
    parser.add_argument('--dry-run', action='store_true', help='只列出动作，不更改开关或服务')
    parser.add_argument('--pause', action='store_true', help=argparse.SUPPRESS)
    sources = parser.add_mutually_exclusive_group()
    sources.add_argument('--no-device-start', action='store_true', help='兼容选项：与默认相同，仅录制已有数据源，不启停相机')
    sources.add_argument('--start-cameras', action='store_true', help='显式开启或复用相机及预览；雷达仍只读检查')
    parser.add_argument('--with-camera-yolo', action='store_true', help='与 --start-cameras 同用，显式开启额外的相机识别服务')
    parser.add_argument('--duration', type=float, help='定时采集秒数；非交互运行时必须指定')
    parser.add_argument('--ready-timeout', type=float, default=25, help='收尾等待上限秒数，1–120')
    args = parser.parse_args(argv)
    import math
    if args.with_camera_yolo and not args.start_cameras:
        parser.error('--with-camera-yolo 需要显式指定 --start-cameras')
    if args.status and args.dry_run:
        parser.error('--status 与 --dry-run 不能同时使用')
    if not math.isfinite(args.ready_timeout) or not 1 <= args.ready_timeout <= 120:
        parser.error('--ready-timeout 需要 1–120 秒')
    if args.duration is not None and (not math.isfinite(args.duration) or not 0 < args.duration <= 86400):
        parser.error('--duration 需要 0–86400 秒（不含0）')
    try:
        config = read_environment(Path.home() / '.config/robot-cloud/archive.env')
    except (OSError, ValueError, CaptureError):
        print('采集未完成：归档环境文件无法读取或格式无效', file=sys.stderr)
        return 2
    control = os.environ.get('ARCHIVE_CONTROL_DIR', config.get('ARCHIVE_CONTROL_DIR',
        str(Path.home() / 'DBProg/RobotHygienism/.runtime/garage-archive-control')))
    session = CaptureSession(control)
    device_values = dict(config)
    device_values.update({key: value for key, value in os.environ.items() if key.startswith('ARCHIVE_')})
    device_values['ARCHIVE_ENV_FILE'] = str(Path.home() / '.config/robot-cloud/archive.env')
    if args.with_camera_yolo:
        device_values['ARCHIVE_START_CAMERA_YOLO'] = 'true'
    if args.dry_run:
        if args.stop:
            print('停止预演：结束本工具录制，保留共享相机、雷达和算法；本次不执行操作。')
            return 0
        print('预演：' + ('开启或复用彩色/深度与 USB 相机；本机雷达仍只读检查。' if args.start_cameras else
              '仅订阅已有相机、雷达数据；不启动、重配或关闭设备。'))
        print('雷达及定位、导航、建图由原有入口管理；采集不启动雷达驱动。')
        if args.start_cameras:
            print('相机开启后恢复低负载彩色/深度网页预览；默认不额外启动识别服务，已有算法保持运行。')
        print('随后开启录制，按 Enter/Ctrl+C 或到时收尾；共享相机、雷达和导航继续运行。')
        print('归档辅助服务：' + ' '.join(RECORDER_UNITS))
        return 0
    if args.status:
        report = session.read(session.control / 'status.json')
        source = session.read(session.control / 'source.json')
        marker = session.read(session.marker)
        print(json.dumps(dict(enabled=GarageGate(session.web_gate).enabled(),
            session=marker, updatedAt=report.get('updatedAt'),
            store=report.get('store'), upload=report.get('upload'),
            lidarDevices=session.lidar_diagnostics(report, marker),
            recordingDds=marker.get('recordingDds'),
            source={key: source.get(key) for key in ('updatedAt', 'connected', 'gateEnabled',
                                                     'gateAcknowledged', 'error', 'capture')}),
            ensure_ascii=False, indent=2))
        return 0
    if args.stop:
        try:
            return session.stop(device_values)
        except OSError:
            print('无法通知本次采集会话结束；未操作任何设备。', file=sys.stderr)
            return 2
    try:
        from .robot_profile import validate_current_profile
        validate_current_profile(values=device_values)
    except (OSError, ValueError, KeyError, TypeError):
        print('机器人身份配置核对失败；未开启采集，请运行机器人接入检查。', file=sys.stderr)
        return 2
    terminal = CaptureTerminal(sys.stdin)
    if not terminal.interactive and args.duration is None:
        parser.error('非交互运行请指定 --duration 秒数，避免采集开关无人收尾')
    stopping = False
    def stop_requested(*_):
        nonlocal stopping
        stopping = True
    def cancelled():
        nonlocal stopping
        if not stopping and terminal.disconnected():
            stopping = True
            _shutdown_notice('采集终端连接已断开，正在结束录制；共享相机保持运行。')
        return stopping
    for signum in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, stop_requested)
    completion_confirmed = True
    sealed_confirmed = False
    try:
        with session.lock():
            claimed = False
            camera_cleanup_required = False
            try:
                # Fail early on an existing gate before starting even a helper.
                if GarageGate(session.web_gate).enabled():
                    raise CaptureError('已有采集正在运行，本次没有更改它；请在原入口结束')
                camera_cleanup_required = args.start_cameras
                print('正在准备归档辅助服务；默认只订阅已有相机、雷达数据，不启动设备或算法。', flush=True)
                session.ensure_recorders()
                if cancelled():
                    print('采集启动已取消，未开启采集租约；保留共享相机和已有算法。')
                    raise _CaptureStartCancelled()
                print(f'正在等待归档进程就绪，最多 {args.ready_timeout:g} 秒。', flush=True)
                session.wait_ready(args.ready_timeout)
                if cancelled():
                    print('采集启动已取消，未开启采集租约；保留共享相机和已有算法。')
                    raise _CaptureStartCancelled()
                try:
                    if args.start_cameras:
                        session.ensure_devices(device_values, cancelled=cancelled)
                    else:
                        session.ensure_passive_sources(device_values, cancelled=cancelled)
                except DeviceStartupCancelled:
                    print('数据源检查已取消，未开启录制；保留共享相机和雷达。', flush=True)
                    raise _CaptureStartCancelled()
                if cancelled():
                    print('采集启动已取消，未开启录制；保留共享相机和雷达。', flush=True)
                    raise _CaptureStartCancelled()
                session.claim()
                claimed = True
                session.start_radar_observer(device_values)
                deadline = time.monotonic() + args.duration if args.duration else None
                print('已有数据源检查已完成，录制会话已开启；正在检查传感器数据是否真正写盘。', flush=True)
                print('雷达未开启不阻止录制；同一 DDS 图后续收到的数据会自动记录，未收到则明确提示缺失。', flush=True)
                print('源端相机未运行或尚未同步时不会生成相机文件；请用 --status 查看实际状态。', flush=True)
                print('尚未确认本次视频和雷达写盘；会持续检查，缺少任一类数据会明确报告不完整。', flush=True)
                print('按 Enter / Ctrl+C 收尾本次录制；共享相机、雷达、导航和建图继续运行。', flush=True)
                while not cancelled() and (deadline is None or time.monotonic() < deadline):
                    if not session.renew():
                        print('采集开关已被其他入口修改，本次会话结束并保留其选择。')
                        break
                    session.refresh_radar_observation()
                    session.observe_sources()
                    session.show_progress()
                    if cancelled():
                        break
                    delay = min(3, max(0, deadline-time.monotonic())) if deadline else 3
                    if terminal.interactive:
                        try:
                            ready, _, _ = select.select([sys.stdin], [], [], delay)
                            if ready:
                                sys.stdin.readline()
                                break
                        except (OSError, ValueError):
                            if cancelled():
                                break
                            raise
                    else:
                        time.sleep(delay)
            finally:
                try:
                    if claimed:
                        _shutdown_notice(f'正在结束本次采集并收尾，最多等待 {args.ready_timeout:g} 秒；请保持窗口打开。')
                        # Close recording before any final diagnostic or
                        # helper cleanup can wait; accepted packets still drain.
                        try:
                            session.release()
                        finally:
                            session.stop_radar_observer()
                        session.observe_sources()
                        if GarageGate(session.web_gate).enabled():
                            print('本次采集租约已结束，保留其他入口开关及共享相机。')
                        elif session.wait_sealed(args.ready_timeout):
                            sealed_confirmed = True
                            print('采集已结束，写入队列和活动片段已收尾；后台继续整理已保存文件。')
                        else:
                            print('采集租约已关闭；本机或源端收尾尚未确认，后台会继续处理，文件保留。', file=sys.stderr)
                            completion_confirmed = False
                        missing = session.missing_data()
                        if missing:
                            print('本次采集不完整：未确认' + '、'.join(missing)
                                  + '数据已写盘；请检查上方数据源检查结果、设备连接和数据状态。',
                                  file=sys.stderr)
                            completion_confirmed = False
                        if (args.start_cameras and session.device_startup is not None
                                and not session.device_startup['ok']):
                            print('本次有设备未全部就绪，已保存可用数据；未就绪设备详见运行日志和 --status。',
                                  file=sys.stderr)
                            completion_confirmed = False
                        session.record_result()
                finally:
                    # Closing the lease or collecting final diagnostics can fail.
                    # Only this capture's auxiliaries may be released; shared
                    # sources remain available even after partial startup.
                    if camera_cleanup_required and not session.close_cameras(device_values):
                        completion_confirmed = False
                    elif not args.start_cameras:
                        session.camera_shutdown = dict(ok=True, skipped=True, reason='passive_sources')
                        marker = session.read(session.marker)
                        if marker.get('token') == session.token:
                            atomic_json(session.marker, {**marker, 'cameraShutdown': session.camera_shutdown})
                    if claimed:
                        try:
                            proof = session.persist_session_end(device_values, sealed=sealed_confirmed,
                                incomplete=not completion_confirmed)
                            if isinstance(proof, dict) and proof.get('captureIncomplete') is True:
                                completion_confirmed = False
                                _shutdown_notice('本次已保存可用数据，但检测到丢帧或采集完整性无法确认，'
                                    '结果标记为未完成；请检查采集日志。', error=True)
                        except (OSError, ValueError, TypeError) as error:
                            completion_confirmed = False
                            marker = session.read(session.marker)
                            if marker.get('token') == session.token:
                                try:
                                    atomic_json(session.marker, {**marker,
                                        'sessionEndError': type(error).__name__})
                                except OSError:
                                    pass
                            _shutdown_notice('采集结束证明尚未持久保存：'+type(error).__name__, error=True)
        return 0 if completion_confirmed else 3
    except _CaptureStartCancelled:
        return 0 if completion_confirmed else 3
    except (CaptureError, DeviceStartupError, OSError, subprocess.SubprocessError) as error:
        print('采集未完成：' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
