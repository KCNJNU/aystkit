"""Isolated GET-only HTTP camera adapter fixtures; no robot/device connection."""
from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import json
import math
import time
import uuid
from pathlib import Path
from types import SimpleNamespace as NS

import aiohttp
import cv2
import numpy as np
import pytest
from aiohttp import web
from multidict import CIMultiDict

from collector import http_camera as module
from collector.http_camera import (
    CAMERA_SOURCES, CAMERA_SOURCE_PROFILES, FRESH_SECONDS, HTTP_CAMERA_URL, MAX_HEADER_BYTES,
    MAX_JPEG_BYTES, HttpCameraCollector, PreviewError,
    read_mjpeg_frame, source_metadata,
)
from collector.media import jpeg_dimensions
from shared.protocol import HTTP_CAMERA_KEYS, validate_envelope


def fixture_jpeg(value=0, width=64, height=48):
    ok, encoded = cv2.imencode('.jpg', np.full((height, width, 3), value, dtype=np.uint8))
    assert ok
    return encoded.tobytes()


def receive_fixture(value, key, raw, age, source_hash=None):
    return value.receive(key, raw, age, source_hash or hashlib.sha256(bytes(raw)).hexdigest())


def relay_response(raw=None, age=0, source_hash=None):
    raw = fixture_jpeg() if raw is None else raw
    return web.Response(body=multipart(raw) + b'--frame--\r\n', headers={
        'Content-Type': 'multipart/x-mixed-replace; boundary=frame',
        'X-Preview-Contract': '1', 'X-Source-SHA256': source_hash or hashlib.sha256(raw).hexdigest(),
        'X-Source-Age-S': str(age)})


class CaptureUploader:
    connection_generation = 0

    def __init__(self):
        self.packets = []

    def enqueue(self, kind, topic, payload, stamp, received):
        envelope = {'schema_version': 1, 'robot_id': 'isolated-http-camera',
                    'source_id': 'isolated-http-source', 'session_id': str(uuid.uuid4()),
                    'sequence': len(self.packets) + 1, 'kind': kind, 'topic': topic,
                    'message_timestamp': stamp, 'collector_timestamp': received, 'payload': payload}
        self.packets.append(validate_envelope(envelope))


def adapter(keys=('usbRear',), clock=None, fps=1, profile='legacy3', http_fps=None):
    uploader = CaptureUploader()
    config = NS(http_camera_url=HTTP_CAMERA_URL, media_keys=keys,
                source_id='isolated-http-source', image_fps=fps, http_camera_profile=profile,
                http_camera_fps=http_fps)
    value = HttpCameraCollector(config, uploader, **({'clock': clock} if clock else {}))
    return value, uploader


def multipart(raw, headers=None):
    values = headers if headers is not None else b'Content-Type: image/jpeg\r\nContent-Length: ' + str(len(raw)).encode() + b'\r\n'
    return b'--frame\r\n' + values + b'\r\n' + raw + b'\r\n'


def test_http_metadata_idle_throttling_keeps_changes_and_reconnect_immediate():
    now = [0.0]
    value, uploader = adapter(clock=lambda: now[0])
    value.publish('usbRear', 'WAITING_UPSTREAM', '相机预览连接中断')
    now[0] = 10
    value.publish('usbRear', 'WAITING_UPSTREAM', '相机预览连接中断')
    assert len(uploader.packets) == 1
    value.publish('usbRear', 'WAITING_UPSTREAM', '等待数据')
    assert len(uploader.packets) == 2
    uploader.connection_generation += 1
    value.publish('usbRear', 'WAITING_UPSTREAM', '等待数据')
    assert len(uploader.packets) == 3
    now[0] = 40
    value.publish('usbRear', 'WAITING_UPSTREAM', '等待数据')
    assert len(uploader.packets) == 4
    value.publish('usbRear', 'ERROR', '上游相机预览异常')
    assert len(uploader.packets) == 5


