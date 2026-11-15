"""Isolated Unix-socket upstream; fixtures never reach a device or production."""
import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from aiohttp import CookieJar, ClientPayloadError, web
from aiohttp.test_utils import TestClient, TestServer

from cloud.backend.app import SESSIONS, create_app
from cloud.backend.native_video import (CAMERA_USAGE_TTL_MS, CHUNK_BYTES, KEYS, MAX_BODY, NATIVE_VIDEO,
                                        sanitize_camera_usage, sanitize_status, socket_path, split_socket_option)
from tests.test_backend import PASSWORD, ORIGIN, config


def box(kind, body=b''):
    return (len(body) + 8).to_bytes(4, 'big') + kind + body


INITIAL = box(b'ftyp', b'isom\0\0\0\0isomiso2') + box(b'moov')
def fragment(keyframe=True, marker=b'ISOLATED_NOT_A_REAL_ROBOT_VIDEO', decode_time=0):
    tfhd = box(b'tfhd', b'\0\x02\0\x20' + (1).to_bytes(4, 'big') +
               (0x01010000).to_bytes(4, 'big'))
    tfdt = box(b'tfdt', b'\0' * 4 + decode_time.to_bytes(4, 'big'))
    flags = 0x02000000 if keyframe else 0x01010000
    trun = box(b'trun', b'\0\0\0\x04' + (1).to_bytes(4, 'big') + flags.to_bytes(4, 'big'))
    return box(b'moof', box(b'traf', tfhd + tfdt + trun)) + box(b'mdat', marker)


FRAGMENT = fragment()


def status_fixture():
    return {'cameras': [dict(id='camera' + str(identity), direction=direction,
        codec='avc1.424032', width=1920, height=1080, requested_fps=30,
        actual_fps=25, encoded_fps=24, h264_mbps=2, frame_age_ms=0,
        frames=0, encoded_frames=0,
        error=None, video_error=None, secret='TEST_ONLY_MUST_NOT_PASS')
        for identity, direction in ((2, 'rear'), (1, 'front'), (3, 'right'), (4, 'left'))],
        'upstream_private_field': 'TEST_ONLY_MUST_NOT_PASS'}


def usage_fixture(owner='WEB', connected=True, age=0):
    return {key: {'owner': owner, 'connected': connected, 'checkedAgeMs': age} for key in KEYS}


class NativeVideoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='isolated-native-video-')
        self.socket = self.temp.name + '/preview.sock'
        self.mode, self.keep_streaming = 'finite', False
        self.status_data = status_fixture()
        self.initial = INITIAL
        self.seen = []
        self.seen_sockets = []
        self.status_code = 200
        upstream = web.Application()
        upstream.add_routes([web.get('/status', self.upstream_status),
                             web.get('/camera/{identity}/video.mp4', self.upstream_video)])
        self.runner = web.AppRunner(upstream, access_log=None)
        await self.runner.setup()
        await web.UnixSite(self.runner, self.socket).start()
        self.app, self.client = await self.new_client()
        self.responses = []
        self.extra_clients = []

    async def new_client(self, path=None, max_playing=4, split_sockets=False):
        with patch.dict(os.environ, {'NATIVE_VIDEO_SOCKET': self.socket if path is None else path,
                                     'NATIVE_VIDEO_MAX_PLAYING': str(max_playing),
                                     'NATIVE_VIDEO_SPLIT_SOCKETS': 'true' if split_sockets else 'false'}):
            app = create_app(config(self.temp.name))
        client = TestClient(TestServer(app), cookie_jar=CookieJar(unsafe=True))
        await client.start_server()
        return app, client

    async def asyncTearDown(self):
        for response in self.responses:
            response.close()
        for client in self.extra_clients:
            await client.close()
        await self.client.close()
        await self.runner.cleanup()
        self.temp.cleanup()

    async def login(self, client=None):
        response = await (client or self.client).post('/api/login', json={'username': 'viewer', 'password': PASSWORD},
                                                     headers={'Origin': ORIGIN})
        self.assertEqual(response.status, 200, await response.text())
        return response.cookies['observer_session'].value

    async def upstream_status(self, request):
        self.seen.append((request.path_qs, dict(request.headers)))
        self.seen_sockets.append((request.path_qs, request.transport.get_extra_info('sockname')))
        if self.status_code == 302:
            return web.Response(status=302, headers={'Location': 'http://unsafe.invalid/private'})
        if self.status_code != 200:
            return web.Response(status=self.status_code, text='TEST_ONLY_PRIVATE_UPSTREAM_ERROR')
        if self.mode == 'large-status':
            return web.Response(body=b'x' * (MAX_BODY + 1), content_type='application/json')
        return web.json_response(self.status_data)

    async def upstream_video(self, request):
        self.seen.append((request.path_qs, dict(request.headers)))
        self.seen_sockets.append((request.path_qs, request.transport.get_extra_info('sockname')))
        if self.mode == 'redirect':
            return web.Response(status=302, headers={'Location': 'http://unsafe.invalid/private'})
        if self.mode == 'upstream-error':
            return web.Response(status=500, text='TEST_ONLY_PRIVATE_UPSTREAM_ERROR')
        if self.mode == 'wrong-type':
            return web.Response(body=INITIAL, content_type='image/jpeg')
        response = web.StreamResponse(headers={'Content-Type': 'video/mp4', 'Set-Cookie': 'TEST_ONLY_BAD_COOKIE=x'})
        await response.prepare(request)
        try:
            await response.write(self.initial)
            await response.write(FRAGMENT)
            while self.keep_streaming and not request.transport.is_closing():
                await asyncio.sleep(.05)
                await response.write(FRAGMENT)
            await response.write_eof()
        except (ConnectionError, RuntimeError, asyncio.CancelledError):
            pass
        return response

    async def stream(self, key='usbFront', client=None, **kwargs):
        response = await (client or self.client).get('/native-camera/' + key + '/video.mp4', **kwargs)
        self.responses.append(response)
        return response

    async def ended(self, response):
        try:
            await asyncio.wait_for(response.read(), 2)
        except ClientPayloadError:
            pass  # An interrupted stream is closed, never converted into JSON.

    async def test_auth_precedes_upstream_and_device_token_does_not_view(self):
        for path in ('/native-camera/status', '/native-camera/usbFront/video.mp4'):
            response = await self.client.get(path, headers={'Authorization': 'Bearer TEST_ONLY_NOT_A_VIEWER'})
            self.assertEqual(response.status, 401)
        self.assertEqual(self.seen, [])

    async def test_disabled_is_explicit_and_never_falls_back_to_jpeg(self):
        app, client = await self.new_client(path='')
        self.extra_clients.append(client)
        await self.login(client)
        value = await (await client.get('/native-camera/status')).json()
        self.assertEqual(value, {'enabled': False, 'cameras': {}, 'maxPlaying': 4})
        self.assertEqual((await self.stream(client=client)).status, 503)
        self.assertEqual(self.seen, [])
        self.assertIsNone(app[NATIVE_VIDEO].client)

    async def test_status_mapping_fields_and_true_null_zero(self):
        self.status_data['cameras'][0].update(actual_fps=0, encoded_fps=None, width=float('inf'))
        # json_response permits this deliberately invalid fixture. Reject the
        # nonfinite document instead of treating it as genuine metadata.
        await self.login()
        unavailable = await (await self.client.get('/native-camera/status')).json()
        self.assertEqual(unavailable['cameras'], {})
        self.status_data['cameras'][0]['width'] = None
        value = await (await self.client.get('/native-camera/status')).json()
        self.assertTrue(value['enabled'])
        self.assertEqual(value['maxPlaying'], 4)
        self.assertEqual(set(value['cameras']), {'usbFront', 'usbLeft', 'usbHub2', 'usbHub3'})
        front = value['cameras']['usbFront']  # Historical key, now rear camera.
        self.assertEqual(front['actual_fps'], 0)
        self.assertIsNone(front['encoded_fps'])
        self.assertIsNone(front['width'])
        self.assertEqual(front['frame_age_ms'], 0)
        self.assertEqual(front['frames'], 0)
        self.assertEqual(front['encoded_frames'], 0)
        self.assertEqual(front['codec'], 'avc1.424032')
        self.assertEqual(front['label'], '后视相机')
        self.assertEqual(value['cameras']['usbLeft']['label'], '前视相机')
        self.assertEqual(value['cameras']['usbHub3']['label'], '右视相机')
        self.assertEqual(value['cameras']['usbHub2']['label'], '左视相机')
        self.assertNotIn('TEST_ONLY_MUST_NOT_PASS', json.dumps(value))

    async def test_queries_methods_unknown_keys_do_not_reach_socket(self):
        await self.login()
        for path in ('/native-camera/status?url=http://unsafe.invalid',
                     '/native-camera/usbFront/video.mp4?token=TEST_ONLY',
                     '/native-camera/other/video.mp4', '/native-camera/camera2/video.mp4',
                     '/native-camera/http%3A%2F%2Funsafe/video.mp4'):
            with self.subTest(path=path):
                response = await self.client.get(path)
                self.assertIn(response.status, (400, 404))
        self.assertEqual((await self.client.post('/native-camera/status')).status, 405)
        self.assertEqual((await self.client.head('/native-camera/usbFront/video.mp4')).status, 405)
        self.assertEqual(self.seen, [])

    async def test_no_forwarded_credentials_headers_query_or_cookie(self):
        await self.login()
        response = await self.stream(headers={'Authorization': 'Bearer TEST_ONLY_NO_FORWARD',
            'X-Forwarded-For': '192.0.2.1', 'Range': 'bytes=0-1'})
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), INITIAL + FRAGMENT)
        self.assertNotIn('Set-Cookie', response.headers)
        self.assertEqual([path for path, _ in self.seen], ['/status', '/camera/camera2/video.mp4'])
        for _, headers in self.seen:
            self.assertNotIn('Authorization', headers)
            self.assertNotIn('Cookie', headers)
            self.assertNotIn('X-Forwarded-For', headers)
            self.assertNotIn('Range', headers)

    async def test_prepared_stream_has_security_headers_and_no_buffering(self):
        await self.login()
        response = await self.stream()
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers['Content-Type'], 'video/mp4')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(response.headers['X-Frame-Options'], 'DENY')
        self.assertEqual(response.headers['X-Accel-Buffering'], 'no')
        self.assertIn("media-src 'self' blob:", response.headers['Content-Security-Policy'])
        self.assertIn("object-src 'none'", response.headers['Content-Security-Policy'])
        await response.read()

    async def test_streaming_initialization_and_chunks_are_bounded(self):
        await self.login()
        self.initial = box(b'ftyp', b'isom\0\0\0\0isomiso2') + box(b'moov', b'x' * (CHUNK_BYTES + 12))
        sizes = []
        write = web.StreamResponse.write
        async def observed_write(response, data):
            if response.headers.get('X-Accel-Buffering') == 'no':
                sizes.append(len(data))
            return await write(response, data)
        with patch.object(web.StreamResponse, 'write', observed_write):
            response = await self.stream()
            self.assertEqual(await response.read(), self.initial + FRAGMENT)
        self.assertGreater(len(sizes), 1)
        self.assertTrue(all(size <= CHUNK_BYTES for size in sizes), sizes)

    async def test_invalid_or_large_initialization_rejected_before_200(self):
        await self.login()
        for initial in (b'NOT_A_VIDEO', box(b'moov'), box(b'ftyp'),
                        (MAX_BODY + 1).to_bytes(4, 'big') + b'ftyp',
                        INITIAL[:24] + box(b'mdat', b'x'),
                        b'\0\0\0\x01ftyp' + (MAX_BODY + 1).to_bytes(8, 'big')):
            with self.subTest(initial=initial[:16]):
                self.initial = initial
                response = await self.stream()
                self.assertEqual(response.status, 503)
                self.assertEqual((await response.json())['error'], 'Service Unavailable')
        self.assertEqual(self.app[NATIVE_VIDEO].streams, {})

    async def test_redirect_wrong_mime_errors_are_not_forwarded(self):
        await self.login()
        for mode in ('redirect', 'wrong-type', 'upstream-error'):
            with self.subTest(mode=mode):
                self.mode = mode
                response = await self.stream()
                self.assertEqual(response.status, 503)
                self.assertNotIn('Location', response.headers)
                self.assertNotIn('PRIVATE', await response.text())

    async def test_status_redirect_size_and_error_fail_safely(self):
        await self.login()
        for code, mode in ((302, 'finite'), (500, 'finite'), (200, 'large-status')):
            with self.subTest(code=code, mode=mode):
                self.status_code, self.mode = code, mode
                response = await self.client.get('/native-camera/status')
                value = await response.json()
                self.assertTrue(value['enabled'])
                self.assertEqual(value['cameras'], {})
                self.assertNotIn('PRIVATE', json.dumps(value))
                self.assertNotIn('Location', response.headers)

    async def test_duplicate_key_and_session_limit_then_release(self):
        await self.login()
        self.keep_streaming = True
        first = await self.stream()
        self.assertEqual(first.status, 200)
        self.assertEqual((await self.stream()).status, 429)
        others = [await self.stream(key) for key in ('usbLeft', 'usbHub2', 'usbHub3')]
        self.assertTrue(all(response.status == 200 for response in others))
        first.close()
        for _ in range(30):
            if not any('usbFront' in group for group in self.app[NATIVE_VIDEO].streams.values()):
                break
            await asyncio.sleep(.05)
        self.assertEqual((await self.stream()).status, 200)

    async def test_global_limit_is_sixteen_streams(self):
        self.keep_streaming = True
        for index in range(4):
            cookie = self.app[SESSIONS].issue('viewer')
            for key in ('usbFront', 'usbLeft', 'usbHub2', 'usbHub3'):
                response = await self.stream(key, headers={'Cookie': 'observer_session=' + cookie})
                self.assertEqual(response.status, 200, (index, key))
        cookie = self.app[SESSIONS].issue('viewer')
        response = await self.stream(headers={'Cookie': 'observer_session=' + cookie})
        self.assertEqual(response.status, 429)

    async def test_max_playing_one_is_advertised_and_enforced(self):
        _, client = await self.new_client(max_playing=1)
        self.extra_clients.append(client)
        await self.login(client)
        self.assertEqual((await (await client.get('/native-camera/status')).json())['maxPlaying'], 1)
        self.keep_streaming = True
        self.assertEqual((await self.stream(client=client)).status, 200)
        self.assertEqual((await self.stream('usbLeft', client=client)).status, 429)

    async def test_logout_immediately_ends_active_native_stream(self):
        await self.login()
        self.keep_streaming = True
        response = await self.stream()
        self.assertEqual(response.status, 200)
        self.assertEqual((await self.client.post('/api/logout', headers={'Origin': ORIGIN})).status, 200)
        await self.ended(response)
        self.assertEqual((await self.stream()).status, 401)
        self.assertEqual(self.app[NATIVE_VIDEO].streams, {})

    async def test_expired_session_ends_active_stream_within_one_second(self):
        await self.login()
        self.keep_streaming = True
        response = await self.stream()
        self.assertEqual(response.status, 200)
        clock = self.app[SESSIONS].clock()
        self.app[SESSIONS].clock = lambda: clock + self.app[SESSIONS].ttl + 1
        await self.ended(response)
        self.assertEqual((await self.stream()).status, 401)
        self.assertEqual(self.app[NATIVE_VIDEO].streams, {})

    async def test_missing_socket_reports_enabled_unavailable_without_details(self):
        _, client = await self.new_client(path=self.temp.name + '/missing.sock')
        self.extra_clients.append(client)
        await self.login(client)
        value = await (await client.get('/native-camera/status')).json()
        self.assertTrue(value['enabled'])
        self.assertEqual(value['cameras'], {})
        self.assertNotIn(self.temp.name, json.dumps(value))
        self.assertEqual((await self.stream(client=client)).status, 503)

    async def test_source_error_or_unsupported_codec_never_opens_video(self):
        await self.login()
        for fields in ({'error': 'TEST_ONLY_PRIVATE_TRACE'}, {'video_error': 'TEST_ONLY_PRIVATE_TRACE'},
                       {'codec': 'javascript:TEST_ONLY'}, {'id': 'camera1'}):
            with self.subTest(fields=fields):
                self.status_data = status_fixture()
                self.status_data['cameras'][0].update(fields)
                self.assertEqual((await self.stream()).status, 503)
        self.assertTrue(all(path == '/status' for path, _ in self.seen))

    async def test_preview_off_reports_real_occupancy_with_no_video_metadata(self):
        await self.login()
        self.status_data = {'cameras': [], 'cameraUsage': usage_fixture('OTHER', True, 0)}
        value = await (await self.client.get('/native-camera/status')).json()
        self.assertTrue(value['enabled'])
        self.assertEqual(value['cameras'], {})
        self.assertEqual(set(value['cameraUsage']), KEYS)
        self.assertTrue(all(row['owner'] == 'OTHER' and row['connected'] is True
                            for row in value['cameraUsage'].values()))
        self.assertEqual((await self.stream()).status, 503)
        self.assertTrue(all(path == '/status' for path, _ in self.seen))

    async def test_fresh_other_owner_allows_existing_preview_without_forwarding_private_details(self):
        await self.login()
        self.status_data['cameraUsage'] = usage_fixture('OTHER', True, 0)
        self.status_data['cameraUsage']['usbFront'].update(pid=1234, path='/TEST_ONLY/private-camera',
                                                        command='TEST_ONLY_PRIVATE_CAPTURE')
        response = await self.stream()
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), INITIAL + FRAGMENT)
        self.assertEqual([path for path, _ in self.seen], ['/status', '/camera/camera2/video.mp4'])
        for _, headers in self.seen:
            self.assertNotIn('Authorization', headers)
            self.assertNotIn('Cookie', headers)
        value = await (await self.client.get('/native-camera/status')).json()
        self.assertNotIn('TEST_ONLY', json.dumps(value))
        self.assertEqual(set(value['cameraUsage']['usbFront']), {'owner', 'connected', 'checkedAgeMs'})

    async def test_other_owner_cannot_bypass_source_or_mp4_validation(self):
        await self.login()
        for fields in ({'error': 'TEST_ONLY_PRIVATE_TRACE'}, {'video_error': 'TEST_ONLY_PRIVATE_TRACE'},
                       {'codec': None}, {'codec': 'javascript:TEST_ONLY'}, {'id': 'camera1'}):
            with self.subTest(fields=fields):
                self.status_data = status_fixture()
                self.status_data['cameraUsage'] = usage_fixture('OTHER', True, 0)
                self.status_data['cameras'][0].update(fields)
                self.assertEqual((await self.stream()).status, 503)
        self.assertTrue(all(path == '/status' for path, _ in self.seen))
        self.status_data = status_fixture()
        self.status_data['cameraUsage'] = usage_fixture('OTHER', True, 0)
        self.initial = b'NOT_A_VIDEO'
        self.assertEqual((await self.stream()).status, 503)
        self.assertEqual(self.app[NATIVE_VIDEO].streams, {})

    async def test_fresh_disconnected_camera_refuses_even_with_valid_video_metadata(self):
        await self.login()
        for owner in ('WEB', 'OTHER', 'NONE', 'UNKNOWN'):
            with self.subTest(owner=owner):
                self.status_data['cameraUsage'] = usage_fixture(owner, False, 0)
                self.assertEqual((await self.stream()).status, 503)
        self.assertTrue(all(path == '/status' for path, _ in self.seen))
        self.assertEqual(self.app[NATIVE_VIDEO].streams, {})

    async def test_usage_hint_does_not_grant_playback_without_actual_native_source(self):
        await self.login()
        self.status_data = {'cameras': [], 'cameraUsage': usage_fixture('WEB', True, 0)}
        self.assertEqual((await self.stream()).status, 503)
        self.assertEqual([path for path, _ in self.seen], ['/status'])
        self.status_data = status_fixture()
        self.status_data['cameraUsage'] = usage_fixture('WEB', True, 0)
        self.mode = 'wrong-type'
        self.assertEqual((await self.stream()).status, 503)

    async def test_no_occupancy_contract_remains_compatible_with_old_native_preview(self):
        await self.login()
        value = await (await self.client.get('/native-camera/status')).json()
        self.assertNotIn('cameraUsage', value)
        response = await self.stream()
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), INITIAL + FRAGMENT)

    async def test_expired_busy_and_disconnected_hint_does_not_claim_current_occupancy(self):
        await self.login()
        self.status_data['cameraUsage'] = usage_fixture('OTHER', False, CAMERA_USAGE_TTL_MS)
        value = await (await self.client.get('/native-camera/status')).json()
        for row in value['cameraUsage'].values():
            self.assertEqual(row['owner'], 'UNKNOWN')
            self.assertIsNone(row['connected'])
            self.assertGreaterEqual(row['checkedAgeMs'], CAMERA_USAGE_TTL_MS)
        # Availability is still verified against the actual status and MP4,
        # not denied by an expired device observation.
        response = await self.stream()
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), INITIAL + FRAGMENT)

    async def test_entire_status_request_duration_ages_cached_observation(self):
        await self.login()
        self.status_data['cameraUsage'] = usage_fixture('OTHER', True, 5000)
        with patch('cloud.backend.native_video.monotonic', side_effect=[100.0, 102.0]):
            value = await (await self.client.get('/native-camera/status')).json()
        self.assertEqual(value['cameraUsage']['usbFront'],
                         {'owner': 'UNKNOWN', 'connected': None, 'checkedAgeMs': 7000.0})
        self.status_data['cameraUsage'] = usage_fixture('OTHER', True, 1000)
        with patch('cloud.backend.native_video.monotonic', side_effect=[100.0, 103.0]):
            value = await (await self.client.get('/native-camera/status')).json()
        self.assertEqual(value['cameraUsage']['usbFront'],
                         {'owner': 'OTHER', 'connected': True, 'checkedAgeMs': 4000.0})

    async def test_delayed_status_does_not_renew_expired_busy_lease_during_video_admission(self):
        await self.login()
        self.status_data['cameraUsage'] = usage_fixture('OTHER', False, 5000)
        with patch('cloud.backend.native_video.monotonic', side_effect=[100.0, 102.0]):
            response = await self.stream()
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), INITIAL + FRAGMENT)
        self.assertEqual([path for path, _ in self.seen], ['/status', '/camera/camera2/video.mp4'])

    async def test_malformed_present_usage_is_unknown_not_an_occupancy_assertion(self):
        await self.login()
        for usage in (None, [], 'TEST_ONLY_PRIVATE_CAPTURE', {'usbFront': {'owner': 'OTHER'}},
                      {'usbFront': {'owner': 'OTHER', 'connected': 'false', 'checkedAgeMs': 0}}):
            with self.subTest(usage=usage):
                self.status_data['cameraUsage'] = usage
                value = await (await self.client.get('/native-camera/status')).json()
                self.assertEqual(value['cameraUsage'], usage_fixture('UNKNOWN', None, None))
                response = await self.stream()
                self.assertEqual(response.status, 200)
                self.assertEqual(await response.read(), INITIAL + FRAGMENT)

    async def test_occupancy_status_and_video_keep_auth_and_query_bounds(self):
        self.status_data['cameraUsage'] = usage_fixture('OTHER', True, 0)
        for path in ('/native-camera/status', '/native-camera/usbFront/video.mp4'):
            self.assertEqual((await self.client.get(path)).status, 401)
        self.assertEqual(self.seen, [])
        await self.login()
        for path in ('/native-camera/status?owner=OTHER', '/native-camera/usbFront/video.mp4?owner=WEB'):
            self.assertEqual((await self.client.get(path)).status, 400)
        self.assertEqual((await self.client.post('/native-camera/status')).status, 405)
        self.assertEqual(self.seen, [])

    async def test_shutdown_ends_live_stream_before_waiting_for_handlers(self):
        await self.login()
        self.keep_streaming = True
        response = await self.stream()
        self.assertEqual(response.status, 200)
        await asyncio.wait_for(self.app.shutdown(), 2)
        await self.ended(response)
        self.assertTrue(self.app[NATIVE_VIDEO].client.closed)
        self.assertEqual(self.app[NATIVE_VIDEO].streams, {})

    async def test_split_video_uses_only_fixed_sibling_socket_per_key(self):
        ids = {'usbFront': 'camera2', 'usbLeft': 'camera1', 'usbHub3': 'camera3', 'usbHub2': 'camera4'}
        for key in ids:
            await web.UnixSite(self.runner, self.temp.name + '/jetson-' + key + '.sock').start()
        app, client = await self.new_client(split_sockets=True)
        self.extra_clients.append(client)
        await self.login(client)
        for key, identity in ids.items():
            response = await self.stream(key, client=client)
            self.assertEqual(response.status, 200)
            self.assertEqual(await response.read(), INITIAL + FRAGMENT)
            self.assertIn(('/camera/' + identity + '/video.mp4',
                           self.temp.name + '/jetson-' + key + '.sock'), self.seen_sockets)
        self.assertTrue(all(socket == self.socket for path, socket in self.seen_sockets if path == '/status'))
        before = len(self.seen_sockets)
        self.assertEqual((await self.stream('../unsafe', client=client)).status, 404)
        self.assertEqual(len(self.seen_sockets), before)
        await app.shutdown()
        self.assertTrue(app[NATIVE_VIDEO].client.closed)
        self.assertTrue(all(value.closed for value in app[NATIVE_VIDEO].video_clients.values()))


