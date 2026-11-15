"""Collector truth, bounds, isolation and real HTTP recovery tests (test-only data)."""
from __future__ import annotations

import ast
import asyncio
import base64
import json
import math
import time
import uuid
from pathlib import Path
from types import SimpleNamespace as NS

import cv2
import numpy as np
import pytest
from aiohttp import web

from collector.config import Config, HTTP_CAMERA_ORIGIN, HTTP_CAMERA_PROFILES, ingest_url
from collector.legacy.telemetry import TelemetryStore, parse_telemetry
from collector.media import MAX_SCAN_RAYS, MediaStore, grid_png, header_timestamp, jpeg_dimensions, laser_scan_points
from collector.state import CollectorState, topic_specs
from collector.transport import LatestQueue, Uploader, retry_delay
from shared.protocol import CAMERA_KEYS, MAP_KEYS, ProtocolError, validate_envelope


def packet(kind='heartbeat', payload=None, sequence=1):
    return {'schema_version': 1, 'robot_id': 'test-only-robot', 'source_id': 'test-source',
            'session_id': str(uuid.uuid4()), 'sequence': sequence, 'kind': kind,
            'topic': '/observer/observation' if kind == 'observation' else '/observer/heartbeat',
            'message_timestamp': None, 'collector_timestamp': time.time(),
            'payload': {'uptime_s': 0} if payload is None else payload}


def empty_observation():
    return {key: {} for key in ('system', 'robot', 'devices', 'ros2', 'pages', 'errors')}


def telemetry(**values):
    return json.dumps({'telemetryTransportSchema': 1, 'telemetrySourceNode': '/chassis_host', **values})


def header(frame='map'):
    now = time.time()
    return NS(frame_id=frame, stamp=NS(sec=int(now), nanosec=int((now % 1) * 1e9)))


def scan_message(ranges=(1.0, 2.0, 3.0), **overrides):
    values = dict(header=header('isolated_scan_frame'), ranges=list(ranges),
                  angle_min=0.0, angle_increment=math.pi / 2, range_min=0.1, range_max=30.0)
    values.update(overrides)
    return NS(**values)


def test_telemetry_preserves_true_zero_missing_command_and_nonfinite():
    parsed = parse_telemetry(telemetry(busVoltageV=0, actualVxMps=None, targetVxMps=1.2,
                                      floatNan=math.nan, floatInf=math.inf))
    assert parsed['busVoltageV'] == 0
    assert parsed['actualVxMps'] is None
    assert parsed['targetVxMps'] == 1.2
    assert parsed['floatNan'] is None and parsed['floatInf'] is None
    store = TelemetryStore()
    assert store.receive(telemetry(zero=0, absent=None, nan=math.nan))
    snap = store.snapshot()
    assert snap['nonFiniteCount'] == 1
    assert all(page['status'] == 'WAITING_UPSTREAM' for page in snap['pages'].values())
    assert all(page['count'] is None for page in snap['pages'].values())


def test_pages_do_not_infer_missing_motion_and_stale():
    now = [0.0]
    store = TelemetryStore(lambda: now[0])
    store.set_graph(1, ['std_msgs/msg/String'])
    assert store.receive(telemetry(serialDebug={'telemetryPages': {'0x00': {'count': 10, 'ageMs': 0}, '0x10': {'count': 0}}}))
    assert store.snapshot()['pages']['0x10']['status'] == 'WAITING_UPSTREAM'
    now[0] = 3.0
    assert store.snapshot()['status'] == 'STALE'
    assert store.snapshot()['pages']['0x00']['status'] == 'STALE'
    store.set_graph(1, ['geometry_msgs/msg/Twist'])
    assert store.snapshot()['status'] == 'ERROR'
    assert store.snapshot()['pages']['0x00']['status'] == 'ERROR'


def test_page_from_previous_serial_session_is_not_current_when_inactive():
    store = TelemetryStore(lambda: 10.0)
    assert store.receive(telemetry(serialDebug={'telemetryPages': {
        '0x00': {'count': 10, 'ageMs': 0, 'active': False},
        '0x10': {'count': 0, 'ageMs': 0, 'active': False},
        '0x11': {'count': 12, 'ageMs': 0, 'active': True},
        '0x12': {'count': 7, 'ageMs': 0},
    }}))
    snapshot = store.snapshot()
    assert snapshot['status'] == 'AVAILABLE'
    assert snapshot['pages']['0x00']['status'] == 'STALE'
    assert snapshot['pages']['0x00']['count'] == 10
    assert snapshot['pages']['0x10']['status'] == 'WAITING_UPSTREAM'
    assert snapshot['pages']['0x11']['status'] == 'AVAILABLE'
    assert snapshot['pages']['0x12']['status'] == 'AVAILABLE'