def test_fetch_timings_are_bounded_and_do_not_publish_or_refresh_frames(caplog):
    now = [0.0]
    value, uploader = adapter(clock=lambda: now[0])
    with caplog.at_level('INFO', logger=module.__name__):
        for _ in range(130):
            value._record_hop('usbRear', .08, .3, 'new_frame')
        assert len(value.hops['usbRear']) == 120
        now[0] = 60
        value._record_hop('usbRear', 1.5, None, 'transport_error')
    assert not value.frames and not uploader.packets
    record = next(item for item in caplog.records if 'Camera fetch diagnostics' in item.message)
    report = json.loads(record.message.split('Camera fetch diagnostics ', 1)[1])
    assert report == {'usbRear': {'samples': 120,
                                'outcomes': {'new_frame': 119, 'transport_error': 1},
                                'fetchP95Ms': 80.0, 'fetchMaxMs': 1500.0,
                                'sourceAgeP95Ms': 300.0}}
    assert not value.hops['usbRear']
    assert 'isolated-http-source' not in record.message


def test_http_preview_rate_is_explicit_and_independent_of_ros_image_rate():
    value, _ = adapter(fps=.5, http_fps=2)
    assert value.interval == .5 and value.config.image_fps == .5
    legacy, _ = adapter(fps=.5)
    assert legacy.interval == 2
    capped, _ = adapter(fps=5)
    assert capped.interval == 1


@pytest.mark.parametrize('fps', [True, 0, 2.01, float('nan'), float('inf'), '2'])
def test_http_preview_rate_rejects_invalid_or_unbounded_values(fps):
    with pytest.raises(ValueError, match='FPS'):
        adapter(http_fps=fps)


class FakeContent:
    def __init__(self, raw):
        self.raw, self.offset = raw, 0

    async def readline(self):
        end = self.raw.find(b'\n', self.offset)
        end = len(self.raw) if end < 0 else end + 1
        value, self.offset = self.raw[self.offset:end], end
        return value

    async def readexactly(self, count):
        value = self.raw[self.offset:self.offset + count]
        self.offset += len(value)
        if len(value) != count:
            raise asyncio.IncompleteReadError(value, count)
        return value


def response(raw, content_type='multipart/x-mixed-replace; boundary=frame'):
    return NS(content_type=content_type.split(';')[0], headers={'Content-Type': content_type},
              content=FakeContent(raw))


def test_fixed_mapping_allowlist_no_ros_aliases():
    assert set().union(*CAMERA_SOURCE_PROFILES.values()) == HTTP_CAMERA_KEYS
    assert CAMERA_SOURCES == {'usbRear': ('camera1', '后视相机'),
                              'usbFront': ('camera2', '前视相机'),
                              'usbLeft': ('camera3', '左视相机')}
    value, uploader = adapter(('fisheye', 'color', 'depth', 'usbRear'))
    assert value.keys == ('usbRear',)
    value.publish('usbRear')
    packet = uploader.packets[-1]
    assert packet['topic'] == '/http_camera/camera1'
    assert packet['payload']['status'] == 'WAITING_UPSTREAM'
    assert packet['payload']['sourcePublisherCount'] is None
    assert packet['message_timestamp'] is None
    assert 'HTTP' in packet['payload']['note']
    with pytest.raises(ValueError, match='not enabled'):
        receive_fixture(value, 'fisheye', fixture_jpeg(), 0)


@pytest.mark.parametrize('profile,expected', [
    ('jetson-hub4', {'usbFront': ('camera2', '后视相机'), 'usbLeft': ('camera1', '前视相机'),
                     'usbHub2': ('camera4', '左视相机'),
                     'usbHub3': ('camera3', '右视相机')}),
    ('jetson-direct3', {'usbFront': ('camera2', '前视相机'), 'usbLeft': ('camera1', '左视相机'),
                        'usbPort21': ('camera3', '扩展相机 · 接口 2.1')}),
])
def test_profiles_preserve_verified_ports_without_reusing_rear_or_hub_identity(profile, expected):
    assert CAMERA_SOURCE_PROFILES[profile] == expected
    value, uploader = adapter(tuple(expected), profile=profile)
    for key, (endpoint, label) in expected.items():
        assert receive_fixture(value, key, fixture_jpeg(), 0)
        packet = uploader.packets[-1]
        assert packet['topic'] == '/http_camera/' + endpoint
        assert packet['payload']['key'] == key and packet['payload']['label'] == label
        assert packet['payload']['status'] == 'AVAILABLE'
        assert packet['message_timestamp'] is None
    assert not any(packet['payload']['key'] == 'usbRear' for packet in uploader.packets)
    assert {packet['payload']['key'] for packet in uploader.packets} == set(expected)


