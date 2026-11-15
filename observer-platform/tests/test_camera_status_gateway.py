"""Isolated camera-usage gateway tests: fake snapshots and loopback HTTP only."""
from __future__ import annotations

from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from deploy.scripts import camera_owner as owner
from deploy.scripts import camera_status_gateway as gate
from tests.test_detections import detection_fixture, marker_fixture, STREAM
from shared.detections import waiting_detections


KEYS = tuple(gate.KEY_PORTS)
MAPPING = {'/dev/video0': 'usbFront', '/dev/video1': 'usbFront',
           '/dev/video2': 'usbLeft', '/dev/video3': 'usbLeft',
           '/dev/video4': 'usbHub2', '/dev/video5': 'usbHub2',
           '/dev/video6': 'usbHub3', '/dev/video7': 'usbHub3'}
CONNECTED = dict.fromkeys(KEYS, True)


@pytest.fixture(autouse=True)
def forbid_real_camera_control(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Real camera/process/service control forbidden in gateway tests')
    monkeypatch.setattr(owner.os, 'kill', forbidden)
    monkeypatch.setattr(owner.subprocess, 'run', forbidden)
    monkeypatch.setattr(owner.subprocess, 'Popen', forbidden)
    for name in ('start_web', 'stop_unit', 'stop_new', 'save_state', 'lock'):
        monkeypatch.setattr(owner.SystemOps, name, forbidden)


def snapshot(*, holders=(), missing=(), errors=(), active=(), unverified=()):
    return owner.Snapshot(units={name: owner.Unit(name, active=name in active,
        verified=name not in unverified) for name in owner.KNOWN_UNITS}, holders=tuple(holders),
        missing=tuple(missing), scan_errors=tuple(errors), partial_visibility=True)


class FakeOps:
    uid = 1000
    settings = owner.Settings()

    def __init__(self, current=None, error=None):
        self.current, self.error, self.calls = current or snapshot(), error, 0

    def snapshot(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.current


def cache(current=None, *, error=None, clock=None, mapping=MAPPING, connected=CONNECTED):
    return gate.UsageCache(FakeOps(current, error), mapper=lambda _: (dict(mapping), dict(connected)),
                           clock=clock or time.monotonic)


def test_initial_unknown_is_not_fresh_idle_and_has_only_three_redacted_fields():
    value = cache().value()
    assert set(value) == set(KEYS)
    for row in value.values():
        assert set(row) == {'owner', 'connected', 'checkedAgeMs'}
        assert row['owner'] == 'UNKNOWN' and row['connected'] is None
        assert row['checkedAgeMs'] > gate.LEASE_MS


def test_no_observed_holder_is_none_without_claiming_all_users_free():
    usage = cache()
    assert usage.probe_once()
    assert usage.ops.calls == 1
    assert all(row['owner'] == 'NONE' and row['connected'] is True for row in usage.value().values())
    assert 'not a guarantee' in gate.__doc__


@pytest.mark.parametrize('unit', tuple(gate.NATIVE_UNITS))
def test_verified_native_descriptor_owner_is_web(unit):
    usage = cache(snapshot(active=(unit,), holders=(owner.Holder(50, 1000, ('/dev/video0',), unit),)))
    usage.probe_once()
    assert usage.value()['usbFront']['owner'] == 'WEB'
    assert usage.value()['usbLeft']['owner'] == 'NONE'


@pytest.mark.parametrize('unit,uid,unverified', [
    (None, 1000, ()), ('calibration-gui.service', 1000, ()),
    (owner.NEW_WEB_UNIT, 0, ()), (owner.CAPTURE_UNITS[1], 1000, ()),
    (owner.NEW_WEB_UNIT, 1000, (owner.NEW_WEB_UNIT,)),
])
def test_other_confirmed_descriptor_holder_is_per_camera_not_all_busy(unit, uid, unverified):
    usage = cache(snapshot(active=(owner.NEW_WEB_UNIT, owner.CAPTURE_UNITS[1]), unverified=unverified,
                            holders=(owner.Holder(991, uid, ('/dev/video5',), unit),)))
    usage.probe_once()
    value = usage.value()
    assert value['usbHub2']['owner'] == 'OTHER'
    assert all(value[key]['owner'] == 'NONE' for key in KEYS if key != 'usbHub2')
    assert all(set(row) == {'owner', 'connected', 'checkedAgeMs'} for row in value.values())
    encoded = json.dumps(value)
    assert all(secret not in encoded for secret in ('/dev/', '.service', 'uid', 'pid'))


def test_other_holder_wins_over_native_holder_on_same_device():
    usage = cache(snapshot(active=(owner.NEW_WEB_UNIT,), holders=(
        owner.Holder(50, 1000, ('/dev/video0',), owner.NEW_WEB_UNIT),
        owner.Holder(90, 1000, ('/dev/video1',), None))))
    usage.probe_once()
    assert usage.value()['usbFront']['owner'] == 'OTHER'


@pytest.mark.parametrize('holder', [owner.Holder(10, -1, tuple(MAPPING), unreadable=True),
                                    owner.Holder(12, 1000, ('/dev/unknown-video',))])
def test_ambiguous_unreadable_holder_never_claims_four_cameras_other(holder):
    usage = cache(snapshot(holders=(holder,)))
    usage.probe_once()
    assert all(row['owner'] == 'UNKNOWN' for row in usage.value().values())


def test_specific_other_holder_remains_confirmed_when_another_holder_is_ambiguous():
    usage = cache(snapshot(holders=(owner.Holder(90, 1000, ('/dev/video0',)),
                                    owner.Holder(10, -1, tuple(MAPPING), unreadable=True))))
    usage.probe_once()
    assert usage.value()['usbFront']['owner'] == 'OTHER'
    assert usage.value()['usbHub3']['owner'] == 'UNKNOWN'


def test_missing_camera_is_not_occupied():
    connected = dict(CONNECTED, usbLeft=False)
    mapping = {node: key for node, key in MAPPING.items() if key != 'usbLeft'}
    usage = cache(snapshot(missing=('2.3',)), mapping=mapping, connected=connected)
    usage.probe_once()
    assert usage.value()['usbLeft']['connected'] is False
    assert usage.value()['usbLeft']['owner'] == 'NONE'


def test_scan_errors_and_probe_exceptions_publish_unknown_without_exception_text():
    for usage in (cache(snapshot(errors=('SECRET /proc/1234/token',))), cache(error=ValueError('SECRET TOKEN'))):
        usage.probe_once()
        assert all(row['owner'] == 'UNKNOWN' and row['connected'] is None for row in usage.value().values())
        assert 'SECRET' not in json.dumps(usage.value())


def test_topology_change_during_snapshot_is_unknown_not_misidentified_holder():
    usage = cache(snapshot(holders=(owner.Holder(44, 1000, ('/dev/video0',)),)))
    mappings = iter([(MAPPING, CONNECTED), ({}, dict.fromkeys(KEYS, False))])
    usage.mapper = lambda _: next(mappings)
    usage.probe_once()
    assert all(row['owner'] == 'UNKNOWN' and row['connected'] is None for row in usage.value().values())


def test_disagreement_of_snapshot_missing_and_sysfs_is_unknown():
    usage = cache(snapshot(missing=('2.4',)))
    usage.probe_once()
    assert usage.value()['usbFront']['owner'] == 'UNKNOWN'
    assert usage.value()['usbFront']['connected'] is None


def test_connected_camera_without_mappable_node_is_unknown():
    usage = cache(mapping={})
    usage.probe_once()
    assert all(row['owner'] == 'UNKNOWN' for row in usage.value().values())


def test_six_second_expiry_does_not_keep_stale_busy_or_stale_connection():
    clock = [10.0]
    usage = cache(snapshot(holders=(owner.Holder(44, 1000, ('/dev/video0',)),)), clock=lambda: clock[0])
    usage.probe_once()
    clock[0] += 5.9
    assert usage.value()['usbFront']['owner'] == 'OTHER'
    clock[0] += .2
    row = usage.value()['usbFront']
    assert row['owner'] == 'UNKNOWN' and row['connected'] is None and row['checkedAgeMs'] >= 6000


def test_probe_age_counts_whole_scan_not_just_publication_time():
    clock = [0.0]
    usage = cache(clock=lambda: clock[0])
    original = usage.ops.snapshot
    def slow_snapshot():
        clock[0] += 7
        return original()
    usage.ops.snapshot = slow_snapshot
    usage.probe_once()
    assert all(row['owner'] == 'UNKNOWN' and row['checkedAgeMs'] == 7000 for row in usage.value().values())


def test_only_one_probe_can_run_and_http_value_does_not_probe():
    usage = cache()
    assert usage._probing.acquire(blocking=False)
    try:
        assert usage.probe_once() is False
        usage.value()
        assert usage.ops.calls == 0
    finally:
        usage._probing.release()


def test_sysfs_mapping_includes_metadata_nodes_without_opening_devices(tmp_path):
    by_path, sysfs, fake_dev = tmp_path / 'by-path', tmp_path / 'sys', tmp_path / 'devices'
    for directory in (by_path, sysfs, fake_dev):
        directory.mkdir()
    for index, (key, port) in enumerate(gate.KEY_PORTS.items()):
        physical = tmp_path / ('1-' + port)
        interface = physical / ('usb-' + port + ':1.0')
        interface.mkdir(parents=True)
        (physical / 'idVendor').write_text('0bda')
        (physical / 'idProduct').write_text('5187' if key in ('usbFront', 'usbLeft') else 'd584')
        for node_index in (index * 2, index * 2 + 1):
            name = 'video' + str(node_index)
            (fake_dev / name).touch()
            node = sysfs / name
            node.mkdir()
            (node / 'device').symlink_to(interface, target_is_directory=True)
        (by_path / ('platform-3610000.usb-usb-0:' + port + ':1.0-video-index0')).symlink_to(fake_dev / ('video' + str(index * 2)))
    settings = owner.Settings(sys_video_root=str(sysfs), dev_by_path_root=str(by_path))
    mapping, connected = gate._node_mapping(settings)
    assert mapping == MAPPING and connected == CONNECTED
    (by_path / 'platform-3610000.usb-usb-0:2.4:1.0-video-index0').unlink()
    mapping, connected = gate._node_mapping(settings)
    assert connected['usbFront'] is False and '/dev/video0' not in mapping


def source_rows():
    return {'cameras': [{'id': identity, 'direction': direction, 'codec': 'avc1.640028',
        'width': 2592 if identity in ('camera1', 'camera2') else 1920,
        'height': 1944 if identity in ('camera1', 'camera2') else 1080,
        'requested_fps': 25, 'actual_fps': 0, 'encoded_fps': 0, 'frames': 0,
        'encoded_frames': 0, 'frame_age_ms': None, 'error': None, 'video_error': None}
        for identity, direction in gate.IDS.items()]}


def test_camera_sanitizer_preserves_zero_null_dimensions_codec_not_fake_frames():
    rows = gate._cameras(json.dumps(source_rows()).encode())
    assert len(rows) == 4
    assert rows[0]['width'] == 2592 and rows[0]['height'] == 1944
    assert rows[0]['actual_fps'] == 0 and rows[0]['frames'] == 0 and rows[0]['frame_age_ms'] is None
    assert gate._cameras(b'{"cameras":[]}') == []


def test_camera_sanitizer_redacts_paths_errors_extra_fields_and_wrong_identity():
    obj = source_rows()
    obj['cameras'][0].update(error='SECRET /home/nvidia/token', video_error='<html>SECRET</html>',
                              password='SECRET', stream='/dev/video0')
    obj['cameras'].append({'id': 'camera5', 'direction': 'front', 'codec': 'avc1.640028'})
    rows = gate._cameras(json.dumps(obj).encode())
    assert len(rows) == 4 and rows[0]['error'] == '相机暂不可用'
    assert rows[0]['video_error'] == '视频暂不可用'
    assert all(text not in json.dumps(rows) for text in ('SECRET', '/dev/', '/home/', 'password'))


@pytest.mark.parametrize('raw', [b'{}', b'[]', b'{"cameras":{},"cameras":[]}',
    b'{"cameras":[{"id":"camera1","direction":"rear","frames":NaN}]}',
    json.dumps({'cameras': source_rows()['cameras'] * 2}).encode(),
    json.dumps({'cameras': [{}] * 33}).encode()])
def test_invalid_or_duplicate_status_is_rejected(raw):
    with pytest.raises((gate.GatewayError, ValueError)):
        gate._cameras(raw)


def test_invalid_nonfinite_boolean_and_huge_camera_numbers_remain_null():
    obj = source_rows()
    obj['cameras'][0].update(width=True, height=-1, actual_fps=241, frames=1.5, encoded_frames=2 ** 53,
                              h264_mbps=10 ** 18, codec='avc1.640028<script>')
    row = gate._cameras(json.dumps(obj).encode())[0]
    assert all(row[field] is None for field in ('width', 'height', 'actual_fps', 'frames', 'encoded_frames', 'h264_mbps', 'codec'))


@pytest.mark.parametrize('number', [b'1' * 100_000, b'1.' + b'1' * 100_000])
def test_source_number_parser_bounds_before_expensive_python_integer_conversion(number):
    with pytest.raises(gate.GatewayError):
        gate._cameras(b'{"cameras":[{"id":"camera1","direction":"rear","frames":' + number + b'}]}')


def mp4():
    def box(kind, body=b''):
        return (len(body) + 8).to_bytes(4, 'big') + kind + body
    return box(b'ftyp', b'isom\x00\x00\x00\x01') + box(b'moov', b'INIT') + box(b'moof', b'FRAME')


@pytest.mark.parametrize('raw', [b'<html>SECRET</html>', b'\x00\x00\x00\x00ftyp',
    (gate.MAX_BODY + 1).to_bytes(4, 'big') + b'ftyp',
    b'\x00\x00\x00\x08moov', b'\x00\x00\x00\x10ftypisom\x00\x00\x00\x01' * 2])
def test_mp4_initialization_rejects_html_unbounded_invalid_and_duplicate_ftyp(raw):
    with pytest.raises(gate.GatewayError):
        gate._mp4_initialization(io.BytesIO(raw))


def test_mp4_initialization_admits_only_ftyp_and_moov_not_first_frame():
    stream = io.BytesIO(mp4())
    initial = gate._mp4_initialization(stream)
    assert initial.endswith(b'moovINIT') and b'FRAME' not in initial
    assert stream.read().endswith(b'FRAME')


class SocketInput:
    def __init__(self, data):
        self.data, self.timeouts = data, []

    def settimeout(self, timeout):
        self.timeouts.append(timeout)

    def recv(self, size):
        result, self.data = self.data[:size], self.data[size:]
        return result


@pytest.mark.parametrize('target,status', [
    ('/status?owner=web', 400), ('/status?', 400), ('/status#x', 400),
    ('http://127.0.0.1:8080/status', 404), ('//status', 404), ('/status/../stop', 404),
    ('/camera/camera5/video.mp4', 404), ('/camera/camera1/video.mp4?url=x', 400),
    ('/start', 404), ('/stop', 404), ('/peer', 404), ('/web', 404), ('/%73tatus', 404),
])
def test_only_exact_targets_are_readable_and_never_owner_actions(target, status):
    with pytest.raises(gate.GatewayError) as error:
        gate._request_path(SocketInput(('GET ' + target + ' HTTP/1.1\r\nHost: test\r\n\r\n').encode()))
    assert error.value.status == status


@pytest.mark.parametrize('method', ['POST', 'PUT', 'DELETE', 'PATCH', 'HEAD', 'CONNECT', 'TRACE', 'OPTIONS'])
def test_all_non_get_methods_rejected(method):
    with pytest.raises(gate.GatewayError) as error:
        gate._request_path(SocketInput((method + ' /status HTTP/1.1\r\n\r\n').encode()))
    assert error.value.status == 405


@pytest.mark.parametrize('headers,body,status', [
    (b'Content-Length: 1\r\n', b'a', 400), (b'Transfer-Encoding: chunked\r\n', b'', 400),
    (b'Host: a\r\nhost: b\r\n', b'', 400), (b'X-Token: \x01\r\n', b'', 400),
    (b'X-Big: ' + b'a' * gate.MAX_REQUEST + b'\r\n', b'', 431),
    (b'', b'GET /status HTTP/1.1\r\n\r\n', 400),
])
def test_invalid_headers_bodies_and_pipelining_rejected(headers, body, status):
    with pytest.raises(gate.GatewayError) as error:
        gate._request_path(SocketInput(b'GET /status HTTP/1.1\r\n' + headers + b'\r\n' + body))
    assert error.value.status == status


def test_request_accepts_only_known_get_paths():
    for path in gate.PATHS:
        assert gate._request_path(SocketInput(('GET ' + path + ' HTTP/1.1\r\n\r\n').encode())) == path


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


@contextmanager
def upstream(mode='normal', *, raw=None, session=None):
    recorded = []
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *args):
            pass

        def do_GET(self):
            recorded.append((self.path, dict(self.headers)))
            body = raw if raw is not None else json.dumps(source_rows()).encode() if self.path == '/status' else mp4()
            self.send_response(302 if mode == 'redirect' else 200)
            self.send_header('Content-Type', 'text/html' if mode == 'html' else
                             'application/json' if self.path == '/status' else 'video/mp4')
            if mode == 'redirect':
                self.send_header('Location', 'http://example.invalid/SECRET')
            if mode == 'encoding':
                self.send_header('Content-Encoding', 'gzip')
            if mode == 'oversize':
                self.send_header('Content-Length', str(gate.MAX_BODY + 1))
            elif mode != 'hold-video' or self.path == '/status':
                self.send_header('Content-Length', str(len(body)))
            self.send_header('Connection', 'close')
            if session is not None:
                self.send_header('X-Video-Session', session)
            self.send_header('X-Private-Token', 'SECRET')
            self.end_headers()
            try:
                if mode == 'drip':
                    for byte in body:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(.02)
                elif mode == 'hold-video' and self.path != '/status':
                    self.wfile.write(body)
                    self.wfile.flush()
                    time.sleep(2)
                else:
                    self.wfile.write(body)
            except (OSError, BrokenPipeError):
                pass
    server = QuietServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    try:
        yield server.server_port, recorded
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@contextmanager
def gateway(source=None, usage=None, detections=None):
    server = gate.GatewayServer(('127.0.0.1', 0), source=source, usage=usage or cache(), detections=detections)
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


def request(server, target='/status', method='GET', headers=None):
    connection = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=2)
    try:
        connection.request(method, target, headers=headers or {})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