@pytest.mark.parametrize('bad', [True, '1', None])
def test_bad_telemetry_schema_rejected(bad):
    from collector.legacy.telemetry import TelemetryParseError
    with pytest.raises(TelemetryParseError):
        parse_telemetry(json.dumps({'telemetryTransportSchema': bad, 'telemetrySourceNode': '/chassis_host'}))


def test_latest_queue_coalesces_and_enforces_keys_bytes():
    queue = LatestQueue(max_keys=2, max_bytes=4096)
    queue.put('first', packet())
    queue.put('first', packet(payload={'uptime_s': 5}))
    assert queue.stats()['keys'] == 1
    assert queue.peek().envelope['payload']['uptime_s'] == 5
    queue.put('second', packet())
    queue.put('third', packet())
    assert queue.stats()['keys'] == 2 and queue.stats()['dropped'] == 1
    old = queue.peek()
    queue.put(old.key, packet(payload={'uptime_s': 7}))
    queue.remove(old)
    assert queue.stats()['keys'] == 2  # In-flight ack cannot remove the newer replacement.
    with pytest.raises(ValueError):
        LatestQueue(max_bytes=50).put('too-big', packet())


def test_latest_queue_expires_old_stream_during_outage():
    now = [0.0]
    queue = LatestQueue(clock=lambda: now[0])
    queue.put('heartbeat', packet())
    now[0] = 11
    assert queue.peek() is None and queue.stats()['bytes'] == 0


@pytest.mark.parametrize('role,keys,capacity,profile', [
    ('primary', Config.media_keys, 13, None),
    ('cameras', ('fisheye', 'color', 'depth', 'depthPoints'), 5, None),
    ('primary', ('bluesea', 'scan'), 4, None),
    ('cameras', ('fisheye',), 2, None),
    ('primary', Config.media_keys + HTTP_CAMERA_PROFILES['legacy3'], 16, 'legacy3'),
    ('primary', Config.media_keys + HTTP_CAMERA_PROFILES['jetson-hub4'], 17, 'jetson-hub4'),
    ('primary', Config.media_keys + HTTP_CAMERA_PROFILES['jetson-direct3'], 16, 'jetson-direct3'),
    ('primary', ('usbHub2', 'usbHub3'), 4, 'jetson-hub4'),
])
def test_uploader_capacity_holds_all_role_packets_through_repeated_sync(role, keys, capacity, profile):
    config = Config('test-only-robot', 'test-source', role=role, media_keys=keys,
                    cloud_url='https://isolated.invalid/api/v1/ingest', token='isolated-test-token-' + 'x' * 32,
                    http_camera_url=HTTP_CAMERA_ORIGIN if profile else '', http_camera_profile=profile or 'legacy3')
    uploader = Uploader(config)  # Never started: these packets remain entirely in test memory.
    assert uploader.queue.max_keys == capacity
    assert uploader.queue.max_bytes == 4 * 1024 * 1024
    expected = {'heartbeat'} | {'media:' + key for key in keys}
    if role == 'primary':
        expected.add('observation')
    for cycle in range(3):
        uploader.enqueue('heartbeat', '/observer/heartbeat', {'uptime_s': cycle})
        if role == 'primary':
            uploader.enqueue('observation', '/observer/observation', empty_observation())
        for key in keys:
            kind = 'image' if key in CAMERA_KEYS else 'map' if key in MAP_KEYS else 'cloud'
            uploader.enqueue('media', '/isolated/queue/' + key,
                             {'key': key, 'kind': kind, 'status': 'WAITING_UPSTREAM'})
        assert set(uploader.queue.entries) == expected
        assert uploader.queue.stats()['keys'] == capacity
        assert uploader.queue.stats()['dropped'] == 0
        assert uploader.queue.entries['heartbeat'].envelope['payload']['uptime_s'] == cycle
    if role == 'cameras':
        with pytest.raises(ValueError, match='primary observation'):
            uploader.enqueue('observation', '/observer/observation', empty_observation())
        with pytest.raises(ValueError, match='not enabled'):
            uploader.enqueue('media', '/bluesea_new/scan',
                             {'key': 'bluesea', 'kind': 'cloud', 'status': 'WAITING_UPSTREAM'})