@pytest.mark.parametrize('profile,keys', [('legacy3', ('usbHub2',)), ('legacy3', ('usbHub3',)),
                                         ('legacy3', ('usbPort21',)), ('jetson-hub4', ('usbRear',)),
                                         ('jetson-hub4', ('usbPort21',)), ('jetson-direct3', ('usbRear',)),
                                         ('jetson-direct3', ('usbHub2',)), ('jetson-direct3', ('usbHub3',)),
                                         ('unknown', ('usbFront',))])
def test_adapter_rejects_cross_profile_keys_even_with_direct_configuration(profile, keys):
    with pytest.raises(ValueError, match='profile'):
        adapter(keys, profile=profile)


@pytest.mark.parametrize('url', ['http://10.48.106.37:8080', 'https://127.0.0.1:18080',
                                'http://127.0.0.1:18080/', 'http://user:pass@127.0.0.1:18080',
                                'http://localhost:18080', 'http://127.0.0.1:18080?url=remote'])
def test_arbitrary_source_urls_rejected(url):
    with pytest.raises(ValueError, match='fixed loopback'):
        HttpCameraCollector(NS(http_camera_url=url), CaptureUploader())


def test_fps_is_capped_and_frame_count_timestamp_only_advance_on_change():
    now = [10.0]
    value, uploader = adapter(clock=lambda: now[0], fps=5)
    assert value.interval == 1
    raw = fixture_jpeg()
    assert receive_fixture(value, 'usbRear', raw, .1)
    first = uploader.packets[-1]['payload']
    assert first['status'] == 'AVAILABLE' and first['frameSeq'] == 1
    assert first['message_timestamp'] is None and first['hz'] is None
    first_received = first['received_timestamp']
    now[0] += 1
    assert not receive_fixture(value, 'usbRear', raw, .01)
    assert len(uploader.packets) == 1
    assert value._metadata('usbRear')['received_timestamp'] == first_received
    now[0] += 1.1
    assert not receive_fixture(value, 'usbRear', raw, .01)
    stale = uploader.packets[-1]['payload']
    assert stale['status'] == 'STALE' and 'data' not in stale
    assert stale['frameSeq'] == 1 and stale['receivedFrames'] == 1
    assert stale['received_timestamp'] == first_received and stale['ageS'] > 2
    now[0] += .1
    assert receive_fixture(value, 'usbRear', fixture_jpeg(90), .1)
    recovered = uploader.packets[-1]['payload']
    assert recovered['status'] == 'AVAILABLE' and recovered['frameSeq'] == 2
    assert recovered['previewHz'] == pytest.approx(1 / 2.2)


def test_identical_frames_independent_per_camera_and_reconnect_keeps_identity():
    now = [10.0]
    value, uploader = adapter(('usbFront', 'usbRear', 'usbLeft'), clock=lambda: now[0])
    raw = fixture_jpeg(90)
    for key in value.keys:
        assert receive_fixture(value, key, raw, 0)
    assert len(uploader.packets) == 3
    uploader.connection_generation += 1
    now[0] += .1
    assert not receive_fixture(value, 'usbFront', raw, .01)
    assert len(uploader.packets) == 4
    assert uploader.packets[-1]['payload']['frameSeq'] == 1
    assert uploader.packets[-1]['payload']['ageS'] == pytest.approx(.1)


def test_resize_preserves_actual_dimensions_and_upload_is_bounded():
    value, uploader = adapter()
    assert receive_fixture(value, 'usbRear', fixture_jpeg(width=2048, height=1200), .01)
    item = uploader.packets[-1]['payload']
    assert (item['width'], item['height']) == (2048, 1200)
    assert item['previewWidth'] <= 960 and item['previewHeight'] <= 720
    raw = base64.b64decode(item['data'])
    assert len(raw) <= MAX_JPEG_BYTES
    assert jpeg_dimensions(raw) == (item['previewWidth'], item['previewHeight'])


@pytest.mark.parametrize('age', [None, '0', True, -1, math.nan, math.inf])
def test_invalid_age_is_not_real_data(age):
    value, _ = adapter()
    with pytest.raises(PreviewError):
        receive_fixture(value, 'usbRear', fixture_jpeg(), age)
    assert not value.frames