@pytest.mark.parametrize('identity', tuple(gate.IDS))
@pytest.mark.parametrize('profile', gate.PREVIEW_PROFILES)
def test_adaptive_profile_path_is_fixed_and_preserves_session_bytes(identity, profile):
    path = f'/camera/{identity}/video-{profile}.mp4'
    with upstream(session=STREAM) as (port, recorded), gateway(gate.PreviewSource(port)) as server:
        status, headers, body = request(server, path, headers={'Cookie': 'SECRET', 'Authorization': 'SECRET'})
        assert status == 200 and body == mp4()
        assert headers['X-Video-Session'] == STREAM
        assert recorded[0][0] == path
        assert 'Cookie' not in recorded[0][1] and 'Authorization' not in recorded[0][1]


@pytest.mark.parametrize('path', [
    '/camera/camera1/video-super.mp4',
    '/camera/camera5/video-balanced.mp4',
    '/camera/camera1/video-balanced.mp4?bitrate=1',
    '/camera/camera1/video-balanced.mp4/../stop',
])
def test_adaptive_profiles_do_not_allow_arbitrary_source_controls(path):
    with pytest.raises(gate.GatewayError):
        gate._request_path(SocketInput(('GET ' + path + ' HTTP/1.1\r\nHost: test\r\n\r\n').encode()))


