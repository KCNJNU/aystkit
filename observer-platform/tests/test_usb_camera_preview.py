"""Isolated fixed-device capture tests: mock all video access, loopback HTTP only."""
from __future__ import annotations

import ast
import http.client
import json
import multiprocessing
import os
from pathlib import Path
import socket
import threading
import time
from types import SimpleNamespace as NS

import cv2
import numpy as np
import pytest

from collector import usb_camera_preview as preview
from collector import http_camera_relay as relay


class Output:
    def __init__(self):
        self.packets = []

    def publish(self, packet):
        self.packets.append(packet)


class Capture:
    def __init__(self, key, clock, stop, *, bad=None, failure=0, count=25, delay=0):
        self.key, self.clock, self.stop = key, clock, stop
        self.bad, self.failure, self.count, self.delay = bad or {}, failure, count, delay
        self.props, self.sets, self.opened, self.releases, self.reads = {}, [], [], 0, 0
        self.frame = np.zeros((preview.HEIGHT, preview.WIDTH, 3), dtype=np.uint8)

    def open(self, path, backend):
        self.opened.append((path, backend))
        return self.failure != -1

    def set(self, key, value):
        self.sets.append((key, value))
        self.props[key] = value
        return True

    def get(self, key):
        return self.bad.get(key, self.props.get(key, 0))

    def read(self):
        self.reads += 1
        self.clock[0] += self.delay or 1 / preview.CAMERAS[self.key]['fps']
        if self.failure and self.reads == self.failure:
            return False, None
        if self.reads >= self.count:
            self.stop.set()
        return True, self.frame

    def release(self):
        self.releases += 1


class Codec:
    def __init__(self, capture, *, encode_delay=0, encoded=None):
        self.capture, self.encode_delay, self.encoded = capture, encode_delay, encoded

    def __getattr__(self, key):
        return getattr(cv2, key)

    def VideoCapture(self):
        return self.capture

    def imencode(self, extension, frame, options):
        self.capture.clock[0] += self.encode_delay
        return cv2.imencode(extension, frame, options) if self.encoded is None else self.encoded


def capture_case(key='camera1', **kwargs):
    now, stop, output = [100.0], threading.Event(), Output()
    capture = Capture(key, now, stop, **kwargs)
    return now, stop, output, capture


@pytest.mark.parametrize('key,port,fps', [('camera1', '2.3', 10), ('camera2', '2.4', 10),
                                       ('camera3', '2.1.3', 30), ('camera4', '2.1.2', 30)])