def test_zero_age_real_and_stale_input_not_encoded():
    fresh, _ = adapter()
    assert receive_fixture(fresh, 'usbRear', fixture_jpeg(), 0)
    value, uploader = adapter()
    assert not receive_fixture(value, 'usbRear', fixture_jpeg(), FRESH_SECONDS + .1)
    assert not value.frames
    assert uploader.packets[-1]['payload']['status'] == 'STALE'
    assert 'data' not in uploader.packets[-1]['payload']


@pytest.mark.parametrize('raw', [b'', b'x' * 15, b'x' * (MAX_JPEG_BYTES + 1), bytearray(b'x' * 30)])
def test_direct_receive_rejects_unbounded_or_non_bytes_input(raw):
    value, _ = adapter()
    with pytest.raises(PreviewError):
        receive_fixture(value, 'usbRear', raw, 0)
    assert not value.frames


def test_multipart_reads_one_part_and_does_not_wait_for_stream_end():
    raw = fixture_jpeg()
    response_obj = response(multipart(raw) + multipart(fixture_jpeg(90)))
    assert asyncio.run(read_mjpeg_frame(response_obj)) == raw
    assert response_obj.content.offset < len(response_obj.content.raw)


@pytest.mark.parametrize('headers', [
    b'Content-Type: image/jpeg\r\nContent-Length: 500001\r\n',
    b'Content-Type: image/jpeg\r\nContent-Length: -1\r\n',
    b'Content-Type: image/png\r\nContent-Length: 100\r\n',
    b'Content-Type: image/jpeg\r\n',
    b'Content-Type: image/jpeg\r\nContent-Length: 100\r\nContent-Length: 100\r\n',
    b'X-Oversized: ' + b'a' * MAX_HEADER_BYTES + b'\r\n',
    b'BadHeader\r\n',
])
def test_bad_multipart_headers_rejected(headers):
    with pytest.raises(PreviewError):
        asyncio.run(read_mjpeg_frame(response(multipart(fixture_jpeg(), headers))))


@pytest.mark.parametrize('content_type', ['image/jpeg', 'multipart/x-mixed-replace',
                                         'multipart/x-mixed-replace; boundary=unexpected'])
def test_multipart_boundary_and_type_are_fixed(content_type):
    with pytest.raises(PreviewError):
        asyncio.run(read_mjpeg_frame(response(multipart(fixture_jpeg()), content_type)))


def test_multipart_truncation_and_initial_boundary_rejected():
    with pytest.raises(asyncio.IncompleteReadError):
        asyncio.run(read_mjpeg_frame(response(multipart(fixture_jpeg())[:-20])))
    with pytest.raises(PreviewError):
        asyncio.run(read_mjpeg_frame(response(b'wrong-boundary\r\n' + multipart(fixture_jpeg()))))


async def serve(handler):
    app = web.Application()
    app.router.add_route('*', '/{tail:.*}', handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    return runner, 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])


def test_real_http_get_only_redirect_error_stale_duplicate_and_recovery(monkeypatch):
    async def scenario():
        mode, raw = ['valid'], [fixture_jpeg()]
        calls = []
        async def handler(request):
            calls.append((request.method, request.path))
            assert request.method == 'GET'
            assert 'Authorization' not in request.headers
            if mode[0] == 'redirect':
                return web.Response(status=302, headers={'Location': '/must-not-follow'})
            assert request.path == '/stream/camera1'
            if mode[0] == 'oversize':
                return relay_response(b'x' * (MAX_JPEG_BYTES + 1))
            if mode[0] == 'bad-jpeg':
                return relay_response(b'x' * 30)
            return relay_response(raw[0], age=3 if mode[0] == 'old' else 0)
        runner, origin = await serve(handler)
        now = [10.0]
        value, uploader = adapter(clock=lambda: now[0])
        # Test-local replacement only, after fixed production URL validation.
        monkeypatch.setattr(module, 'HTTP_CAMERA_URL', origin)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1), trust_env=False) as session:
                await value.poll(session)
                assert uploader.packets[-1]['payload']['status'] == 'AVAILABLE'
                assert uploader.packets[-1]['payload']['frameSeq'] == 1
                now[0] += 2.1
                await value.poll(session)
                assert uploader.packets[-1]['payload']['status'] == 'STALE'
                assert 'data' not in uploader.packets[-1]['payload']
                mode[0] = 'redirect'
                await value.poll(session)
                assert uploader.packets[-1]['payload']['status'] == 'ERROR'
                assert '/must-not-follow' not in {path for _, path in calls}
                mode[0] = 'oversize'
                await value.poll(session)
                assert uploader.packets[-1]['payload']['status'] == 'ERROR'
                mode[0] = 'old'
                before = len(calls)
                await value.poll(session)
                assert calls[before:] == [('GET', '/stream/camera1')]
                assert uploader.packets[-1]['payload']['status'] == 'STALE'
                mode[0] = 'bad-jpeg'
                await value.poll(session)
                assert uploader.packets[-1]['payload']['status'] == 'ERROR'
                mode[0], raw[0] = 'valid', fixture_jpeg(90)
                await value.poll(session)
                assert uploader.packets[-1]['payload']['status'] == 'AVAILABLE'
                assert uploader.packets[-1]['payload']['frameSeq'] == 2
        finally:
            await runner.cleanup()
    asyncio.run(scenario())