def test_live_status_and_video_forward_exact_bytes_without_cookie_or_auth_to_source():
    usage = cache(snapshot(holders=(owner.Holder(991, 1000, ('/dev/video5',)),)))
    usage.probe_once()
    with upstream() as (port, recorded), gateway(gate.PreviewSource(port), usage) as server:
        status, headers, body = request(server, headers={'Cookie': 'SECRET', 'Authorization': 'SECRET'})
        assert status == 200 and headers['Cache-Control'] == 'no-store'
        value = json.loads(body)
        assert len(value['cameras']) == 4 and value['cameraUsage']['usbHub2']['owner'] == 'OTHER'
        assert usage.ops.calls == 1, 'An HTTP request must never perform a descriptor scan'
        status, headers, body = request(server, '/camera/camera2/video.mp4', headers={'Cookie': 'SECRET'})
        assert status == 200 and headers['Content-Type'] == 'video/mp4' and body == mp4()
        assert headers['X-Accel-Buffering'] == 'no'
        assert recorded[0][0] == '/status' and recorded[1][0] == '/camera/camera2/video.mp4'
        assert all('cookie' not in {key.lower() for key in row[1]} and 'authorization' not in {
            key.lower() for key in row[1]} for row in recorded)


@pytest.mark.parametrize('session', [STREAM, None, 'SECRET', 'A' * 32, 'a' * 33])
def test_video_session_is_the_only_extra_source_header_forwarded(session):
    with upstream(session=session) as (port, _), gateway(gate.PreviewSource(port)) as server:
        status, headers, body = request(server, '/camera/camera2/video.mp4')
        assert status == 200 and body == mp4()
        assert headers.get('X-Video-Session') == (STREAM if session == STREAM else None)
        assert 'X-Private-Token' not in headers and b'SECRET' not in body