def test_uploader_preserves_explicit_bounded_queue():
    queue = LatestQueue(max_keys=1, max_bytes=4096)
    uploader = Uploader(Config('test-only-robot', 'test-source',
                               cloud_url='https://isolated.invalid/api/v1/ingest', token='x' * 32), queue=queue)
    assert uploader.queue is queue
    uploader.enqueue('heartbeat', '/observer/heartbeat', {'uptime_s': 0})
    uploader.enqueue('observation', '/observer/observation', empty_observation())
    assert queue.stats()['keys'] == 1 and queue.stats()['dropped'] == 1
    assert queue.max_bytes == 4096


@pytest.mark.parametrize('url', ['http://example.com', 'https://u:password@example.com', 'https://example.com?secret=1', 'ftp://example.com', 'http://192.168.1.100'])
def test_transport_refuses_insecure_remote_and_embedded_credentials(url):
    with pytest.raises(ValueError):
        ingest_url(url, allow_insecure_local=True)


def test_transport_explicit_loopback_only_http_and_backoff():
    assert ingest_url('http://127.0.0.1:8080', True).endswith('/api/v1/ingest')
    assert ingest_url('https://example.com').endswith('/api/v1/ingest')
    with pytest.raises(ValueError):
        ingest_url('http://127.0.0.1:8080')
    assert 0.375 <= retry_delay(0, 0) <= 0.5
    assert retry_delay(100, 1) == 3


def test_cameras_role_cannot_sample_system_or_upload_main_observation():
    class ForbiddenCollector:
        def collect(self):
            pytest.fail('Camera role cannot sample system')
    config = Config('test-only-robot', 'test-camera', 'cameras', media_keys=('fisheye', 'color', 'depth', 'depthPoints'))
    state = CollectorState(config, collector=ForbiddenCollector())
    assert state.collector is None and state.telemetry is None and state.devices is None
    assert set(state.specs) == {'fisheye', 'color', 'depth', 'depthPoints'}
    with pytest.raises(ValueError):
        state.observation()


def test_new_registered_bluesea_and_old_scan_topics(monkeypatch):
    specs = topic_specs(Config('test-only-robot', 'test-rk'))
    assert specs['registered'][1] == '/cloud_registered_body_xyzi'
    assert specs['bluesea'][1] == '/bluesea_new/scan'
    assert specs['scan'][1] == '/scan'
    monkeypatch.setenv('REGISTERED_TOPIC', '/cloud_registered')
    assert topic_specs(Config('test-only-robot', 'test-rk'))['registered'][1] == '/cloud_registered'
    monkeypatch.setenv('COLLECTOR_TOPIC_BLUESEA', '/isolated/blue/scan')
    monkeypatch.setenv('COLLECTOR_TOPIC_SCAN', '/isolated/other/scan')
    overridden = topic_specs(Config('test-only-robot', 'test-rk'))
    assert overridden['bluesea'][1:3] == ('/isolated/blue/scan', 'sensor_msgs/msg/LaserScan')
    assert overridden['scan'][0:3] == ('二维雷达扫描', '/isolated/other/scan', 'sensor_msgs/msg/LaserScan')


def test_primary_scan_defaults_and_camera_allowlist_unchanged(monkeypatch):
    assert len(Config.media_keys) == 11
    assert {'bluesea', 'scan'} <= set(Config.media_keys)
    monkeypatch.setenv('COLLECTOR_ROLE', 'cameras')
    monkeypatch.setenv('COLLECTOR_CHASSIS_SOURCE', 'ros2')
    monkeypatch.delenv('COLLECTOR_MEDIA_KEYS', raising=False)
    camera = Config.from_env(audit=True)
    assert set(camera.media_keys) == {'fisheye', 'color', 'depth', 'depthPoints'}
    assert set(topic_specs(camera)) == set(camera.media_keys)
    for key in ('bluesea', 'scan'):
        monkeypatch.setenv('COLLECTOR_MEDIA_KEYS', key)
        with pytest.raises(ValueError, match='unauthorized key'):
            Config.from_env(audit=True)


def test_scan_projection_preserves_real_angles_distances_and_frame():
    message = scan_message()
    assert np.allclose(laser_scan_points(message, 4000), [[1, 0, 0], [0, 2, 0], [-3, 0, 0]])
    clockwise = scan_message(angle_min=math.pi / 2, angle_increment=-math.pi / 2)
    assert np.allclose(laser_scan_points(clockwise, 4000), [[0, 1, 0], [2, 0, 0], [0, -3, 0]])


