"""Capture may begin before radar publishers exist; no real services or ROS."""
import json
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from collector import archive_capture_session as capture
from collector import archive_devices as devices
from collector.archive_core import atomic_json
from collector.archive_ros import ArchiveRosAdapter


def test_camera_session_claims_lease_with_all_radars_inactive_and_keeps_missing_evidence(
        tmp_path, monkeypatch, capsys):
    now, elapsed = [2_000_000_000.0], [0.0]
    service_calls, helper_calls, claimed = [], [], []

    def service_runner(command, **_options):
        service_calls.append(command)
        assert command in [
            ['systemctl', '--user', 'start', unit] for unit in capture.RECORDER_UNITS
        ]
        return SimpleNamespace(returncode=0)

    session = capture.CaptureSession(tmp_path/'control', clock=lambda: now[0], runner=service_runner)

    def report(video_written=0):
        topics = {topic: dict(type=kind, received=0, written=0,
                             state='WAITING_UPSTREAM', ageS=None, error=None)
                  for _, _, _, required in capture.LIDAR_DEVICES for topic, kind in required}
        topics['/berxel/color/image_raw'] = dict(received=video_written, written=video_written,
                                                state='AVAILABLE' if video_written else 'WAITING_UPSTREAM')
        atomic_json(session.control/'status.json', dict(updatedAt=capture.datetime_iso(now[0]),
            store=dict(active={}, errors={}), ros=dict(active=bool(video_written),
                state='LISTENING' if video_written else 'WAITING_UPSTREAM',
                topics=topics, gateError=None, tickError=None, subscriptionCleanupPending=0,
                queueMessages=0, queueBytes=0, inFlightMessages=0)))

    def helper_runner(command, **options):
        helper_calls.append((command, options))
        host = 'ipc' if command[0] == '/usr/bin/python3' else 'jetson'
        rows = ([dict(key=key, state='inactive', message='not running')
                 for key in ('mid360', 'stl19p', 'bluesea')] if host == 'ipc'
                else [dict(key='usb-cameras', state='started')])
        ok = host == 'jetson'
        return subprocess.CompletedProcess(command, 0 if ok else 1,
            json.dumps(dict(role=host, recordingOnly=True, ok=ok, devices=rows)).encode(), b'')

    original_claim = session.claim
    def claim():
        original_claim()
        claimed.append(session.read(session.gate)['enabled'])

    def sleep_with_video(seconds):
        assert session.read(session.gate)['enabled'] is True
        now[0] += seconds
        elapsed[0] += seconds
        report(video_written=2)

    report()
    monkeypatch.setattr(capture, 'CaptureSession', lambda _control: session)
    monkeypatch.setattr(capture.Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(capture, 'read_environment', lambda _path: {})
    monkeypatch.setattr(capture.sys, 'stdin', SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(capture.signal, 'signal', lambda *_args: None)
    monkeypatch.setattr(capture.time, 'monotonic', lambda: elapsed[0])
    monkeypatch.setattr(capture.time, 'sleep', sleep_with_video)
    monkeypatch.setattr(capture, 'start_capture_devices',
        lambda *_args, **_options: pytest.fail('passive capture started camera sources'))
    monkeypatch.setattr(capture, 'inspect_capture_radar',
        lambda values, **options: devices.inspect_capture_radar(values, runner=helper_runner, **options))
    monkeypatch.setattr(capture, 'stop_capture_cameras',
        lambda *_args, **_options: pytest.fail('passive capture stopped camera sources'))
    monkeypatch.setattr(session, 'claim', claim)
    monkeypatch.setattr(session, 'wait_sealed', lambda _seconds: True)
    monkeypatch.setattr(session, 'persist_session_end', lambda *_args, **_options: None)

    # Code 3 means missing data was reported after a real lease, not that
    # startup was refused (code 2), and does not claim nonexistent radar data.
    assert capture.main(['--duration', '.1']) == 3
    assert claimed == [True]
    assert session.video_sources == {'/berxel/color/image_raw'}
    assert session.lidar_sources == set()
    assert session.read(session.gate)['enabled'] is False
    marker = session.read(session.marker)
    assert marker['state'] == 'stopped'
    assert marker['missingData'] == ['雷达']
    assert marker['recordingDds'] == dict(state='unchanged', reason='no_verified_driver_graph')
    assert all(not row['complete'] for row in marker['lidarDevices'])
    assert len(helper_calls) == 1
    command, options = helper_calls[0]
    assert '--status' in command and command[-2:] == ['--recording-only', '--status']
    assert '--read-only-sources' in command
    assert options['timeout'] == devices.LIDAR_STATUS_TIMEOUT == 8
    assert service_calls == [['systemctl', '--user', 'start', unit] for unit in capture.RECORDER_UNITS]
    output = capsys.readouterr()
    assert '录制会话已开启' in output.out
    assert '采集未完成：' not in output.err
    assert '未确认雷达数据已写盘' in output.err


def test_existing_subscriptions_record_camera_then_late_radar_without_restart():
    class Node:
        def __init__(self):
            self.subscriptions, self.destroyed = {}, []

        def create_subscription(self, _kind, topic, callback, _qos, **options):
            assert options == {'raw': True}
            value = SimpleNamespace(callback=callback, topic=topic)
            self.subscriptions[topic] = value
            return value

        def destroy_subscription(self, subscription):
            self.destroyed.append(subscription)

    class Sink:
        def __init__(self):
            self.rows = []
            self.lock = threading.Lock()

        def record(self, *args, **_options):
            with self.lock:
                self.rows.append(args)
            return True

        def tick(self):
            pass

    def written(adapter, topic, count):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if adapter.snapshot()['topics'][topic]['written'] == count:
                return
            time.sleep(.005)
        assert adapter.snapshot()['topics'][topic]['written'] == count

    sink, node = Sink(), Node()
    adapter = ArchiveRosAdapter(sink, lambda: True, sensors=('Berxel彩色', '雷达MID360'))
    adapter.bind(node, lambda name: name, lambda _spec: {})
    try:
        adapter.reconcile_gate()
        subscriptions = dict(node.subscriptions)
        worker = adapter.worker
        assert adapter.snapshot()['state'] == 'WAITING_UPSTREAM'
        node.subscriptions['/berxel/color/image_raw'].callback(b'camera-before-radar')
        written(adapter, '/berxel/color/image_raw', 1)
        state = adapter.snapshot()
        assert state['state'] == 'LISTENING'
        assert state['topics']['/livox/lidar']['state'] == 'WAITING_UPSTREAM'
        assert state['topics']['/livox/lidar']['written'] == 0
        assert state['topics']['/livox/imu']['written'] == 0

        # A late publisher in the same DDS graph uses the subscriptions that
        # already exist; callbacks preserve original CDR without any startup.
        adapter.reconcile_gate()
        node.subscriptions['/livox/lidar'].callback(b'lidar-appears-later')
        node.subscriptions['/livox/imu'].callback(b'imu-appears-later')
        written(adapter, '/livox/lidar', 1)
        written(adapter, '/livox/imu', 1)
        assert node.subscriptions == subscriptions
        assert node.destroyed == []
        assert adapter.worker is worker
        assert [(row[1], row[3]) for row in sink.rows] == [
            ('/berxel/color/image_raw', b'camera-before-radar'),
            ('/livox/lidar', b'lidar-appears-later'),
            ('/livox/imu', b'imu-appears-later'),
        ]
    finally:
        assert adapter.close()