def test_detections_fixed_file_route_never_contacts_capture_or_scans_owners(tmp_path):
    path = tmp_path / 'detections.json'
    value = detection_fixture()
    value['cameras']['color']['results'][0]['markers'] = [dict(marker_fixture(), label='SECRET')]
    value.update(generatedMonotonic=100, token='SECRET')
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    usage = cache()
    with upstream() as (port, recorded), gateway(gate.PreviewSource(port), usage,
            gate.DetectionFile(path, lambda: 100.25)) as server:
        status, headers, raw = request(server, '/detections', headers={'Cookie': 'SECRET', 'Authorization': 'SECRET'})
        assert status == 200 and headers['Cache-Control'] == 'no-store'
        result = json.loads(raw)
        assert result['cameras']['color']['results'][0]['ageMs'] == 400
        assert result['cameras']['color']['results'][0]['markers'] == [marker_fixture()]
        assert b'SECRET' not in raw and b'generatedMonotonic' not in raw
        assert recorded == [] and usage.ops.calls == 0
        for target in ('/detections?file=/SECRET', '/detections?', '/detections#secret'):
            assert request(server, target)[0] == 400
        for method in ('POST', 'HEAD', 'DELETE'):
            assert request(server, '/detections', method=method)[0] == 405
        path.unlink()
        assert json.loads(request(server, '/detections')[2]) == waiting_detections()


