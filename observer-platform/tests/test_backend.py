import base64
import copy
import hashlib
import tempfile
import time
import unittest
import uuid
from unittest.mock import patch

from aiohttp import CookieJar, WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from cloud.backend.app import CONFIG, SESSIONS, STORE, create_app
from cloud.backend.security import Config, RateLimiter, hash_password, verify_password
from cloud.backend.store import ReplayError, Store
from shared.protocol import CAMERA_KEYS, HTTP_CAMERA_KEYS, MEDIA_KEYS, validate_envelope
from tests.test_protocol import envelope, jpeg

TOKEN = 'isolated-device-token-12345678901234567890'
CAMERA_TOKEN = 'isolated-camera-token-12345678901234567890'
PASSWORD = 'isolated-viewer-password'
ORIGIN = 'http://127.0.0.1:19088'
PASSWORD_HASH = hash_password(PASSWORD, salt=b'isolated-tests!!')


def config(directory, **overrides):
    values = dict(robot_id='test-robot', device_credentials=[
        {'token': TOKEN, 'robot_id': 'test-robot', 'source_id': 'rk3588', 'role': 'primary',
         'media_keys': list((MEDIA_KEYS - CAMERA_KEYS - {'depthPoints'}) | HTTP_CAMERA_KEYS)},
        {'token': CAMERA_TOKEN, 'robot_id': 'test-robot', 'source_id': 'jetson', 'role': 'cameras',
         'media_keys': list((CAMERA_KEYS - HTTP_CAMERA_KEYS) | {'depthPoints'})}],
        viewer_username='viewer', viewer_password_hash=PASSWORD_HASH,
        session_secret='isolated-session-secret-12345678901234567890',
        public_origin=ORIGIN, data_dir=directory, allow_insecure_local=True)
    values.update(overrides)
    return Config(**values)


class BackendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='robot-cloud-test-')
        self.app = create_app(config(self.temp.name))
        self.client = TestClient(TestServer(self.app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def login(self):
        result = await self.client.post('/api/login', json={'username': 'viewer', 'password': PASSWORD},
                                        headers={'Origin': ORIGIN})
        self.assertEqual(result.status, 200, await result.text())
        return result

    async def ingest(self, obj, token=TOKEN):
        return await self.client.post('/api/v1/ingest', json=obj, headers={'Authorization': 'Bearer ' + token})

    async def test_anonymous_and_device_cannot_view(self):
        for path in ('/snapshot.json', '/visuals.json', '/camera/color.jpg', '/visual/map.png', '/api/robots', '/api/session'):
            result = await self.client.get(path, headers={'Authorization': 'Bearer ' + TOKEN})
            self.assertEqual(result.status, 401, path)
        self.assertEqual((await self.client.get('/healthz')).status, 200)
        result = await self.client.get('/', allow_redirects=False)
        self.assertEqual(result.status, 302)
        self.assertEqual(result.headers['Location'], '/login')

    async def test_viewer_cookie_cannot_ingest_and_origin_csrf(self):
        result = await self.client.post('/api/login', json={'username': 'viewer', 'password': PASSWORD},
                                       headers={'Origin': 'https://other.example'})
        self.assertEqual(result.status, 403)
        login = await self.login()
        self.assertTrue(login.cookies['observer_session']['httponly'])
        self.assertEqual(login.cookies['observer_session']['samesite'], 'Strict')
        self.assertEqual((await self.client.post('/api/v1/ingest', json=envelope())).status, 401)
        self.assertEqual((await self.client.post('/api/logout')).status, 403)
        self.assertEqual((await self.client.get('/api/session')).status, 200)
        self.assertEqual((await self.client.post('/api/logout', headers={'Origin': ORIGIN})).status, 200)
        self.assertEqual((await self.client.get('/snapshot.json')).status, 401)

    async def test_source_role_and_key_isolation(self):
        obj = envelope()
        obj['source_id'] = 'jetson'
        self.assertEqual((await self.ingest(obj, CAMERA_TOKEN)).status, 403)
        self.assertEqual((await self.ingest(obj, TOKEN)).status, 403)
        obj = envelope('media')
        obj['source_id'] = 'jetson'
        self.assertEqual((await self.ingest(obj, CAMERA_TOKEN)).status, 403)
        obj['payload'] = {'key': 'fisheye', 'kind': 'image', 'status': 'WAITING_UPSTREAM', 'ageS': None}
        self.assertEqual((await self.ingest(obj, CAMERA_TOKEN)).status, 200)

    async def test_scan_media_primary_auth_and_stale_hides_points(self):
        clock = [time.time()]
        self.app[STORE].clock = lambda: clock[0]
        self.assertEqual(self.app[STORE].visuals()['bluesea']['status'], 'WAITING_UPSTREAM')
        obj = envelope('media', timestamp=clock[0])
        for index, key in enumerate(('bluesea', 'scan'), 1):
            obj['sequence'] = index
            obj['topic'] = '/bluesea_new/scan' if key == 'bluesea' else '/scan'
            obj['payload'] = {'key': key, 'kind': 'cloud', 'status': 'AVAILABLE', 'ageS': 0,
                              'points': [[1.0, 0.0, 0.0]], 'totalPoints': 1, 'frameId': key + '_frame'}
            camera = dict(obj, source_id='jetson')
            self.assertEqual((await self.ingest(camera, CAMERA_TOKEN)).status, 403)
            self.assertEqual((await self.ingest(obj)).status, 200)
        self.assertEqual((await self.client.get('/visuals.json')).status, 401)
        await self.login()
        current = await (await self.client.get('/visuals.json')).json()
        for key in ('bluesea', 'scan'):
            self.assertEqual(current[key]['points'], [[1.0, 0.0, 0.0]])
            self.assertEqual(current[key]['frameId'], key + '_frame')
        clock[0] += 3
        stale = await (await self.client.get('/visuals.json')).json()
        for key in ('bluesea', 'scan'):
            self.assertEqual(stale[key]['status'], 'STALE')
            self.assertNotIn('points', stale[key])

    async def test_http_cameras_primary_owner_auth_and_stale_hides_jpeg(self):
        clock = [time.time()]
        self.app[STORE].clock = lambda: clock[0]
        obj = envelope('media', timestamp=clock[0])
        for index, key in enumerate(sorted(HTTP_CAMERA_KEYS), 1):
            self.assertEqual((await self.client.get('/camera/' + key + '.jpg')).status, 401)
            obj['sequence'] = index
            obj['topic'] = '/observer/http_camera/' + key
            obj['payload'] = {'key': key, 'kind': 'image', 'status': 'AVAILABLE', 'ageS': 0,
                              'data': base64.b64encode(jpeg()).decode(), 'mime_type': 'image/jpeg',
                              'message_timestamp': None, 'frameSeq': index,
                              'source': 'Jetson HTTP preview'}
            camera = dict(obj, source_id='jetson')
            self.assertEqual((await self.ingest(camera, CAMERA_TOKEN)).status, 403)
            self.assertEqual((await self.ingest(obj, CAMERA_TOKEN)).status, 403)
            self.assertEqual((await self.ingest(obj)).status, 200)
        await self.login()
        current = (await (await self.client.get('/snapshot.json')).json())['devices']['cameras']
        self.assertEqual(set(current), CAMERA_KEYS)
        for key in sorted(HTTP_CAMERA_KEYS):
            self.assertEqual(current[key]['status'], 'AVAILABLE')
            self.assertEqual(current[key]['previewUrl'], '/camera/' + key + '.jpg')
            self.assertIsNone(current[key]['message_timestamp'])
            response = await self.client.get('/camera/' + key + '.jpg')
            self.assertEqual(response.status, 200)
            self.assertEqual(response.content_type, 'image/jpeg')
            self.assertEqual(await response.read(), jpeg())
            self.assertTrue(response.headers['X-Frame-Sequence'].startswith(obj['session_id'] + ':'))
            self.assertEqual(response.headers['X-Frame-Sequence'], current[key]['frameIdentity'])
            self.assertEqual(float(response.headers['X-Frame-Age-S']), 0)
            self.assertEqual(float(response.headers['X-Frame-TTL-S']), 2)
        for key in CAMERA_KEYS - HTTP_CAMERA_KEYS:
            self.assertEqual(current[key]['status'], 'WAITING_UPSTREAM')
            self.assertEqual((await self.client.get('/camera/' + key + '.jpg')).status, 204)
        self.assertEqual((await self.client.get('/camera/unknown.jpg')).status, 404)
        clock[0] += 3
        # Heartbeats confirm only collector connectivity, never refresh a cached frame.
        heartbeat = envelope('heartbeat', sequence=len(HTTP_CAMERA_KEYS) + 1, timestamp=clock[0], session=obj['session_id'])
        self.assertEqual((await self.ingest(heartbeat)).status, 200)
        stale = (await (await self.client.get('/snapshot.json')).json())['devices']['cameras']
        for key in sorted(HTTP_CAMERA_KEYS):
            self.assertEqual(stale[key]['status'], 'STALE')
            self.assertGreaterEqual(stale[key]['ageS'], 3)
            response = await self.client.get('/camera/' + key + '.jpg')
            self.assertEqual(response.status, 204)
            self.assertEqual(await response.read(), b'')

    async def test_camera_source_age_adds_upload_delay_before_stale_check(self):
        clock = [time.time()]
        self.app[STORE].clock = lambda: clock[0]
        obj = envelope('media', timestamp=clock[0] - .9)
        obj['payload'] = {'key': 'usbFront', 'kind': 'image', 'status': 'AVAILABLE',
                          'ageS': 1.2, 'frameSeq': 7, 'data': base64.b64encode(jpeg()).decode(),
                          'mime_type': 'image/jpeg'}
        self.assertEqual((await self.ingest(obj)).status, 200)
        await self.login()
        current = (await (await self.client.get('/snapshot.json')).json())['devices']['cameras']['usbFront']
        self.assertAlmostEqual(current['ageS'], 2.1, places=5)
        self.assertEqual(current['status'], 'STALE')
        response = await self.client.get('/camera/usbFront.jpg')
        self.assertEqual(response.status, 204)
        self.assertEqual(await response.read(), b'')

    async def test_camera_future_clock_never_subtracts_source_age(self):
        clock = [time.time()]
        self.app[STORE].clock = lambda: clock[0]
        obj = envelope('media', timestamp=clock[0] + .4)
        obj['message_timestamp'] = clock[0] + .2
        obj['payload'] = {'key': 'usbRear', 'kind': 'image', 'status': 'AVAILABLE',
                          'ageS': 1.2, 'frameSeq': 1, 'data': base64.b64encode(jpeg()).decode(),
                          'mime_type': 'image/jpeg', 'received_timestamp': clock[0] + .1,
                          'message_timestamp': obj['message_timestamp']}
        self.assertEqual((await self.ingest(obj)).status, 200)
        await self.login()
        response = await self.client.get('/camera/usbRear.jpg')
        self.assertEqual(response.status, 200)
        self.assertEqual(float(response.headers['X-Frame-Age-S']), 1.2)
        clock[0] += .7
        response = await self.client.get('/camera/usbRear.jpg')
        self.assertEqual(response.status, 200)
        self.assertAlmostEqual(float(response.headers['X-Frame-Age-S']), 1.9, places=5)
        clock[0] += .2
        response = await self.client.get('/camera/usbRear.jpg')
        self.assertEqual(response.status, 204)
        self.assertEqual(await response.read(), b'')

    async def test_camera_received_and_sampling_timestamps_remain_lower_bounds(self):
        clock = [time.time()]
        self.app[STORE].clock = lambda: clock[0]
        session = str(uuid.uuid4())
        await self.login()
        for sequence, received_age, sampled_age in ((1, 1.7, 1.3), (2, 1.1, 1.8)):
            with self.subTest(received_age=received_age, sampled_age=sampled_age):
                obj = envelope('media', sequence=sequence, timestamp=clock[0] - .1, session=session)
                obj['message_timestamp'] = clock[0] - sampled_age
                obj['payload'] = {'key': 'usbLeft', 'kind': 'image', 'status': 'AVAILABLE',
                                  'ageS': .4, 'frameSeq': sequence,
                                  'data': base64.b64encode(jpeg()).decode(), 'mime_type': 'image/jpeg',
                                  'received_timestamp': clock[0] - received_age,
                                  'message_timestamp': obj['message_timestamp']}
                self.assertEqual((await self.ingest(obj)).status, 200)
                clock[0] += .1
                response = await self.client.get('/camera/usbLeft.jpg')
                self.assertEqual(response.status, 200)
                self.assertAlmostEqual(float(response.headers['X-Frame-Age-S']),
                                       max(received_age, sampled_age) + .1, places=5)

    async def test_legacy_camera_jpeg_has_same_identity_age_and_ttl_contract(self):
        clock = [time.time()]
        self.app[STORE].clock = lambda: clock[0]
        self.assertEqual((await self.ingest(envelope('heartbeat', timestamp=clock[0]))).status, 200)
        obj = envelope('media', timestamp=clock[0])
        obj['source_id'] = 'jetson'
        obj['payload'] = {'key': 'color', 'kind': 'image', 'status': 'AVAILABLE', 'ageS': .25,
                          'frameSeq': 0, 'data': base64.b64encode(jpeg()).decode(),
                          'mime_type': 'image/jpeg'}
        self.assertEqual((await self.ingest(obj, CAMERA_TOKEN)).status, 200)
        await self.login()
        current = (await (await self.client.get('/snapshot.json')).json())['devices']['cameras']['color']
        response = await self.client.get('/camera/color.jpg')
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), jpeg())
        self.assertEqual(current['sequence'], 0)
        self.assertEqual(response.headers['X-Frame-Sequence'], obj['session_id'] + ':0')
        self.assertEqual(response.headers['X-Frame-Sequence'], current['frameIdentity'])
        self.assertEqual(float(response.headers['X-Frame-Age-S']), .25)
        self.assertEqual(float(response.headers['X-Frame-TTL-S']), 2)

    async def test_camera_identity_changes_across_sessions_with_repeated_frame_number(self):
        clock = [time.time()]
        self.app[STORE].clock = lambda: clock[0]
        await self.login()
        identities = []
        for _ in range(2):
            obj = envelope('media', timestamp=clock[0])
            obj['payload'] = {'key': 'usbFront', 'kind': 'image', 'status': 'AVAILABLE', 'ageS': 0,
                              'frameSeq': 1, 'data': base64.b64encode(jpeg()).decode(),
                              'mime_type': 'image/jpeg'}
            self.assertEqual((await self.ingest(obj)).status, 200)
            current = (await (await self.client.get('/snapshot.json')).json())['devices']['cameras']['usbFront']
            response = await self.client.get('/camera/usbFront.jpg')
            self.assertEqual(response.status, 200)
            self.assertEqual(current['sequence'], 1)
            self.assertEqual(current['frameIdentity'], obj['session_id'] + ':1')
            self.assertEqual(response.headers['X-Frame-Sequence'], current['frameIdentity'])
            identities.append(current['frameIdentity'])
            clock[0] += .1
        self.assertNotEqual(identities[0], identities[1])

    async def test_camera_missing_frame_sequence_uses_integer_upload_identity(self):
        clock = [time.time()]
        self.app[STORE].clock = lambda: clock[0]
        obj = envelope('heartbeat', timestamp=clock[0])
        self.assertEqual((await self.ingest(obj)).status, 200)
        media = envelope('media', sequence=2, timestamp=clock[0], session=obj['session_id'])
        media['payload'] = {'key': 'usbFront', 'kind': 'image', 'status': 'AVAILABLE', 'ageS': 0,
                            'data': base64.b64encode(jpeg()).decode(), 'mime_type': 'image/jpeg'}
        self.assertEqual((await self.ingest(media)).status, 200)
        await self.login()
        current = (await (await self.client.get('/snapshot.json')).json())['devices']['cameras']['usbFront']
        response = await self.client.get('/camera/usbFront.jpg')
        self.assertEqual(current['sequence'], 0)  # Existing display field remains compatible.
        self.assertEqual(current['frameIdentity'], obj['session_id'] + ':2')
        self.assertEqual(response.headers['X-Frame-Sequence'], current['frameIdentity'])

    async def test_image_response_reads_one_frame_bundle_not_separate_cache_lookups(self):
        clock = [time.time()]
        store = self.app[STORE]
        store.clock = lambda: clock[0]
        obj = envelope('media', timestamp=clock[0])
        obj['payload'] = {'key': 'usbFront', 'kind': 'image', 'status': 'AVAILABLE', 'ageS': .25,
                          'frameSeq': 8, 'data': base64.b64encode(jpeg()).decode(),
                          'mime_type': 'image/jpeg'}
        self.assertEqual((await self.ingest(obj)).status, 200)
        original = store._media_entry_item
        replacement = copy.deepcopy(store.media['usbFront'])
        replacement['metadata']['frameSeq'] = 9

        def replace_after_metadata(key, entry, now):
            item = original(key, entry, now)
            store.media[key] = replacement
            return item

        await self.login()
        with patch.object(store, '_media_entry_item', side_effect=replace_after_metadata), \
             patch.object(store, 'image', side_effect=AssertionError('separate binary lookup')), \
             patch.object(store, 'image_sequence', side_effect=AssertionError('separate identity lookup')):
            response = await self.client.get('/camera/usbFront.jpg')
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), jpeg())
        self.assertEqual(response.headers['X-Frame-Sequence'], obj['session_id'] + ':8')
        self.assertEqual(float(response.headers['X-Frame-Age-S']), .25)
        self.assertEqual(store.image_sequence('usbFront'), obj['session_id'] + ':9')

    async def test_camera_conditional_same_frame_has_empty_body_and_current_growing_age(self):
        clock = [time.time()]
        self.app[STORE].clock = lambda: clock[0]
        obj = envelope('media', timestamp=clock[0])
        obj['payload'] = {'key': 'usbFront', 'kind': 'image', 'status': 'AVAILABLE', 'ageS': .25,
                          'frameSeq': 0, 'data': base64.b64encode(jpeg()).decode(),
                          'mime_type': 'image/jpeg'}
        self.assertEqual((await self.ingest(obj)).status, 200)
        await self.login()
        first = await self.client.get('/camera/usbFront.jpg')
        self.assertEqual(first.status, 200)
        self.assertEqual(await first.read(), jpeg())
        tag = first.headers['ETag']
        self.assertEqual(first.headers['X-Frame-Image-SHA256'], hashlib.sha256(jpeg()).hexdigest())
        self.assertRegex(tag, '^"[0-9a-f]{64}"$')
        identity = first.headers['X-Frame-Sequence']
        for age in (.5, 1.0, 1.5):
            clock[0] = obj['collector_timestamp'] + age
            response = await self.client.get('/camera/usbFront.jpg', headers={'If-None-Match': tag})
            self.assertEqual(response.status, 304)
            self.assertEqual(await response.read(), b'')
            self.assertEqual(response.headers['ETag'], tag)
            self.assertEqual(response.headers['X-Frame-Image-SHA256'], first.headers['X-Frame-Image-SHA256'])
            self.assertEqual(response.headers['X-Frame-Sequence'], identity)
            self.assertAlmostEqual(float(response.headers['X-Frame-Age-S']), age + .25)
            self.assertEqual(float(response.headers['X-Frame-TTL-S']), 2)
            self.assertEqual(response.headers['Cache-Control'], 'no-store')
        clock[0] = obj['collector_timestamp'] + 1.8
        response = await self.client.get('/camera/usbFront.jpg', headers={'If-None-Match': tag})
        self.assertEqual(response.status, 204)
        self.assertEqual(await response.read(), b'')
        self.assertNotIn('ETag', response.headers)
        self.assertNotIn('X-Frame-Image-SHA256', response.headers)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    async def test_camera_conditional_validator_cannot_bypass_auth_or_logout(self):
        obj = envelope('media')
        obj['payload'] = {'key': 'usbFront', 'kind': 'image', 'status': 'AVAILABLE', 'ageS': 0,
                          'frameSeq': 1, 'data': base64.b64encode(jpeg()).decode(),
                          'mime_type': 'image/jpeg'}
        self.assertEqual((await self.ingest(obj)).status, 200)
        guessed = '"' + '0' * 64 + '"'
        anonymous = await self.client.get('/camera/usbFront.jpg', headers={'If-None-Match': guessed})
        self.assertEqual(anonymous.status, 401)
        self.assertNotIn('ETag', anonymous.headers)
        await self.login()
        first = await self.client.get('/camera/usbFront.jpg')
        await first.read()
        tag = first.headers['ETag']
        self.assertEqual((await self.client.post('/api/logout', headers={'Origin': ORIGIN})).status, 200)
        logged_out = await self.client.get('/camera/usbFront.jpg', headers={'If-None-Match': tag})
        self.assertEqual(logged_out.status, 401)
        self.assertNotIn('ETag', logged_out.headers)
        self.assertEqual(logged_out.headers['Cache-Control'], 'no-store')

    async def test_camera_conditional_binds_key_session_counter_and_actual_representation(self):
        clock = [time.time()]
        self.app[STORE].clock = lambda: clock[0]
        await self.login()
        obj = envelope('media', timestamp=clock[0])
        obj['payload'] = {'key': 'usbFront', 'kind': 'image', 'status': 'AVAILABLE', 'ageS': 0,
                          'frameSeq': 1, 'data': base64.b64encode(jpeg()).decode(),
                          'mime_type': 'image/jpeg'}
        self.assertEqual((await self.ingest(obj)).status, 200)
        first = await self.client.get('/camera/usbFront.jpg')
        await first.read()
        previous_tag = first.headers['ETag']
        previous_identity = first.headers['X-Frame-Sequence']
        # Change only pixels with the same session/counter: a strong validator
        # must notice representation changes even when a source counter is bad.
        original = jpeg()
        changed = original[:-2] + b'\xff\xfe\x00\x06test' + original[-2:]
        obj['sequence'] += 1
        obj['payload']['data'] = base64.b64encode(changed).decode()
        self.assertEqual((await self.ingest(obj)).status, 200)
        response = await self.client.get('/camera/usbFront.jpg', headers={'If-None-Match': previous_tag})
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), changed)
        self.assertEqual(response.headers['X-Frame-Image-SHA256'], hashlib.sha256(changed).hexdigest())
        self.assertEqual(response.headers['X-Frame-Sequence'], previous_identity)
        self.assertNotEqual(response.headers['ETag'], previous_tag)
        previous_tag = response.headers['ETag']
        # Another key with exactly the same identity and pixels also differs.
        obj['sequence'] += 1
        obj['payload']['key'] = 'usbLeft'
        self.assertEqual((await self.ingest(obj)).status, 200)
        response = await self.client.get('/camera/usbLeft.jpg', headers={'If-None-Match': previous_tag})
        self.assertEqual(response.status, 200)
        await response.read()
        self.assertEqual(response.headers['X-Frame-Sequence'], previous_identity)
        self.assertNotEqual(response.headers['ETag'], previous_tag)
        previous_tag = response.headers['ETag']
        obj['sequence'] += 1
        obj['payload']['frameSeq'] += 1
        self.assertEqual((await self.ingest(obj)).status, 200)
        response = await self.client.get('/camera/usbLeft.jpg', headers={'If-None-Match': previous_tag})
        self.assertEqual(response.status, 200)
        await response.read()
        self.assertNotEqual(response.headers['ETag'], previous_tag)
        previous_tag = response.headers['ETag']
        # A new source session may legitimately restart its frame number.
        clock[0] += .1
        obj['session_id'], obj['sequence'] = str(uuid.uuid4()), 1
        obj['collector_timestamp'] = clock[0]
        self.assertEqual((await self.ingest(obj)).status, 200)
        response = await self.client.get('/camera/usbLeft.jpg', headers={'If-None-Match': previous_tag})
        self.assertEqual(response.status, 200)
        await response.read()
        self.assertNotEqual(response.headers['ETag'], previous_tag)

    async def test_camera_conditional_only_one_exact_strong_validator_matches(self):
        clock = [time.time()]
        self.app[STORE].clock = lambda: clock[0]
        obj = envelope('media', timestamp=clock[0])
        obj['payload'] = {'key': 'usbFront', 'kind': 'image', 'status': 'AVAILABLE', 'ageS': 0,
                          'frameSeq': 1, 'data': base64.b64encode(jpeg()).decode(),
                          'mime_type': 'image/jpeg'}
        self.assertEqual((await self.ingest(obj)).status, 200)
        await self.login()
        first = await self.client.get('/camera/usbFront.jpg')
        await first.read()
        tag = first.headers['ETag']
        for value in ('*', 'W/' + tag, tag + ', "other"', '"different"', 'malformed'):
            with self.subTest(value=value):
                response = await self.client.get('/camera/usbFront.jpg', headers={'If-None-Match': value})
                self.assertEqual(response.status, 200)
                self.assertEqual(await response.read(), jpeg())
        response = await self.client.get('/camera/usbFront.jpg', headers=[('If-None-Match', tag), ('If-None-Match', tag)])
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.read(), jpeg())

    async def test_map_conditional_metadata_keeps_nonmatching_image_response(self):
        clock = [time.time()]
        self.app[STORE].clock = lambda: clock[0]
        obj = envelope('media', timestamp=clock[0])
        self.assertEqual((await self.ingest(obj)).status, 200)
        await self.login()
        response = await self.client.get('/visual/map.png', headers={'If-None-Match': '*'})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.content_type, 'image/png')
        self.assertTrue(await response.read())
        self.assertRegex(response.headers['ETag'], '^"[0-9a-f]{64}"$')
        self.assertEqual(response.headers['X-Frame-TTL-S'], 'event')
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    async def test_corrected_age_preserves_map_event_and_costmap_cloud_timeouts(self):
        clock = [time.time()]
        self.app[STORE].clock = lambda: clock[0]
        obj = envelope('media', timestamp=clock[0] - .9)
        session = obj['session_id']
        for sequence, key in enumerate(('map', 'localCostmap', 'lidar'), 1):
            item = envelope('media', sequence=sequence, timestamp=clock[0] - .9, session=session)
            item['payload']['key'] = key
            item['payload']['ageS'] = 1.2
            if key == 'lidar':
                item['payload'] = {'key': key, 'kind': 'cloud', 'status': 'AVAILABLE',
                                   'ageS': 1.2, 'points': [[1, 0, 0]], 'totalPoints': 1}
            self.assertEqual((await self.ingest(item)).status, 200)
        current = self.app[STORE].visuals()
        self.assertEqual(current['map']['status'], 'AVAILABLE')
        self.assertEqual(current['localCostmap']['status'], 'AVAILABLE')
        self.assertEqual(current['lidar']['status'], 'STALE')
        self.assertNotIn('points', current['lidar'])
        self.assertNotIn('frameIdentity', current['map'])
        clock[0] += 3
        current = self.app[STORE].visuals()
        self.assertEqual(current['map']['status'], 'AVAILABLE')
        self.assertEqual(current['localCostmap']['status'], 'STALE')
        self.assertIsNotNone(self.app[STORE].image('map'))
        self.assertIsNone(self.app[STORE].image('localCostmap'))

    async def test_sequence_replay_session_reset_and_sqlite(self):
        obj = envelope()
        self.assertEqual((await self.ingest(obj)).status, 200)
        response = await self.ingest(obj)
        self.assertEqual(response.status, 409)
        self.assertEqual((await response.json())['reason'], 'duplicate')
        current = dict(obj, sequence=3)
        self.assertEqual((await self.ingest(current)).status, 200)
        response = await self.ingest(dict(obj, sequence=2))
        self.assertEqual((await response.json())['reason'], 'out_of_order')
        new = dict(obj, session_id=str(uuid.uuid4()), collector_timestamp=time.time() + .001)
        self.assertEqual((await self.ingest(new)).status, 200)
        response = await self.ingest(dict(obj, sequence=4, collector_timestamp=time.time() + .002))
        self.assertEqual((await response.json())['reason'], 'retired_session')
        # Independent second Store reads durable checkpoint, not an app media cache.
        second = Store(config(self.temp.name).validate())
        try:
            with self.assertRaises(ReplayError):
                second.ingest(validate_envelope(new))
        finally:
            second.close()

    async def test_old_sample_with_new_sequence_does_not_refresh(self):
        obj = envelope()
        self.assertEqual((await self.ingest(obj)).status, 200)
        old = dict(obj, sequence=2, collector_timestamp=obj['collector_timestamp'] - 10)
        response = await self.ingest(old)
        self.assertEqual(response.status, 409)
        self.assertEqual((await response.json())['reason'], 'old_timestamp')

    async def test_heartbeat_offline_observation_stale_truth_and_age(self):
        clock = [time.time()]
        self.app[STORE].clock = lambda: clock[0]
        obj = envelope(timestamp=clock[0])
        obj['payload']['system'] = {'status': 'AVAILABLE', 'cpu': {'percent': 0}}
        obj['payload']['robot'] = {'status': 'AVAILABLE', 'data': {'value': 0, 'missing': None,
            'debug': {'fields': {'x': {'state': 'AVAILABLE', 'ageMs': 100, 'value': 0}}}}}
        obj['payload']['pages'] = {'0x10': {'status': 'AVAILABLE', 'ageMs': 100}}
        self.assertEqual((await self.ingest(obj)).status, 200)
        await self.login()
        snap = await (await self.client.get('/snapshot.json')).json()
        self.assertEqual(snap['rk3588']['cpu']['percent'], 0)
        self.assertIsNone(snap['robot']['data']['missing'])
        clock[0] += 3
        snap = self.app[STORE].snapshot()
        self.assertEqual(snap['pages']['0x10']['status'], 'STALE')
        self.assertGreaterEqual(snap['robot']['data']['debug']['fields']['x']['ageMs'], 3100)
        clock[0] += 3
        heartbeat = envelope('heartbeat', sequence=2, timestamp=clock[0], session=obj['session_id'])
        self.assertEqual((await self.ingest(heartbeat)).status, 200)
        snap = self.app[STORE].snapshot()
        self.assertTrue(snap['cloud']['online'])
        self.assertEqual(snap['cloud']['status'], 'STALE')
        self.assertEqual(snap['rk3588']['status'], 'STALE')
        self.assertEqual(snap['cloud']['lastObservation'], obj['collector_timestamp'])
        clock[0] += 11
        self.assertFalse(self.app[STORE].snapshot()['cloud']['online'])

    async def test_event_diagnostics_costmap_thresholds(self):
        clock = [time.time()]
        self.app[STORE].clock = lambda: clock[0]
        obj = envelope(timestamp=clock[0])
        obj['payload']['devices'] = {'sensors': {
            'map': {'status': 'AVAILABLE', 'ageS': 100, 'updateMode': 'event'},
            'diagnostics': {'status': 'AVAILABLE', 'ageS': 0},
            'localCostmap': {'status': 'AVAILABLE', 'ageS': 0}}}
        self.assertEqual((await self.ingest(obj)).status, 200)
        clock[0] += 2.5
        sensors = self.app[STORE].snapshot()['devices']['sensors']
        self.assertEqual(sensors['map']['status'], 'AVAILABLE')
        self.assertEqual(sensors['diagnostics']['status'], 'AVAILABLE')
        self.assertEqual(sensors['localCostmap']['status'], 'AVAILABLE')
        clock[0] += 1
        self.assertEqual(self.app[STORE].snapshot()['devices']['sensors']['diagnostics']['status'], 'STALE')

    async def test_png_http_auth_and_offline_hides_media(self):
        clock = [time.time()]
        self.app[STORE].clock = lambda: clock[0]
        obj = envelope('media', timestamp=clock[0])
        self.assertEqual((await self.ingest(obj)).status, 200)
        await self.login()
        result = await self.client.get('/visual/map.png')
        self.assertEqual(result.status, 200)
        self.assertEqual(result.content_type, 'image/png')
        clock[0] += 11
        self.assertEqual((await self.client.get('/visual/map.png')).status, 204)
        self.assertEqual((await (await self.client.get('/visuals.json')).json())['map']['status'], 'STALE')

    async def test_new_session_heartbeat_does_not_revive_old_map_or_observation(self):
        clock = [time.time()]
        self.app[STORE].clock = lambda: clock[0]
        obj = envelope(timestamp=clock[0])
        self.assertEqual((await self.ingest(obj)).status, 200)
        media = envelope('media', sequence=2, timestamp=clock[0], session=obj['session_id'])
        self.assertEqual((await self.ingest(media)).status, 200)
        self.assertIsNotNone(self.app[STORE].image('map'))
        clock[0] += 1
        heartbeat = envelope('heartbeat', sequence=1, timestamp=clock[0])
        self.assertEqual((await self.ingest(heartbeat)).status, 200)
        self.assertEqual(self.app[STORE].cloud_status()['status'], 'STALE')
        self.assertIsNone(self.app[STORE].image('map'))

    async def test_old_heartbeat_first_receipt_is_offline(self):
        obj = envelope('heartbeat', timestamp=time.time() - 30)
        self.assertEqual((await self.ingest(obj)).status, 200)
        self.assertFalse(self.app[STORE].cloud_status()['online'])

    async def test_multiple_tabs_share_cookie_without_disconnect(self):
        await self.login()
        first = await self.client.ws_connect('/ws', headers={'Origin': ORIGIN})
        second = await self.client.ws_connect('/ws', headers={'Origin': ORIGIN})
        self.assertEqual((await first.receive(timeout=3)).type, WSMsgType.TEXT)
        self.assertEqual((await second.receive(timeout=3)).type, WSMsgType.TEXT)
        self.assertFalse(first.closed)
        self.assertFalse(second.closed)
        await first.close()
        await second.close()

    async def test_websocket_auth_origin_and_expiry(self):
        await self.login()
        with self.assertRaises(Exception):
            await self.client.ws_connect('/ws', headers={'Origin': 'https://other.example'})
        ws = await self.client.ws_connect('/ws', headers={'Origin': ORIGIN})
        message = await ws.receive(timeout=3)
        self.assertEqual(message.type, WSMsgType.TEXT)
        self.assertIn('cloud', message.json())
        self.app[SESSIONS].active.clear()
        message = await ws.receive(timeout=3)
        self.assertEqual(message.type, WSMsgType.CLOSE)
        self.assertEqual(message.data, 1008)
        await ws.close()

    async def test_request_limit_and_query_credentials_rejected(self):
        response = await self.client.post('/api/v1/ingest?token=not-a-real-token', json=envelope(),
                                           headers={'Authorization': 'Bearer ' + TOKEN})
        self.assertEqual(response.status, 400)
        response = await self.client.post('/api/v1/ingest', data=b' ' * 1_048_577,
                                          headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + TOKEN})
        self.assertEqual(response.status, 413)

    async def test_production_secure_cookie_and_plain_http_rejected(self):
        # The app is isolated; simulate only a configured loopback reverse proxy.
        cfg = self.app[CONFIG]
        cfg.allow_insecure_local = False
        cfg.public_origin = 'https://observer.example.com'
        obj = envelope()
        self.assertEqual((await self.ingest(obj)).status, 403)
        headers = {'Origin': cfg.public_origin, 'X-Forwarded-Proto': 'https'}
        result = await self.client.post('/api/login', json={'username': 'viewer', 'password': PASSWORD}, headers=headers)
        self.assertEqual(result.status, 200)
        self.assertTrue(result.cookies['observer_session']['secure'])

    async def test_login_rate_limits_invalid_password(self):
        statuses = []
        for _ in range(6):
            result = await self.client.post('/api/login', json={'username': 'viewer', 'password': 'invalid-password'}, headers={'Origin': ORIGIN})
            statuses.append(result.status)
        self.assertEqual(statuses, [401, 401, 401, 401, 401, 429])