class NativeVideoValidationTests(unittest.TestCase):
    def test_usage_owner_enum_boolean_null_and_zero_are_exact(self):
        for owner in ('WEB', 'OTHER', 'NONE', 'UNKNOWN'):
            for connected in (True, False, None):
                with self.subTest(owner=owner, connected=connected):
                    usage = sanitize_camera_usage(usage_fixture(owner, connected, 0))
                    self.assertEqual(usage, usage_fixture(owner, connected, 0))

    def test_usage_lease_is_fixed_and_adds_transport_age_before_expiration(self):
        self.assertEqual(CAMERA_USAGE_TTL_MS, 6000)
        value = usage_fixture('OTHER', False, 1000)
        for row in value.values():
            row['leaseTTL'] = 999999999
        self.assertEqual(sanitize_camera_usage(value, 4999), usage_fixture('OTHER', False, 5999))
        self.assertEqual(sanitize_camera_usage(value, 5000), usage_fixture('UNKNOWN', None, 6000))
        self.assertEqual(sanitize_camera_usage(value, 5001), usage_fixture('UNKNOWN', None, 6001))

    def test_usage_malformed_types_cannot_assert_busy_or_disconnection(self):
        unknown = usage_fixture('UNKNOWN', None, None)
        for value in (None, [], True, 'OTHER', {'owner': 'OTHER'}):
            with self.subTest(value=value):
                self.assertEqual(sanitize_camera_usage(value), unknown)
        invalid_rows = [{'owner': value, 'connected': False, 'checkedAgeMs': 0}
                        for value in (None, True, False, 1, [], {}, 'other', 'BUSY', 'TEST_ONLY')]
        invalid_rows += [{'owner': 'OTHER', 'connected': value, 'checkedAgeMs': 0}
                         for value in (0, 1, 'false', [], {})]
        invalid_rows += [{'owner': 'OTHER', 'connected': False, 'checkedAgeMs': value}
                         for value in (None, True, False, -1, float('nan'), float('inf'), '0',
                                       [], {}, 10 ** 400)]
        invalid_rows += [{'owner': 'OTHER', 'checkedAgeMs': 0},
                         {'owner': 'OTHER', 'connected': False}]
        for row in invalid_rows:
            with self.subTest(row=row):
                self.assertEqual(sanitize_camera_usage({key: row for key in KEYS}), unknown)
        for elapsed in (None, True, -1, float('nan'), float('inf'), '0', 10 ** 400):
            with self.subTest(elapsed=elapsed):
                self.assertEqual(sanitize_camera_usage(usage_fixture('OTHER', False, 0), elapsed), unknown)

    def test_usage_sanitizer_is_fixed_keyed_and_drops_paths_pids_secrets(self):
        value = usage_fixture('OTHER', True, 0)
        value['TEST_ONLY_UNTRUSTED_KEY'] = dict(owner='OTHER', connected=False, checkedAgeMs=0)
        for row in value.values():
            row.update(pid=987, path='/TEST_ONLY/private', args=['TEST_ONLY_SECRET'])
        result = sanitize_camera_usage(value)
        self.assertEqual(result, usage_fixture('OTHER', True, 0))
        self.assertNotIn('TEST_ONLY', json.dumps(result))
        self.assertNotIn('pid', json.dumps(result))
        self.assertNotIn('path', json.dumps(result))

    def test_usage_malformed_one_key_does_not_erase_other_independent_observations(self):
        value = usage_fixture('OTHER', True, 0)
        value['usbFront'] = None
        del value['usbHub2']
        result = sanitize_camera_usage(value)
        self.assertEqual(result['usbFront'], {'owner': 'UNKNOWN', 'connected': None, 'checkedAgeMs': None})
        self.assertEqual(result['usbHub2'], {'owner': 'UNKNOWN', 'connected': None, 'checkedAgeMs': None})
        self.assertEqual(result['usbLeft'], value['usbLeft'])
        self.assertEqual(result['usbHub3'], value['usbHub3'])

    def test_usage_empty_cameras_stays_enabled_and_old_contract_omits_hint(self):
        self.assertEqual(sanitize_status({'cameras': []}), {'enabled': True, 'cameras': {}})
        value = sanitize_status({'cameras': [], 'cameraUsage': None})
        self.assertTrue(value['enabled'])
        self.assertEqual(value['cameraUsage'], usage_fixture('UNKNOWN', None, None))

    def test_source_frame_counts_are_nonnegative_safe_integers_only(self):
        for value in (0, 1, 2 ** 53 - 1):
            with self.subTest(value=value):
                status = status_fixture()
                status['cameras'][0].update(frames=value, encoded_frames=value)
                camera = sanitize_status(status)['cameras']['usbFront']
                self.assertEqual(camera['frames'], value)
                self.assertEqual(camera['encoded_frames'], value)
        for value in (None, True, False, -1, 2 ** 53, 1.0, '1', float('nan'), float('inf')):
            with self.subTest(value=value):
                status = status_fixture()
                status['cameras'][0].update(frames=value, encoded_frames=value)
                camera = sanitize_status(status)['cameras']['usbFront']
                self.assertIsNone(camera['frames'])
                self.assertIsNone(camera['encoded_frames'])

    def test_split_socket_env_is_strict_opt_in(self):
        self.assertFalse(split_socket_option('false'))
        self.assertTrue(split_socket_option('true'))
        for value in ('', 'TRUE', 'yes', '1', '0', None, True, False):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    split_socket_option(value)

    def test_socket_path_is_only_normalized_absolute_unix(self):
        self.assertEqual(socket_path(''), '')
        self.assertEqual(socket_path('/run/native-preview/jetson.sock'), '/run/native-preview/jetson.sock')
        for value in ('relative.sock', 'http://localhost:8080', '/', '/tmp/../run/x.sock',
                      '/run//x.sock', '/tmp/x.sock?query', '/tmp/x.sock\0', '/tmp/' + 'x' * 110):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    socket_path(value)

    def test_direction_id_pair_is_fixed_and_duplicate_is_ambiguous(self):
        value = status_fixture()
        value['cameras'][0]['id'] = 'camera1'
        self.assertNotIn('usbFront', sanitize_status(value)['cameras'])
        value = status_fixture()
        value['cameras'].append(dict(value['cameras'][0]))
        self.assertNotIn('usbFront', sanitize_status(value)['cameras'])

    def test_errors_are_generic_and_bad_types_null(self):
        value = status_fixture()
        value['cameras'][0].update(error='TEST_ONLY_PRIVATE_TRACE', video_error='<script>TEST_ONLY</script>',
                                   actual_fps=True, frame_age_ms=-1, h264_mbps=float('nan'), codec='javascript:TEST_ONLY')
        camera = sanitize_status(value)['cameras']['usbFront']
        self.assertEqual(camera['error'], '相机暂不可用')
        self.assertEqual(camera['video_error'], '视频暂不可用')
        for key in ('actual_fps', 'frame_age_ms', 'h264_mbps', 'codec'):
            self.assertIsNone(camera[key])
        self.assertNotIn('TEST_ONLY', json.dumps(camera))