def test_source_off_still_exposes_live_usage_with_empty_camera_rows():
    class OffSource:
        def status(self):
            return []
        def close(self):
            pass
    usage = cache(snapshot(holders=(owner.Holder(991, 1000, ('/dev/video0',)),)))
    usage.probe_once()
    with gateway(OffSource(), usage) as server:
        status, _, raw = request(server)
        value = json.loads(raw)
        assert status == 200 and value['cameras'] == [] and value['cameraUsage']['usbFront']['owner'] == 'OTHER'


@pytest.mark.parametrize('mode', ['redirect', 'html', 'encoding', 'oversize'])
def test_invalid_source_status_does_not_leak_diagnostics_and_retains_real_usage(mode):
    usage = cache()
    usage.probe_once()
    with upstream(mode, raw=b'SECRET') as (port, recorded), gateway(gate.PreviewSource(port), usage) as server:
        status, _, raw = request(server)
        value = json.loads(raw)
        assert status == 200 and value['cameras'] == []
        assert value['cameraUsage']['usbFront']['owner'] == 'NONE'
        assert b'SECRET' not in raw and len(recorded) == 1


@pytest.mark.parametrize('mode,raw', [('redirect', None), ('html', None), ('encoding', None), ('normal', b'<html>SECRET</html>')])
def test_bad_video_initialization_has_empty_fixed_error_never_html_frames(mode, raw):
    with upstream(mode, raw=raw) as (port, recorded), gateway(gate.PreviewSource(port)) as server:
        status, headers, body = request(server, '/camera/camera1/video.mp4')
        assert status == 502 and body == b'' and b'SECRET' not in body
        assert headers['Content-Type'].startswith('text/plain')
        assert len(recorded) == 1


