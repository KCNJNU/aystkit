"""Isolated loopback relay tests; no Jetson, ROS, camera, or production connection."""
from __future__ import annotations

import ast
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
from pathlib import Path
import socket
import threading
import time
from types import SimpleNamespace as NS

import cv2
import numpy as np
import pytest

from collector import http_camera_relay as relay
from tests.test_protocol import jpeg


def part(raw=None, header=None):
    raw = jpeg() if raw is None else raw
    header = (b'Content-Type: image/jpeg\r\nContent-Length: ' + str(len(raw)).encode() + b'\r\n') if header is None else header
    return b'--frame\r\n' + header + b'\r\n' + raw + b'\r\n'


class Upstream(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, *args):
        pass


@pytest.fixture
def running_relay(request):
    settings = getattr(request, 'param', '640x480')
    settings = settings if isinstance(settings, dict) else {'preview_size': settings}
    state = NS(calls=[], mode='valid', status=200, body=None, content_type=None,
               stream_body=None, stream_type=None,
               headers=[], active=0, maximum=0, lock=threading.Lock(),
               held=threading.Event(), release=threading.Event(), closed=threading.Event(),
               preview_size=settings.get('preview_size', '640x480'),
               keep_alive=settings.get('keep_alive', False))
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_GET(self):
            state.calls.append((self.command, self.path, dict(self.headers)))
            with state.lock:
                state.active += 1
                state.maximum = max(state.maximum, state.active)
                if state.active == relay.MAX_WORKERS:
                    state.held.set()
            try:
                if state.mode == 'hold':
                    state.release.wait(2)
                if state.mode == 'timeout':
                    time.sleep(1.4)
                if state.mode == 'slow_headers':
                    self.connection.sendall(b'HTTP/1.1 200 OK\r\nContent-Type: ')
                    for _ in range(30):
                        self.connection.sendall(b'a')
                        time.sleep(.06)
                    return
                stream = self.path.startswith('/stream/')
                body = state.stream_body if stream else state.body
                if body is None:
                    body = (json.dumps({camera: {'age_s': 0, 'error': ''} for camera in ('camera1', 'camera2', 'camera3', 'camera4')}).encode()
                            if not stream else part())
                content_type = ((state.stream_type if stream else state.content_type)
                                or ('multipart/x-mixed-replace; boundary=frame' if stream else 'application/json'))
                self.send_response(state.status)
                self.send_header('Content-Type', content_type)
                for name, value in state.headers:
                    self.send_header(name, value)
                if not any(name.lower() == 'content-length' for name, _ in state.headers) and not (state.mode == 'streaming' and stream):
                    self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()
                if state.mode == 'streaming' and stream:
                    try:
                        for _ in range(100):
                            time.sleep(.01)
                            self.wfile.write(part())
                            self.wfile.flush()
                    except (OSError, ConnectionError):
                        state.closed.set()
            finally:
                with state.lock:
                    state.active -= 1
    source = Upstream(('127.0.0.1', 0), Handler)
    server = relay._RelayServer(('127.0.0.1', 0), upstream_port=source.server_address[1],
                                preview_size=state.preview_size, keep_alive=state.keep_alive)
    state.server = server
    threads = [threading.Thread(target=site.serve_forever, kwargs={'poll_interval': .02}, daemon=True)
               for site in (source, server)]
    for thread in threads:
        thread.start()
    try:
        yield server.server_address[1], state
    finally:
        state.release.set()
        for site in (server, source):
            site.shutdown()
            site.server_close()
        for thread in threads:
            thread.join(timeout=2)


def request(port, path='/status', method='GET', body=None, headers=None):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=4)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def raw_request(port, raw):
    with socket.create_connection(('127.0.0.1', port), timeout=4) as connection:
        connection.sendall(raw)
        chunks = []
        while True:
            try:
                value = connection.recv(65536)
            except ConnectionResetError:
                break
            if not value:
                break
            chunks.append(value)
        return b''.join(chunks)


def test_status_is_finite_json_and_does_not_forward_credentials(running_relay):
    port, state = running_relay
    status, headers, body = request(port, headers={'Authorization': 'test-only-never-forward',
                                                  'Cookie': 'test-only-never-forward'})
    assert status == 200
    assert json.loads(body)['camera1']['age_s'] == 0
    assert int(headers['Content-Length']) == len(body)
    assert 'X-Preview-Contract' not in headers  # Legacy /status stays unchanged.
    assert headers['Connection'] == 'close' and headers['Cache-Control'] == 'no-store'
    method, path, sent = state.calls[-1]
    assert (method, path) == ('GET', '/status')
    assert not {'Authorization', 'Cookie'} & set(sent)
    assert sent['Accept-Encoding'] == 'identity'