def test_finite_preview_drains_delayed_tail_and_reuses_one_connection(monkeypatch):
    async def scenario():
        transports = []
        async def handler(request):
            transports.append(request.transport)
            raw = fixture_jpeg(len(transports) * 30)
            body = multipart(raw) + b'--frame--\r\n'
            response_obj = web.StreamResponse(headers={
                'Content-Type': 'multipart/x-mixed-replace; boundary=frame',
                'Content-Length': str(len(body)), 'X-Preview-Contract': '1',
                'X-Source-SHA256': hashlib.sha256(raw).hexdigest(), 'X-Source-Age-S': '0.1'})
            await response_obj.prepare(request)
            await response_obj.write(body[:-13])
            await asyncio.sleep(.03)
            await response_obj.write(body[-13:])
            await response_obj.write_eof()
            return response_obj
        runner, origin = await serve(handler)
        value, uploader = adapter()
        monkeypatch.setattr(module, 'HTTP_CAMERA_URL', origin)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1),
                                             connector=aiohttp.TCPConnector(limit=1), trust_env=False) as session:
                await value.poll(session)
                await value.poll(session)
            assert len(transports) == 2 and transports[0] is transports[1]
            assert uploader.packets[-1]['payload']['frameSeq'] == 2
            assert uploader.packets[-1]['payload']['status'] == 'AVAILABLE'
        finally:
            await runner.cleanup()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['bad-tail', 'extra-data', 'chunked'])
def test_private_finite_preview_rejects_bad_terminator_extra_data_and_chunked(monkeypatch, mode):
    async def scenario():
        raw = fixture_jpeg()
        async def handler(request):
            body = multipart(raw) + b'--frame--\r\n'
            headers = {'Content-Type': 'multipart/x-mixed-replace; boundary=frame',
                       'X-Preview-Contract': '1', 'X-Source-SHA256': hashlib.sha256(raw).hexdigest(),
                       'X-Source-Age-S': '0.1'}
            if mode == 'bad-tail':
                body = body[:-2] + b'xx'
            elif mode == 'extra-data':
                body += b'extra'
            if mode == 'chunked':
                response_obj = web.StreamResponse(headers=headers)
                response_obj.enable_chunked_encoding()
                await response_obj.prepare(request)
                await response_obj.write(body)
                await response_obj.write_eof()
                return response_obj
            return web.Response(body=body, headers=headers)
        runner, origin = await serve(handler)
        value, uploader = adapter()
        monkeypatch.setattr(module, 'HTTP_CAMERA_URL', origin)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1), trust_env=False) as session:
                await value.poll(session)
            assert uploader.packets[-1]['payload']['status'] == 'ERROR'
            assert not value.frames
        finally:
            await runner.cleanup()
    asyncio.run(scenario())


def test_timeout_preserves_fresh_frame_but_stop_invalidates_it(monkeypatch):
    async def scenario():
        async def handler(request):
            await asyncio.sleep(.1)
            return web.json_response({'camera1': {'error': '', 'age_s': 0}})
        runner, origin = await serve(handler)
        value, uploader = adapter()
        receive_fixture(value, 'usbRear', fixture_jpeg(), 0)
        monkeypatch.setattr(module, 'HTTP_CAMERA_URL', origin)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=.02)) as session:
                await value.poll(session)
            assert len(uploader.packets) == 1
            assert uploader.packets[-1]['payload']['status'] == 'AVAILABLE'
            value.start()
            started = time.monotonic()
            value.stop()
            assert time.monotonic() - started < 3.2
            assert not value.thread.is_alive()
            assert uploader.packets[-1]['payload']['status'] == 'STALE'
        finally:
            await runner.cleanup()
    asyncio.run(scenario())