def test_scan_filters_invalid_distances_before_bounded_decimation():
    message = scan_message([0, -1, math.nan, math.inf, -math.inf, 0.09, 30.01, 0.1, 30.0],
                           angle_increment=0.1)
    points = np.asarray(laser_scan_points(message, 4000))
    assert points.shape == (2, 3)
    assert np.allclose(np.linalg.norm(points, axis=1), [0.1, 30.0])
    assert np.allclose(points[:, :2], [[0.1 * math.cos(.7), 0.1 * math.sin(.7)],
                                     [30 * math.cos(.8), 30 * math.sin(.8)]])
    assert laser_scan_points(scan_message([0, math.nan], range_min=0), 1) == []
    sparse = [math.inf] * 9999
    sparse[-1] = 2.5
    points = laser_scan_points(scan_message(sparse, angle_increment=.0001), 1)
    assert len(points) == 1 and math.isclose(math.hypot(*points[0][:2]), 2.5)


@pytest.mark.parametrize('limit', [1, 17, 1500, 4000])
def test_scan_projection_point_and_ray_bounds(limit):
    message = scan_message(np.ones(MAX_SCAN_RAYS), angle_increment=2 * math.pi / MAX_SCAN_RAYS)
    points = laser_scan_points(message, limit)
    assert 0 < len(points) <= limit
    assert all(point[2] == 0 and all(math.isfinite(v) for v in point) for point in points)
    # Verify oversized input is rejected before iteration/conversion/allocation.
    class OversizedRanges:
        def __len__(self):
            return MAX_SCAN_RAYS + 1
        def __iter__(self):
            pytest.fail('Oversized ranges must not be iterated')
    message.ranges = OversizedRanges()
    with pytest.raises(ValueError, match='ray count'):
        laser_scan_points(message, limit)


@pytest.mark.parametrize('limit', [0, -1, 4001, 1.5, True])
def test_scan_rejects_invalid_sample_limit(limit):
    with pytest.raises(ValueError, match='point limit'):
        laser_scan_points(scan_message(), limit)


@pytest.mark.parametrize('invalid', [
    {'angle_min': math.nan}, {'angle_min': math.inf}, {'angle_min': True},
    {'angle_increment': math.inf}, {'angle_increment': 0}, {'angle_increment': 7},
    {'range_min': -1}, {'range_min': math.nan}, {'range_max': math.inf},
    {'range_max': 0.1}, {'range_max': 0}, {'range_max': 1_000_001},
    {'range_min': '0'}, {'ranges': [[1.0, 2.0]]},
])
def test_scan_invalid_metadata_reports_error_without_points(invalid):
    media = MediaStore(Config('test-only-robot', 'test-rk', media_keys=('bluesea',)))
    media.receive('bluesea', scan_message(**invalid), '/bluesea_new/scan')
    snap = media.status('bluesea')
    assert snap['status'] == 'ERROR' and 'points' not in snap
    validate_envelope(dict(packet(), kind='media', topic='/bluesea_new/scan', payload=snap))