@pytest.mark.parametrize('camera', ['camera1', 'camera2', 'camera3', 'camera4'])
@pytest.mark.parametrize('running_relay', ['640x480', '320x240'], indirect=True)
def test_single_frame_finite_response_closes_original_stream(running_relay, camera):
    port, state = running_relay
    state.mode = 'streaming'
    status, headers, body = request(port, '/stream/' + camera)
    assert status == 200
    assert headers['Content-Type'] == 'multipart/x-mixed-replace; boundary=frame'
    assert int(headers['Content-Length']) == len(body)
    assert headers['X-Preview-Contract'] == '1'
    assert headers['X-Source-SHA256'] == hashlib.sha256(jpeg()).hexdigest()
    assert 0 <= float(headers['X-Source-Age-S']) < 1
    preview = relay._preview_jpeg(jpeg(), state.preview_size)
    assert body == part(preview) + b'--frame--\r\n'
    assert body.count(preview) == 1
    assert state.closed.wait(1), 'The source stream must be closed after its first frame'


@pytest.mark.parametrize('path,expected', [('/status?next=http://remote', 400), ('/status#x', 400),
    ('/stream/camera5', 404), ('/stream/camera0', 404), ('/stream/%63amera1', 404), ('http://example.invalid/status', 404),
    ('/control', 404), ('/../status', 404), ('/', 404)])
def test_paths_and_queries_fail_without_upstream(running_relay, path, expected):
    port, state = running_relay
    assert request(port, path)[0] == expected
    assert not state.calls


@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS', 'HEAD', 'CONNECT'])
def test_no_writes_or_other_methods(running_relay, method):
    port, state = running_relay
    assert request(port, method=method, body=b'ignored')[0] == 405
    assert not state.calls


@pytest.mark.parametrize('wire,expected', [
    (b'GET /status HTTP/1.1\r\nHost: localhost\r\nContent-Length: 1\r\n\r\nx', 400),
    (b'GET /status HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n', 400),
    (b'GET /status HTTP/1.1\r\nHost: a\r\nHost: b\r\n\r\n', 400),
    (b'GET /status HTTP/1.1\r\nBroken\r\n\r\n', 400),
    (b'GET /status HTTP/1.1\r\nX-Large: ' + b'a' * relay.MAX_REQUEST_HEADERS + b'\r\n\r\n', 431),
])
def test_request_headers_body_and_duplicate_bounds(running_relay, wire, expected):
    port, state = running_relay
    assert raw_request(port, wire).startswith(('HTTP/1.1 ' + str(expected)).encode())
    assert not state.calls


@pytest.mark.parametrize('body,content_type,headers,status', [
    (b'{}', 'text/html', [], 200), (b'{bad}', None, [], 200), (b'[]', None, [], 200),
    (b'{"age":NaN}', None, [], 200), (b' ' * (relay.MAX_STATUS_BYTES + 1), None, [], 200),
    (b'{}', None, [('Content-Length', '40000')], 200),
    (b'{}', None, [('Content-Length', '-1')], 200),
    (b'{}', None, [('Content-Length', '3')], 200),
    (b'{}', None, [('Content-Encoding', 'gzip')], 200),
    (b'{}', None, [('Location', 'http://must-not-follow.invalid')], 302),
    (b'{}', None, [], 500),
])
def test_status_validation_errors_never_return_cached_success(running_relay, body, content_type, headers, status):
    port, state = running_relay
    assert request(port)[0] == 200
    state.body, state.content_type, state.headers, state.status = body, content_type, headers, status
    result = request(port)
    assert result[0] == 422 and result[2] == b''
    assert len(state.calls) == 2


@pytest.mark.parametrize('body,content_type', [
    (part(b'x' * 30), None), (part(b'\xff\xd8' + b'x' * 27 + b'X'), None),
    (part(header=b'Content-Type: image/jpeg\r\nContent-Length: 500001\r\n'), None),
    (part(header=b'Content-Type: image/jpeg\r\nContent-Length: 15\r\n'), None),
    (part(header=b'Content-Type: image/png\r\nContent-Length: 30\r\n'), None),
    (part(header=b'Content-Type: image/jpeg\r\n'), None),
    (part(header=b'Content-Type: image/jpeg\r\nContent-Length: 30\r\nContent-Length: 30\r\n'), None),
    (part(header=b'X: ' + b'a' * relay.MAX_PART_HEADERS + b'\r\n'), None),
    (part(header=b'Broken\r\n'), None),
    (b'--wrong\r\n' + part(), None),
    (part(), 'multipart/x-mixed-replace; boundary=wrong'),
    (part(), 'multipart/x-mixed-replace; boundary=FRAME'),
    (part(), 'image/jpeg'),
    (part()[:-20], None),
])
def test_stream_binary_header_and_size_validation(running_relay, body, content_type):
    port, state = running_relay
    state.stream_body, state.stream_type = body, content_type
    result = request(port, '/stream/camera1')
    assert result[0] == 422 and result[2] == b''