def test_fixed_ports_negotiate_actual_mjpeg_dimensions_and_rate(key, port, fps):
    now, stop, output, capture = capture_case(key)
    preview._capture_session(key, output, stop, Codec(capture), lambda: now[0], lambda _: True)
    assert capture.opened == [('/dev/v4l/by-path/platform-3610000.usb-usb-0:' + port + ':1.0-video-index0', cv2.CAP_V4L2)]
    assert (cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG')) in capture.sets
    assert (cv2.CAP_PROP_BUFFERSIZE, 1) in capture.sets
    assert capture.releases == 1
    frames = [item for item in output.packets if item.get('jpeg')]
    assert frames and frames[0]['frames'] >= 5
    assert all(item['negotiated'] == {'width': 640, 'height': 360, 'fps': fps, 'fourcc': 'MJPG', 'buffer_size': 1} for item in frames)
    assert all(item['observed_at'] < 100 + item['frames'] / fps for item in frames)
    assert all(b['observed_at'] - a['observed_at'] >= .199 for a, b in zip(frames, frames[1:]))
    for item in frames:
        image = cv2.imdecode(np.frombuffer(item['jpeg'], dtype=np.uint8), cv2.IMREAD_COLOR)
        assert image.shape == (360, 640, 3) and len(item['jpeg']) <= 500_000


@pytest.mark.parametrize('bad', [{cv2.CAP_PROP_FRAME_WIDTH: 1920}, {cv2.CAP_PROP_FRAME_HEIGHT: 1080},
                                {cv2.CAP_PROP_FPS: 25}, {cv2.CAP_PROP_FPS: float('nan')},
                                {cv2.CAP_PROP_FOURCC: cv2.VideoWriter_fourcc(*'YUYV')}])
def test_failed_actual_negotiation_is_error_and_releases_without_read(bad):
    now, stop, output, capture = capture_case(bad=bad)
    preview._capture_session('camera1', output, stop, Codec(capture), lambda: now[0], lambda _: True)
    assert capture.reads == 0 and capture.releases == 1
    assert '未接受' in output.packets[-1]['error']
    assert output.packets[-1]['negotiated'] is not None
    assert not any('jpeg' in item for item in output.packets)


@pytest.mark.parametrize('failure', [-1, 8])
def test_open_or_read_failure_does_not_preserve_a_fresh_packet(failure):
    now, stop, output, capture = capture_case(failure=failure)
    preview._capture_session('camera1', output, stop, Codec(capture), lambda: now[0], lambda _: True)
    assert capture.releases == 1 and output.packets[-1]['error']
    assert 'jpeg' not in output.packets[-1]
    state = preview.PreviewState(lambda: now[0])
    for item in output.packets:
        state.apply('camera1', item)
    assert state.frame('camera1') is None and state.status()['camera1']['age_s'] is None


def test_missing_camera_never_opens_any_guessed_video_index():
    now, stop, output, capture = capture_case()
    preview._capture_session('camera1', output, stop, Codec(capture), lambda: now[0], lambda _: False)
    assert not capture.opened and not capture.reads and not capture.releases
    assert output.packets[-1]['error'] == '相机设备不存在'


def test_unverified_buffer_size_does_not_claim_one_frame_freshness():
    now, stop, output, capture = capture_case(bad={cv2.CAP_PROP_BUFFERSIZE: 0})
    preview._capture_session('camera1', output, stop, Codec(capture), lambda: now[0], lambda _: True)
    assert capture.reads == 0 and capture.releases == 1
    assert output.packets[-1]['error'] == '相机未接受单帧采集缓冲'


def test_runtime_long_gap_reopens_instead_of_timestamping_an_old_buffer_as_new():
    now, stop, output, capture = capture_case()
    preview._capture_session('camera1', output, stop, Codec(capture, encode_delay=.6), lambda: now[0], lambda _: True)
    assert capture.reads == 5 and capture.releases == 1
    assert output.packets[-1]['error'] == '取帧间隔过长，重新建立采集'
    frames = [item for item in output.packets if item.get('jpeg')]
    assert len(frames) == 1 and frames[0]['observed_at'] == pytest.approx(100.3)


def test_age_is_read_start_minus_buffer_not_encoding_completion():
    now, stop, output, capture = capture_case(count=7)
    preview._capture_session('camera1', output, stop, Codec(capture, encode_delay=.15), lambda: now[0], lambda _: True)
    first = next(item for item in output.packets if item.get('jpeg'))
    assert first['observed_at'] == pytest.approx(100.3)
    state = preview.PreviewState(lambda: 100.65)
    state.apply('camera1', first)
    assert state.status()['camera1']['age_s'] == pytest.approx(.35)
    assert state.status()['camera1']['age_basis'] == 'conservative_read_observation'


@pytest.mark.parametrize('delay,encode_delay', [(2.1, 0), (0, 2.1)])
def test_blocked_read_or_slow_encode_never_publishes_fresh_old_frame(delay, encode_delay):
    now, stop, output, capture = capture_case(delay=delay)
    preview._capture_session('camera1', output, stop, Codec(capture, encode_delay=encode_delay), lambda: now[0], lambda _: True)
    assert output.packets[-1]['error'] and not any('jpeg' in item for item in output.packets)
    assert capture.releases == 1


@pytest.mark.parametrize('raw', [b'', b'bad-jpeg' * 10, b'\xff\xd8' + b'x' * 500000 + b'\xff\xd9'],
                         ids=['empty', 'bad-signature', 'oversized'])
def test_invalid_encoded_size_or_signature_releases(raw):
    now, stop, output, capture = capture_case()
    preview._capture_session('camera1', output, stop, Codec(capture, encoded=(True, np.frombuffer(raw, np.uint8))),
                             lambda: now[0], lambda _: True)
    assert output.packets[-1]['error'] == 'JPEG 编码失败' and capture.releases == 1


def test_latest_shared_slot_error_replaces_success_without_reconsuming_a_version():
    output = preview.LatestSlot(multiprocessing.get_context('spawn'))
    assert output.publish({'jpeg': b'previous'})
    assert output.publish({'error': '读取相机失败'})
    version, value = output.read_after(0)
    assert version == 2 and value == {'error': '读取相机失败'}
    assert output.read_after(version) is None
    with pytest.raises(ValueError, match='limit'):
        output.publish({'jpeg': b'x' * (preview.MAX_JPEG_BYTES + 1)})
    with pytest.raises(ValueError, match='limit'):
        output.publish({'error': 'x' * (preview.MAX_METADATA_BYTES + 1)})


def abandon_slot_lock(slot, ready):
    slot.lock.acquire()
    memoryview(slot.buffer).cast('B')[:100] = b'x' * 100  # Simulate death mid-packet copy.
    ready.set()
    os._exit(0)


def test_child_death_mid_large_packet_cannot_block_other_cameras_or_supervision(monkeypatch):
    context = multiprocessing.get_context('spawn')
    stuck, healthy = preview.LatestSlot(context), preview.LatestSlot(context)
    assert stuck.publish({'jpeg': b'x' * preview.MAX_JPEG_BYTES})
    assert healthy.publish({'error': '', 'frames': 1})
    ready = context.Event()
    child = context.Process(target=abandon_slot_lock, args=(stuck, ready))
    child.start()
    try:
        assert ready.wait(5)
        child.join(timeout=1)
        assert not child.is_alive()
        began = time.monotonic()
        assert stuck.read_after(0) is None
        assert healthy.read_after(0)[1]['frames'] == 1
        assert time.monotonic() - began < .15
        state, supervisor = preview.PreviewState(), None
        supervisor = preview.CaptureSupervisor(state, context)
        ended = []
        def start(key):
            return {'process': NS(is_alive=lambda: key != 'camera1'),
                    'slot': stuck if key == 'camera1' else healthy, 'version': 0,
                    'heard': time.monotonic(), 'stop': NS(set=lambda: None)}
        monkeypatch.setattr(supervisor, '_start', start)
        monkeypatch.setattr(supervisor, '_end', lambda item: ended.append(item) or True)
        supervisor.start()
        deadline = time.monotonic() + 1
        while state.status()['camera2']['frames'] != 1 and time.monotonic() < deadline:
            time.sleep(.02)
        assert state.status()['camera2']['frames'] == 1
        assert ended and supervisor.thread.is_alive()
        supervisor.stop()
        assert not supervisor.thread.is_alive()
    finally:
        if child.is_alive():
            child.kill()
            child.join(timeout=1)


def test_one_spawn_failure_does_not_stop_other_cameras_and_stop_has_single_owner(monkeypatch):
    state, context = preview.PreviewState(), multiprocessing.get_context('spawn')
    supervisor = preview.CaptureSupervisor(state, context)
    calls, ended = [], []
    def start(key):
        calls.append(key)
        if key == 'camera1':
            raise OSError('isolated process start failure')
        slot = preview.LatestSlot(context)
        slot.publish({'error': '', 'frames': 42})
        return {'process': NS(is_alive=lambda: True), 'slot': slot, 'version': 0,
                'heard': time.monotonic(), 'stop': NS(set=lambda: None)}
    monkeypatch.setattr(supervisor, '_start', start)
    monkeypatch.setattr(supervisor, '_end', lambda child: ended.append(child) or True)
    supervisor.start()
    deadline = time.monotonic() + 1
    while state.status()['camera4']['frames'] != 42 and time.monotonic() < deadline:
        time.sleep(.02)
    assert state.status()['camera4']['frames'] == 42
    assert state.status()['camera1']['error'] == '无法启动独立相机采集'
    assert calls.count('camera1') == 1
    supervisor.stop()
    assert not supervisor.thread.is_alive() and len(ended) == 3


def packet():
    ok, raw = cv2.imencode('.jpg', np.zeros((360, 640, 3), dtype=np.uint8))
    assert ok
    return {'error': '', 'observed_at': 100.0, 'jpeg': raw.tobytes(), 'frames': 5, 'encoded_frames': 1,
            'negotiated': {'width': 640, 'height': 360, 'fourcc': 'MJPG', 'fps': 10}}


def test_status_and_http_reads_never_renew_age_and_camera_errors_are_independent():
    now, state = [100.0], preview.PreviewState(lambda: now[0])
    for key in preview.CAMERAS:
        state.apply(key, packet())
    now[0] = 101
    assert state.status()['camera1']['age_s'] == 1
    assert state.frame('camera1') == packet()['jpeg']
    state.fail('camera1', '读取相机失败')
    assert state.frame('camera1') is None and state.frame('camera2') is not None
    now[0] = 102.001
    assert all(item['error'] and item['age_s'] is None for item in state.status().values())
    assert all(state.frame(key) is None for key in preview.CAMERAS)


@pytest.fixture
def server():
    now = [100.0]
    state = preview.PreviewState(lambda: now[0])
    for key in preview.CAMERAS:
        state.apply(key, packet())
    value = preview.PreviewServer(state, ('127.0.0.1', 0))
    thread = threading.Thread(target=value.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
    thread.start()
    try:
        yield value.server_address[1], state, now
    finally:
        value.shutdown()
        value.server_close()
        thread.join(timeout=1)


def request(port, path='/status', method='GET', body=None):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
    try:
        connection.request(method, path, body=body)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def test_status_and_finite_frames_compatible_with_existing_relay(server):
    port, state, now = server
    status, headers, body = request(port)
    assert status == 200 and headers['Content-Type'] == 'application/json'
    assert set(json.loads(body)) == set(preview.CAMERAS) and len(body) < 32768
    source = relay._PreviewSource(upstream_port=port)
    for key in preview.CAMERAS:
        kind, body, metadata = source.fetch('/stream/' + key)
        assert kind == 'multipart/x-mixed-replace; boundary=frame'
        assert body.endswith(b'--frame--\r\n') and metadata['X-Preview-Contract'] == '1'
        assert float(metadata['X-Source-Age-S']) >= 0
        status, headers, raw = request(port, '/stream/' + key)
        assert status == 200 and int(headers['Content-Length']) == len(raw)
        assert headers['Connection'] == 'close' and headers['Cache-Control'] == 'no-store'
    now[0] = 102.001
    assert request(port, '/stream/camera1')[0] == 422
    with pytest.raises(relay.RelayError) as error:
        source.fetch('/stream/camera1')
    assert error.value.status == 422


@pytest.mark.parametrize('path,method,body,expected', [
    ('/stream/camera5', 'GET', None, 404), ('/', 'GET', None, 404),
    ('http://example.invalid/status', 'GET', None, 404), ('/status?x=1', 'GET', None, 400),
    ('/status#x', 'GET', None, 400), ('/stream/%63amera1', 'GET', None, 404),
    ('/status', 'POST', b'data', 405), ('/status', 'PUT', None, 405),
    ('/status', 'PATCH', None, 405), ('/status', 'DELETE', None, 405),
    ('/status', 'HEAD', None, 405), ('/status', 'GET', b'data', 400),
])
def test_http_only_fixed_get_routes(server, path, method, body, expected):
    assert request(server[0], path, method, body)[0] == expected


def test_http_headers_threads_and_slow_clients_are_bounded(server):
    port = server[0]
    clients = []
    try:
        for _ in range(preview.MAX_HTTP_WORKERS):
            connection = socket.create_connection(('127.0.0.1', port), timeout=2)
            connection.sendall(b'GET /status HTTP/1.1\r\nX-Hold: ')
            clients.append(connection)
        time.sleep(.05)
        assert request(port)[0] == 503
        started = time.monotonic()
        for connection in clients:
            data = connection.recv(4096)
            assert not data or data.startswith(b'HTTP/1.1 408')
        assert time.monotonic() - started < 1.5
    finally:
        for connection in clients:
            connection.close()
    with socket.create_connection(('127.0.0.1', port), timeout=2) as connection:
        connection.sendall(b'GET /status HTTP/1.1\r\nX-Large: ' + b'x' * preview.MAX_HEADERS + b'\r\n\r\n')
        assert connection.recv(4096).startswith(b'HTTP/1.1 431')


def test_shutdown_terminates_blocked_child_with_bounded_joins():
    events = []
    class Process:
        alive = True
        def join(self, timeout):
            assert 0 <= timeout <= .3
            events.append('join')
        def is_alive(self):
            return self.alive
        def terminate(self):
            events.append('terminate')
        def kill(self):
            events.append('kill')
            self.alive = False
    child = {'process': Process(), 'stop': NS(set=lambda: events.append('stop'))}
    assert preview.CaptureSupervisor._end(child)
    assert events == ['stop', 'join', 'terminate', 'join', 'kill', 'join']


def test_fixed_bootstrap_cli_does_not_expose_paths_addresses_or_robot_control():
    with pytest.raises(SystemExit) as error:
        preview.main(['--device', '/dev/video999'])
    assert error.value.code == 2
    with pytest.raises(ValueError, match='loopback'):
        preview.PreviewServer(preview.PreviewState(), ('0.0.0.0', 0))
    source = Path(preview.__file__).read_text()
    tree = ast.parse(source)
    calls = {node.func.attr for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert not {'create_publisher', 'create_client', 'ActionClient', 'Serial', 'post', 'put', 'patch', 'delete'} & calls
    assert 'rclpy' not in source and 'subprocess' not in source
    assert preview.HOST == '127.0.0.1' and preview.PORT == 8080
    assert preview.MAX_HTTP_WORKERS == 5 and len(preview.CAMERAS) == 4


def test_existing_8080_source_prevents_starting_any_competing_capture(monkeypatch):
    events = []
    monkeypatch.setattr(preview, '_instance_lock', lambda: 999)
    monkeypatch.setattr(preview.os, 'close', lambda value: events.append(('close-lock', value)))
    class Supervisor:
        def __init__(self, state):
            pass
        def start(self):
            pytest.fail('An occupied HTTP port must prevent opening any video device')
    monkeypatch.setattr(preview, 'CaptureSupervisor', Supervisor)
    def occupied(*args, **kwargs):
        raise OSError('isolated port already in use')
    monkeypatch.setattr(preview, 'PreviewServer', occupied)
    with pytest.raises(OSError, match='already in use'):
        preview.main([])
    assert events == [('close-lock', 999)]


def test_main_signal_stops_capture_before_releasing_instance_lock(monkeypatch):
    events, handlers = [], {}
    monkeypatch.setattr(preview, '_instance_lock', lambda: 999)
    monkeypatch.setattr(preview.os, 'close', lambda value: events.append('unlock'))
    monkeypatch.setattr(preview.signal, 'signal', lambda kind, callback: handlers.update({kind: callback}))
    class Supervisor:
        def __init__(self, state):
            pass
        def start(self):
            events.append('start')
        def stop(self):
            events.append('stop')
    class Server:
        def __init__(self, state):
            events.append('bind')
        def __enter__(self):
            return self
        def __exit__(self, *args):
            events.append('server-close')
        def handle_request(self):
            handlers[preview.signal.SIGTERM]()
    monkeypatch.setattr(preview, 'CaptureSupervisor', Supervisor)
    monkeypatch.setattr(preview, 'PreviewServer', Server)
    preview.main([])
    assert events == ['bind', 'start', 'stop', 'server-close', 'unlock']