@pytest.mark.parametrize('key,topic', [('bluesea', '/bluesea_new/scan'), ('scan', '/scan')])
def test_scan_media_waiting_available_empty_stale_and_recovery(key, topic):
    now = [0.0]
    state = CollectorState(Config('test-only-robot', 'test-rk', media_keys=(key,)),
                           collector=NS(collect=lambda: {}), clock=lambda: now[0])
    initial = state.media.status(key)
    assert initial['status'] == 'WAITING_UPSTREAM' and 'points' not in initial
    message = scan_message()
    state.receive_sensor(key, message)
    snap = state.media.status(key, state.sensor_snapshot()[key])
    assert snap['status'] == 'AVAILABLE' and snap['kind'] == 'cloud'
    assert snap['frameId'] == message.header.frame_id
    assert snap['topic'] == topic and snap['source'] == 'test-rk'
    assert snap['message_timestamp'] == header_timestamp(message)
    assert snap['totalPoints'] == 3 and snap['frameSeq'] == 1
    assert 'Z=0' in snap['note'] and 'TF' in snap['note']
    assert state.sensor_snapshot()[key]['value']['rayCount'] == 3
    queued = []
    uploader = NS(connection_generation=0, enqueue=lambda *args: queued.append(args))
    state.media.publish(uploader, state.sensor_snapshot())
    kind, sent_topic, payload, stamp, received = queued[-1]
    assert kind == 'media' and sent_topic == topic and stamp == snap['message_timestamp']
    validate_envelope(dict(packet(), kind=kind, topic=sent_topic, payload=payload,
                           message_timestamp=stamp, collector_timestamp=received))
    now[0] = 3
    stale = state.media.status(key)
    assert stale['status'] == 'STALE' and 'points' not in stale
    state.media.publish(uploader, state.sensor_snapshot())
    assert queued[-1][2]['status'] == 'STALE' and 'points' not in queued[-1][2]
    state.receive_sensor(key, scan_message([0, math.nan, math.inf, 31.0]))
    empty = state.media.status(key)
    assert empty['status'] == 'NO_DATA' and empty['points'] == [] and empty['totalPoints'] == 4
    validate_envelope(dict(packet(), kind='media', topic=topic, payload=empty,
                           message_timestamp=empty['message_timestamp']))
    now[0] = 4.1
    state.receive_sensor(key, scan_message([]))
    assert state.media.status(key)['status'] == 'NO_DATA'
    now[0] = 5.2
    state.receive_sensor(key, scan_message(angle_increment=math.nan))
    assert state.media.status(key)['status'] == 'ERROR' and 'points' not in state.media.status(key)
    now[0] = 6.3
    state.receive_sensor(key, scan_message([1.0]))
    assert state.media.status(key)['status'] == 'AVAILABLE'
    assert state.media.status(key)['points'] == [[1.0, 0.0, 0.0]]


def test_observation_contract_without_upstream_real_data():
    fake_system = NS(collect=lambda: {'status': 'AVAILABLE', 'cpu': {'value': 0, 'status': 'AVAILABLE'}})
    state = CollectorState(Config('test-only-robot', 'test-rk'), collector=fake_system)
    payload = state.observation()
    validate_envelope(packet('observation', payload))
    assert payload['system']['cpu']['value'] == 0
    assert payload['robot']['data'] is None
    assert payload['robot']['status'] == 'WAITING_UPSTREAM'
    assert payload['ros2']['status'] == 'WAITING_UPSTREAM'
    assert '工控机' in payload['system']['source']


def test_real_sample_stamp_zero_and_simulation_unknown():
    assert header_timestamp(NS(header=NS(stamp=NS(sec=0, nanosec=0)))) is None
    assert header_timestamp(NS(header=NS(stamp=NS(sec=20, nanosec=0)))) is None
    assert abs(header_timestamp(NS(header=header())) - time.time()) < 1


def test_image_jpeg_and_stale_do_not_return_old_frame():
    now = [0.0]
    config = Config('test-only-robot', 'test-rk', media_keys=('fisheye',))
    media = MediaStore(config, lambda: now[0])
    message = NS(width=4, height=2, step=12, encoding='rgb8', is_bigendian=False,
                 header=header('camera'), data=bytes([0] * 24))
    media.receive('fisheye', message, '/fisheye_front/image_raw')
    snap = media.status('fisheye')
    jpeg = base64.b64decode(snap['data'])
    assert jpeg_dimensions(jpeg) == (4, 2)
    assert snap['frameId'] == 'camera' and snap['status'] == 'AVAILABLE'
    validate_envelope(dict(packet(), kind='media', topic='/fisheye_front/image_raw', payload=snap,
                           message_timestamp=snap['message_timestamp']))
    now[0] = 3
    stale = media.status('fisheye')
    assert stale['status'] == 'STALE' and 'data' not in stale


def test_compressed_bridge_resizes_to_cloud_preview_pixel_bounds():
    config = Config('test-only-robot', 'test-rk', media_keys=('fisheye',))
    media = MediaStore(config)
    # Explicitly test-only image: an oversized valid compressed upstream frame.
    image = np.zeros((1200, 2048, 3), dtype=np.uint8)
    ok, encoded = cv2.imencode('.jpg', image)
    assert ok
    message = NS(format='jpeg', header=header('camera'), data=encoded.tobytes())
    media.receive('fisheye', message, '/observer_camera/fisheye/compressed', compressed=True)
    snap = media.status('fisheye')
    assert snap['status'] == 'AVAILABLE'
    assert snap['width'] == 2048 and snap['height'] == 1200
    assert snap['previewWidth'] <= 960 and snap['previewHeight'] <= 720
    assert jpeg_dimensions(base64.b64decode(snap['data'])) == (snap['previewWidth'], snap['previewHeight'])
    validate_envelope(dict(packet(), kind='media', topic='/observer_camera/fisheye/compressed', payload=snap,
                           message_timestamp=snap['message_timestamp']))