def test_outer_upstream_headers_are_bounded(running_relay):
    port, state = running_relay
    state.headers = [('X-Large', 'a' * (relay.MAX_RESPONSE_HEADERS + 1))]
    assert request(port)[0] == 422


def test_connection_failure_is_502_without_cached_bytes():
    # Reserve but do not listen on a loopback port, so the failure is isolated.
    with socket.socket() as reserved:
        reserved.bind(('127.0.0.1', 0))
        source = relay._PreviewSource(upstream_port=reserved.getsockname()[1])
        with pytest.raises(relay.RelayError) as failure:
            source.fetch('/status')
        assert failure.value.status == 502


@pytest.mark.parametrize('mode', ['timeout', 'slow_headers'])
def test_absolute_upstream_timeout_including_slow_drip(running_relay, mode):
    port, state = running_relay
    state.mode = mode
    started = time.monotonic()
    assert request(port)[0] == 504
    assert time.monotonic() - started < 1.6


def test_client_slow_headers_have_absolute_deadline(running_relay):
    port, state = running_relay
    with socket.create_connection(('127.0.0.1', port), timeout=4) as connection:
        started = time.monotonic()
        connection.sendall(b'GET /status HTTP/1.1\r\nX-Slow: ')
        response = connection.recv(4096)
        assert time.monotonic() - started < relay.CLIENT_TIMEOUT + .6
        assert not response or response.startswith(b'HTTP/1.1 408')
    assert not state.calls


def test_at_most_five_handlers_and_busy_requests_do_not_reach_source(running_relay):
    port, state = running_relay
    assert relay.MAX_WORKERS == 5
    state.mode = 'hold'
    results = []
    threads = [threading.Thread(target=lambda: results.append(request(port)[0]))
               for _ in range(relay.MAX_WORKERS)]
    for thread in threads:
        thread.start()
    try:
        assert state.held.wait(.8)
        assert request(port)[0] == 503
        assert len(state.calls) == relay.MAX_WORKERS
        assert state.maximum == relay.MAX_WORKERS
    finally:
        state.release.set()
        for thread in threads:
            thread.join(timeout=2)
    assert results == [200] * relay.MAX_WORKERS
    state.mode = 'valid'
    assert request(port)[0] == 200