def test_status_drip_body_has_absolute_deadline_even_connection_close_detaches_socket(monkeypatch):
    monkeypatch.setattr(gate, 'STATUS_TIMEOUT', .12)
    with upstream('drip') as (port, _):
        source = gate.PreviewSource(port)
        started = time.monotonic()
        assert source.status() == []
        assert time.monotonic() - started < .7
        assert source._connections == {}
        source.close()


def test_status_header_budget_is_enforced():
    wrapper = gate._HeaderBudget(io.BytesIO(b'a' * (gate.MAX_HEADERS + 1) + b'\n'))
    with pytest.raises(gate.GatewayError):
        wrapper.readline()


def test_fixed_server_host_cannot_be_made_public_or_changed_to_client_input():
    with pytest.raises(ValueError):
        gate.GatewayServer(('0.0.0.0', 0), source=SimpleNamespace(), usage=cache())
    with pytest.raises(SystemExit) as result:
        gate.main(['--host', '0.0.0.0'])
    assert result.value.code == 2


def test_gateway_worker_and_video_limits_leave_status_capacity():
    assert gate.MAX_STREAMS == 16 and gate.MAX_WORKERS >= gate.MAX_STREAMS + 4
    with gateway(SimpleNamespace(status=lambda: [], close=lambda: None)) as server:
        acquired = []
        for _ in range(gate.MAX_STREAMS):
            assert server.stream_slots.acquire(blocking=False)
            acquired.append(True)
        try:
            status, _, raw = request(server)
            assert status == 200 and 'cameraUsage' in json.loads(raw)
            status, _, raw = request(server, '/camera/camera1/video.mp4')
            assert status == 503 and raw == b''
        finally:
            for _ in acquired:
                server.stream_slots.release()