@pytest.mark.parametrize('fps,input_fps,expected', [(2,5,20),(5,5,50)])
def test_image_rate_gate_preserves_cadence_without_drift_or_catchup(fps,input_fps,expected):
    now = [100.0]
    media = MediaStore(Config('test-only-robot','test-rk',image_fps=fps,
                              media_keys=('color',)), lambda:now[0])
    message = NS(width=4,height=2,step=12,encoding='rgb8',is_bigendian=False,
                 header=header('camera'),data=bytes(24))
    for index in range(50):
        now[0] = 100 + index / input_fps - (0.0000001 if index % 2 else 0)
        media.receive('color',message,'/berxel/color/image_raw')
    assert media.frame_sequences['color'] == expected
    now[0] = 200
    media.receive('color',message,'/berxel/color/image_raw')
    count = media.frame_sequences['color']
    for _ in range(10):
        media.receive('color',message,'/berxel/color/image_raw')
    assert media.frame_sequences['color'] == count == expected + 1


def test_invalid_compressed_bridge_reports_error_before_enqueue():
    config = Config('test-only-robot', 'test-rk', media_keys=('fisheye',))
    media = MediaStore(config)
    message = NS(format='jpeg', header=header('camera'), data=b'\xff\xd8\x00\x00\xff\xd9')
    media.receive('fisheye', message, '/observer_camera/fisheye/compressed', compressed=True)
    snap = media.status('fisheye')
    assert snap['status'] == 'ERROR' and 'data' not in snap


def test_map_origin_hash_dedup_and_resend_after_reconnect():
    now = [0.0]
    config = Config('test-only-robot', 'test-rk', media_keys=('map',))
    media = MediaStore(config, lambda: now[0])
    origin = NS(position=NS(x=3.0, y=-2.0, z=0.0), orientation=NS(x=0.0, y=0.0, z=0.0, w=1.0))
    message = NS(header=header('map'), info=NS(width=2, height=2, resolution=0.05, origin=origin), data=[0, 100, -1, 0])
    media.receive('map', message, '/map')
    assert media.status('map')['origin']['position']['x'] == 3.0
    sent = []
    uploader = NS(connection_generation=0, enqueue=lambda *args: sent.append(args))
    sensors = {'map': {'status': 'AVAILABLE', 'topic': '/map', 'hz': None}}
    media.publish(uploader, sensors)
    assert len(sent) == 1
    validate_envelope(dict(packet(), kind='media', topic='/map', payload=sent[-1][2],
                           message_timestamp=sent[-1][2]['message_timestamp']))
    now[0] = 1
    media.receive('map', message, '/map')
    media.publish(uploader, sensors)
    assert len(sent) == 1
    uploader.connection_generation = 1
    media.publish(uploader, sensors)
    assert len(sent) == 2
    assert sent[-1][2]['content_hash'] == sent[0][2]['content_hash']
    now[0] = 32
    media.publish(uploader, sensors)
    assert len(sent) == 3  # Periodic full sync covers cloud restarts that retain an HTTP connection.


def test_cloud_sampling_uses_real_xyz_and_filters_nonfinite():
    from collector.legacy.point_codec import cloud_points
    points = np.array([[0, 0, 0], [1, 2, 3], [math.nan, 1, 2]], dtype='<f4')
    message = NS(fields=[NS(name=key, datatype=7, offset=i * 4) for i, key in enumerate(('x', 'y', 'z'))],
                 width=3, height=1, row_step=36, point_step=12, is_bigendian=False, data=points.tobytes())
    assert cloud_points(message, 4000) == [[0.0, 0.0, 0.0], [1.0, 2.0, 3.0]]
    assert len(cloud_points(message, 1)) <= 1