def test_three_cameras_fail_independently_and_stream_redirect_not_followed(monkeypatch):
    async def scenario():
        calls = []
        async def handler(request):
            calls.append(request.path)
            if request.path == '/stream/camera2':
                return web.Response(status=307, headers={'Location': '/must-not-follow'})
            assert request.path in ('/stream/camera1', '/stream/camera3')
            return relay_response()
        runner, origin = await serve(handler)
        value, uploader = adapter(('usbRear', 'usbFront', 'usbLeft'))
        monkeypatch.setattr(module, 'HTTP_CAMERA_URL', origin)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1), trust_env=False) as session:
                await value.poll(session)
            values = {packet['payload']['key']: packet['payload'] for packet in uploader.packets}
            assert values['usbRear']['status'] == values['usbLeft']['status'] == 'AVAILABLE'
            assert values['usbFront']['status'] == 'ERROR' and 'data' not in values['usbFront']
            assert '/must-not-follow' not in calls
        finally:
            await runner.cleanup()
    asyncio.run(scenario())


def test_single_get_transport_time_counts_toward_bound_source_age(monkeypatch):
    async def scenario():
        now, calls = [10.0], []
        async def handler(request):
            calls.append(request.path)
            now[0] += .5  # Isolated simulated complete single GET transit delay.
            return relay_response(age=1.6)
        runner, origin = await serve(handler)
        value, uploader = adapter(clock=lambda: now[0])
        monkeypatch.setattr(module, 'HTTP_CAMERA_URL', origin)
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1)) as session:
                await value.poll(session)
            assert calls == ['/stream/camera1']
            assert uploader.packets[-1]['payload']['status'] == 'STALE'
            assert not value.frames
        finally:
            await runner.cleanup()
    asyncio.run(scenario())


def test_adapter_source_has_no_device_ros_control_or_arbitrary_http_methods():
    source = Path(module.__file__).read_text()
    tree = ast.parse(source)
    forbidden = {'post', 'put', 'patch', 'delete', 'request', 'VideoCapture',
                 'create_publisher', 'create_client', 'ActionClient', 'Serial'}
    calls = {node.func.attr for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)}
    assert not calls & forbidden
    assert '/dev/' not in source and 'import rclpy' not in source
    assert 'allow_redirects=False' in source and 'trust_env=False' in source
    assert 'auto_decompress=False' in source


def valid_contract():
    return CIMultiDict({'X-Preview-Contract': '1', 'X-Source-SHA256': 'a' * 64,
                        'X-Source-Age-S': '0'})


@pytest.mark.parametrize('field,bad', [
    ('X-Preview-Contract', ''), ('X-Preview-Contract', '2'),
    ('X-Source-SHA256', 'A' * 64), ('X-Source-SHA256', 'a' * 63), ('X-Source-SHA256', 'g' * 64),
    ('X-Source-Age-S', ''), ('X-Source-Age-S', 'NaN'), ('X-Source-Age-S', 'Infinity'),
    ('X-Source-Age-S', '-1'), ('X-Source-Age-S', 'true'), ('X-Source-Age-S', '0 '),
    ('X-Source-Age-S', '1_0'), ('X-Source-Age-S', '1e999'), ('X-Source-Age-S', '9' * 33),
])
def test_private_contract_rejects_invalid_metadata(field, bad):
    headers = valid_contract()
    headers[field] = bad
    with pytest.raises(PreviewError):
        source_metadata(headers)


@pytest.mark.parametrize('field', ['X-Preview-Contract', 'X-Source-SHA256', 'X-Source-Age-S'])
def test_private_contract_requires_exactly_one_of_each_header(field):
    headers = valid_contract()
    headers.add(field.lower(), headers[field])
    with pytest.raises(PreviewError):
        source_metadata(headers)
    del headers[field]
    with pytest.raises(PreviewError):
        source_metadata(headers)
    assert source_metadata(valid_contract()) == ('a' * 64, 0)