def test_fixed_cli_lazy_codec_dependencies_no_control_or_cache(monkeypatch):
    with pytest.raises(SystemExit) as failure:
        relay.main(['--upstream-port', '9999'])
    assert failure.value.code == 2
    with pytest.raises(ValueError, match='fixed loopback'):
        relay._RelayServer(('0.0.0.0', 0))
    source = Path(relay.__file__).read_text()
    tree = ast.parse(source)
    modules = {node.module.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    modules.update(alias.name.split('.')[0] for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names)
    assert modules <= {'__future__', 'argparse', 'hashlib', 'http', 'json', 'math', 're', 'socket', 'socketserver', 'threading', 'time', 'cv2', 'numpy'}
    assert '/dev/' not in source and 'rclpy' not in modules
    top_level = {alias.name for node in tree.body if isinstance(node, ast.Import) for alias in node.names}
    assert not {'cv2', 'numpy'} & top_level
    assert relay.HOST == '127.0.0.1' and relay.PORT == 18081 and relay.UPSTREAM_PORT == 8080
    assert relay.PATHS == {'/status', '/stream/camera1', '/stream/camera2', '/stream/camera3', '/stream/camera4'}
    events = []
    class FakeServer:
        def __init__(self, *args, **kwargs):
            assert not args and kwargs == {'preview_size': '640x480', 'keep_alive': False}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            events.append('closed')
        def serve_forever(self, **kwargs):
            events.append('serve')
            raise KeyboardInterrupt()
    monkeypatch.setattr(relay, '_RelayServer', FakeServer)
    relay.main([])
    assert events == ['serve', 'closed']


@pytest.mark.parametrize('size', ['320x240', '640x480'])
def test_cli_bounded_preview_size_reaches_server_without_changing_endpoints(monkeypatch, size):
    received = []
    class FakeServer:
        def __init__(self, **kwargs):
            received.append(kwargs)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def serve_forever(self, **kwargs):
            raise KeyboardInterrupt()
    monkeypatch.setattr(relay, '_RelayServer', FakeServer)
    relay.main(['--preview-size', size])
    assert received == [{'preview_size': size, 'keep_alive': False}]
    assert relay.HOST == '127.0.0.1' and relay.PORT == 18081 and relay.UPSTREAM_PORT == 8080


@pytest.mark.parametrize('size', ['0x0', '1920x1080', '320X240', '320x240 ', '', None, True, [320, 240]])
def test_internal_preview_size_is_enum_not_arbitrary_dimensions(size):
    for build in (lambda: relay._PreviewSource(preview_size=size),
                  lambda: relay._RelayServer(('127.0.0.1', 0), preview_size=size),
                  lambda: relay._preview_jpeg(jpeg(), size)):
        with pytest.raises(ValueError, match='Preview size'):
            build()


@pytest.mark.parametrize('arguments', [['--preview-size', '1920x1080'], ['--preview-size', '320X240'],
                                        ['--preview-size', '320x240', '--upstream-port', '9999']])
def test_cli_rejects_unbounded_size_or_source_override_before_start(monkeypatch, arguments):
    monkeypatch.setattr(relay, '_codec', lambda: pytest.fail('Invalid CLI must not load a codec or listen'))
    with pytest.raises(SystemExit) as error:
        relay.main(arguments)
    assert error.value.code == 2


def fixture_image(width, height):
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:height // 2, :width // 2] = (0, 0, 240)
    image[:height // 2, width // 2:] = (0, 240, 0)
    image[height // 2:, :width // 2] = (240, 0, 0)
    image[height // 2:, width // 2:] = (0, 240, 240)
    ok, raw = cv2.imencode('.jpg', image, [cv2.IMWRITE_JPEG_QUALITY, 90])
    assert ok
    return raw.tobytes(), image


@pytest.mark.parametrize('size,width,height,expected', [
    ('640x480', 1280, 720, (640, 360)), ('640x480', 800, 1200, (320, 480)),
    ('640x480', 320, 240, (320, 240)), ('640x480', 1920, 1080, (640, 360)),
    ('320x240', 1280, 720, (320, 180)), ('320x240', 800, 1200, (160, 240)),
    ('320x240', 640, 360, (320, 180)), ('320x240', 640, 480, (320, 240)),
    ('320x240', 160, 120, (160, 120)),
])
def test_preview_real_jpeg_proportional_resize_without_crop(size, width, height, expected, monkeypatch):
    raw, original = fixture_image(width, height)
    calls = []
    encode = cv2.imencode
    def capture(extension, image, options):
        calls.append((extension, options))
        return encode(extension, image, options)
    monkeypatch.setattr(cv2, 'imencode', capture)
    preview = relay._preview_jpeg(raw, size)
    assert relay._jpeg_dimensions(preview) == expected
    decoded = cv2.imdecode(np.frombuffer(preview, dtype=np.uint8), cv2.IMREAD_COLOR)
    assert decoded.shape == (expected[1], expected[0], 3)
    assert calls == [('.jpg', [cv2.IMWRITE_JPEG_QUALITY, 60])]
    for y, x in ((.1, .1), (.1, .9), (.9, .1), (.9, .9)):
        actual = decoded[int(y * expected[1]), int(x * expected[0])].astype(int)
        wanted = original[int(y * height), int(x * width)].astype(int)
        assert np.max(np.abs(actual - wanted)) < 10
    assert len(preview) <= relay.MAX_JPEG_BYTES
    if width > 640 or height > 480:
        assert len(preview) < len(raw)


def resize_sof(raw, width, height):
    value = bytearray(raw)
    index = value.index(b'\xff\xc0')
    value[index + 5:index + 7] = height.to_bytes(2, 'big')
    value[index + 7:index + 9] = width.to_bytes(2, 'big')
    return bytes(value)


@pytest.mark.parametrize('width,height', [(4097, 1), (1, 4097), (4096, 1954), (4000, 2001), (0, 1), (1, 0)])
@pytest.mark.parametrize('size', ['640x480', '320x240'])
def test_pixel_bombs_rejected_before_codec_allocation(width, height, monkeypatch, size):
    def forbidden():
        pytest.fail('Oversized source must fail before decoder import/allocation')
    monkeypatch.setattr(relay, '_codec', forbidden)
    with pytest.raises(relay.RelayError) as failure:
        relay._preview_jpeg(resize_sof(jpeg(), width, height), size)
    assert failure.value.status == 422


@pytest.mark.parametrize('width,height', [(4096, 1), (1, 4096), (4000, 2000), (4096, 1953)])
def test_dimension_boundary_allows_4096_side_and_eight_million_pixels(width, height):
    assert relay._jpeg_dimensions(resize_sof(jpeg(), width, height)) == (width, height)


@pytest.mark.parametrize('raw', [b'\xff\xd8' + b'x' * 30 + b'\xff\xd9', jpeg()[:-5] + b'\xff\xd9',
                               b'not-a-jpeg', b'x' * (relay.MAX_JPEG_BYTES + 1)],
                         ids=['invalid-markers', 'truncated', 'bad-signature', 'oversized'])
@pytest.mark.parametrize('size', ['640x480', '320x240'])
def test_bad_jpeg_never_returns_old_or_passthrough_bytes(raw, size):
    with pytest.raises(relay.RelayError) as failure:
        relay._preview_jpeg(raw, size)
    assert failure.value.status == 422


@pytest.mark.parametrize('running_relay', ['640x480', '320x240'], indirect=True)
def test_real_production_path_closes_source_before_encoding_then_returns_small_frame(running_relay, monkeypatch):
    port, state = running_relay
    raw, _ = fixture_image(1280, 720)
    state.stream_body, state.mode = part(raw), 'streaming'
    original = relay._preview_jpeg
    encoded = []
    def verify_close_before_decode(value, size):
        assert state.closed.wait(.8), 'Upstream must be closed before CPU image work'
        assert size == state.preview_size
        result = original(value, size)
        encoded.append(result)
        return result
    monkeypatch.setattr(relay, '_preview_jpeg', verify_close_before_decode)
    status, headers, body = request(port, '/stream/camera2')
    assert status == 200 and len(encoded) == 1
    assert int(headers['Content-Length']) == len(body)
    assert body == part(encoded[0]) + b'--frame--\r\n'
    assert relay._jpeg_dimensions(encoded[0]) == ((640, 360) if state.preview_size == '640x480' else (320, 180))
    assert len(encoded[0]) < len(raw)


def test_startup_dependency_check_fails_before_binding(monkeypatch):
    def missing():
        raise ImportError('isolated missing codec')
    def forbidden(*args, **kwargs):
        pytest.fail('A missing codec must not start listening')
    monkeypatch.setattr(relay, '_codec', missing)
    monkeypatch.setattr(relay, '_RelayServer', forbidden)
    with pytest.raises(SystemExit) as failure:
        relay.main([])
    assert failure.value.code == 2


@pytest.mark.parametrize('item', [None, {}, {'age_s': None}, {'age_s': -1}, {'age_s': '0'},
                                 {'age_s': True}, {'age_s': float('nan')},
                                 {'age_s': 10 ** 500},
                                 {'age_s': 0, 'error': 'camera disconnected'}])
def test_stream_rejects_invalid_matching_source_status_without_reading_jpeg(running_relay, item):
    port, state = running_relay
    state.body = json.dumps({'camera1': item}).encode()
    status, headers, body = request(port, '/stream/camera1')
    assert status == 422 and body == b''
    assert 'X-Source-SHA256' not in headers
    assert [path for _, path, _ in state.calls] == ['/status']


def test_matching_source_error_does_not_poison_other_camera(running_relay):
    port, state = running_relay
    state.body = json.dumps({'camera1': {'age_s': 0, 'error': 'offline'},
                             'camera2': {'age_s': 0, 'error': ''}}).encode()
    assert request(port, '/stream/camera1')[0] == 422
    assert request(port, '/stream/camera2')[0] == 200


@pytest.mark.parametrize('size', ['640x480', '320x240'])
def test_stream_contract_age_includes_local_status_frame_and_encoding_time(monkeypatch, size):
    now, calls = [10.0], []
    monkeypatch.setattr(relay, 'time', NS(monotonic=lambda: now[0]))
    value = relay._PreviewSource(preview_size=size)
    def fetch(path):
        calls.append(path)
        if path == '/status':
            now[0] += .1
            return 'application/json', b'{"camera1":{"age_s":0.2,"error":""}}'
        now[0] += .2
        return 'image/jpeg', jpeg()
    def encode(raw, preview_size):
        assert preview_size == size
        now[0] += .05
        return raw
    monkeypatch.setattr(value, '_fetch_closed', fetch)
    monkeypatch.setattr(relay, '_preview_jpeg', encode)
    _, _, headers = value.fetch('/stream/camera1')
    assert calls == ['/status', '/stream/camera1']
    assert float(headers['X-Source-Age-S']) == pytest.approx(.55)
    assert headers['X-Preview-Contract'] == '1'
    assert headers['X-Source-SHA256'] == hashlib.sha256(jpeg()).hexdigest()


def test_original_jpeg_identity_survives_lossy_preview_collision(running_relay):
    port, state = running_relay
    first = np.full((720, 1280, 3), 100, dtype=np.uint8)
    second = first.copy()
    second[100, 100] = 116
    _, a = cv2.imencode('.jpg', first, [cv2.IMWRITE_JPEG_QUALITY, 100])
    _, b = cv2.imencode('.jpg', second, [cv2.IMWRITE_JPEG_QUALITY, 100])
    a, b = a.tobytes(), b.tobytes()
    assert a != b and relay._preview_jpeg(a) == relay._preview_jpeg(b)
    state.stream_body = part(a)
    status_a, headers_a, body_a = request(port, '/stream/camera1')
    state.stream_body = part(b)
    status_b, headers_b, body_b = request(port, '/stream/camera1')
    assert status_a == status_b == 200 and body_a == body_b
    assert headers_a['X-Source-SHA256'] == hashlib.sha256(a).hexdigest()
    assert headers_b['X-Source-SHA256'] == hashlib.sha256(b).hexdigest()
    assert headers_a['X-Source-SHA256'] != headers_b['X-Source-SHA256']


def test_default_output_compatible_and_size_change_never_renews_source_identity(monkeypatch):
    from tests.test_http_camera import adapter
    raw, _ = fixture_image(640, 360)
    assert relay._preview_jpeg(raw) == relay._preview_jpeg(raw, '640x480')
    results = []
    def fetch(path):
        if path == '/status':
            return 'application/json', b'{"camera1":{"age_s":0.1,"error":""}}'
        assert path == '/stream/camera1'
        return 'image/jpeg', raw
    for size in ('640x480', '320x240'):
        source = relay._PreviewSource(preview_size=size)
        monkeypatch.setattr(source, '_fetch_closed', fetch)
        _, body, headers = source.fetch('/stream/camera1')
        result = relay._preview_jpeg(raw, size)
        assert body == part(result) + b'--frame--\r\n'
        assert headers['X-Source-SHA256'] == hashlib.sha256(raw).hexdigest()
        assert float(headers['X-Source-Age-S']) >= .1
        results.append((result, headers))
    assert results[0][0] != results[1][0]
    assert relay._jpeg_dimensions(results[0][0]) == (640, 360)
    assert relay._jpeg_dimensions(results[1][0]) == (320, 180)
    now = [10.0]
    receiver, uploader = adapter(clock=lambda: now[0])
    digest = results[0][1]['X-Source-SHA256']
    assert receiver.receive('usbRear', results[0][0], .1, digest)
    original_stamp = receiver.frames['usbRear']['observed_at']
    now[0] += .5
    assert not receiver.receive('usbRear', results[1][0], .01, digest)
    assert receiver.frames['usbRear']['observed_at'] == original_stamp
    assert len(uploader.packets) == 1 and receiver._metadata('usbRear')['ageS'] == pytest.approx(.6)
    now[0] = 12.001
    assert not receiver.receive('usbRear', results[1][0], .01, digest)
    assert uploader.packets[-1]['payload']['status'] == 'STALE'
    assert 'data' not in uploader.packets[-1]['payload']


KEEP_ALIVE_SETTINGS = {'preview_size': '320x240', 'keep_alive': True}


@pytest.mark.parametrize('running_relay', [KEEP_ALIVE_SETTINGS], indirect=True)
def test_keep_alive_two_real_gets_reuse_socket_and_revalidate_hash_age(running_relay):
    port, state = running_relay
    first, _ = fixture_image(640, 360)
    second, _ = fixture_image(320, 180)
    assert first != second
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
    try:
        state.stream_body = part(first)
        state.body = b'{"camera1":{"age_s":0.1,"error":""}}'
        connection.request('GET', '/stream/camera1')
        response = connection.getresponse()
        headers_first = dict(response.getheaders())
        body_first = response.read()
        original_socket = connection.sock
        assert response.status == 200 and headers_first['Connection'] == 'keep-alive'
        assert int(headers_first['Content-Length']) == len(body_first)
        assert body_first == part(relay._preview_jpeg(first, '320x240')) + b'--frame--\r\n'

        state.stream_body = part(second)
        state.body = b'{"camera1":{"age_s":0.7,"error":""}}'
        connection.request('GET', '/stream/camera1')
        response = connection.getresponse()
        headers_second = dict(response.getheaders())
        body_second = response.read()
        assert response.status == 200 and connection.sock is original_socket
        assert int(headers_second['Content-Length']) == len(body_second)
        assert body_second == part(relay._preview_jpeg(second, '320x240')) + b'--frame--\r\n'
        assert headers_first['X-Source-SHA256'] == hashlib.sha256(first).hexdigest()
        assert headers_second['X-Source-SHA256'] == hashlib.sha256(second).hexdigest()
        assert .1 <= float(headers_first['X-Source-Age-S']) < .6
        assert .7 <= float(headers_second['X-Source-Age-S']) < 1.2
        assert [path for _, path, _ in state.calls] == ['/status', '/stream/camera1'] * 2
        assert all(sent['Connection'] == 'close' for _, _, sent in state.calls)
    finally:
        connection.close()


@pytest.mark.parametrize('running_relay', [KEEP_ALIVE_SETTINGS], indirect=True)
@pytest.mark.parametrize('last_path,last_headers,last_status', [
    ('/status', {}, 200), ('/bad', {}, 404),
    ('/stream/camera2', {'Connection': 'Keep-Alive, cLoSe'}, 200),
])
def test_keep_alive_status_error_and_explicit_close_end_reused_connection(running_relay, last_path, last_headers, last_status):
    port, _ = running_relay
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
    try:
        connection.request('GET', '/stream/camera1')
        first = connection.getresponse()
        first.read()
        assert first.getheader('Connection') == 'keep-alive'
        connection.request('GET', last_path, headers=last_headers)
        last = connection.getresponse()
        last.read()
        assert last.status == last_status and last.getheader('Connection') == 'close'
        assert connection.sock is None
    finally:
        connection.close()


@pytest.mark.parametrize('running_relay', [KEEP_ALIVE_SETTINGS], indirect=True)
def test_keep_alive_http10_still_closes(running_relay):
    port, _ = running_relay
    result = raw_request(port, b'GET /stream/camera1 HTTP/1.0\r\nConnection: keep-alive\r\n\r\n')
    assert result.startswith(b'HTTP/1.1 200') and b'Connection: close\r\n' in result


@pytest.mark.parametrize('running_relay', [KEEP_ALIVE_SETTINGS], indirect=True)
@pytest.mark.parametrize('suffix', [b'GET /stream/camera2 HTTP/1.1\r\nHost: a\r\n\r\n', b'extra-body'])
def test_keep_alive_rejects_coalesced_pipeline_and_body_without_discarding_bytes(running_relay, suffix):
    port, state = running_relay
    result = raw_request(port, b'GET /stream/camera1 HTTP/1.1\r\nHost: a\r\n\r\n' + suffix)
    assert result.startswith(b'HTTP/1.1 400') and b'Connection: close\r\n' in result
    assert not state.calls


@pytest.mark.parametrize('running_relay', [KEEP_ALIVE_SETTINGS], indirect=True)
def test_keep_alive_half_close_sends_one_complete_frame_then_releases_slot(running_relay):
    port, state = running_relay
    with socket.create_connection(('127.0.0.1', port), timeout=2) as connection:
        connection.sendall(b'GET /stream/camera1 HTTP/1.1\r\nHost: a\r\n\r\n')
        connection.shutdown(socket.SHUT_WR)
        received = bytearray()
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                break
            received.extend(chunk)
    head, body = bytes(received).split(b'\r\n\r\n', 1)
    assert head.startswith(b'HTTP/1.1 200') and body.endswith(b'\r\n--frame--\r\n')
    assert len(state.calls) == 2
    deadline = time.monotonic() + .5
    while state.server.slots._value != relay.MAX_WORKERS and time.monotonic() < deadline:
        time.sleep(.01)
    assert state.server.slots._value == relay.MAX_WORKERS


@pytest.mark.parametrize('running_relay', [KEEP_ALIVE_SETTINGS], indirect=True)
def test_keep_alive_idle_limit_closes_without_another_source_fetch(running_relay, monkeypatch):
    monkeypatch.setattr(relay, 'KEEP_ALIVE_IDLE_SECONDS', .15)
    port, state = running_relay
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
    try:
        connection.request('GET', '/stream/camera1')
        response = connection.getresponse()
        response.read()
        sock = connection.sock
        started = time.monotonic()
        sock.settimeout(.6)
        assert sock.recv(1024) == b''
        assert time.monotonic() - started < .5 and len(state.calls) == 2
    finally:
        connection.close()


@pytest.mark.parametrize('running_relay', [KEEP_ALIVE_SETTINGS], indirect=True)
def test_keep_alive_hard_lifetime_does_not_restart_after_requests(running_relay, monkeypatch):
    monkeypatch.setattr(relay, 'KEEP_ALIVE_LIFETIME_SECONDS', .35)
    monkeypatch.setattr(relay, 'KEEP_ALIVE_IDLE_SECONDS', 1.0)
    port, _ = running_relay
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
    started = time.monotonic()
    try:
        for index in range(3):
            connection.request('GET', '/stream/camera1')
            response = connection.getresponse()
            assert response.status == 200
            response.read()
            if index < 2:
                time.sleep(.07)
        sock = connection.sock
        sock.settimeout(.6)
        assert sock.recv(1024) == b''
        assert .25 < time.monotonic() - started < .6
    finally:
        connection.close()


@pytest.mark.parametrize('running_relay', [KEEP_ALIVE_SETTINGS], indirect=True)
def test_keep_alive_request_count_final_response_closes(running_relay, monkeypatch):
    monkeypatch.setattr(relay, 'KEEP_ALIVE_MAX_REQUESTS', 2)
    port, state = running_relay
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
    try:
        for expected in ('keep-alive', 'close'):
            connection.request('GET', '/stream/camera1')
            response = connection.getresponse()
            response.read()
            assert response.status == 200 and response.getheader('Connection') == expected
        assert connection.sock is None and len(state.calls) == 4
    finally:
        connection.close()


@pytest.mark.parametrize('running_relay', [KEEP_ALIVE_SETTINGS], indirect=True)
def test_keep_alive_second_header_has_independent_absolute_deadline(running_relay, monkeypatch):
    monkeypatch.setattr(relay, 'CLIENT_TIMEOUT', .15)
    monkeypatch.setattr(relay, 'KEEP_ALIVE_IDLE_SECONDS', 1.0)
    port, state = running_relay
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=1)
    try:
        connection.request('GET', '/stream/camera1')
        first = connection.getresponse()
        first.read()
        sock = connection.sock
        sock.sendall(b'GET /stream/camera2 HTTP/1.1\r\nX-Slow: ')
        started = time.monotonic()
        sock.settimeout(.6)
        result = sock.recv(1024)
        assert not result or result.startswith(b'HTTP/1.1 408')
        assert time.monotonic() - started < .5
        assert len(state.calls) == 2
    finally:
        connection.close()


@pytest.mark.parametrize('running_relay', [KEEP_ALIVE_SETTINGS], indirect=True)
def test_keep_alive_five_idle_workers_remain_bounded_and_close_releases_capacity(running_relay):
    port, state = running_relay
    connections = [http.client.HTTPConnection('127.0.0.1', port, timeout=1) for _ in range(relay.MAX_WORKERS)]
    try:
        for connection in connections:
            connection.request('GET', '/stream/camera1')
            response = connection.getresponse()
            response.read()
            assert response.status == 200 and response.getheader('Connection') == 'keep-alive'
        assert state.server.slots._value == 0
        assert request(port)[0] == 503
        assert len(state.calls) == relay.MAX_WORKERS * 2
        connections[0].close()
        deadline = time.monotonic() + .5
        while state.server.slots._value < 1 and time.monotonic() < deadline:
            time.sleep(.01)
        assert request(port)[0] == 200
    finally:
        for connection in connections:
            connection.close()


@pytest.mark.parametrize('enabled', ['yes', 1, None])
def test_keep_alive_constructor_requires_boolean(enabled):
    with pytest.raises(ValueError, match='explicit boolean'):
        relay._RelayServer(('127.0.0.1', 0), keep_alive=enabled)


def test_keep_alive_cli_explicit_opt_in_preserves_fixed_source(monkeypatch):
    received = []
    class FakeServer:
        def __init__(self, **kwargs):
            received.append(kwargs)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def serve_forever(self, **kwargs):
            raise KeyboardInterrupt()
    monkeypatch.setattr(relay, '_RelayServer', FakeServer)
    relay.main(['--keep-alive', '--preview-size', '320x240'])
    assert received == [{'preview_size': '320x240', 'keep_alive': True}]
    assert relay.HOST == '127.0.0.1' and relay.PORT == 18081 and relay.UPSTREAM_PORT == 8080