class ConfigSecurityTests(unittest.TestCase):
    def test_hash_and_bounded_rate_limiter(self):
        self.assertTrue(verify_password(PASSWORD, PASSWORD_HASH))
        self.assertFalse(verify_password('wrong-password', PASSWORD_HASH))
        self.assertFalse(verify_password(PASSWORD, PASSWORD_HASH.replace('16384', '1073741824')))
        limiter = RateLimiter(maximum=2)
        self.assertTrue(limiter.allow('a', 0, 1))
        self.assertFalse(limiter.allow('a', 0, 1))
        limiter.allow('b', 0, 1)
        limiter.allow('c', 0, 1)
        self.assertEqual(len(limiter.buckets), 2)

    def test_fail_closed_config_and_single_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                config(directory, public_origin='http://example.com', allow_insecure_local=False).validate()
            with self.assertRaises(ValueError):
                config(directory, session_secret='weak').validate()
            cfg = config(directory)
            cfg.device_credentials[0]['media_keys'] = list(MEDIA_KEYS)
            with self.assertRaises(ValueError):
                cfg.validate()

    def test_camera_role_cannot_be_granted_scan_media_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            for key in ('bluesea', 'scan'):
                cfg = config(directory)
                cfg.device_credentials[0]['media_keys'].remove(key)
                cfg.device_credentials[1]['media_keys'].append(key)
                with self.assertRaisesRegex(ValueError, 'only upload camera frames'):
                    cfg.validate()

    def test_secure_transport_trusted_proxy_only(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = config(directory, public_origin='https://observer.example.com', allow_insecure_local=False).validate()
            class Request:
                secure, remote, headers = False, '198.51.100.11', {'X-Forwarded-Proto': 'https'}
            self.assertFalse(cfg.is_secure(Request()))
            Request.remote = '127.0.0.1'
            self.assertTrue(cfg.is_secure(Request()))
            Request.headers = {}
            self.assertFalse(cfg.is_secure(Request()))


if __name__ == '__main__':
    unittest.main()