def test_worker_budget_rejects_requests_without_spawning_unbounded_threads():
    with gateway(SimpleNamespace(status=lambda: [], close=lambda: None)) as server:
        for _ in range(gate.MAX_WORKERS):
            assert server.slots.acquire(blocking=False)
        try:
            status, _, raw = request(server)
            assert status == 503 and raw == b''
            assert server._requests == set()
        finally:
            for _ in range(gate.MAX_WORKERS):
                server.slots.release()


def test_source_close_closes_detached_response_socket_and_blocks_future_connections():
    with upstream() as (port, _):
        source = gate.PreviewSource(port)
        connection, response, sock = source._open('/camera/camera1/video.mp4', 'video/mp4')
        assert connection.sock is None, 'Connection: close detaches the socket to HTTPResponse'
        assert source._connections[connection] is sock
        assert source._responses[connection] is response
        source.close()
        # Buffered bytes can survive socket.shutdown(), so expecting read() to
        # raise is racy. The source must explicitly close the response object,
        # discard buffered bytes and release its socket file descriptor.
        assert response.isclosed() and response.fp is None
        assert response.read1(1) == b'' and sock.fileno() == -1
        assert source._responses == {} and source._connections == {}
        response.close()
        source._release(connection)
        assert source._connections == {}
        with pytest.raises(gate.GatewayError) as error:
            source._open('/camera/camera2/video.mp4', 'video/mp4')
        assert error.value.status == 503


def test_shutdown_closes_a_detached_response_while_worker_waits_for_network_bytes():
    with upstream('hold-video') as (port, _):
        source = gate.PreviewSource(port)
        connection, response, sock = source._open('/camera/camera1/video.mp4', 'video/mp4')
        # Exhaust available MP4 bytes, then let a worker block on the network.
        assert response.read(len(mp4())) == mp4()
        done, entered = threading.Event(), threading.Event()
        def read_rest():
            entered.set()
            try:
                response.read1(1)
            except (OSError, http.client.HTTPException):
                pass
            finally:
                done.set()
        worker = threading.Thread(target=read_rest, daemon=True)
        worker.start()
        assert entered.wait(.5)
        assert not done.wait(.05), 'The test worker must actually wait for network data before shutdown'
        started = time.monotonic()
        source.close()
        assert time.monotonic() - started < .5 and done.wait(.5)
        worker.join(timeout=.5)
        assert response.isclosed() and sock.fileno() == -1
        assert source._connections == {} and source._responses == {}
        source._release(connection)


def test_no_control_engine_device_open_start_stop_or_signal_calls_in_gateway_source():
    source = Path(gate.__file__).read_text()
    assert 'camera_owner.Engine' not in source
    assert '.start_web(' not in source and '.stop_unit(' not in source and '.save_state(' not in source
    assert '.lock(' not in source and 'os.kill' not in source and 'fuser -k' not in source
    assert 'cv2' not in source and 'VideoCapture' not in source and 'create_publisher' not in source
    assert '.snapshot()' in source and '127.0.0.1' in source