def test_new_original_hash_advances_even_when_preview_bytes_are_identical():
    now = [10.0]
    value, uploader = adapter(clock=lambda: now[0])
    raw = fixture_jpeg()
    assert value.receive('usbRear', raw, .1, 'a' * 64)
    now[0] += .5
    assert value.receive('usbRear', raw, .1, 'b' * 64)
    assert uploader.packets[-1]['payload']['frameSeq'] == 2
    assert uploader.packets[-1]['message_timestamp'] is None
    assert 'source_hash' not in uploader.packets[-1]['payload']
    assert uploader.packets[-1]['payload']['sourceHz'] is None


def test_same_original_hash_never_refreshes_age_even_if_encoded_bytes_change():
    now = [10.0]
    value, uploader = adapter(clock=lambda: now[0])
    assert value.receive('usbRear', fixture_jpeg(), .1, 'a' * 64)
    observed = value.frames['usbRear']['observed_at']
    received = value.frames['usbRear']['metadata']['received_timestamp']
    now[0] += .7
    assert not value.receive('usbRear', fixture_jpeg(90), .01, 'a' * 64)
    assert len(uploader.packets) == 1
    assert value.frames['usbRear']['observed_at'] == observed
    assert value.frames['usbRear']['metadata']['received_timestamp'] == received
    assert value._metadata('usbRear')['ageS'] == pytest.approx(.8)
    now[0] += 1.31
    assert not value.receive('usbRear', fixture_jpeg(150), .01, 'a' * 64)
    assert uploader.packets[-1]['payload']['status'] == 'STALE'
    assert uploader.packets[-1]['payload']['frameSeq'] == 1


def test_small_relay_jpeg_is_decoded_but_not_reencoded(monkeypatch):
    raw = fixture_jpeg(width=640, height=480)
    decode = cv2.imdecode
    calls = []
    def observed(*args):
        calls.append('decode')
        return decode(*args)
    def forbidden(*args):
        pytest.fail('Validated relay JPEG must not be re-encoded at quality 70')
    monkeypatch.setattr(cv2, 'imdecode', observed)
    monkeypatch.setattr(cv2, 'imencode', forbidden)
    value, uploader = adapter()
    assert value.receive('usbRear', raw, 0, 'a' * 64)
    assert calls == ['decode']
    assert base64.b64decode(uploader.packets[-1]['payload']['data']) == raw
    with pytest.raises(ValueError):
        value.receive('usbRear', b'not-a-valid-jpeg-image', 0, 'a' * 64)


@pytest.mark.parametrize('status', [502, 503, 504])
def test_gateway_retry_keeps_only_genuinely_fresh_frame_and_never_renews_it(monkeypatch, status):
    async def scenario():
        async def handler(request):
            assert request.path == '/stream/camera1'
            return web.Response(status=status)
        runner, origin = await serve(handler)
        now = [10.0]
        value, uploader = adapter(clock=lambda: now[0])
        value.receive('usbRear', fixture_jpeg(), .1, 'a' * 64)
        received = value.frames['usbRear']['metadata']['received_timestamp']
        monkeypatch.setattr(module, 'HTTP_CAMERA_URL', origin)
        try:
            async with aiohttp.ClientSession() as session:
                now[0] += .3
                await value.poll(session)
                assert len(uploader.packets) == 1
                assert value._metadata('usbRear')['ageS'] == pytest.approx(.4)
                assert value.frames['usbRear']['metadata']['received_timestamp'] == received
                now[0] = 12.001
                await value.poll(session)
                assert uploader.packets[-1]['payload']['status'] == 'STALE'
                assert uploader.packets[-1]['payload']['frameSeq'] == 1
                assert 'data' not in uploader.packets[-1]['payload']
        finally:
            await runner.cleanup()
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['explicit-source-error', 'metadata', 'bad-jpeg'])
def test_explicit_invalid_data_immediately_invalidates_fresh_cache(monkeypatch, mode):
    async def scenario():
        retry = [False]
        async def handler(request):
            if retry[0]:
                return web.Response(status=503)
            if mode == 'explicit-source-error':
                return web.Response(status=422)
            if mode == 'metadata':
                return web.Response(body=multipart(fixture_jpeg()),
                                    headers={'Content-Type': 'multipart/x-mixed-replace; boundary=frame'})
            return relay_response(b'not-a-valid-jpeg-image', source_hash='a' * 64)
        runner, origin = await serve(handler)
        now = [10.0]
        value, uploader = adapter(clock=lambda: now[0])
        value.receive('usbRear', fixture_jpeg(), .1, 'a' * 64)
        monkeypatch.setattr(module, 'HTTP_CAMERA_URL', origin)
        try:
            async with aiohttp.ClientSession() as session:
                now[0] += .3
                await value.poll(session)
                assert uploader.packets[-1]['payload']['status'] == 'ERROR'
                assert 'data' not in uploader.packets[-1]['payload']
                before = len(uploader.packets)
                retry[0] = True
                await value.poll(session)
                assert len(uploader.packets) == before  # A retry cannot revive an explicit invalidation.
        finally:
            await runner.cleanup()
    asyncio.run(scenario())