def test_collector_ast_has_no_robot_control_or_production_dependency():
    root = Path(__file__).parents[1] / 'collector'
    forbidden_calls = {'create_publisher', 'create_client', 'ActionClient', 'Serial'}
    forbidden_modules = {'serial', 'rk3588_observer', 'local_chassis', 'web_server'}
    for path in root.rglob('*.py'):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id if isinstance(node.func, ast.Name) else ''
                assert name not in forbidden_calls, path
            if isinstance(node, ast.Import):
                assert not any(part.name.split('.')[0] in forbidden_modules for part in node.names), path
            if isinstance(node, ast.ImportFrom):
                local_receiver = (path == root / 'state.py' and node.level == 1 and
                                  node.module == 'local_chassis' and
                                  {part.name for part in node.names} == {'LocalChassis', 'SOURCE_DESCRIPTION'})
                assert local_receiver or (node.module or '').split('.')[0] not in forbidden_modules, path
        text = path.read_text()
        assert '/dev/tty' not in text
        if path == root / 'robot_shutdown_motion.py':
            # The explicit shutdown tool reads existing actual feedback; it
            # never takes over the chassis. Keep this exception bounded to
            # the two fixed snapshot endpoints and an explicit GET request.
            endpoints = next(node.value for node in tree.body
                             if isinstance(node, ast.Assign)
                             and any(isinstance(target, ast.Name) and target.id == 'LOCAL_SNAPSHOTS'
                                     for target in node.targets))
            assert ast.literal_eval(endpoints) == (
                'http://127.0.0.1:8766/snapshot.json',
                'http://127.0.0.1:8765/snapshot.json',
            )
            requests = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute) and node.func.attr == 'Request']
            assert len(requests) == 1
            assert any(keyword.arg == 'method' and isinstance(keyword.value, ast.Constant)
                       and keyword.value.value == 'GET' for keyword in requests[0].keywords)
        elif path != root / 'local_chassis.py':
            assert '127.0.0.1:8765' not in text
    main = (root / 'main.py').read_text()
    assert 'enable_rosout=False' in main and 'start_parameter_services=False' in main


async def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError('Timed out waiting for test HTTP recovery')
        await asyncio.sleep(0.01)


async def run_server(handler, port=0):
    app = web.Application()
    app.router.add_post('/api/v1/ingest', handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', port)
    await site.start()
    return runner, site, site._server.sockets[0].getsockname()[1]


def test_http_503_then_ack_does_not_consume_first_sequence():
    async def scenario():
        received = []
        async def handler(request):
            assert request.headers['Authorization'] == 'Bearer ' + 'test-token-' + 'x' * 32
            received.append(await request.json())
            return web.json_response({'accepted': True}, status=503 if len(received) == 1 else 200)
        runner, _, port = await run_server(handler)
        uploader = Uploader(Config('test-only-robot', 'test-source', cloud_url=f'http://127.0.0.1:{port}/api/v1/ingest', token='test-token-' + 'x' * 32, allow_insecure_local=True))
        uploader.enqueue('heartbeat', '/observer/heartbeat', {'uptime_s': 0})
        task = asyncio.create_task(uploader.run())
        try:
            await wait_until(lambda: uploader.sent == 1)
            assert [entry['sequence'] for entry in received] == [1, 1]
            assert uploader.sequence == 1
        finally:
            uploader.stop_event.set()
            await task
            await runner.cleanup()
    asyncio.run(scenario())


def test_http_response_lost_duplicate_advances_checkpoint():
    async def scenario():
        received = []
        async def handler(request):
            value = await request.json()
            received.append(value)
            if len(received) == 1:
                request.transport.close()  # Simulate commit succeeded but HTTP ack was lost.
                return web.Response()
            if value['sequence'] == 1:
                return web.json_response({'reason': 'duplicate', 'expected_sequence': 2}, status=409)
            return web.json_response({'accepted': True})
        runner, _, port = await run_server(handler)
        uploader = Uploader(Config('test-only-robot', 'test-source', cloud_url=f'http://127.0.0.1:{port}/api/v1/ingest', token='x' * 32, allow_insecure_local=True))
        uploader.enqueue('heartbeat', '/observer/heartbeat', {'uptime_s': 0})
        uploader.enqueue('observation', '/observer/observation', empty_observation())
        task = asyncio.create_task(uploader.run())
        try:
            await wait_until(lambda: uploader.sequence == 3)
            # The retry scheduler may choose observation after the lost
            # heartbeat receipt. A duplicate commits only the sequence: both
            # unconfirmed payloads are retained and get their own later ACK.
            assert [entry['sequence'] for entry in received] == [1, 1, 2, 3]
            assert [entry['kind'] for entry in received] == ['heartbeat', 'observation', 'heartbeat', 'observation']
            assert len({entry['session_id'] for entry in received}) == 1
        finally:
            uploader.stop_event.set()
            await task
            await runner.cleanup()
    asyncio.run(scenario())


def test_cloud_stops_restarts_with_empty_state_and_collector_recovers():
    async def scenario():
        initial = []
        async def original(request):
            initial.append(await request.json())
            return web.json_response({'accepted': True})
        runner, _, port = await run_server(original)
        uploader = Uploader(Config('test-only-robot', 'test-source', cloud_url=f'http://127.0.0.1:{port}/api/v1/ingest', token='x' * 32, allow_insecure_local=True))
        uploader.enqueue('heartbeat', '/observer/heartbeat', {'uptime_s': 0})
        task = asyncio.create_task(uploader.run())
        replacement = None
        try:
            await wait_until(lambda: uploader.sequence == 1)
            old_session = uploader.session_id
            await runner.cleanup()
            uploader.enqueue('observation', '/observer/observation', empty_observation())
            await wait_until(lambda: not uploader.connected)
            received = []
            async def restored(request):
                value = await request.json()
                received.append(value)
                if value['sequence'] != 1:
                    return web.json_response({'reason': 'new_session_sequence', 'expected_sequence': 1}, status=409)
                return web.json_response({'accepted': True})
            replacement, _, _ = await run_server(restored, port)
            await wait_until(lambda: uploader.sent == 2)
            assert uploader.sequence == 1 and uploader.session_id != old_session
            assert [entry['sequence'] for entry in received] == [2, 1]
            assert uploader.connection_generation >= 3
        finally:
            uploader.stop_event.set()
            await task
            if replacement:
                await replacement.cleanup()
            await runner.cleanup()
    asyncio.run(scenario())


def test_http_redirect_never_forwards_credentials():
    async def scenario():
        calls = []
        async def handler(request):
            calls.append(request.path)
            return web.Response(status=302, headers={'Location': '/should-never-follow'})
        runner, _, port = await run_server(handler)
        uploader = Uploader(Config('test-only-robot', 'test-source', cloud_url=f'http://127.0.0.1:{port}/api/v1/ingest', token='x' * 32, allow_insecure_local=True))
        uploader.enqueue('heartbeat', '/observer/heartbeat', {'uptime_s': 0})
        task = asyncio.create_task(uploader.run())
        try:
            await wait_until(lambda: uploader.queue.stats()['keys'] == 0)
            assert calls == ['/api/v1/ingest'] and uploader.sent == 0
        finally:
            uploader.stop_event.set()
            await task
            await runner.cleanup()
    asyncio.run(scenario())


@pytest.mark.parametrize('status', [401, 403])
def test_device_auth_permanent_error_pauses_without_reconnect_storm(status):
    async def scenario():
        received = []
        async def handler(request):
            received.append(await request.json())
            return web.json_response({'error': 'test-only-auth-failure'}, status=status)
        runner, _, port = await run_server(handler)
        uploader = Uploader(Config('test-only-robot', 'test-source', cloud_url=f'http://127.0.0.1:{port}/api/v1/ingest', token='x' * 32, allow_insecure_local=True))
        uploader.enqueue('heartbeat', '/observer/heartbeat', {'uptime_s': 0})
        task = asyncio.create_task(uploader.run())
        try:
            await wait_until(lambda: uploader.permanent_error is not None)
            uploader.enqueue('heartbeat', '/observer/heartbeat', {'uptime_s': 1})
            await asyncio.sleep(0.05)
            assert len(received) == 1 and uploader.sequence == 0 and not uploader.connected
            assert uploader.queue.stats()['keys'] == 1
        finally:
            uploader.stop_event.set()
            await task
            await runner.cleanup()
    asyncio.run(scenario())


def test_http_429_respects_retry_after_and_retains_sequence():
    async def scenario():
        received = []
        async def handler(request):
            received.append((time.monotonic(), await request.json()))
            return web.json_response({'accepted': len(received) > 1}, status=429 if len(received) == 1 else 200,
                                     headers={'Retry-After': '1'})
        runner, _, port = await run_server(handler)
        uploader = Uploader(Config('test-only-robot', 'test-source', cloud_url=f'http://127.0.0.1:{port}/api/v1/ingest', token='x' * 32, allow_insecure_local=True))
        uploader.enqueue('heartbeat', '/observer/heartbeat', {'uptime_s': 0})
        task = asyncio.create_task(uploader.run())
        try:
            await wait_until(lambda: uploader.sent == 1)
            assert [entry[1]['sequence'] for entry in received] == [1, 1]
            assert received[1][0] - received[0][0] >= 0.9
        finally:
            uploader.stop_event.set()
            await task
            await runner.cleanup()
    asyncio.run(scenario())