def test_local_decode_time_counts_without_extending_two_second_limit(monkeypatch):
    now = [10.0]
    value, uploader = adapter(clock=lambda: now[0])
    raw = fixture_jpeg()
    preview = module.preview_jpeg
    def delayed(data):
        result = preview(data)
        now[0] += .2
        return result
    monkeypatch.setattr(module, 'preview_jpeg', delayed)
    assert not value.receive('usbRear', raw, 1.9, 'a' * 64)
    assert uploader.packets[-1]['payload']['status'] == 'STALE' and not value.frames


@pytest.mark.parametrize('profile,keys', [
    ('legacy3', ('usbRear', 'usbFront', 'usbLeft')),
    ('jetson-hub4', ('usbFront', 'usbLeft', 'usbHub2', 'usbHub3')),
    ('jetson-direct3', ('usbFront', 'usbLeft', 'usbPort21')),
])
def test_independent_loops_stagger_start_and_slow_route_does_not_block_healthy_routes(monkeypatch, profile, keys):
    async def scenario():
        value, _ = adapter(keys, profile=profile)
        value.interval = .08  # Compress isolated scheduling time; production minimum remains 1 second.
        calls = {key: [] for key in value.keys}
        started = time.monotonic()
        async def camera(session, key):
            calls[key].append(time.monotonic() - started)
            await asyncio.sleep(.28 if key == keys[0] else .002)
        monkeypatch.setattr(value, '_camera', camera)
        task = asyncio.create_task(value._run())
        await asyncio.sleep(.29)
        value.stopping.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        assert len(calls[keys[0]]) == 1
        assert all(len(calls[key]) >= 3 for key in keys[1:])
        first = [calls[key][0] for key in value.keys]
        assert all(a < b for a, b in zip(first, first[1:])) and first[-1] < .15
        for key in keys[1:]:
            assert min(b - a for a, b in zip(calls[key], calls[key][1:])) >= .07
    asyncio.run(scenario())


@pytest.mark.parametrize('profile,healthy', [('jetson-hub4', {'usbHub2', 'usbHub3'}),
                                          ('jetson-direct3', {'usbPort21'})])
def test_runtime_has_independent_connections_and_only_healthy_ports_publish_frames(monkeypatch, profile, healthy):
    async def scenario():
        keys = tuple(CAMERA_SOURCE_PROFILES[profile])
        calls, gate = [], asyncio.Event()
        async def handler(request):
            calls.append(request.path)
            if len(calls) == len(keys):
                gate.set()
            await asyncio.wait_for(gate.wait(), .6)
            if request.path in ('/stream/camera1', '/stream/camera2'):
                return web.Response(status=422)
            return relay_response()
        runner, origin = await serve(handler)
        value, uploader = adapter(keys, profile=profile)
        value.interval = .04  # Compress isolated start staggering only.
        async def once(session, key, phase):
            assert session.connector.limit == len(keys)
            await asyncio.sleep(phase)
            await value._camera(session, key)
        monkeypatch.setattr(value, '_camera_loop', once)
        monkeypatch.setattr(module, 'HTTP_CAMERA_URL', origin)
        try:
            await asyncio.wait_for(value._run(), 1)
            assert set(calls) == {'/stream/' + camera for camera, _ in CAMERA_SOURCE_PROFILES[profile].values()}
            values = {packet['payload']['key']: packet['payload'] for packet in uploader.packets}
            assert set(values) == set(keys) and 'usbRear' not in values
            assert {key for key, item in values.items() if item['status'] == 'AVAILABLE'} == healthy
            assert values['usbFront']['status'] == values['usbLeft']['status'] == 'ERROR'
            assert 'data' not in values['usbFront'] and 'data' not in values['usbLeft']
        finally:
            await runner.cleanup()
    asyncio.run(scenario())
